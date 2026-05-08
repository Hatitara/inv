"""
analytics.py
============
Фінансове ядро: всі показники, корисні при виборі та оцінці ОВДП.

Функції:
  generate_coupon_dates()   — розклад виплат
  day_count_fraction()      — Act/Act, 30/360, Act/365, Act/360
  accrued_interest()        — НКД
  clean_price() / dirty_price()
  ytm()                     — YTM після військового збору
  ytm_pretax()              — YTM до оподаткування
  macaulay_duration()       — Дюрація Маколея
  modified_duration()       — Модифікована дюрація
  convexity()               — Опуклість
  dv01()                    — Вартість 1 базисного пункту
  simple_yield()            — Поточна доходність (поточний купон / ціна)
  holding_period_return()   — HPR для вже куплених паперів
  breakeven_rate_change()   — Зміна ставки, при якій прибуток = 0
  real_yield()              — Реальна доходність (YTM - інфляція)
  bond_metrics()            — Зводна таблиця всіх метрик для одного паперу
  portfolio_analytics()     — Агрегат по портфелю
  build_cash_flow_schedule()
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from config import MILITARY_LEVY, TAX_TOTAL, YTM_RATE_BOUNDS, YTM_SOLVER_MAX_ITER, YTM_SOLVER_TOLERANCE

log = logging.getLogger("analytics")


# ─────────────────────────────────────────────────────────────────────────────
# ДОПОМІЖНІ ФУНКЦІЇ
# ─────────────────────────────────────────────────────────────────────────────

def generate_coupon_dates(
    issue_date: date,
    maturity_date: date,
    freq: int,
) -> List[date]:
    """Backward scheduling від maturity назад з кроком 12//freq місяців."""
    if freq == 0:
        return []
    months_step = 12 // freq
    dates: List[date] = []
    current = maturity_date
    while current > issue_date:
        dates.append(current)
        month = current.month - months_step
        year  = current.year
        while month <= 0:
            month += 12
            year  -= 1
        last_day = calendar.monthrange(year, month)[1]
        current  = date(year, month, min(current.day, last_day))
    return sorted(dates)


def _leap_in_period(start: date, end: date) -> bool:
    return any(
        (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
        for y in range(start.year, end.year + 1)
    )


def day_count_fraction(
    start: date,
    end: date,
    convention: str = "ACT/ACT",
    period_start: Optional[date] = None,
    period_end:   Optional[date] = None,
) -> float:
    """
    Day Count Fraction між двома датами.
    ACT/ACT  — ICMA-стандарт (потребує period_start/period_end)
    30/360   — Bond Basis
    ACT/365  — Fixed
    ACT/360  — Money Market
    """
    actual = (end - start).days
    if actual < 0:
        return 0.0

    if convention == "ACT/ACT":
        if period_start and period_end:
            period_len = (period_end - period_start).days
            denom = float(period_len) if period_len > 0 else 365.0
        else:
            denom = 366.0 if _leap_in_period(start, end) else 365.0
        return actual / denom

    if convention == "30/360":
        d1, m1, y1 = start.day, start.month, start.year
        d2, m2, y2 = end.day,   end.month,   end.year
        d1 = min(d1, 30)
        if d1 == 30 and d2 == 31:
            d2 = 30
        return (360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)) / 360.0

    if convention == "ACT/365":
        return actual / 365.0

    if convention == "ACT/360":
        return actual / 360.0

    raise ValueError(f"Невідома конвенція: {convention}")


# ─────────────────────────────────────────────────────────────────────────────
# ЦІНОУТВОРЕННЯ
# ─────────────────────────────────────────────────────────────────────────────

def accrued_interest(
    face_value:      float,
    coupon_rate_pct: float,
    last_coupon_date: date,
    settlement_date:  date,
    next_coupon_date: date,
    convention:      str = "ACT/ACT",
) -> float:
    """НКД на 1 папір у валюті номіналу."""
    if coupon_rate_pct == 0 or last_coupon_date >= settlement_date:
        return 0.0

    period_dcf = day_count_fraction(last_coupon_date, next_coupon_date,
                                    convention, last_coupon_date, next_coupon_date)
    if period_dcf <= 0:
        return 0.0

    elapsed_dcf = day_count_fraction(last_coupon_date, settlement_date,
                                     convention, last_coupon_date, next_coupon_date)

    coupon_per_period = face_value * coupon_rate_pct / 100.0 * period_dcf
    return round(coupon_per_period * (elapsed_dcf / period_dcf), 6)


def dirty_price(face_value: float, clean_price_pct: float, ai: float) -> float:
    """Повна ціна = чиста ціна + НКД."""
    return face_value * clean_price_pct / 100.0 + ai


def clean_price_from_dirty(face_value: float, dirty: float, ai: float) -> float:
    return (dirty - ai) / face_value * 100.0


def _get_ai_for_bond(bond: pd.Series, settle: date) -> float:
    """Утиліта: НКД для рядка з dim_bonds."""
    if bond["is_discount"]:
        return 0.0
    pay_dates = bond["coupon_pay_dates"]
    past   = [d for d in pay_dates if d <= settle]
    future = [d for d in pay_dates if d >  settle]
    if not past or not future:
        return 0.0
    return accrued_interest(
        face_value=bond["face_value"],
        coupon_rate_pct=bond["coupon_rate_pct"],
        last_coupon_date=max(past),
        settlement_date=settle,
        next_coupon_date=min(future),
        convention=bond["day_count"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# ДОХОДНОСТІ
# ─────────────────────────────────────────────────────────────────────────────

def _build_cashflows(
    face_value:      float,
    coupon_rate_pct: float,
    coupon_freq:     int,
    settlement_date: date,
    maturity_date:   date,
    pay_dates:       List[date],
    day_count:       str,
    net_coupon_factor: float,   # 1.0 - tax
) -> List[Tuple[float, float]]:
    """
    Повертає [(t_years, net_cf), ...] для всіх майбутніх виплат.
    Тіло погашення додається без податку.
    """
    cfs: List[Tuple[float, float]] = []
    future = sorted(d for d in pay_dates if d > settlement_date)
    coupon_gross = face_value * coupon_rate_pct / 100.0 / max(coupon_freq, 1)
    coupon_net   = coupon_gross * net_coupon_factor

    maturity_added = False
    for d in future:
        t  = day_count_fraction(settlement_date, d, day_count)
        cf = coupon_net
        if d >= maturity_date:
            cf += face_value
            maturity_added = True
        cfs.append((t, cf))

    if not maturity_added and maturity_date > settlement_date:
        t = day_count_fraction(settlement_date, maturity_date, day_count)
        cfs.append((t, face_value))

    return cfs


def ytm(
    face_value:      float,
    coupon_rate_pct: float,
    coupon_freq:     int,
    price_pct:       float,
    settlement_date: date,
    maturity_date:   date,
    pay_dates:       List[date],
    day_count:       str  = "ACT/ACT",
    after_tax:       bool = True,
    is_discount:     bool = False,
) -> float:
    """
    YTM у % річних. after_tax=True — після військового збору 1.5%.

    Дисконтний папір: аналітична формула r = (FV_net/P)^(1/T) - 1
    Купонний папір:  brentq на NPV(r)=0
    """
    tax   = MILITARY_LEVY if after_tax else 0.0
    price = face_value * price_pct / 100.0
    T     = day_count_fraction(settlement_date, maturity_date, day_count)

    if T <= 0:
        return 0.0

    if is_discount or coupon_freq == 0:
        gain     = face_value - price
        net_gain = gain * (1 - tax)
        fv_net   = price + net_gain
        if fv_net <= 0 or price <= 0:
            return 0.0
        return round(((fv_net / price) ** (1.0 / T) - 1.0) * 100, 6)

    cfs = _build_cashflows(face_value, coupon_rate_pct, coupon_freq,
                           settlement_date, maturity_date, pay_dates,
                           day_count, net_coupon_factor=1.0 - tax)
    if not cfs:
        return 0.0

    def _npv(r: float) -> float:
        return sum(cf / (1 + r) ** t for t, cf in cfs) - price

    try:
        r = brentq(_npv, *YTM_RATE_BOUNDS,
                   xtol=YTM_SOLVER_TOLERANCE, maxiter=YTM_SOLVER_MAX_ITER)
        return round(r * 100, 6)
    except ValueError:
        return float("nan")


def simple_yield(
    face_value:      float,
    coupon_rate_pct: float,
    clean_price_pct: float,
) -> float:
    """Current yield = річний купон / повна ціна × 100%."""
    price = face_value * clean_price_pct / 100.0
    if price <= 0:
        return 0.0
    return round(face_value * coupon_rate_pct / 100.0 / price * 100, 4)


def ytm_pretax(
    face_value: float, coupon_rate_pct: float, coupon_freq: int,
    price_pct: float, settlement_date: date, maturity_date: date,
    pay_dates: List[date], day_count: str = "ACT/ACT", is_discount: bool = False,
) -> float:
    return ytm(face_value, coupon_rate_pct, coupon_freq, price_pct,
               settlement_date, maturity_date, pay_dates, day_count,
               after_tax=False, is_discount=is_discount)


# ─────────────────────────────────────────────────────────────────────────────
# ДЮРАЦІЯ ТА ОПУКЛІСТЬ
# ─────────────────────────────────────────────────────────────────────────────

def macaulay_duration(
    face_value:      float,
    coupon_rate_pct: float,
    coupon_freq:     int,
    ytm_pct:         float,
    settlement_date: date,
    maturity_date:   date,
    pay_dates:       List[date],
    day_count:       str  = "ACT/ACT",
    after_tax:       bool = True,
    is_discount:     bool = False,
) -> float:
    """Дюрація Маколея у роках."""
    T = day_count_fraction(settlement_date, maturity_date, day_count)
    if is_discount or coupon_freq == 0:
        return round(T, 6)

    r   = ytm_pct / 100.0
    tax = MILITARY_LEVY if after_tax else 0.0
    cfs = _build_cashflows(face_value, coupon_rate_pct, coupon_freq,
                           settlement_date, maturity_date, pay_dates,
                           day_count, net_coupon_factor=1.0 - tax)
    if not cfs:
        return 0.0

    price       = sum(cf / (1 + r) ** t for t, cf in cfs)
    weighted    = sum(t * cf / (1 + r) ** t for t, cf in cfs)
    return round(weighted / price, 6) if price else 0.0


def modified_duration(
    face_value:      float,
    coupon_rate_pct: float,
    coupon_freq:     int,
    ytm_pct:         float,
    settlement_date: date,
    maturity_date:   date,
    pay_dates:       List[date],
    day_count:       str  = "ACT/ACT",
    after_tax:       bool = True,
    is_discount:     bool = False,
) -> float:
    """Modified Duration = Macaulay / (1 + YTM/freq)."""
    mac = macaulay_duration(face_value, coupon_rate_pct, coupon_freq, ytm_pct,
                            settlement_date, maturity_date, pay_dates,
                            day_count, after_tax, is_discount)
    freq_denom = max(coupon_freq, 1)
    r = ytm_pct / 100.0
    return round(mac / (1 + r / freq_denom), 6)


def convexity(
    face_value:      float,
    coupon_rate_pct: float,
    coupon_freq:     int,
    ytm_pct:         float,
    settlement_date: date,
    maturity_date:   date,
    pay_dates:       List[date],
    day_count:       str  = "ACT/ACT",
    after_tax:       bool = True,
    is_discount:     bool = False,
) -> float:
    """
    Опуклість (Convexity).
    C = [Σ t*(t+1)*CF/(1+r)^(t+2)] / P
    Показує, наскільки дюрація змінюється при русі ставок.
    """
    if is_discount or coupon_freq == 0:
        T = day_count_fraction(settlement_date, maturity_date, day_count)
        r = ytm_pct / 100.0
        tax  = MILITARY_LEVY if after_tax else 0.0
        gain = (face_value - face_value) * (1 - tax) + face_value
        p    = gain / (1 + r) ** T
        return round(T * (T + 1) / (1 + r) ** 2, 6)

    r   = ytm_pct / 100.0
    tax = MILITARY_LEVY if after_tax else 0.0
    cfs = _build_cashflows(face_value, coupon_rate_pct, coupon_freq,
                           settlement_date, maturity_date, pay_dates,
                           day_count, net_coupon_factor=1.0 - tax)
    if not cfs:
        return 0.0

    price  = sum(cf / (1 + r) ** t for t, cf in cfs)
    cvx    = sum(t * (t + 1) * cf / (1 + r) ** (t + 2) for t, cf in cfs)
    return round(cvx / price, 6) if price else 0.0


def dv01(
    face_value:    float,
    mod_dur:       float,
    price_pct:     float,
    position_lots: float = 1.0,
) -> float:
    """
    DV01 (Dollar Value of 1bp) — зміна вартості позиції при +1 б.п. YTM.
    DV01 = -ModDur × P_dirty × 0.0001 × lots
    """
    p_dirty = face_value * price_pct / 100.0
    return round(-mod_dur * p_dirty * 0.0001 * position_lots, 4)


# ─────────────────────────────────────────────────────────────────────────────
# ДОДАТКОВІ АНАЛІТИЧНІ МЕТРИКИ
# ─────────────────────────────────────────────────────────────────────────────

def holding_period_return(
    buy_price_pct:     float,
    current_price_pct: float,
    coupons_received:  float,   # Сума отриманих купонів (після ВЗ) на 1 папір
    face_value:        float,
) -> float:
    """
    HPR = (Поточна брудна ціна + отримані купони - куплено) / куплено × 100%
    """
    buy    = face_value * buy_price_pct     / 100.0
    current= face_value * current_price_pct / 100.0
    if buy <= 0:
        return 0.0
    return round((current - buy + coupons_received) / buy * 100, 4)


def breakeven_rate_change(
    ytm_pct:  float,
    mod_dur:  float,
    cvx:      float,
    horizon_years: float = 1.0,
) -> float:
    # 1. Додаємо захисну перевірку
    if mod_dur <= 0:
        return 0.0

    carry = ytm_pct * horizon_years
    a = 0.5 * cvx
    b = mod_dur
    c = -carry / 100.0
    
    discriminant = b ** 2 - 4 * a * c
    
    if discriminant < 0 or a == 0:
        # 2. Додатковий захист тут (на випадок дуже малих значень)
        if abs(mod_dur) < 1e-7:
            return 0.0
        be = carry / (mod_dur * 100.0)
    else:
        be = (-b + np.sqrt(discriminant)) / (2 * a)
        
    return round(be * 10_000, 2)


def real_yield(ytm_after_tax_pct: float, inflation_pct: float) -> float:
    """Реальна доходність за формулою Фішера: (1+ytm)/(1+inf) - 1."""
    r = ytm_after_tax_pct / 100.0
    i = inflation_pct     / 100.0
    return round(((1 + r) / (1 + i) - 1) * 100, 4)


def spread_over_key_rate(ytm_after_tax_pct: float, key_rate_pct: float) -> float:
    """Спред YTM над обліковою ставкою НБУ, б.п."""
    return round((ytm_after_tax_pct - key_rate_pct) * 100, 1)


# ─────────────────────────────────────────────────────────────────────────────
# ЗВОДНИЙ РОЗРАХУНОК МЕТРИК ДЛЯ ОДНОГО ПАПЕРУ
# ─────────────────────────────────────────────────────────────────────────────

def bond_metrics(
    bond:            pd.Series,
    settle:          date,
    price_pct:       Optional[float] = None,  # якщо None — берем market_price_pct
    fx_rates:        Optional[Dict[str, float]] = None,
    key_rate_pct:    float = 14.5,
    inflation_pct:   float = 9.7,
) -> Dict:
    """
    Повний набір метрик для одного паперу.
    Повертає словник, готовий для pd.DataFrame.
    """
    if fx_rates is None:
        fx_rates = {"UAH": 1.0, "USD": 41.0, "EUR": 44.5}

    if price_pct is None:
        price_pct = float(bond.get("market_price_pct", 100.0))

    pay_dates:  List[date] = bond["coupon_pay_dates"]
    is_disc:    bool       = bond["is_discount"]
    face:       float      = bond["face_value"]
    ccy:        str        = bond["currency"]
    freq:       int        = bond["coupon_freq"]
    coupon_pct: float      = bond["coupon_rate_pct"]
    dc:         str        = bond["day_count"]
    mat:        date       = bond["maturity_date"].date() if hasattr(bond["maturity_date"], "date") else bond["maturity_date"]

    fx = fx_rates.get(ccy, 1.0)
    T  = day_count_fraction(settle, mat, dc)

    # НКД
    ai = _get_ai_for_bond(bond, settle)

    # YTM
    ytm_net  = ytm(face, coupon_pct, freq, price_pct, settle, mat,
                   pay_dates, dc, after_tax=True,  is_discount=is_disc)
    ytm_brut = ytm(face, coupon_pct, freq, price_pct, settle, mat,
                   pay_dates, dc, after_tax=False, is_discount=is_disc)

    # Duration
    mac = macaulay_duration(face, coupon_pct, freq, ytm_net if not np.isnan(ytm_net) else 0.0,
                            settle, mat, pay_dates, dc, after_tax=True, is_discount=is_disc)
    mod = modified_duration(face, coupon_pct, freq, ytm_net if not np.isnan(ytm_net) else 0.0,
                            settle, mat, pay_dates, dc, after_tax=True, is_discount=is_disc)
    cvx = convexity(face, coupon_pct, freq, ytm_net if not np.isnan(ytm_net) else 0.0,
                    settle, mat, pay_dates, dc, after_tax=True, is_discount=is_disc)

    dv = dv01(face, mod, price_pct)
    be = breakeven_rate_change(ytm_net or 0.0, mod, cvx)

    curr_yield = simple_yield(face, coupon_pct, price_pct) if not is_disc else 0.0
    ry         = real_yield(ytm_net or 0.0, inflation_pct)
    spread     = spread_over_key_rate(ytm_net or 0.0, key_rate_pct)

    dirty = dirty_price(face, price_pct, ai)

    days_to_mat = max((mat - settle).days, 0)

    return {
        # Ідентифікатори
        "isin":                   bond["isin"],
        "series_code":            bond.get("series_code", bond["isin"]),
        "currency":               ccy,
        "is_discount":            is_disc,
        "day_count":              dc,
        # Параметри
        "face_value":             face,
        "coupon_rate_pct":        coupon_pct,
        "coupon_freq":            freq,
        "maturity_date":          mat,
        "days_to_maturity":       days_to_mat,
        "years_to_maturity":      round(T, 4),
        # Ціноутворення
        "clean_price_pct":        round(price_pct, 4),
        "accrued_interest":       round(ai, 4),
        "dirty_price_uah":        round(dirty * fx, 2),
        "fx_rate":                fx,
        # Доходність
        "ytm_gross_pct":          ytm_brut,
        "ytm_net_pct":            ytm_net,        # після ВЗ 1.5%
        "military_levy_drag_bp":  round((ytm_brut - ytm_net) * 100, 1) if not np.isnan(ytm_net) else None,
        "current_yield_pct":      curr_yield,
        "real_yield_pct":         ry,
        "spread_vs_key_rate_bp":  spread,
        # Ризик
        "macaulay_duration_yrs":  mac,
        "modified_duration_yrs":  mod,
        "convexity":              cvx,
        "dv01_uah":               round(dv * fx, 4),
        "breakeven_chg_bp":       be,
        # Ринкові
        "market_ytm_icu_pct":     float(bond.get("market_ytm_pct", 0.0) or 0.0),
        "volume_mln_uah":         float(bond.get("volume_mln_uah", 0.0) or 0.0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ПОРТФЕЛЬНА АНАЛІТИКА
# ─────────────────────────────────────────────────────────────────────────────

def portfolio_analytics(
    bonds_df:     pd.DataFrame,
    portfolio_df: pd.DataFrame,
    settle:       date,
    fx_rates:     Dict[str, float],
    key_rate_pct: float = 14.5,
    inflation_pct: float = 9.7,
) -> pd.DataFrame:
    """
    Будує детальну зводну таблицю по портфелю.
    Кожен рядок = одна ISIN-позиція + всі метрики + ринкова вартість.
    """
    rows = []
    for _, pos in portfolio_df.iterrows():
        isin = pos["isin"]
        bond_row = bonds_df[bonds_df["isin"] == isin]
        if bond_row.empty:
            log.warning("ISIN %s не в реєстрі — пропускаю", isin)
            continue
        bond = bond_row.iloc[0]

        buy_price = float(pos["avg_buy_price_pct"])
        lots      = float(pos["amount_lots"])
        ccy       = bond["currency"]
        fx        = fx_rates.get(ccy, 1.0)
        face      = bond["face_value"]

        # Метрики за поточною ринковою ціною
        metrics = bond_metrics(bond, settle, price_pct=None,
                               fx_rates=fx_rates, key_rate_pct=key_rate_pct,
                               inflation_pct=inflation_pct)

        # Вартість за купівельною ціною
        buy_ai = _get_ai_for_bond(bond, pos["buy_date"].date()
                                  if hasattr(pos["buy_date"], "date") else pos["buy_date"])
        cost_uah = (face * buy_price / 100.0 + buy_ai) * lots * fx + float(pos.get("commission_uah", 0))

        # Поточна ринкова вартість
        market_value_uah = metrics["dirty_price_uah"] * lots

        # P&L
        pnl_uah = market_value_uah - cost_uah

        # HPR (без врахування отриманих купонів — спрощено)
        hpr = holding_period_return(buy_price, metrics["clean_price_pct"], 0.0, face)

        row = {
            **metrics,
            # Позиція
            "position_lots":      lots,
            "avg_buy_price_pct":  buy_price,
            "cost_basis_uah":     round(cost_uah, 2),
            "market_value_uah":   round(market_value_uah, 2),
            "unrealized_pnl_uah": round(pnl_uah, 2),
            "unrealized_pnl_pct": round(pnl_uah / cost_uah * 100, 4) if cost_uah else 0,
            "hpr_pct":            hpr,
            # DV01 на всю позицію
            "position_dv01_uah":  round(metrics["dv01_uah"] * lots, 2),
            "nominal_total_uah":  round(face * lots * fx, 2),
        }
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    total_mv = df["market_value_uah"].sum()
    df["portfolio_weight_pct"] = (df["market_value_uah"] / total_mv * 100).round(4)
    df["dur_contribution"]     = (df["modified_duration_yrs"] * df["portfolio_weight_pct"] / 100).round(6)

    return df.sort_values("portfolio_weight_pct", ascending=False).reset_index(drop=True)


def portfolio_summary_stats(analytics_df: pd.DataFrame) -> Dict:
    """Агреговані показники по всьому портфелю."""
    if analytics_df.empty:
        return {}

    total_mv   = analytics_df["market_value_uah"].sum()
    total_cost = analytics_df["cost_basis_uah"].sum()
    w          = analytics_df["portfolio_weight_pct"] / 100.0

    def wavg(col): return (analytics_df[col] * w).sum()

    return {
        "total_market_value_uah":    round(total_mv, 0),
        "total_cost_basis_uah":      round(total_cost, 0),
        "total_unrealized_pnl_uah":  round(total_mv - total_cost, 0),
        "total_unrealized_pnl_pct":  round((total_mv - total_cost) / total_cost * 100, 2),
        "portfolio_ytm_net_pct":     round(wavg("ytm_net_pct"), 4),
        "portfolio_ytm_gross_pct":   round(wavg("ytm_gross_pct"), 4),
        "portfolio_macaulay_dur":    round(wavg("macaulay_duration_yrs"), 4),
        "portfolio_modified_dur":    round(analytics_df["dur_contribution"].sum(), 4),
        "portfolio_convexity":       round(wavg("convexity"), 4),
        "total_dv01_uah":            round(analytics_df["position_dv01_uah"].sum(), 2),
        "num_positions":             len(analytics_df),
        "uah_weight_pct":            round(analytics_df[analytics_df["currency"] == "UAH"]["portfolio_weight_pct"].sum(), 2),
        "usd_weight_pct":            round(analytics_df[analytics_df["currency"] == "USD"]["portfolio_weight_pct"].sum(), 2),
        "max_concentration_pct":     round(analytics_df["portfolio_weight_pct"].max(), 2),
        "portfolio_real_yield_pct":  round(wavg("real_yield_pct"), 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CASH FLOW SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────

def build_cash_flow_schedule(
    bonds_df:     pd.DataFrame,
    portfolio_df: pd.DataFrame,
    as_of:        date,
    fx_rates:     Dict[str, float],
) -> pd.DataFrame:
    """Розгорнутий часовий ряд майбутніх виплат (купони + погашення)."""
    records = []
    for _, pos in portfolio_df.iterrows():
        isin  = pos["isin"]
        lots  = float(pos["amount_lots"])
        bond_row = bonds_df[bonds_df["isin"] == isin]
        if bond_row.empty:
            continue
        bond  = bond_row.iloc[0]
        face  = bond["face_value"]
        ccy   = bond["currency"]
        fx    = fx_rates.get(ccy, 1.0)
        coupon_pct = bond["coupon_rate_pct"]
        freq  = bond["coupon_freq"]
        mat   = bond["maturity_date"].date() if hasattr(bond["maturity_date"], "date") else bond["maturity_date"]
        dc    = bond["day_count"]
        pay_dates: List[date] = bond["coupon_pay_dates"]

        if bond["is_discount"] or freq == 0:
            if mat > as_of:
                records.append({
                    "isin": isin, "series_code": bond.get("series_code", isin),
                    "cf_date": mat, "cf_type": "PRINCIPAL",
                    "gross_uah": round(face * lots * fx, 2),
                    "tax_uah":   0.0,
                    "net_uah":   round(face * lots * fx, 2),
                    "currency":  ccy, "position_lots": lots,
                })
        else:
            sorted_dates = sorted(pay_dates)
            prev = bond["issue_date"].date() if hasattr(bond["issue_date"], "date") else bond["issue_date"]

            for d in sorted_dates:
                if d <= as_of:
                    prev = d
                    continue
                nxt = d
                dcf   = day_count_fraction(prev, nxt, dc, prev, nxt)
                gross = face * coupon_pct / 100.0 * dcf * lots * fx
                tax   = gross * MILITARY_LEVY
                net   = gross - tax
                cf_type = "BOTH" if d >= mat else "COUPON"

                if d >= mat:
                    principal = face * lots * fx
                    gross += principal
                    net   += principal

                records.append({
                    "isin": isin, "series_code": bond.get("series_code", isin),
                    "cf_date": d, "cf_type": cf_type,
                    "gross_uah": round(gross, 2),
                    "tax_uah":   round(tax, 2),
                    "net_uah":   round(net, 2),
                    "currency":  ccy, "position_lots": lots,
                })
                prev = d

    if not records:
        return pd.DataFrame(columns=["isin","series_code","cf_date","cf_type",
                                     "gross_uah","tax_uah","net_uah","currency","position_lots"])
    df = pd.DataFrame(records)
    df["cf_date"] = pd.to_datetime(df["cf_date"])
    return df.sort_values("cf_date").reset_index(drop=True)