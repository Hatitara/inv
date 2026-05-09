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
from datetime import date, datetime
from typing import Dict, List, Optional

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
# 5. ДЕМО-ДАНІ (для тестування без реального портфеля)
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