"""
manual_input.py
===============
Ручний ввід даних для ОВДП Портфельного Менеджера.

Три джерела:
  1. NBU OVDP Excel  — реєстр від bank.gov.ua/ua/markets/ovdp/search
                       (метадата: ISIN, валюта, дати, купон, період)
  2. ICU Telegram    — ринкові котировки з бота ICU (bid/ask + yield)
  3. Власний портфель — мінімальний ввід (ISIN + кількість),
                        решта полів автозаповнюється з реєстру.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date
from typing import Dict, List, Optional, Union

import pandas as pd

from data_loader import parse_bond_registry_from_dict, parse_portfolio_input

log = logging.getLogger("manual_input")

UA_ISIN_RE = re.compile(r"UA\d{10}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. NBU OVDP EXCEL
# ─────────────────────────────────────────────────────────────────────────────

NBU_COLUMN_MAP = {
    "ISIN":                                                  "isin",
    "Вид облігації":                                         "bond_type",
    "Поточна номінальна вартість станом на початок дня":     "face_value",
    "Валюта випуску":                                        "currency",
    "Дата випуску":                                          "issue_date",
    "Дата погашення":                                        "maturity_date",
    "Кількість облігацій в обігу":                            "outstanding",
    "Номінальний рівень дохідності, %":                      "coupon_rate_pct",
    "Періодичність купонних виплат, днів":                   "coupon_period_days",
}


def parse_nbu_ovdp_excel(raw: Union[str, bytes]) -> pd.DataFrame:
    """
    Парсить Excel з реєстром ОВДП НБУ (bank.gov.ua → результати пошуку).
    Повертає DataFrame, готовий для parse_bond_registry_from_dict.
    """
    src = io.BytesIO(raw) if isinstance(raw, bytes) else raw
    raw_df = pd.read_excel(src, header=0)
    raw_df = raw_df.rename(columns=NBU_COLUMN_MAP)

    if "isin" not in raw_df.columns:
        log.error("NBU Excel: не знайдено колонки ISIN. Колонки: %s",
                  list(raw_df.columns))
        return pd.DataFrame()

    bonds: List[Dict] = []
    for _, r in raw_df.iterrows():
        isin = str(r.get("isin", "")).strip().upper()
        if not UA_ISIN_RE.fullmatch(isin):
            continue

        period_days = _coerce_float(r.get("coupon_period_days"), default=0.0)
        coupon = _coerce_float(r.get("coupon_rate_pct"), default=0.0)
        # period_days → платежів на рік: 91→4, 182→2, 365→1, дисконт→0
        if coupon == 0 or period_days == 0:
            freq = 0
        else:
            freq = max(1, round(365 / period_days))

        bonds.append({
            "isin":            isin,
            "series_code":     isin,
            "issuer":          "Мінфін України",
            "currency":        str(r.get("currency", "UAH")).strip().upper() or "UAH",
            "face_value":      _coerce_float(r.get("face_value"), default=1000.0),
            "coupon_rate_pct": coupon,
            "coupon_freq":     int(freq),
            "day_count":       "ACT/ACT",
            "issue_date":      _parse_dmy(r.get("issue_date")),
            "maturity_date":   _parse_dmy(r.get("maturity_date")),
        })

    df = parse_bond_registry_from_dict(bonds)
    log.info("NBU Excel: %d ОВДП розпарсено", len(df))
    return df


def _parse_dmy(v) -> str:
    """DD.MM.YYYY → YYYY-MM-DD (для pandas.to_datetime)."""
    if pd.isna(v) or v == "":
        return ""
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    try:
        return pd.to_datetime(s, dayfirst=True, errors="raise").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return s


def _coerce_float(v, default: float = 0.0) -> float:
    if pd.isna(v):
        return default
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 2. ICU TELEGRAM TEXT
# ─────────────────────────────────────────────────────────────────────────────

# Один лот:
#   ISIN: /UA4000231559
#   Назва: Джарилгач
#   Дата погашення: 10.06.2026
#   ICU продає: 1065,53₴ | 13,50% SIM
#   ICU купує: 1064,66₴ | 14,50% SIM
ICU_BLOCK_RE   = re.compile(r"ISIN:\s*/?(UA\d{10}).*?(?=ISIN:\s*/?UA\d{10}|\Z)", re.DOTALL)
ICU_NAME_RE    = re.compile(r"Назва:\s*(.+)")
ICU_MATUR_RE   = re.compile(r"Дата\s+погашення:\s*(\d{2}\.\d{2}\.\d{4})")
# "1065,53₴ | 13,50% SIM" або "1065,53 ₴ | 13,50 % YTM" або "-" (немає котировки)
ICU_QUOTE_RE   = re.compile(
    r"([\d\s]+[,.]\d+)\s*₴?\s*\|\s*([\d\s]+[,.]\d+)\s*%\s*(SIM|YTM)",
    re.IGNORECASE,
)
ICU_SELL_RE    = re.compile(r"ICU\s*продає:\s*(.+)")
ICU_BUY_RE     = re.compile(r"ICU\s*купує:\s*(.+)")


def parse_icu_telegram(text: str) -> pd.DataFrame:
    """
    Парсить повідомлення з Telegram-бота ICU.
    Повертає DataFrame з колонками:
      isin, name, maturity_date,
      ask_price_uah, ask_yield_pct, ask_yield_type,
      bid_price_uah, bid_yield_pct, bid_yield_type
    """
    rows: List[Dict] = []
    for block in ICU_BLOCK_RE.finditer(text):
        chunk = block.group(0)
        isin = block.group(1)

        name_m  = ICU_NAME_RE.search(chunk)
        matur_m = ICU_MATUR_RE.search(chunk)
        sell_m  = ICU_SELL_RE.search(chunk)
        buy_m   = ICU_BUY_RE.search(chunk)

        ask_px, ask_yld, ask_typ = _parse_icu_quote(sell_m.group(1) if sell_m else "")
        bid_px, bid_yld, bid_typ = _parse_icu_quote(buy_m.group(1)  if buy_m  else "")

        rows.append({
            "isin":             isin,
            "name":             _clean_name(name_m.group(1)) if name_m else "",
            "maturity_date":    pd.to_datetime(matur_m.group(1), dayfirst=True,
                                               errors="coerce") if matur_m else pd.NaT,
            "ask_price_uah":    ask_px,
            "ask_yield_pct":    ask_yld,
            "ask_yield_type":   ask_typ,
            "bid_price_uah":    bid_px,
            "bid_yield_pct":    bid_yld,
            "bid_yield_type":   bid_typ,
        })

    df = pd.DataFrame(rows).drop_duplicates("isin").reset_index(drop=True)
    log.info("ICU Telegram: %d котировок розпарсено", len(df))
    return df


def _parse_icu_quote(s: str):
    """'1065,53₴ | 13,50% SIM' → (1065.53, 13.50, 'SIM').  '-' → (nan, nan, '')."""
    m = ICU_QUOTE_RE.search(s)
    if not m:
        return float("nan"), float("nan"), ""
    px  = float(m.group(1).replace(",", ".").replace(" ", ""))
    yld = float(m.group(2).replace(",", ".").replace(" ", ""))
    typ = m.group(3).upper()
    return px, yld, typ


def _clean_name(s: str) -> str:
    return re.sub(r"[❗\s]+", " ", s).strip().strip("•").strip()


# ─────────────────────────────────────────────────────────────────────────────
# 3. ОБ'ЄДНАННЯ NBU + ICU У ЄДИНИЙ РЕЄСТР
# ─────────────────────────────────────────────────────────────────────────────

def merge_icu_into_registry(
    bonds_df: pd.DataFrame,
    icu_df: pd.DataFrame,
    side: str = "ask",
) -> pd.DataFrame:
    """
    Зливає ICU-котировки в реєстр облігацій.

    side='ask' → беремо ціну/доходність "ICU продає" (по якій ви купуєте)
    side='bid' → беремо "ICU купує"
    """
    if bonds_df.empty:
        return bonds_df
    if icu_df.empty or "isin" not in icu_df.columns:
        log.info("ICU порожній — реєстр без котировок")
        return bonds_df

    px_col  = f"{side}_price_uah"
    yld_col = f"{side}_yield_pct"
    typ_col = f"{side}_yield_type"

    icu_slim = icu_df[["isin", "name", px_col, yld_col, typ_col]].copy()
    merged = bonds_df.merge(icu_slim, on="isin", how="left")

    # ICU price у грн → у % від номіналу
    face = merged["face_value"].astype(float).replace(0, 1000.0)
    merged["market_price_pct"] = (merged[px_col] / face * 100).fillna(
        merged.get("market_price_pct", 100.0)
    )
    merged["market_ytm_pct"]   = merged[yld_col].fillna(
        merged.get("market_ytm_pct", 0.0)
    )
    merged["yield_type"] = merged[typ_col].fillna("")
    merged["icu_name"]   = merged["name"].fillna("")

    merged = merged.drop(columns=[px_col, yld_col, typ_col, "name"])
    log.info("ICU злито в реєстр: %d/%d паперів отримали котировки",
             merged["market_ytm_pct"].gt(0).sum(), len(merged))
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 4. РУЧНИЙ ВВІД ПОРТФЕЛЯ З АВТОПІДТЯГУВАННЯМ З РЕЄСТРУ
# ─────────────────────────────────────────────────────────────────────────────

def parse_manual_portfolio(
    rows: List[Dict],
    registry_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Мінімальний ввід портфеля. Обов'язкове: isin, amount_lots.
    avg_buy_price_pct автопідтягується з registry_df['market_price_pct']
    (якщо не задано вручну). buy_date default = today.

    rows: [{"isin": "UA4000231559", "amount_lots": 60}, ...]
    registry_df: вихід merge_icu_into_registry() або parse_bond_registry_from_dict()
    """
    enriched: List[Dict] = []
    price_lookup: Dict[str, float] = {}
    if registry_df is not None and not registry_df.empty:
        if "market_price_pct" in registry_df.columns:
            price_lookup = dict(zip(
                registry_df["isin"].astype(str).str.upper(),
                registry_df["market_price_pct"].astype(float),
            ))

    for r in rows:
        isin = str(r.get("isin", "")).strip().upper().lstrip("/")
        if not UA_ISIN_RE.fullmatch(isin):
            log.warning("Пропускаю некоректний ISIN: %r", r.get("isin"))
            continue

        price = r.get("avg_buy_price_pct")
        if price is None:
            price = price_lookup.get(isin, 100.0)

        enriched.append({
            "isin":              isin,
            "amount_lots":       float(r.get("amount_lots", 0)),
            "avg_buy_price_pct": float(price),
            "buy_date":          r.get("buy_date", date.today()),
            "commission_uah":    float(r.get("commission_uah", 0.0)),
            "account_id":        str(r.get("account_id", "manual")),
        })

    return parse_portfolio_input(enriched)
