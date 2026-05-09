"""
ib_parser.py
============
Парсер звітів Interactive Brokers (Activity Statement CSV).

Витягує позиції по ОВДП (українським ОВДП — ISIN починається з "UA")
з Activity Statement і повертає DataFrame, сумісний з
``parse_portfolio_input`` із data_loader.py.

Формат файлу:
    Activity Statement CSV — секційний CSV від IB Client Portal.
    Кожен рядок починається з назви секції та маркера ``Header``/``Data``.
    Релевантні секції:
      - ``Trades``                            — виконані угоди (пріоритет)
      - ``Open Positions``                    — поточні залишки (fallback)
      - ``Financial Instrument Information``  — ISIN/опис інструмента

Обмеження:
    - IB вказує кількість облігацій у номіналі (face amount), а не лотах.
      ``amount_lots = quantity / face_value`` (default face=1000).
    - Ціна T. Price у IB вже у % від номіналу — як того й очікує проєкт.
    - Комісія в валюті угоди; конверсія в UAH — на стороні викликача.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from data_loader import parse_portfolio_input

log = logging.getLogger("ib_parser")

UA_ISIN_RE = re.compile(r"^UA\d{10}$")
DEFAULT_FACE_VALUE = 1000.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. Розбиття CSV на секції
# ─────────────────────────────────────────────────────────────────────────────

def _split_ib_sections(text: str) -> Dict[str, pd.DataFrame]:
    """
    Розбиває Activity Statement CSV на секції.
    Повертає {section_name: DataFrame з рядками типу "Data"}.
    """
    sections: Dict[str, List[List[str]]] = defaultdict(list)
    headers: Dict[str, List[str]] = {}

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 2:
            continue
        section, kind = row[0], row[1]
        payload = row[2:]
        if kind == "Header":
            headers[section] = payload
        elif kind == "Data":
            sections[section].append(payload)

    out: Dict[str, pd.DataFrame] = {}
    for section, rows in sections.items():
        cols = headers.get(section)
        if not cols:
            continue
        # Вирівнюємо довжини рядків під заголовок
        norm = [r + [""] * (len(cols) - len(r)) if len(r) < len(cols) else r[: len(cols)]
                for r in rows]
        out[section] = pd.DataFrame(norm, columns=cols)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2. Допоміжне: ISIN з Security ID або Symbol
# ─────────────────────────────────────────────────────────────────────────────

def _build_isin_lookup(fii_df: Optional[pd.DataFrame]) -> Dict[str, str]:
    """
    Будує мапу Symbol → ISIN з секції Financial Instrument Information.
    """
    lookup: Dict[str, str] = {}
    if fii_df is None or fii_df.empty:
        return lookup
    sym_col = "Symbol" if "Symbol" in fii_df.columns else None
    sid_col = "Security ID" if "Security ID" in fii_df.columns else None
    if not sym_col or not sid_col:
        return lookup
    for _, r in fii_df.iterrows():
        sym = str(r[sym_col]).strip().upper()
        sid = str(r[sid_col]).strip().upper()
        if sym and UA_ISIN_RE.match(sid):
            lookup[sym] = sid
    return lookup


def _resolve_isin(symbol: str, isin_lookup: Dict[str, str]) -> Optional[str]:
    """ISIN з Symbol (для OTC-облігацій IB сам Symbol часто = ISIN) або з мапи."""
    s = (symbol or "").strip().upper()
    if UA_ISIN_RE.match(s):
        return s
    return isin_lookup.get(s)


def _is_bond(asset_category: str) -> bool:
    return "BOND" in (asset_category or "").upper()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Перетворення секції Trades у позиції портфеля
# ─────────────────────────────────────────────────────────────────────────────

def _trades_to_portfolio_rows(
    trades_df: pd.DataFrame,
    isin_lookup: Dict[str, str],
    face_value: float,
) -> List[Dict]:
    rows: List[Dict] = []
    if trades_df.empty:
        return rows

    # Тільки реальні угоди (не SubTotal/Total)
    if "DataDiscriminator" in trades_df.columns:
        trades_df = trades_df[
            trades_df["DataDiscriminator"].str.lower().isin({"order", "trade"})
        ]

    for _, r in trades_df.iterrows():
        try:
            if not _is_bond(r.get("Asset Category", "")):
                continue
            isin = _resolve_isin(r.get("Symbol", ""), isin_lookup)
            if not isin:
                continue

            qty = float(str(r.get("Quantity", "0")).replace(",", ""))
            if qty == 0:
                continue
            price = float(str(r.get("T. Price", "0")).replace(",", ""))
            commission = abs(float(str(r.get("Comm/Fee", "0")).replace(",", "")))
            dt_raw = str(r.get("Date/Time", "")).split(",")[0].strip()
            buy_dt = pd.to_datetime(dt_raw, errors="coerce")

            rows.append({
                "isin":              isin,
                "amount_lots":       qty / face_value,
                "avg_buy_price_pct": price,
                "buy_date":          buy_dt if not pd.isna(buy_dt) else date.today(),
                "commission_uah":    commission,
                "account_id":        "ib",
            })
        except (ValueError, KeyError) as e:
            log.debug("Пропущено рядок Trades: %s", e)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fallback: Open Positions (без дат купівлі та комісій)
# ─────────────────────────────────────────────────────────────────────────────

def _open_positions_to_portfolio_rows(
    pos_df: pd.DataFrame,
    isin_lookup: Dict[str, str],
    face_value: float,
) -> List[Dict]:
    rows: List[Dict] = []
    if pos_df.empty:
        return rows

    if "DataDiscriminator" in pos_df.columns:
        pos_df = pos_df[pos_df["DataDiscriminator"].str.lower() == "summary"]

    for _, r in pos_df.iterrows():
        try:
            if not _is_bond(r.get("Asset Category", "")):
                continue
            isin = _resolve_isin(r.get("Symbol", ""), isin_lookup)
            if not isin:
                continue

            qty = float(str(r.get("Quantity", "0")).replace(",", ""))
            if qty == 0:
                continue
            price = float(str(r.get("Cost Price", "0")).replace(",", ""))

            rows.append({
                "isin":              isin,
                "amount_lots":       qty / face_value,
                "avg_buy_price_pct": price,
                "buy_date":          date.today(),
                "commission_uah":    0.0,
                "account_id":        "ib",
            })
        except (ValueError, KeyError) as e:
            log.debug("Пропущено рядок Open Positions: %s", e)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 5. Публічна точка входу
# ─────────────────────────────────────────────────────────────────────────────

def parse_ib_activity_csv(
    raw: str | bytes,
    face_value: float = DEFAULT_FACE_VALUE,
) -> pd.DataFrame:
    """
    Парсить Activity Statement CSV від Interactive Brokers і повертає
    DataFrame з ОВДП-позиціями, готовий для злиття з реєстром облігацій.

    Алгоритм:
        1) Спершу пробуємо секцію Trades (повна історія купівель).
        2) Якщо її немає — fallback на Open Positions (без дат і комісій).
        3) Фільтруємо Asset Category == "Bonds" + ISIN ∈ UA-простору.
        4) Агрегуємо по ISIN через ``parse_portfolio_input``
           (середньозважена ціна, сума лотів, мін. дата, сума комісій).

    :param raw: вміст CSV-файлу (str або bytes)
    :param face_value: номінал облігації для конвертації Quantity → лоти
                       (default 1000 — стандарт для ОВДП)
    """
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw

    sections = _split_ib_sections(text)
    if not sections:
        log.error("IB CSV: жодної секції не розпізнано")
        return pd.DataFrame()

    isin_lookup = _build_isin_lookup(sections.get("Financial Instrument Information"))

    rows = _trades_to_portfolio_rows(
        sections.get("Trades", pd.DataFrame()), isin_lookup, face_value,
    )
    if not rows:
        log.info("IB CSV: Trades порожній — fallback на Open Positions")
        rows = _open_positions_to_portfolio_rows(
            sections.get("Open Positions", pd.DataFrame()), isin_lookup, face_value,
        )

    if not rows:
        log.warning("IB CSV: ОВДП-позицій не знайдено")
        return pd.DataFrame()

    log.info("IB CSV: %d сирих ОВДП-операцій → агрегація", len(rows))
    return parse_portfolio_input(rows)
