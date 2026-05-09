"""
portfolio.py — Портфельна аналітика ОВДП
"""
import numpy as np
import pandas as pd
from datetime import date
from modules.bond_math import (
    compute_all_metrics, accrued_interest, macaulay_duration,
    modified_duration, dv01, convexity, today
)


def build_portfolio_df(
    holdings: list[dict],   # [{"isin": ..., "quantity": int, "avg_buy_price": float}]
    market_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    holdings — список позицій портфеля
    market_df — merged ICU + OVDP df з розрахованими метриками
    Returns enriched portfolio DataFrame.
    """
    rows = []
    for h in holdings:
        isin = h['isin']
        qty = h['quantity']
        avg_price = h.get('avg_buy_price', None)

        row_m = market_df[market_df['isin'] == isin]
        if row_m.empty:
            continue
        r = row_m.iloc[0]

        nominal = float(r.get('nominal', 1000))
        market_dirty = float(r.get('sell_price') or r.get('buy_price') or nominal)

        market_value = market_dirty * qty
        face_value = nominal * qty

        # P&L vs avg buy
        if avg_price and avg_price > 0:
            cost_basis = avg_price * qty
            unrealized_pnl = market_value - cost_basis
            unrealized_pnl_pct = (market_value - cost_basis) / cost_basis * 100
        else:
            cost_basis = None
            unrealized_pnl = None
            unrealized_pnl_pct = None

        # Accrued coupon income
        ai_per_bond = float(r.get('НКД (Accrued Interest), ₴', 0) or 0)
        total_ai = ai_per_bond * qty

        # Duration / DV01 at portfolio level
        mod_dur = float(r.get('Модифікована дюрація', 0) or 0)
        dv01_per_bond = float(r.get('DV01, ₴', 0) or 0)
        total_dv01 = dv01_per_bond * qty

        mac_dur = float(r.get('Дюрація Макколея, рок.', 0) or 0)
        ytm = float(r.get('YTM / SIM, %', 0) or 0)
        annual_coupon_income = float(r.get('Річний купон, ₴', 0) or 0) * qty

        conv = float(r.get('Опуклість (Convexity)', 0) or 0)

        rows.append({
            'ISIN': isin,
            'Назва': r.get('name') or '—',
            'К-сть': qty,
            'Ринк. ціна (брудна), ₴': round(market_dirty, 2),
            'Ринк. вартість, ₴': round(market_value, 2),
            'Номінальна вартість, ₴': round(face_value, 2),
            'Сер. ціна купівлі, ₴': round(avg_price, 2) if avg_price else None,
            'Собівартість, ₴': round(cost_basis, 2) if cost_basis else None,
            'НКД (накоп.), ₴': round(total_ai, 2),
            'Нереаліз. P&L, ₴': round(unrealized_pnl, 2) if unrealized_pnl is not None else None,
            'Нереаліз. P&L, %': round(unrealized_pnl_pct, 2) if unrealized_pnl_pct is not None else None,
            'YTM/SIM ринк., %': ytm,
            'Дюрація Макколея, рок.': mac_dur,
            'Мод. дюрація': mod_dur,
            'Опуклість (Convexity)': conv,
            'DV01 (портф.), ₴': round(total_dv01, 2),
            'Річний купон. дохід, ₴': round(annual_coupon_income, 2),
            'Дата погашення': r.get('maturity'),
        })

    return pd.DataFrame(rows)


def portfolio_summary(portfolio_df: pd.DataFrame) -> dict:
    """Агреговані показники портфеля."""
    if portfolio_df.empty:
        return {}
    total_market = portfolio_df['Ринк. вартість, ₴'].sum()
    total_face = portfolio_df['Номінальна вартість, ₴'].sum()
    total_ai = portfolio_df['НКД (накоп.), ₴'].sum()
    total_dv01 = portfolio_df['DV01 (портф.), ₴'].sum()
    total_coupon = portfolio_df['Річний купон. дохід, ₴'].sum()

    # Weighted average YTM / Duration / Convexity (MV-weighted, consistent set)
    weights = portfolio_df['Ринк. вартість, ₴'] / total_market if total_market else 1
    wav_ytm    = (portfolio_df['YTM/SIM ринк., %'] * weights).sum()
    wav_dur    = (portfolio_df['Дюрація Макколея, рок.'] * weights).sum()
    wav_moddur = (portfolio_df['Мод. дюрація'] * weights).sum()
    if 'Опуклість (Convexity)' in portfolio_df.columns:
        wav_conv = (portfolio_df['Опуклість (Convexity)'] * weights).sum()
    else:
        wav_conv = 0.0

    pnl_rows = portfolio_df['Нереаліз. P&L, ₴'].dropna()
    total_pnl = pnl_rows.sum() if not pnl_rows.empty else None

    return {
        "Ринкова вартість портфеля, ₴": round(total_market, 2),
        "Номінальна вартість, ₴": round(total_face, 2),
        "Накоп. купон. дохід (НКД), ₴": round(total_ai, 2),
        "Річний купон. дохід, ₴": round(total_coupon, 2),
        "Нереалізований P&L, ₴": round(total_pnl, 2) if total_pnl is not None else "н/д",
        "Серед. зважений YTM, %": round(wav_ytm, 2),
        "Серед. зважена дюрація (Маккол.), рок.": round(wav_dur, 3),
        "Серед. зважена мод. дюрація": round(wav_moddur, 3),
        "Серед. зважена опуклість": round(wav_conv, 3),
        "DV01 портфеля, ₴": round(total_dv01, 2),
    }
