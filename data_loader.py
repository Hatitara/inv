"""
data_loader.py
==============
Завантаження всіх зовнішніх даних:
  - Курси НБУ (поточні + на дату)
  - Облікова ставка НБУ
  - Реєстр ОВДП (ручне введення або ICU CSV/Excel)
  - Результати аукціонів Мінфіну
  - Ринкові котировки ICU (таблиця YTM з їхнього сайту / вставлена вручну)
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
    FALLBACK_FX, ICU_COLUMN_MAP, MINFIN_AUCTIONS_URL,
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
# 3. РЕЄСТР ОБЛІГАЦІЙ — ручне введення / ICU Excel / CSV
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
                # Ринкові дані (можуть бути заповнені пізніше з ICU)
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


def parse_icu_table(raw: str | bytes, file_type: str = "csv") -> pd.DataFrame:
    """
    Парсить таблицю з ICU Research (вставлена або завантажена).
    file_type: 'csv' | 'excel' | 'paste' (tab-separated текст)

    Повертає DataFrame з колонками:
      isin, maturity_date, coupon_rate_pct, ytm_icu_pct,
      clean_price_pct, accrued_int, volume_mln
    """
    try:
        if file_type == "excel":
            df = pd.read_excel(io.BytesIO(raw) if isinstance(raw, bytes) else raw,
                               header=0)
        elif file_type in ("csv", "paste"):
            sep = "\t" if file_type == "paste" else ","
            df = pd.read_csv(
                io.StringIO(raw if isinstance(raw, str) else raw.decode("utf-8")),
                sep=sep,
            )
        else:
            raise ValueError(f"Невідомий тип файлу: {file_type}")

        # Перейменовуємо колонки за картою
        rename_map = {}
        for src, dst in ICU_COLUMN_MAP.items():
            for col in df.columns:
                if src.lower() in col.lower():
                    rename_map[col] = dst
                    break
        df = df.rename(columns=rename_map)

        # Нормалізація дат
        if "maturity_date" in df.columns:
            df["maturity_date"] = pd.to_datetime(
                df["maturity_date"], dayfirst=True, errors="coerce"
            )

        # Числові поля
        for col in ["coupon_rate_pct", "ytm_icu_pct", "clean_price_pct",
                    "accrued_int", "volume_mln"]:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].astype(str).str.replace(",", ".").str.replace("%", ""),
                    errors="coerce",
                )

        log.info("ICU-таблиця: %d рядків", len(df))
        return df

    except Exception as e:
        log.error("Помилка парсингу ICU-даних: %s", e)
        return pd.DataFrame()


def merge_icu_market_data(
    bonds_df: pd.DataFrame,
    icu_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Зливає ринкові котировки ICU в реєстр облігацій по ISIN.
    ICU-дані мають пріоритет над ручними ринковими цінами.
    """
    if icu_df.empty or "isin" not in icu_df.columns:
        return bonds_df

    icu_cols = [c for c in ["isin", "ytm_icu_pct", "clean_price_pct",
                             "accrued_int", "volume_mln"]
                if c in icu_df.columns]
    merged = bonds_df.merge(icu_df[icu_cols], on="isin", how="left", suffixes=("", "_icu"))

    if "ytm_icu_pct" in merged.columns:
        merged["market_ytm_pct"] = merged["ytm_icu_pct"].fillna(merged["market_ytm_pct"])
    if "clean_price_pct" in merged.columns:
        merged["market_price_pct"] = merged["clean_price_pct"].fillna(merged["market_price_pct"])

    log.info("ICU-дані злиті: %d паперів отримали котировки",
             merged["market_ytm_pct"].notna().sum())
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 4. ПОРТФЕЛЬ КОРИСТУВАЧА — введення вручну або ICU-формат
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


def parse_icu_portfolio_csv(raw: str) -> pd.DataFrame:
    """
    Парсить експорт портфеля з ICU (формат їхнього кабінету).
    Автоматично нормалізує різні варіанти заголовків.
    """
    try:
        df = pd.read_csv(io.StringIO(raw), sep=None, engine="python")
        col_map = {}
        for col in df.columns:
            cl = col.lower().strip()
            if "isin" in cl:                           col_map[col] = "isin"
            elif any(x in cl for x in ["кільк", "lots", "кол-во", "облігацій"]): col_map[col] = "amount_lots"
            elif any(x in cl for x in ["ціна", "price", "курс куп"]):            col_map[col] = "avg_buy_price_pct"
            elif any(x in cl for x in ["дата", "date", "куплено"]):              col_map[col] = "buy_date"
        df = df.rename(columns=col_map)

        for c in ["amount_lots", "avg_buy_price_pct"]:
            if c in df.columns:
                df[c] = pd.to_numeric(
                    df[c].astype(str).str.replace(",", "."), errors="coerce"
                )
        return parse_portfolio_input(df.to_dict("records"))
    except Exception as e:
        log.error("Помилка парсингу ICU-портфеля: %s", e)
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