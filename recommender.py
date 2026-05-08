"""
recommender.py
==============
Рушій рекомендацій: аналізує ринок ОВДП (дані ICU) та поточний портфель,
дає пріоритизовані поради BUY / HOLD / REDUCE / SELL.

Логіка скорингу:
  1. Фільтрація: мінімальна YTM, термін, валюта
  2. Скоринг кожного паперу: YTM, duration fit, ліквідність, диверсифікація
  3. GAP-аналіз: порівняння портфеля з цільовими параметрами
  4. Формування списку дій з обґрунтуванням
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from analytics import bond_metrics, portfolio_summary_stats
from config import (
    MAX_SINGLE_ISIN_PCT, MILITARY_LEVY, SCORE_WEIGHTS,
    TARGET_DURATION_YEARS, TARGET_YTM_MIN_PCT,
)

log = logging.getLogger("recommender")


# ─────────────────────────────────────────────────────────────────────────────
# ПАРАМЕТРИ СТРАТЕГІЇ (можна передавати з UI)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategyParams:
    """Налаштування стратегії для генерації рекомендацій."""

    # Доходність
    min_ytm_net_pct:      float = TARGET_YTM_MIN_PCT
    prefer_ytm_above_pct: float = 16.0

    # Дюрація
    target_dur_min:  float = TARGET_DURATION_YEARS[0]
    target_dur_max:  float = TARGET_DURATION_YEARS[1]

    # Ризики
    max_single_isin_pct:  float = MAX_SINGLE_ISIN_PCT
    max_usd_weight_pct:   float = 30.0
    max_discount_pct:     float = 20.0

    # Ліквідність
    min_volume_mln_uah:   float = 50.0      # мінімальний обсяг торгів

    # Горизонт
    min_days_to_maturity: int   = 60
    max_days_to_maturity: int   = 3 * 365   # 3 роки

    # Поточна макро
    key_rate_pct:         float = 14.5
    inflation_pct:        float = 9.7

    # Бажаний розмір нової позиції (лотів за рекомендацією)
    suggested_lot_size:   int   = 50

    # Валютні пріоритети
    preferred_currencies: List[str] = field(default_factory=lambda: ["UAH", "USD"])


# ─────────────────────────────────────────────────────────────────────────────
# СКОРИНГ
# ─────────────────────────────────────────────────────────────────────────────

def _score_ytm(ytm_net: float, params: StrategyParams) -> float:
    """Нормований score [0,1] за доходністю."""
    if ytm_net is None or np.isnan(ytm_net):
        return 0.0
    if ytm_net < params.min_ytm_net_pct:
        return 0.0
    # Лінійна інтерполяція від min_ytm до prefer_ytm → [0.3, 1.0]
    if ytm_net >= params.prefer_ytm_above_pct:
        return 1.0
    span = params.prefer_ytm_above_pct - params.min_ytm_net_pct
    if span <= 0:
        return 1.0
    return 0.3 + 0.7 * (ytm_net - params.min_ytm_net_pct) / span


def _score_duration_fit(mod_dur: float, params: StrategyParams) -> float:
    """
    Score [0,1]: 1.0 якщо дюрація в цільовому діапазоні,
    зменшується лінійно за межами.
    """
    lo, hi = params.target_dur_min, params.target_dur_max
    if lo <= mod_dur <= hi:
        return 1.0
    if mod_dur < lo:
        return max(0.0, 1.0 - (lo - mod_dur) / lo)
    return max(0.0, 1.0 - (mod_dur - hi) / hi)


def _score_liquidity(volume_mln: float, params: StrategyParams) -> float:
    """Score [0,1] за обсягом торгів."""
    if volume_mln <= 0:
        return 0.3   # невідомо — нейтральний
    if volume_mln < params.min_volume_mln_uah:
        return max(0.0, volume_mln / params.min_volume_mln_uah * 0.5)
    return min(1.0, 0.5 + 0.5 * np.log1p(volume_mln / params.min_volume_mln_uah) / 3)


def _score_diversification(
    isin: str,
    portfolio_df: Optional[pd.DataFrame],
    total_portfolio_value: float,
    face_value: float,
    suggested_lots: int,
    fx: float,
) -> float:
    """
    Score [0,1]: вищий якщо папір відсутній або має малу вагу в портфелі.
    """
    if portfolio_df is None or portfolio_df.empty or total_portfolio_value <= 0:
        return 1.0

    existing = portfolio_df[portfolio_df["isin"] == isin]
    current_weight = 0.0
    if not existing.empty:
        current_mv = existing["market_value_uah"].sum()
        current_weight = current_mv / total_portfolio_value * 100
        new_value = face_value * suggested_lots * fx
        new_weight = (current_mv + new_value) / (total_portfolio_value + new_value) * 100
    else:
        new_value  = face_value * suggested_lots * fx
        new_weight = new_value / (total_portfolio_value + new_value) * 100

    # Штраф за концентрацію
    if new_weight > MAX_SINGLE_ISIN_PCT:
        return max(0.0, 1.0 - (new_weight - MAX_SINGLE_ISIN_PCT) / MAX_SINGLE_ISIN_PCT)
    return 1.0


def score_bond(
    metrics:     Dict,
    params:      StrategyParams,
    portfolio_df: Optional[pd.DataFrame] = None,
    total_portfolio_value: float = 0.0,
) -> float:
    """Зважений фінальний score [0, 10]."""
    s_ytm  = _score_ytm(metrics.get("ytm_net_pct", 0), params)
    s_dur  = _score_duration_fit(metrics.get("modified_duration_yrs", 0), params)
    s_liq  = _score_liquidity(metrics.get("volume_mln_uah", 0), params)
    s_div  = _score_diversification(
        isin=metrics["isin"],
        portfolio_df=portfolio_df,
        total_portfolio_value=total_portfolio_value,
        face_value=metrics["face_value"],
        suggested_lots=params.suggested_lot_size,
        fx=metrics.get("fx_rate", 1.0),
    )

    w = SCORE_WEIGHTS
    score = (
        s_ytm  * w["ytm_net"]         +
        s_dur  * w["duration_fit"]    +
        s_liq  * w["liquidity"]       +
        s_div  * w["diversification"]
    )
    return round(score * 10, 2)   # → [0, 10]


# ─────────────────────────────────────────────────────────────────────────────
# GAP-АНАЛІЗ ПОРТФЕЛЯ
# ─────────────────────────────────────────────────────────────────────────────

def portfolio_gap_analysis(
    portfolio_stats:  Dict,
    analytics_df:     pd.DataFrame,
    params:           StrategyParams,
) -> List[Dict]:
    """
    Виявляє відхилення портфеля від цільових параметрів.
    Повертає список проблем (gap) з рекомендованими діями.
    """
    gaps: List[Dict] = []

    ytm_net = portfolio_stats.get("portfolio_ytm_net_pct", 0)
    mod_dur = portfolio_stats.get("portfolio_modified_dur", 0)
    usd_w   = portfolio_stats.get("usd_weight_pct", 0)
    max_con = portfolio_stats.get("max_concentration_pct", 0)
    real_y  = portfolio_stats.get("portfolio_real_yield_pct", 0)

    if ytm_net < params.min_ytm_net_pct:
        gaps.append({
            "type": "LOW_YIELD",
            "severity": "HIGH",
            "message": f"Портфельна YTM ({ytm_net:.2f}%) нижче мінімуму ({params.min_ytm_net_pct:.1f}%)",
            "action": "Замінити низькодоходні папери на вищодоходні або додати довгі ОВДП",
        })

    if not (params.target_dur_min <= mod_dur <= params.target_dur_max):
        if mod_dur < params.target_dur_min:
            gaps.append({
                "type": "SHORT_DURATION",
                "severity": "MEDIUM",
                "message": f"Дюрація ({mod_dur:.2f} р.) нижче цільового діапазону ({params.target_dur_min}-{params.target_dur_max} р.)",
                "action": "Докупити папери з терміном 1.5–3 роки для збільшення дюрації",
            })
        else:
            gaps.append({
                "type": "LONG_DURATION",
                "severity": "MEDIUM",
                "message": f"Дюрація ({mod_dur:.2f} р.) вище цільового діапазону",
                "action": "Скоротити довгі позиції або докупити короткі папери",
            })

    if usd_w > params.max_usd_weight_pct:
        gaps.append({
            "type": "FX_OVERWEIGHT",
            "severity": "MEDIUM",
            "message": f"USD-позиція ({usd_w:.1f}%) перевищує ліміт ({params.max_usd_weight_pct:.0f}%)",
            "action": "Зменшити USD-експозицію при погашенні або продати частину",
        })

    if max_con > params.max_single_isin_pct:
        worst = analytics_df.sort_values("portfolio_weight_pct", ascending=False).iloc[0]
        gaps.append({
            "type": "CONCENTRATION",
            "severity": "HIGH",
            "message": f"Концентрація {worst['isin']}: {max_con:.1f}% > ліміту {params.max_single_isin_pct:.0f}%",
            "action": f"Скоротити позицію {worst['isin']} або реінвестувати в інші ISIN",
        })

    if real_y < 0:
        gaps.append({
            "type": "NEGATIVE_REAL_YIELD",
            "severity": "MEDIUM",
            "message": f"Реальна доходність від'ємна ({real_y:.2f}%) — портфель програє інфляції",
            "action": "Шукати папери з вищою номінальною доходністю",
        })

    if not gaps:
        gaps.append({
            "type": "OK",
            "severity": "LOW",
            "message": "Портфель відповідає цільовим параметрам",
            "action": "Моніторинг. Розглянути нарощення при появі привабливих випусків",
        })

    return gaps


# ─────────────────────────────────────────────────────────────────────────────
# РЕКОМЕНДАЦІЇ ПО ІСНУЮЧИХ ПОЗИЦІЯХ
# ─────────────────────────────────────────────────────────────────────────────

def generate_position_signals(
    analytics_df: pd.DataFrame,
    params:       StrategyParams,
    key_rate_pct: float,
) -> pd.DataFrame:
    """
    Для кожної позиції в портфелі генерує сигнал HOLD / ADD / REDUCE / SELL
    з обґрунтуванням.
    """
    rows = []
    for _, row in analytics_df.iterrows():
        ytm_net = row.get("ytm_net_pct", 0) or 0
        mod_dur = row.get("modified_duration_yrs", 0) or 0
        be_bp   = row.get("breakeven_chg_bp", 0) or 0
        days    = row.get("days_to_maturity", 0) or 0
        weight  = row.get("portfolio_weight_pct", 0) or 0
        pnl_pct = row.get("unrealized_pnl_pct", 0) or 0

        reasons = []
        score   = 5   # базовий

        # Доходність
        if ytm_net >= params.prefer_ytm_above_pct:
            reasons.append(f"YTM {ytm_net:.2f}% — вище цільового рівня ✓")
            score += 2
        elif ytm_net < params.min_ytm_net_pct:
            reasons.append(f"YTM {ytm_net:.2f}% — нижче мінімуму ✗")
            score -= 2
        else:
            reasons.append(f"YTM {ytm_net:.2f}% — в нормі")

        # Дюрація
        if params.target_dur_min <= mod_dur <= params.target_dur_max:
            reasons.append(f"Дюрація {mod_dur:.2f} р. — в цільовому діапазоні ✓")
            score += 1
        else:
            reasons.append(f"Дюрація {mod_dur:.2f} р. — поза діапазоном")

        # Концентрація
        if weight > params.max_single_isin_pct:
            reasons.append(f"Концентрація {weight:.1f}% — перевищує ліміт ✗")
            score -= 2

        # Breakeven
        if be_bp < 50:
            reasons.append(f"Breakeven лише {be_bp:.0f} б.п. — висока чутливість до ставок ✗")
            score -= 1
        else:
            reasons.append(f"Breakeven {be_bp:.0f} б.п. — прийнятний буфер ✓")

        # Термін
        if days < params.min_days_to_maturity:
            reasons.append(f"До погашення {days} днів — незабаром реінвест ⚠")
            score -= 1

        # PnL
        if pnl_pct > 3:
            reasons.append(f"P&L +{pnl_pct:.1f}% — можна зафіксувати прибуток")

        # Сигнал
        if score >= 7:
            signal = "ADD"
            color  = "🟢"
        elif score >= 5:
            signal = "HOLD"
            color  = "🟡"
        elif score >= 3:
            signal = "REDUCE"
            color  = "🟠"
        else:
            signal = "SELL"
            color  = "🔴"

        rows.append({
            "isin":         row["isin"],
            "series_code":  row.get("series_code", row["isin"]),
            "signal":       signal,
            "signal_icon":  color,
            "signal_score": score,
            "reasons":      " | ".join(reasons),
            "ytm_net_pct":  ytm_net,
            "mod_dur":      mod_dur,
            "weight_pct":   weight,
            "unrealized_pnl_pct": pnl_pct,
        })

    return pd.DataFrame(rows).sort_values("signal_score", ascending=False)


# ─────────────────────────────────────────────────────────────────────────────
# РЕКОМЕНДАЦІЇ ДО КУПІВЛІ (ринок ICU)
# ─────────────────────────────────────────────────────────────────────────────

def generate_buy_recommendations(
    all_bonds_df:  pd.DataFrame,
    portfolio_df:  Optional[pd.DataFrame],
    analytics_df:  Optional[pd.DataFrame],
    settle:        date,
    fx_rates:      Dict,
    params:        StrategyParams,
    top_n:         int = 5,
) -> pd.DataFrame:
    """
    Скорингує всі доступні ринкові папери та повертає топ-N для купівлі.
    Виключає папери з незадовільними параметрами.
    """
    total_mv = analytics_df["market_value_uah"].sum() if analytics_df is not None and not analytics_df.empty else 0.0

    scored_rows = []
    for _, bond in all_bonds_df.iterrows():
        try:
            m = bond_metrics(bond, settle, price_pct=None, fx_rates=fx_rates,
                             key_rate_pct=params.key_rate_pct,
                             inflation_pct=params.inflation_pct)
        except Exception as e:
            log.warning("Помилка метрик %s: %s", bond["isin"], e)
            continue

        ytm_net = m.get("ytm_net_pct", 0) or 0
        days    = m.get("days_to_maturity", 0) or 0
        ccy     = m.get("currency", "UAH")

        # Базові фільтри
        if ytm_net < params.min_ytm_net_pct:
            continue
        if days < params.min_days_to_maturity:
            continue
        if days > params.max_days_to_maturity:
            continue
        if ccy not in params.preferred_currencies:
            continue

        score = score_bond(m, params, analytics_df, total_mv)

        # Збільшена позиція в портфелі — знижуємо score
        existing_w = 0.0
        if analytics_df is not None and not analytics_df.empty:
            ex = analytics_df[analytics_df["isin"] == bond["isin"]]
            if not ex.empty:
                existing_w = float(ex["portfolio_weight_pct"].iloc[0])

        # Обґрунтування
        reasons = []
        if ytm_net >= params.prefer_ytm_above_pct:
            reasons.append(f"Висока YTM {ytm_net:.2f}%")
        spread = m.get("spread_vs_key_rate_bp", 0) or 0
        if spread > 150:
            reasons.append(f"Спред +{spread:.0f} б.п. над НБУ")
        if m.get("modified_duration_yrs", 0) > params.target_dur_min:
            reasons.append(f"Дюрація {m['modified_duration_yrs']:.2f} р.")
        if existing_w > 0:
            reasons.append(f"Вже {existing_w:.1f}% портфеля")
        else:
            reasons.append("Новий папір — диверсифікація")

        scored_rows.append({
            "isin":                  bond["isin"],
            "series_code":           bond.get("series_code", bond["isin"]),
            "currency":              ccy,
            "maturity_date":         m["maturity_date"],
            "days_to_maturity":      days,
            "coupon_rate_pct":       m["coupon_rate_pct"],
            "market_price_pct":      m["clean_price_pct"],
            "ytm_gross_pct":         m["ytm_gross_pct"],
            "ytm_net_pct":           ytm_net,
            "modified_duration_yrs": m["modified_duration_yrs"],
            "breakeven_chg_bp":      m.get("breakeven_chg_bp", 0),
            "real_yield_pct":        m.get("real_yield_pct", 0),
            "spread_vs_kr_bp":       spread,
            "volume_mln_uah":        m.get("volume_mln_uah", 0),
            "current_weight_pct":    existing_w,
            "recommendation_score":  score,
            "suggested_lots":        params.suggested_lot_size,
            "reasons":               " | ".join(reasons) if reasons else "—",
        })

    if not scored_rows:
        return pd.DataFrame()

    df = (
        pd.DataFrame(scored_rows)
        .sort_values("recommendation_score", ascending=False)
        .reset_index(drop=True)
        .head(top_n)
    )

    # Пріоритет: 1 = найкращий
    df.insert(0, "priority", range(1, len(df) + 1))
    log.info("Рекомендацій до купівлі: %d", len(df))
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ГОЛОВНА ТОЧКА ВХОДУ
# ─────────────────────────────────────────────────────────────────────────────

def run_recommendations(
    all_bonds_df:  pd.DataFrame,
    portfolio_df:  pd.DataFrame,
    analytics_df:  pd.DataFrame,
    settle:        date,
    fx_rates:      Dict,
    params:        StrategyParams,
) -> Dict:
    """
    Запускає повний цикл рекомендацій. Повертає словник з:
      - 'buy'       : DataFrame топ-рекомендацій до купівлі
      - 'signals'   : DataFrame сигналів по наявних позиціях
      - 'gaps'      : List[Dict] GAP-аналізу портфеля
      - 'stats'     : Dict загальної статистики
    """
    stats = portfolio_summary_stats(analytics_df)

    gaps = portfolio_gap_analysis(stats, analytics_df, params)

    signals = generate_position_signals(analytics_df, params, params.key_rate_pct) \
              if not analytics_df.empty else pd.DataFrame()

    buy_recs = generate_buy_recommendations(
        all_bonds_df=all_bonds_df,
        portfolio_df=portfolio_df,
        analytics_df=analytics_df,
        settle=settle,
        fx_rates=fx_rates,
        params=params,
        top_n=5,
    )

    return {
        "buy":     buy_recs,
        "signals": signals,
        "gaps":    gaps,
        "stats":   stats,
    }