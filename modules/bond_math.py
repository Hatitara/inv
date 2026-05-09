"""
bond_math.py — Базові розрахунки для ОВДП (Ukrainian Government Bonds)
"""
import numpy as np
import pandas as pd
from datetime import date, datetime
from typing import Optional


SETTLE_DATE = date(2025, 5, 9)   # T+0 — буде замінено на today()


def today() -> date:
    return date.today()


def days_between(d1: date, d2: date) -> int:
    return (d2 - d1).days


def generate_coupon_schedule(
    issue_date: date,
    maturity_date: date,
    coupon_frequency_days: int,
) -> list[date]:
    """Generate all future coupon payment dates (from today onwards)."""
    from datetime import timedelta
    dates = []
    n = 1
    while True:
        d = issue_date + timedelta(days=coupon_frequency_days * n)
        if d > maturity_date:
            break
        if d >= today():
            dates.append(d)
        n += 1
    # Always include maturity as last cash flow
    if maturity_date >= today() and (not dates or dates[-1] != maturity_date):
        dates.append(maturity_date)
    return sorted(set(dates))


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=n)


def accrued_interest(
    nominal: float,
    coupon_rate_annual: float,      # fraction, e.g. 0.1547
    coupon_frequency_days: int,
    issue_date: date,
    settle: date,
) -> float:
    """НКД — накопичений купонний дохід (Accrued Interest)."""
    coupon_per_period = nominal * coupon_rate_annual * coupon_frequency_days / 365
    # days since last coupon
    periods_elapsed = days_between(issue_date, settle) // coupon_frequency_days
    last_coupon_date = _add_days(issue_date, periods_elapsed * coupon_frequency_days)
    days_since_last = days_between(last_coupon_date, settle)
    ai = nominal * coupon_rate_annual * days_since_last / 365
    return round(ai, 4)


def dirty_price_from_ytm(
    nominal: float,
    coupon_rate_annual: float,
    coupon_frequency_days: int,
    issue_date: date,
    maturity_date: date,
    ytm: float,                     # fraction per annum
    settle: Optional[date] = None,
) -> float:
    """
    Dirty price (повна ціна) через YTM методом дисконтування грошових потоків.
    Для SIM (simple interest to maturity) — окремий розрахунок.
    """
    if settle is None:
        settle = today()
    schedule = generate_coupon_schedule(issue_date, maturity_date, coupon_frequency_days)
    coupon = nominal * coupon_rate_annual * coupon_frequency_days / 365
    pv = 0.0
    for pmt_date in schedule:
        t = days_between(settle, pmt_date) / 365
        cf = coupon if pmt_date != maturity_date else coupon + nominal
        if pmt_date == maturity_date and pmt_date not in schedule[:-1]:
            cf = coupon + nominal
        pv += cf / (1 + ytm) ** t
    return round(pv, 4)


def ytm_from_price(
    nominal: float,
    coupon_rate_annual: float,
    coupon_frequency_days: int,
    issue_date: date,
    maturity_date: date,
    dirty_price: float,
    settle: Optional[date] = None,
    use_sim: bool = False,
) -> float:
    """
    Знаходимо YTM (або SIM) чисельно методом Брента.
    """
    if settle is None:
        settle = today()
    if use_sim:
        return _sim_from_price(nominal, coupon_rate_annual, coupon_frequency_days,
                               issue_date, maturity_date, dirty_price, settle)
    from scipy.optimize import brentq
    schedule = generate_coupon_schedule(issue_date, maturity_date, coupon_frequency_days)
    coupon = nominal * coupon_rate_annual * coupon_frequency_days / 365

    def npv(y):
        pv = 0.0
        for pmt_date in schedule:
            t = days_between(settle, pmt_date) / 365
            cf = coupon
            if pmt_date == schedule[-1]:
                cf += nominal
            pv += cf / (1 + y) ** t
        return pv - dirty_price

    try:
        return brentq(npv, -0.9999, 5.0, xtol=1e-8)
    except Exception:
        return np.nan


def _sim_from_price(
    nominal, coupon_rate_annual, coupon_frequency_days,
    issue_date, maturity_date, dirty_price, settle
) -> float:
    """
    SIM = Simple Interest to Maturity.
    Price = sum(CF_i) / (1 + r * T)  — проста ставка на весь горизонт.
    Розв'язується чисельно.
    """
    from scipy.optimize import brentq
    schedule = generate_coupon_schedule(issue_date, maturity_date, coupon_frequency_days)
    coupon = nominal * coupon_rate_annual * coupon_frequency_days / 365
    T = days_between(settle, maturity_date) / 365

    def npv(r):
        pv = 0.0
        for pmt_date in schedule:
            t = days_between(settle, pmt_date) / 365
            cf = coupon
            if pmt_date == schedule[-1]:
                cf += nominal
            pv += cf / (1 + r * t)
        return pv - dirty_price

    try:
        return brentq(npv, -0.9999, 5.0, xtol=1e-8)
    except Exception:
        return np.nan


def macaulay_duration(
    nominal: float,
    coupon_rate_annual: float,
    coupon_frequency_days: int,
    issue_date: date,
    maturity_date: date,
    ytm: float,
    settle: Optional[date] = None,
) -> float:
    """Дюрація Макколея (у роках)."""
    if settle is None:
        settle = today()
    schedule = generate_coupon_schedule(issue_date, maturity_date, coupon_frequency_days)
    coupon = nominal * coupon_rate_annual * coupon_frequency_days / 365
    pv_total = 0.0
    weighted = 0.0
    for pmt_date in schedule:
        t = days_between(settle, pmt_date) / 365
        cf = coupon
        if pmt_date == schedule[-1]:
            cf += nominal
        pv_cf = cf / (1 + ytm) ** t
        pv_total += pv_cf
        weighted += t * pv_cf
    if pv_total == 0:
        return np.nan
    return round(weighted / pv_total, 4)


def modified_duration(mac_dur: float, ytm: float) -> float:
    """Модифікована дюрація = MacD / (1 + YTM)."""
    return round(mac_dur / (1 + ytm), 4)


def dv01(dirty_price: float, mod_dur: float) -> float:
    """DV01 — зміна ціни при зсуві ставки на 1 б.п. (0.01%)."""
    return round(dirty_price * mod_dur * 0.0001, 4)


def convexity(
    nominal: float,
    coupon_rate_annual: float,
    coupon_frequency_days: int,
    issue_date: date,
    maturity_date: date,
    ytm: float,
    settle: Optional[date] = None,
) -> float:
    """Опуклість (Convexity)."""
    if settle is None:
        settle = today()
    schedule = generate_coupon_schedule(issue_date, maturity_date, coupon_frequency_days)
    coupon = nominal * coupon_rate_annual * coupon_frequency_days / 365
    pv_total = 0.0
    conv = 0.0
    for pmt_date in schedule:
        t = days_between(settle, pmt_date) / 365
        cf = coupon
        if pmt_date == schedule[-1]:
            cf += nominal
        pv_cf = cf / (1 + ytm) ** t
        pv_total += pv_cf
        conv += t * (t + 1) * pv_cf
    if pv_total == 0:
        return np.nan
    return round(conv / (pv_total * (1 + ytm) ** 2), 4)


def current_yield(coupon_annual: float, dirty_price: float) -> float:
    """Поточна дохідність = річний купон / брудна ціна."""
    if dirty_price == 0:
        return np.nan
    return round(coupon_annual / dirty_price, 6)


def compute_all_metrics(
    nominal: float,
    coupon_rate_pct: float,         # in percent, e.g. 15.47
    coupon_frequency_days: int,
    issue_date: date,
    maturity_date: date,
    market_price: float,            # dirty price from ICU
    ytm_pct: float,                 # YTM from ICU in percent
    use_sim: bool = False,
    settle: Optional[date] = None,
) -> dict:
    if settle is None:
        settle = today()

    r = coupon_rate_pct / 100
    ytm = ytm_pct / 100

    ai = accrued_interest(nominal, r, coupon_frequency_days, issue_date, settle)
    clean_price = market_price - ai
    coupon_annual = nominal * r

    mac_dur = macaulay_duration(nominal, r, coupon_frequency_days,
                                issue_date, maturity_date, ytm, settle)
    mod_dur = modified_duration(mac_dur, ytm)
    dv01_val = dv01(market_price, mod_dur)
    conv = convexity(nominal, r, coupon_frequency_days,
                     issue_date, maturity_date, ytm, settle)
    cy = current_yield(coupon_annual, market_price)
    ttm = days_between(settle, maturity_date) / 365

    return {
        "НКД (Accrued Interest), ₴": round(ai, 2),
        "Чиста ціна (Clean Price), ₴": round(clean_price, 2),
        "Брудна ціна (Dirty Price), ₴": round(market_price, 2),
        "Річний купон, ₴": round(coupon_annual, 2),
        "YTM / SIM, %": round(ytm_pct, 2),
        "Поточна дохідність, %": round(cy * 100, 2),
        "До погашення, рок.": round(ttm, 3),
        "Дюрація Макколея, рок.": mac_dur,
        "Модифікована дюрація": mod_dur,
        "DV01, ₴": dv01_val,
        "Опуклість (Convexity)": conv,
    }
