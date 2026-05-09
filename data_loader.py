"""
data_loader.py
==============
Завантаження всіх зовнішніх даних:
  - Курси НБУ (поточні + на дату)
  - Облікова ставка НБУ
  - Реєстр ОВДП з API Мінфіну (аукціонні результати)
  - Реєстр ОВДП (ручне введення або CSV)
  - Результати аукціонів Мінфіну
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from config import (
    FALLBACK_FX, MINFIN_AUCTIONS_URL,
    NBU_FX_DATE, NBU_FX_TODAY, NBU_KEY_RATE,
)

log = logging.getLogger("data_loader")


# ─────────────────────────────────────────────────────────────────────────────
# БАЗОВИЙ HTTP-КЛІЄНТ
# ─────────────────────────────────────────────────────────────────────────────

class _HttpClient:
    TIMEOUT   = 10
    RETRIES   = 3
    HEADERS   = {"User-Agent": "OVDP-Portfolio-Manager/1.0"}

    def get(self, url: str) -> Optional[list | dict]:
        for attempt in range(1, self.RETRIES + 1):
            try:
                r = requests.get(url, timeout=self.TIMEOUT, headers=self.HEADERS)
                r.raise_for_status()
                return r.json()
            except requests.HTTPError as e:
                log.warning("HTTP %s → %s (спроба %d)", e.response.status_code, url, attempt)
                return None
            except (requests.ConnectionError, requests.Timeout):
                log.warning("Мережева помилка (спроба %d/%d): %s", attempt, self.RETRIES, url)
            except ValueError:
                log.error("Невалідний JSON: %s", url)
                return None
        return None


_http = _HttpClient()


# ─────────────────────────────────────────────────────────────────────────────
# 1. НБУ — КУРСИ ВАЛЮТ
# ─────────────────────────────────────────────────────────────────────────────

def load_fx_rates(on_date: Optional[date] = None) -> Dict[str, float]:
    """
    Повертає {ccy: rate_uah}.
    on_date=None → сьогоднішній офіційний курс.
    """
    if on_date is None:
        data = _http.get(NBU_FX_TODAY)
    else:
        # НБУ формат дати: YYYYMMDD
        data = _http.get(NBU_FX_DATE.format(
            ccy="USD", d=on_date.strftime("%Y%m%d")
        ))
        # Завантажуємо всі валюти окремо (НБУ не підтримує all+date разом)
        all_ccys = ["USD", "EUR", "GBP"]
        result = {"UAH": 1.0}
        for ccy in all_ccys:
            raw = _http.get(NBU_FX_DATE.format(ccy=ccy, d=on_date.strftime("%Y%m%d")))
            if raw and isinstance(raw, list):
                try:
                    result[ccy] = float(raw[0]["rate"])
                except (IndexError, KeyError, ValueError):
                    result[ccy] = FALLBACK_FX.get(ccy, 1.0)
            else:
                result[ccy] = FALLBACK_FX.get(ccy, 1.0)
        return result

    if not data:
        log.warning("NBU FX недоступний → fallback")
        return FALLBACK_FX.copy()

    rates = {"UAH": 1.0}
    for item in data:
        try:
            rates[item["cc"]] = float(item["rate"])
        except (KeyError, ValueError):
            continue
    log.info("FX-курсів завантажено: %d", len(rates))
    return rates


# ─────────────────────────────────────────────────────────────────────────────
# 2. НБУ — ОБЛІКОВА СТАВКА
# ─────────────────────────────────────────────────────────────────────────────

def load_nbu_key_rate() -> float:
    """Повертає поточну облікову ставку НБУ у % річних."""
    data = _http.get(NBU_KEY_RATE)
    if not data or not isinstance(data, list):
        log.warning("Облікова ставка недоступна → fallback 14.5%%")
        return 14.5
    try:
        # Беремо найостанніший запис
        df = pd.DataFrame(data)
        df["startDate"] = pd.to_datetime(df["startDate"], dayfirst=True, errors="coerce")
        df = df.dropna(subset=["startDate"]).sort_values("startDate", ascending=False)
        rate = float(df.iloc[0]["rate"])
        log.info("Облікова ставка НБУ: %.2f%%", rate)
        return rate
    except (KeyError, IndexError, ValueError):
        log.warning("Помилка парсингу облікової ставки → 14.5%%")
        return 14.5


# ─────────────────────────────────────────────────────────────────────────────
# 3. РЕЄСТР ОБЛІГАЦІЙ — ручне введення або CSV
# ─────────────────────────────────────────────────────────────────────────────

def parse_bond_registry_from_dict(bonds: List[Dict]) -> pd.DataFrame:
    """
    Приймає список словників з параметрами облігацій.
    Генерує купонні дати та нормалізує типи.
    """
    from analytics import generate_coupon_dates   # відкладений імпорт

    records = []
    for b in bonds:
        try:
            issue_dt    = pd.to_datetime(b["issue_date"])
            maturity_dt = pd.to_datetime(b["maturity_date"])
            freq        = int(b.get("coupon_freq", 0))
            coupon      = float(b.get("coupon_rate_pct", 0.0))
            is_discount = (coupon == 0.0 or freq == 0)

            pay_dates = generate_coupon_dates(
                issue_date    = issue_dt.date(),
                maturity_date = maturity_dt.date(),
                freq          = freq,
            )

            records.append({
                "isin":             b["isin"],
                "series_code":      b.get("series_code", b["isin"]),
                "issuer":           b.get("issuer", "Мінфін України"),
                "currency":         b.get("currency", "UAH"),
                "face_value":       float(b.get("face_value", 1000.0)),
                "coupon_rate_pct":  coupon,
                "coupon_freq":      freq,
                "day_count":        b.get("day_count", "ACT/ACT"),
                "issue_date":       issue_dt,
                "maturity_date":    maturity_dt,
                "coupon_pay_dates": pay_dates,   # List[date]
                "is_discount":      is_discount,
                # Ринкові дані (аукціонна ціна / YTM з Мінфіну)
                "market_price_pct": float(b.get("market_price_pct", 100.0)),
                "market_ytm_pct":   float(b.get("market_ytm_pct", 0.0)),
                "volume_mln_uah":   float(b.get("volume_mln_uah", 0.0)),
            })
        except (KeyError, ValueError) as e:
            log.error("Помилка ISIN %s: %s", b.get("isin", "?"), e)

    df = pd.DataFrame(records)
    if df.empty:
        return df

    # Дедублікація
    df = df.drop_duplicates("isin").reset_index(drop=True)
    log.info("Реєстр облігацій: %d паперів", len(df))
    return df


def load_minfin_bonds(limit: int = 100) -> pd.DataFrame:
    """
    Завантажує список активних ОВДП з API Мінфіну (результати аукціонів).
    Повертає список словників, готових для parse_bond_registry_from_dict().

    API може повертати {'data': [...]} або просто [...].
    Дедублікує по ISIN, залишаючи найновіший аукціон.
    """
    base = MINFIN_AUCTIONS_URL.split("?")[0]
    data = _http.get(f"{base}?limit={limit}")
    if not data:
        log.warning("Мінфін API недоступний")
        return pd.DataFrame()

    if isinstance(data, dict):
        records_raw = (
            data.get("data") or data.get("items") or
            data.get("results") or data.get("auctions") or []
        )
    elif isinstance(data, list):
        records_raw = data
    else:
        log.error("Мінфін API: неочікуваний формат відповіді")
        return pd.DataFrame()

    today = date.today()
    bonds: List[Dict] = []

    for r in records_raw:
        try:
            isin = str(r.get("isin") or r.get("ISIN") or "").strip().upper()
            if len(isin) < 12:
                continue

            mat_raw = (r.get("maturity") or r.get("date_maturity") or
                       r.get("maturityDate") or r.get("redemption_date") or "")
            maturity = pd.to_datetime(mat_raw, dayfirst=False, errors="coerce")
            if pd.isnull(maturity) or maturity.date() <= today:
                continue  # пропускаємо погашені

            issue_raw = (r.get("date") or r.get("date_auction") or
                         r.get("issueDate") or r.get("placement_date") or "")
            issue = pd.to_datetime(issue_raw, dayfirst=False, errors="coerce")
            if pd.isnull(issue):
                issue = pd.Timestamp(today)

            currency = str(r.get("currency") or r.get("ccy") or "UAH").upper()
            if currency not in ("UAH", "USD", "EUR"):
                currency = "UAH"

            coupon = float(r.get("coupon") or r.get("coupon_rate") or
                           r.get("rate") or r.get("couponRate") or 0.0)

            freq_raw = (r.get("period") or r.get("coupon_freq") or
                        r.get("frequency") or r.get("couponFreq"))
            try:
                freq = int(freq_raw) if freq_raw is not None else (4 if coupon > 0 else 0)
            except (ValueError, TypeError):
                freq = 4 if coupon > 0 else 0

            face = float(r.get("face") or r.get("nominal") or
                         r.get("face_value") or r.get("faceValue") or 1000.0)

            price = float(r.get("price") or r.get("price_avg") or
                          r.get("weighted_price") or r.get("avgPrice") or 100.0)
            if price <= 0:
                price = 100.0

            ytm_raw = (r.get("yield") or r.get("ytm") or
                       r.get("yield_avg") or r.get("avgYield") or 0.0)
            ytm_pct = float(ytm_raw) if ytm_raw else 0.0

            amount = float(r.get("amount") or r.get("volume") or
                           r.get("amount_placed") or r.get("placedAmount") or 0.0)
            volume_mln = round(amount / 1_000_000, 1) if amount > 0 else 0.0

            code = str(r.get("code") or r.get("series") or
                       r.get("series_code") or r.get("seriesCode") or isin)

            bonds.append({
                "isin":             isin,
                "series_code":      code,
                "currency":         currency,
                "face_value":       face,
                "coupon_rate_pct":  coupon,
                "coupon_freq":      freq,
                "day_count":        "ACT/ACT",
                "issue_date":       issue.strftime("%Y-%m-%d"),
                "maturity_date":    maturity.strftime("%Y-%m-%d"),
                "market_price_pct": price,
                "market_ytm_pct":   ytm_pct,
                "volume_mln_uah":   volume_mln,
            })
        except (TypeError, ValueError, KeyError) as e:
            log.debug("Пропускаю запис Мінфіну: %s", e)

    if not bonds:
        log.warning("Мінфін: не знайдено активних ОВДП (або всі погашені)")
        return pd.DataFrame()

    # Дедублікація по ISIN — залишаємо найновіший аукціон
    df_raw = pd.DataFrame(bonds)
    df_raw = df_raw.drop_duplicates("isin", keep="last").reset_index(drop=True)
    log.info("Мінфін: %d активних ОВДП завантажено", len(df_raw))
    return df_raw


# ─────────────────────────────────────────────────────────────────────────────
# 4. ПОРТФЕЛЬ КОРИСТУВАЧА — введення вручну або CSV-файл
# ─────────────────────────────────────────────────────────────────────────────

def parse_portfolio_input(rows: List[Dict]) -> pd.DataFrame:
    """
    Приймає список позицій з UI або CSV і повертає DataFrame портфеля.

    Обов'язкові поля: isin, amount_lots, avg_buy_price_pct
    Необов'язкові: buy_date, commission_uah, account_id
    """
    records = []
    for r in rows:
        try:
            records.append({
                "isin":               str(r["isin"]).strip().upper(),
                "amount_lots":        float(r["amount_lots"]),
                "avg_buy_price_pct":  float(r.get("avg_buy_price_pct", 100.0)),
                "buy_date":           pd.to_datetime(r.get("buy_date", date.today())),
                "commission_uah":     float(r.get("commission_uah", 0.0)),
                "account_id":         str(r.get("account_id", "main")),
            })
        except (KeyError, ValueError) as e:
            log.warning("Пропускаю рядок портфеля (помилка: %s): %s", e, r)

    df = pd.DataFrame(records)
    if not df.empty:
        # Агрегуємо дублікати (якщо один ISIN куплено частинами)
        df = (
            df.groupby("isin", as_index=False)
            .agg(
                amount_lots       = ("amount_lots",       "sum"),
                avg_buy_price_pct = ("avg_buy_price_pct", "mean"),
                buy_date          = ("buy_date",          "min"),
                commission_uah    = ("commission_uah",    "sum"),
                account_id        = ("account_id",        "first"),
            )
        )
        log.info("Портфель: %d позицій, %.0f лотів загалом",
                 len(df), df["amount_lots"].sum())
    return df


def parse_portfolio_csv(raw: str | bytes) -> pd.DataFrame:
    """
    Парсить CSV/Excel-файл з позиціями портфеля.
    Автоматично розпізнає заголовки (ISIN, кількість, ціна купівлі, дата).
    """
    try:
        if isinstance(raw, bytes):
            # Пробуємо Excel
            try:
                df = pd.read_excel(io.BytesIO(raw), header=0)
            except Exception:
                df = pd.read_csv(io.StringIO(raw.decode("utf-8", errors="replace")),
                                 sep=None, engine="python")
        else:
            df = pd.read_csv(io.StringIO(raw), sep=None, engine="python")

        col_map: Dict[str, str] = {}
        for col in df.columns:
            cl = col.lower().strip()
            if "isin" in cl:
                col_map[col] = "isin"
            elif any(x in cl for x in ["кільк", "lots", "кол-во", "облігацій", "кількість"]):
                col_map[col] = "amount_lots"
            elif any(x in cl for x in ["ціна", "price", "курс куп", "avg", "buy_price"]):
                col_map[col] = "avg_buy_price_pct"
            elif any(x in cl for x in ["дата", "date", "куплено"]):
                col_map[col] = "buy_date"
            elif any(x in cl for x in ["комісія", "commission"]):
                col_map[col] = "commission_uah"
        df = df.rename(columns=col_map)

        for c in ["amount_lots", "avg_buy_price_pct"]:
            if c in df.columns:
                df[c] = pd.to_numeric(
                    df[c].astype(str).str.replace(",", "."), errors="coerce"
                )
        return parse_portfolio_input(df.to_dict("records"))
    except Exception as e:
        log.error("Помилка парсингу файлу портфеля: %s", e)
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# 5. INTERACTIVE BROKERS — Account Statement / Activity Statement (CSV)
# ─────────────────────────────────────────────────────────────────────────────

def _ib_find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """Повертає першу колонку df, що збігається (case-insensitive) з candidates."""
    cols = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        key = cand.lower().strip()
        if key in cols:
            return cols[key]
    return None


def _ib_to_float(val) -> float:
    """str → float: прибирає коми, пробіли, знак %."""
    try:
        return float(str(val).replace(",", "").replace(" ", "").replace("%", ""))
    except (ValueError, TypeError):
        return 0.0


def _ib_parse_description(desc: str) -> Tuple[float, Optional[pd.Timestamp]]:
    """
    Витягує купон і дату погашення з текстового поля Description IB.
    Підтримує формати:
      '14.5% 15OCT2025'  '14.500 15/10/2025'  '0% DISC 01SEP2025'
      'UA Govt 16% 01MAR2026'
    """
    coupon = 0.0
    maturity = None

    m = re.search(r"(\d+[.,]?\d*)\s*%", desc)
    if m:
        coupon = float(m.group(1).replace(",", "."))

    # DDMMMYYYY (01OCT2025)
    m = re.search(r"(\d{1,2})([A-Z]{3})(\d{4})", desc.upper())
    if m:
        try:
            maturity = pd.to_datetime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", format="%d %b %Y",
                errors="coerce",
            )
        except Exception:
            pass

    # DD/MM/YYYY або YYYY-MM-DD
    if maturity is None or pd.isnull(maturity):
        m = re.search(r"(\d{2})[/\-](\d{2})[/\-](\d{4})", desc)
        if m:
            maturity = pd.to_datetime(
                desc[m.start(): m.end()], dayfirst=True, errors="coerce"
            )

    return coupon, (maturity if maturity is not None and not pd.isnull(maturity) else None)


def _ib_split_sections(raw: str) -> Dict[str, pd.DataFrame]:
    """
    Розбиває IB Activity Statement CSV на словник секцій.
    Формат рядка: SectionName,Header|Data,col1,col2,...

    Повертає {section_name: DataFrame}.
    """
    sections: Dict[str, list] = {}      # name -> list of rows
    headers:  Dict[str, List[str]] = {} # name -> column names

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue

        section, row_type = parts[0], parts[1]
        rest = parts[2:]

        if row_type == "Header":
            headers[section] = rest
            sections.setdefault(section, [])
        elif row_type == "Data":
            cols = headers.get(section)
            if cols is None:
                continue
            # Вирівнюємо довжину
            row = rest + [""] * max(0, len(cols) - len(rest))
            sections[section].append(row[: len(cols)])

    return {
        name: pd.DataFrame(rows, columns=headers[name])
        for name, rows in sections.items()
        if name in headers and rows
    }


def parse_ib_statement(
    raw: str | bytes,
) -> Tuple[List[Dict], pd.DataFrame]:
    """
    Парсить Activity Statement / Account Statement з Interactive Brokers (CSV).

    Читає:
      • «Open Positions»  (Asset Category = Bonds) → реєстр паперів + позиції
      • «Trades»          (Asset Category = Bonds) → дати першої купівлі

    Повертає:
      bonds_list   — список dict для parse_bond_registry_from_dict()
      portfolio_df — DataFrame для відображення / подальшого аналізу
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")

    sections = _ib_split_sections(raw)
    today = date.today()

    # ── Open Positions ────────────────────────────────────────────────────────
    bonds_list:     List[Dict] = []
    portfolio_rows: List[Dict] = []

    pos_df = sections.get("Open Positions")
    if pos_df is not None and not pos_df.empty:
        # Фільтрація тільки облігацій
        cat_col = _ib_find_col(pos_df, ["Asset Category", "Asset_Category", "assetCategory"])
        if cat_col:
            pos_df = pos_df[pos_df[cat_col].str.strip().str.lower() == "bonds"]

        isin_col     = _ib_find_col(pos_df, ["ISIN", "isin"])
        symbol_col   = _ib_find_col(pos_df, ["Symbol", "symbol", "Ticker"])
        desc_col     = _ib_find_col(pos_df, ["Description", "description", "Instrument"])
        qty_col      = _ib_find_col(pos_df, ["Quantity", "quantity", "Pos"])
        mult_col     = _ib_find_col(pos_df, ["Mult", "mult", "Multiplier", "FaceValue"])
        cost_col     = _ib_find_col(pos_df, ["Cost Price", "CostPrice", "Avg Cost", "AvgCost"])
        price_col    = _ib_find_col(pos_df, ["Close Price", "ClosePrice", "Mark Price", "Price"])
        ccy_col      = _ib_find_col(pos_df, ["Currency", "currency", "Curr"])
        coupon_col   = _ib_find_col(pos_df, ["Coupon", "coupon", "Coupon Rate", "CouponRate"])
        maturity_col = _ib_find_col(pos_df, ["Maturity Date", "MaturityDate", "Expiry", "Maturity"])
        ytm_col      = _ib_find_col(pos_df, ["Yield", "YTM", "Accrued YTM"])

        for _, row in pos_df.iterrows():
            try:
                # ISIN
                isin = str(row[isin_col]).strip().upper() if isin_col else ""
                if not isin or isin in ("NAN", ""):
                    sym = str(row[symbol_col]).strip().upper() if symbol_col else ""
                    isin = sym if len(sym) >= 12 else ""
                if len(isin) < 12:
                    continue

                ccy = str(row[ccy_col]).strip().upper() if ccy_col else "UAH"
                if ccy not in ("UAH", "USD", "EUR"):
                    ccy = "UAH"

                qty = _ib_to_float(row[qty_col]) if qty_col else 0.0
                if qty == 0:
                    continue

                mult       = _ib_to_float(row[mult_col]) if mult_col else 1000.0
                face       = mult if mult > 1 else 1000.0
                cost_price = _ib_to_float(row[cost_col])  if cost_col  else 100.0
                mkt_price  = _ib_to_float(row[price_col]) if price_col else 100.0

                # IB іноді дає абсолютну ціну, не %
                if mkt_price  > 500: mkt_price  = mkt_price  / face * 100
                if cost_price > 500: cost_price = cost_price / face * 100

                # Купон і погашення — явні колонки або витяг з Description
                coupon   = _ib_to_float(row[coupon_col]) if coupon_col else 0.0
                maturity: Optional[pd.Timestamp] = None
                if maturity_col:
                    maturity = pd.to_datetime(str(row[maturity_col]), errors="coerce")
                    if pd.isnull(maturity):
                        maturity = None

                desc = str(row[desc_col]).strip() if desc_col else ""
                if coupon == 0 or maturity is None:
                    c, m = _ib_parse_description(desc)
                    if coupon == 0 and c:
                        coupon = c
                    if maturity is None and m is not None:
                        maturity = m

                if maturity is None or maturity.date() <= today:
                    continue

                ytm_pct = _ib_to_float(row[ytm_col]) if ytm_col else 0.0
                freq    = 4 if coupon > 0 else 0  # ОВДП — квартально за замовчуванням

                # Дату випуску не знаємо точно — беремо ~2 роки до погашення як placeholder
                issue_approx = (maturity - pd.DateOffset(years=2)).strftime("%Y-%m-%d")

                bonds_list.append({
                    "isin":             isin,
                    "series_code":      str(row[symbol_col]).strip() if symbol_col else isin,
                    "currency":         ccy,
                    "face_value":       face,
                    "coupon_rate_pct":  coupon,
                    "coupon_freq":      freq,
                    "day_count":        "ACT/ACT",
                    "issue_date":       issue_approx,
                    "maturity_date":    maturity.strftime("%Y-%m-%d"),
                    "market_price_pct": mkt_price,
                    "market_ytm_pct":   ytm_pct,
                    "volume_mln_uah":   0.0,
                })
                portfolio_rows.append({
                    "isin":              isin,
                    "amount_lots":       abs(qty),
                    "avg_buy_price_pct": cost_price,
                    "buy_date":          str(today),  # уточнимо з Trades нижче
                    "commission_uah":    0.0,
                })

            except Exception as e:
                log.debug("IB Open Positions: пропускаю рядок: %s", e)

    # ── Trades → уточнення дати першої купівлі ────────────────────────────────
    trades_df = sections.get("Trades")
    if trades_df is not None and not trades_df.empty:
        cat_col2 = _ib_find_col(trades_df, ["Asset Category", "Asset_Category"])
        if cat_col2:
            trades_df = trades_df[trades_df[cat_col2].str.strip().str.lower() == "bonds"]

        isin_col2  = _ib_find_col(trades_df, ["ISIN", "isin"])
        sym_col2   = _ib_find_col(trades_df, ["Symbol", "symbol"])
        date_col   = _ib_find_col(trades_df, ["Date/Time", "DateTime", "TradeDate", "Date"])
        bs_col     = _ib_find_col(trades_df, ["Buy/Sell", "BuySell", "Action", "Side"])

        buy_dates: Dict[str, date] = {}
        for _, row in trades_df.iterrows():
            try:
                bs = str(row[bs_col]).strip().upper() if bs_col else ""
                if "BUY" not in bs and bs != "B":
                    continue
                isin2 = str(row[isin_col2]).strip().upper() if isin_col2 else ""
                if not isin2 or isin2 == "NAN":
                    sym2  = str(row[sym_col2]).strip().upper() if sym_col2 else ""
                    isin2 = sym2 if len(sym2) >= 12 else ""
                if len(isin2) < 12:
                    continue
                dt = pd.to_datetime(str(row[date_col]).strip(), errors="coerce") if date_col else None
                if dt is None or pd.isnull(dt):
                    continue
                # Зберігаємо найранішу дату купівлі
                if isin2 not in buy_dates or dt.date() < buy_dates[isin2]:
                    buy_dates[isin2] = dt.date()
            except Exception:
                pass

        for r in portfolio_rows:
            if r["isin"] in buy_dates:
                r["buy_date"] = str(buy_dates[r["isin"]])

    portfolio_df = parse_portfolio_input(portfolio_rows) if portfolio_rows else pd.DataFrame()
    log.info("IB Statement: %d паперів, %d позицій", len(bonds_list), len(portfolio_df))
    return bonds_list, portfolio_df


# ─────────────────────────────────────────────────────────────────────────────
# 6. ДЕМО-ДАНІ (для тестування без реального портфеля)
# ─────────────────────────────────────────────────────────────────────────────

DEMO_BONDS = [
    {"isin": "UA4000228894", "series_code": "ОВДП-14.5-Q4-2025",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 14.5, "coupon_freq": 4,
     "day_count": "ACT/ACT", "issue_date": "2023-10-15", "maturity_date": "2025-10-15",
     "market_price_pct": 99.20, "market_ytm_pct": 14.9, "volume_mln_uah": 320},

    {"isin": "UA4000231245", "series_code": "ОВДП-16.0-S1-2026",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 16.0, "coupon_freq": 2,
     "day_count": "ACT/ACT", "issue_date": "2024-03-01", "maturity_date": "2026-03-01",
     "market_price_pct": 100.50, "market_ytm_pct": 15.7, "volume_mln_uah": 180},

    {"isin": "UA4000234712", "series_code": "ОВДП-17.5-S2-2026",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 17.5, "coupon_freq": 2,
     "day_count": "ACT/ACT", "issue_date": "2024-06-15", "maturity_date": "2026-06-15",
     "market_price_pct": 101.10, "market_ytm_pct": 16.8, "volume_mln_uah": 95},

    {"isin": "UA4000237801", "series_code": "ОВДП-18.0-Q-2027",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 18.0, "coupon_freq": 4,
     "day_count": "ACT/ACT", "issue_date": "2024-09-01", "maturity_date": "2027-09-01",
     "market_price_pct": 99.80, "market_ytm_pct": 18.1, "volume_mln_uah": 210},

    {"isin": "UA4000215600", "series_code": "ОВДП-USD-5.7-2026",
     "currency": "USD", "face_value": 1000, "coupon_rate_pct": 5.7, "coupon_freq": 2,
     "day_count": "30/360", "issue_date": "2024-01-15", "maturity_date": "2026-01-15",
     "market_price_pct": 98.50, "market_ytm_pct": 6.5, "volume_mln_uah": 45},

    {"isin": "UA4000219987", "series_code": "ОВДП-DISC-2025",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 0.0, "coupon_freq": 0,
     "day_count": "ACT/365", "issue_date": "2024-09-01", "maturity_date": "2025-09-01",
     "market_price_pct": 87.50, "market_ytm_pct": 16.2, "volume_mln_uah": 75},

    {"isin": "UA4000241053", "series_code": "ОВДП-19.25-Q-2028",
     "currency": "UAH", "face_value": 1000, "coupon_rate_pct": 19.25, "coupon_freq": 4,
     "day_count": "ACT/ACT", "issue_date": "2025-01-20", "maturity_date": "2028-01-20",
     "market_price_pct": 100.00, "market_ytm_pct": 19.0, "volume_mln_uah": 410},
]

DEMO_PORTFOLIO = [
    {"isin": "UA4000228894", "amount_lots": 60,  "avg_buy_price_pct": 96.8,  "buy_date": "2024-01-12"},
    {"isin": "UA4000231245", "amount_lots": 100, "avg_buy_price_pct": 99.0,  "buy_date": "2024-03-07"},
    {"isin": "UA4000215600", "amount_lots": 20,  "avg_buy_price_pct": 98.75, "buy_date": "2024-02-03"},
    {"isin": "UA4000219987", "amount_lots": 200, "avg_buy_price_pct": 86.50, "buy_date": "2024-09-07"},
]