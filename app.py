"""
app.py — ОВДП Портфельний Менеджер (Streamlit)
================================================
Запуск: streamlit run app.py

Вкладки:
  📊 Ринок     — всі доступні ОВДП з метриками
  💼 Портфель  — введення / завантаження позицій
  📈 Аналітика — деталі по кожній позиції + cash flows
  🎯 Рекомендації — GAP-аналіз + сигнали + топ-N купити
  ⚙️  Налаштування — параметри стратегії + джерела даних
"""

from __future__ import annotations

import io
import warnings
from datetime import date, datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

warnings.filterwarnings("ignore")

# ── Локальні модулі ───────────────────────────────────────────────────────────
from analytics import (
    bond_metrics, build_cash_flow_schedule,
    portfolio_analytics, portfolio_summary_stats,
)
from config import (
    APP_TITLE, FALLBACK_FX, MAX_SINGLE_ISIN_PCT,
    PAGE_ICON, TARGET_DURATION_YEARS, TARGET_YTM_MIN_PCT,
)
from data_loader import (
    DEMO_BONDS, DEMO_PORTFOLIO,
    load_fx_rates, load_minfin_bonds, load_nbu_key_rate,
    parse_bond_registry_from_dict, parse_ib_statement,
    parse_portfolio_csv, parse_portfolio_input,
)
from manual_input import (
    merge_icu_into_registry, parse_icu_telegram,
    parse_manual_portfolio, parse_nbu_ovdp_excel,
)
from recommender import StrategyParams, run_recommendations

# ─────────────────────────────────────────────────────────────────────────────
# КОНФІГУРАЦІЯ СТОРІНКИ
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title=APP_TITLE,
    page_icon=PAGE_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

# Мінімальний CSS
st.markdown("""
<style>
  .metric-box {background:#1e2a3a;border-radius:8px;padding:12px 16px;margin:4px 0}
  .signal-BUY, .signal-ADD  {color:#2ecc71;font-weight:700}
  .signal-HOLD              {color:#f39c12;font-weight:700}
  .signal-REDUCE            {color:#e67e22;font-weight:700}
  .signal-SELL              {color:#e74c3c;font-weight:700}
  .gap-HIGH   {color:#e74c3c}
  .gap-MEDIUM {color:#f39c12}
  .gap-LOW    {color:#2ecc71}
  div[data-testid="stDataFrame"] {font-size:13px}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE — ініціалізація
# ─────────────────────────────────────────────────────────────────────────────

def _init_state():
    defaults = {
        "bonds_df":       pd.DataFrame(),
        "portfolio_df":   pd.DataFrame(),
        "fx_rates":       FALLBACK_FX.copy(),
        "key_rate":       14.5,
        "inflation":      9.7,
        "settle_date":    date.today(),
        "data_loaded":    False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

S = st.session_state   # коротший alias


# ─────────────────────────────────────────────────────────────────────────────
# ХЕЛПЕРИ
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def _load_fx() -> Dict[str, float]:
    return load_fx_rates()

@st.cache_data(ttl=3600, show_spinner=False)
def _load_key_rate() -> float:
    return load_nbu_key_rate()

def _fmt_pct(v, digits=2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.{digits}f}%"

def _fmt_uah(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"₴{v:,.0f}"

def _color_val(val, positive_good=True) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "—"
    color = "green" if (val >= 0) == positive_good else "red"
    return f'<span style="color:{color}">{val:+.2f}</span>'

def _ensure_loaded():
    if S["bonds_df"].empty:
        st.warning("⚠️ Завантажте або введіть дані облігацій у вкладці **⚙️ Налаштування**")
        st.stop()

def _build_analytics() -> pd.DataFrame:
    if S["bonds_df"].empty or S["portfolio_df"].empty:
        return pd.DataFrame()
    return portfolio_analytics(
        bonds_df=S["bonds_df"],
        portfolio_df=S["portfolio_df"],
        settle=S["settle_date"],
        fx_rates=S["fx_rates"],
        key_rate_pct=S["key_rate"],
        inflation_pct=S["inflation"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/4/49/Flag_of_Ukraine.svg/320px-Flag_of_Ukraine.svg.png", width=80)
    st.title("🇺🇦 ОВДП Менеджер")
    st.caption("Senior Portfolio Analytics")

    st.divider()

    # Дата розрахунку
    S["settle_date"] = st.date_input(
        "📅 Дата розрахунку",
        value=S["settle_date"],
        min_value=date(2020, 1, 1),
        max_value=date(2030, 12, 31),
    )

    # Макро-параметри
    with st.expander("📊 Макро-параметри"):
        if st.button("🔄 Завантажити з НБУ"):
            with st.spinner("Підключення до НБУ API..."):
                S["fx_rates"]  = _load_fx()
                S["key_rate"]  = _load_key_rate()
            st.success("Оновлено!")

        S["key_rate"]  = st.number_input("Облікова ставка НБУ, %", 0.0, 50.0, S["key_rate"], 0.25)
        S["inflation"] = st.number_input("Інфляція, %", 0.0, 100.0, S["inflation"], 0.5)
        st.caption(f"USD/UAH: {S['fx_rates'].get('USD', 41.0):.2f}")
        st.caption(f"EUR/UAH: {S['fx_rates'].get('EUR', 44.5):.2f}")

    st.divider()

    # Завантаження демо
    if st.button("🎯 Завантажити DEMO-дані", use_container_width=True, type="secondary"):
        with st.spinner("Завантаження..."):
            S["bonds_df"]    = parse_bond_registry_from_dict(DEMO_BONDS)
            S["portfolio_df"]= parse_portfolio_input(DEMO_PORTFOLIO)
            S["data_loaded"] = True
        st.success(f"Завантажено {len(S['bonds_df'])} паперів, {len(S['portfolio_df'])} позицій")

    # Статус
    if not S["bonds_df"].empty:
        st.success(f"✅ Реєстр: {len(S['bonds_df'])} ISIN")
    else:
        st.warning("❌ Реєстр порожній")

    if not S["portfolio_df"].empty:
        st.success(f"✅ Портфель: {len(S['portfolio_df'])} позицій")
    else:
        st.info("ℹ️ Портфель не введено")


# ─────────────────────────────────────────────────────────────────────────────
# ВКЛАДКИ
# ─────────────────────────────────────────────────────────────────────────────

tab_market, tab_portfolio, tab_analytics, tab_recs, tab_settings = st.tabs([
    "📊 Ринок", "💼 Портфель", "📈 Аналітика", "🎯 Рекомендації", "⚙️ Налаштування"
])


# ══════════════════════════════════════════════════════════════════════════════
# ТАБ 1: РИНОК
# ══════════════════════════════════════════════════════════════════════════════

with tab_market:
    st.header("📊 Доступні ОВДП — ринкові параметри")

    _ensure_loaded()

    # Фільтри
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        flt_ccy = st.multiselect("Валюта", ["UAH", "USD", "EUR"],
                                  default=["UAH", "USD"])
    with col_f2:
        flt_disc = st.radio("Тип", ["Всі", "Купонні", "Дисконтні"], horizontal=True)
    with col_f3:
        flt_mat  = st.slider("Термін до погашення, міс.",
                              1, 60, (1, 36), step=1)

    # Розрахунок метрик для всіх паперів
    all_metrics = []
    for _, bond in S["bonds_df"].iterrows():
        try:
            m = bond_metrics(bond, S["settle_date"],
                             fx_rates=S["fx_rates"],
                             key_rate_pct=S["key_rate"],
                             inflation_pct=S["inflation"])
            all_metrics.append(m)
        except Exception:
            pass

    if not all_metrics:
        st.warning("Немає даних для відображення")
        st.stop()

    mdf = pd.DataFrame(all_metrics)

    # Фільтрація
    mdf = mdf[mdf["currency"].isin(flt_ccy)]
    if flt_disc == "Купонні":
        mdf = mdf[~mdf["is_discount"]]
    elif flt_disc == "Дисконтні":
        mdf = mdf[mdf["is_discount"]]
    mdf = mdf[
        (mdf["days_to_maturity"] >= flt_mat[0] * 30) &
        (mdf["days_to_maturity"] <= flt_mat[1] * 30)
    ]

    # Таблиця
    display_cols = {
        "series_code":           "Серія",
        "currency":              "Валюта",
        "coupon_rate_pct":       "Купон %",
        "maturity_date":         "Погашення",
        "days_to_maturity":      "Днів",
        "clean_price_pct":       "Ціна %",
        "ytm_gross_pct":         "YTM brut%",
        "ytm_net_pct":           "YTM net%",
        "modified_duration_yrs": "Mod.Dur",
        "dv01_uah":              "DV01",
        "real_yield_pct":        "Real Yield%",
        "spread_vs_key_rate_bp": "Спред, б.п.",
        "breakeven_chg_bp":      "Breakeven, б.п.",
        "volume_mln_uah":        "Обсяг млн",
    }

    st.dataframe(
        mdf[list(display_cols.keys())]
          .rename(columns=display_cols)
          .sort_values("YTM net%", ascending=False)
          .style
          .background_gradient(subset=["YTM net%"], cmap="RdYlGn")
          .background_gradient(subset=["Mod.Dur"], cmap="Blues")
          .format({
              "Купон %": "{:.2f}%", "Ціна %": "{:.2f}%",
              "YTM brut%": "{:.2f}%", "YTM net%": "{:.2f}%",
              "Mod.Dur": "{:.3f}", "DV01": "{:.2f}",
              "Real Yield%": "{:.2f}%", "Спред, б.п.": "{:.0f}",
              "Breakeven, б.п.": "{:.0f}", "Обсяг млн": "{:.0f}",
          }),
        use_container_width=True,
        height=400,
    )

    # YTM vs Duration scatter
    st.subheader("YTM vs Modified Duration — Efficient Frontier")
    fig = px.scatter(
        mdf, x="modified_duration_yrs", y="ytm_net_pct",
        color="currency", size="volume_mln_uah",
        size_max=40, hover_name="series_code",
        hover_data=["coupon_rate_pct", "maturity_date", "breakeven_chg_bp"],
        labels={"modified_duration_yrs": "Modified Duration (роки)",
                "ytm_net_pct": "YTM net (%)"},
        title="Ризик-доходність: розмір = обсяг торгів",
        color_discrete_map={"UAH": "#3498db", "USD": "#2ecc71", "EUR": "#e74c3c"},
    )
    fig.add_hline(y=S["key_rate"], line_dash="dot",
                  annotation_text=f"Облікова ставка НБУ {S['key_rate']}%",
                  line_color="orange")
    fig.update_layout(template="plotly_dark", height=420)
    st.plotly_chart(fig, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# ТАБ 2: ПОРТФЕЛЬ
# ══════════════════════════════════════════════════════════════════════════════

with tab_portfolio:
    st.header("💼 Мій портфель ОВДП")

    method = st.radio(
        "Спосіб введення портфеля",
        ["✏️ Ввести вручну", "📁 Завантажити файл CSV/Excel"],
        horizontal=True,
    )

    if method == "✏️ Ввести вручну":
        st.caption("Введіть позиції. ISIN — обов'язковий (14 символів, напр. UA4000228894)")

        if S["bonds_df"].empty:
            isin_options = ["—"]
        else:
            isin_options = S["bonds_df"]["isin"].tolist()

        # Динамічна таблиця
        default_rows = []
        if not S["portfolio_df"].empty:
            for _, r in S["portfolio_df"].iterrows():
                default_rows.append({
                    "ISIN":               r["isin"],
                    "Кількість лотів":    int(r["amount_lots"]),
                    "Ціна купівлі, %":    float(r["avg_buy_price_pct"]),
                    "Дата купівлі":       str(r["buy_date"])[:10],
                    "Комісія UAH":        float(r.get("commission_uah", 0)),
                })
        else:
            default_rows = [
                {"ISIN": "", "Кількість лотів": 50, "Ціна купівлі, %": 100.0,
                 "Дата купівлі": "2024-01-01", "Комісія UAH": 0.0}
            ]

        edited = st.data_editor(
            pd.DataFrame(default_rows),
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "ISIN": st.column_config.TextColumn("ISIN", max_chars=14),
                "Кількість лотів":  st.column_config.NumberColumn(min_value=1, step=1),
                "Ціна купівлі, %":  st.column_config.NumberColumn(min_value=1.0, max_value=200.0, step=0.01),
                "Дата купівлі":     st.column_config.TextColumn(),
                "Комісія UAH":      st.column_config.NumberColumn(min_value=0.0, step=10.0),
            },
        )

        st.caption("💡 Якщо лишити «Ціна купівлі, %» = 100, автоматично підтягнеться ринкова з реєстру (ICU/NBU).")

        if st.button("💾 Зберегти портфель", type="primary"):
            rows = []
            for _, r in edited.iterrows():
                if not str(r.get("ISIN", "")).strip():
                    continue
                price = r["Ціна купівлі, %"]
                # 100.0 → "не задано", тягнемо з реєстру
                row = {
                    "isin":           str(r["ISIN"]).strip().upper(),
                    "amount_lots":    r["Кількість лотів"],
                    "buy_date":       r["Дата купівлі"],
                    "commission_uah": r.get("Комісія UAH", 0),
                }
                if price and float(price) != 100.0:
                    row["avg_buy_price_pct"] = price
                rows.append(row)
            S["portfolio_df"] = parse_manual_portfolio(rows, registry_df=S["bonds_df"])
            st.success(f"✅ Збережено {len(S['portfolio_df'])} позицій")

    else:
        st.caption("CSV або Excel з колонками: ISIN, кількість лотів, ціна купівлі %, дата купівлі")
        uploaded = st.file_uploader("Оберіть файл", type=["csv", "xlsx", "xls"])
        if uploaded and st.button("📥 Обробити файл", type="primary"):
            raw = uploaded.read()
            S["portfolio_df"] = parse_portfolio_csv(raw)
            if not S["portfolio_df"].empty:
                st.success(f"✅ Імпортовано {len(S['portfolio_df'])} позицій")
            else:
                st.error("Не вдалося розпізнати формат. Перевірте колонки: ISIN, кількість, ціна купівлі")

    # Показуємо поточний портфель
    if not S["portfolio_df"].empty:
        st.subheader("Поточні позиції")
        st.dataframe(S["portfolio_df"], use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# ТАБ 3: АНАЛІТИКА ПОРТФЕЛЯ
# ══════════════════════════════════════════════════════════════════════════════

with tab_analytics:
    st.header("📈 Аналітика портфеля")

    if S["bonds_df"].empty or S["portfolio_df"].empty:
        st.info("Завантажте реєстр облігацій та введіть портфель (або натисніть DEMO у сайдбарі)")
        st.stop()

    with st.spinner("Розрахунок метрик..."):
        an_df = _build_analytics()

    if an_df.empty:
        st.warning("Немає позицій для аналізу")
        st.stop()

    stats = portfolio_summary_stats(an_df)

    # KPI-панель
    st.subheader("Ключові показники портфеля")
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    c1.metric("Ринкова вартість", _fmt_uah(stats["total_market_value_uah"]))
    c2.metric("YTM net (після ВЗ)", _fmt_pct(stats["portfolio_ytm_net_pct"]))
    c3.metric("Modified Duration", f"{stats['portfolio_modified_dur']:.3f} р.")
    c4.metric("DV01 (весь портфель)", _fmt_uah(abs(stats["total_dv01_uah"])))
    c5.metric("P&L нереаліз.",
              _fmt_uah(stats["total_unrealized_pnl_uah"]),
              f"{stats['total_unrealized_pnl_pct']:+.2f}%")
    c6.metric("Реальна доходність", _fmt_pct(stats["portfolio_real_yield_pct"]))

    st.divider()

    # Детальна таблиця
    st.subheader("Деталі по позиціях")
    pos_cols = {
        "series_code":           "Серія",
        "currency":              "Валюта",
        "position_lots":         "Лоти",
        "avg_buy_price_pct":     "Ціна купівлі %",
        "clean_price_pct":       "Ринкова ціна %",
        "accrued_interest":      "НКД",
        "market_value_uah":      "Ринк. вартість",
        "unrealized_pnl_uah":    "P&L UAH",
        "unrealized_pnl_pct":    "P&L %",
        "ytm_net_pct":           "YTM net%",
        "modified_duration_yrs": "Mod.Dur",
        "convexity":             "Опуклість",
        "dv01_uah":              "DV01",
        "breakeven_chg_bp":      "Breakeven б.п.",
        "portfolio_weight_pct":  "Вага %",
    }
    st.dataframe(
        an_df[list(pos_cols.keys())].rename(columns=pos_cols)
          .style
          .background_gradient(subset=["YTM net%"], cmap="RdYlGn")
          .map(lambda v: "color:red" if isinstance(v, (int,float)) and v < 0 else "",
                    subset=["P&L UAH", "P&L %"])
          .format({
              "Ціна купівлі %": "{:.2f}", "Ринкова ціна %": "{:.2f}",
              "НКД": "{:.2f}", "Ринк. вартість": "₴{:,.0f}",
              "P&L UAH": "₴{:+,.0f}", "P&L %": "{:+.2f}%",
              "YTM net%": "{:.2f}%", "Mod.Dur": "{:.3f}",
              "Опуклість": "{:.3f}", "DV01": "{:.2f}",
              "Breakeven б.п.": "{:.0f}", "Вага %": "{:.2f}%",
          }),
        use_container_width=True,
        height=350,
    )

    # Графіки
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Структура портфеля")
        pie_fig = px.pie(
            an_df, values="market_value_uah", names="series_code",
            hole=0.4, title="За ринковою вартістю",
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        pie_fig.update_layout(template="plotly_dark", height=350)
        st.plotly_chart(pie_fig, use_container_width=True)

    with col_right:
        st.subheader("Duration Ladder")
        dur_fig = px.bar(
            an_df.sort_values("modified_duration_yrs"),
            x="series_code", y="modified_duration_yrs",
            color="ytm_net_pct",
            color_continuous_scale="RdYlGn",
            labels={"modified_duration_yrs": "Modified Duration (р.)",
                    "ytm_net_pct": "YTM net%"},
            title="Дюрація позицій",
        )
        dur_fig.update_layout(template="plotly_dark", height=350)
        st.plotly_chart(dur_fig, use_container_width=True)

    # Cash Flows
    st.subheader("📅 Cash Flow Schedule")
    cf_df = build_cash_flow_schedule(
        S["bonds_df"], S["portfolio_df"], S["settle_date"], S["fx_rates"]
    )
    if not cf_df.empty:
        cf_df["month"]   = cf_df["cf_date"].dt.to_period("M").astype(str)
        cf_monthly       = cf_df.groupby("month")["net_uah"].sum().reset_index()
        cf_monthly["cum"]= cf_monthly["net_uah"].cumsum()

        cf_fig = go.Figure()
        cf_fig.add_bar(x=cf_monthly["month"], y=cf_monthly["net_uah"],
                       name="Net CF (міс.)", marker_color="#3498db")
        cf_fig.add_scatter(x=cf_monthly["month"], y=cf_monthly["cum"],
                           name="Накопичено", mode="lines+markers",
                           line=dict(color="#2ecc71", width=2), yaxis="y2")
        cf_fig.update_layout(
            template="plotly_dark", height=380,
            title="Майбутні грошові потоки (після ВЗ 1.5%)",
            yaxis=dict(title="UAH"),
            yaxis2=dict(title="Накопичено UAH", overlaying="y", side="right"),
            legend=dict(orientation="h"),
        )
        st.plotly_chart(cf_fig, use_container_width=True)

        with st.expander("Деталі Cash Flows"):
            st.dataframe(
                cf_df[["series_code","cf_date","cf_type","gross_uah","tax_uah","net_uah","position_lots"]]
                  .rename(columns={"series_code":"Серія","cf_date":"Дата","cf_type":"Тип",
                                   "gross_uah":"Брutto","tax_uah":"ВЗ 1.5%","net_uah":"Net UAH",
                                   "position_lots":"Лоти"}),
                use_container_width=True,
            )

    # Експорт
    st.divider()
    if st.button("⬇️ Завантажити аналітику CSV"):
        csv = an_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("📥 Зберегти analytics.csv", csv,
                           file_name="ovdp_analytics.csv", mime="text/csv")


# ══════════════════════════════════════════════════════════════════════════════
# ТАБ 4: РЕКОМЕНДАЦІЇ
# ══════════════════════════════════════════════════════════════════════════════

with tab_recs:
    st.header("🎯 Рекомендації")

    if S["bonds_df"].empty:
        st.info("Завантажте реєстр облігацій (або DEMO)")
        st.stop()

    # Параметри стратегії з UI
    with st.expander("⚙️ Параметри стратегії", expanded=False):
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            p_ytm_min   = st.number_input("Мін. YTM net, %",   0.0, 30.0, TARGET_YTM_MIN_PCT, 0.5)
            p_ytm_pref  = st.number_input("Цільова YTM net, %", 0.0, 40.0, 16.0, 0.5)
        with sc2:
            p_dur_min   = st.number_input("Мін. дюрація, р.",   0.0, 5.0, TARGET_DURATION_YEARS[0], 0.1)
            p_dur_max   = st.number_input("Макс. дюрація, р.",  0.0, 10.0, TARGET_DURATION_YEARS[1], 0.1)
        with sc3:
            p_max_con   = st.number_input("Макс. концентрація %", 5.0, 100.0, MAX_SINGLE_ISIN_PCT, 5.0)
            p_lots      = st.number_input("Розмір рекомендації (лотів)", 10, 500, 50, 10)

    params = StrategyParams(
        min_ytm_net_pct=p_ytm_min,
        prefer_ytm_above_pct=p_ytm_pref,
        target_dur_min=p_dur_min,
        target_dur_max=p_dur_max,
        max_single_isin_pct=p_max_con,
        suggested_lot_size=p_lots,
        key_rate_pct=S["key_rate"],
        inflation_pct=S["inflation"],
    )

    # Запуск аналізу
    with st.spinner("Аналіз ринку та портфеля..."):
        an_df = _build_analytics()
        recs  = run_recommendations(
            all_bonds_df=S["bonds_df"],
            portfolio_df=S["portfolio_df"],
            analytics_df=an_df,
            settle=S["settle_date"],
            fx_rates=S["fx_rates"],
            params=params,
        )

    # ── GAP-аналіз ────────────────────────────────────────────────────────────
    st.subheader("🔍 GAP-аналіз портфеля")
    for gap in recs["gaps"]:
        sev = gap["severity"]
        icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
        with st.container():
            st.markdown(f"**{icon} {gap['message']}**")
            st.caption(f"→ {gap['action']}")

    st.divider()

    # ── Сигнали по позиціях ───────────────────────────────────────────────────
    if not recs["signals"].empty:
        st.subheader("📊 Сигнали по наявних позиціях")

        for _, row in recs["signals"].iterrows():
            icon_map = {"ADD": "🟢", "HOLD": "🟡", "REDUCE": "🟠", "SELL": "🔴"}
            icon = icon_map.get(row["signal"], "⚪")
            with st.expander(
                f"{icon} **{row['signal']}** — {row['series_code']} "
                f"| YTM {row['ytm_net_pct']:.2f}% | Dur {row['mod_dur']:.2f}р. | "
                f"Вага {row['weight_pct']:.1f}%"
            ):
                st.caption(row["reasons"])

    st.divider()

    # ── Топ-N купити ──────────────────────────────────────────────────────────
    st.subheader("🛒 Топ рекомендацій до купівлі")

    if recs["buy"].empty:
        st.warning("Немає паперів, що відповідають фільтрам. Спробуйте знизити мінімальну YTM або розширити дюрацію.")
    else:
        buy_df = recs["buy"]

        for _, row in buy_df.iterrows():
            score_color = "#2ecc71" if row["recommendation_score"] >= 7 else \
                          "#f39c12" if row["recommendation_score"] >= 5 else "#e74c3c"
            with st.container():
                hdr_cols = st.columns([0.5, 3, 1.5, 1.5, 1.5, 1.5, 1.5])
                hdr_cols[0].markdown(f"**#{row['priority']}**")
                hdr_cols[1].markdown(f"**{row['series_code']}** `{row['isin']}`")
                hdr_cols[2].metric("YTM net", f"{row['ytm_net_pct']:.2f}%")
                hdr_cols[3].metric("Mod.Dur", f"{row['modified_duration_yrs']:.2f}р.")
                hdr_cols[4].metric("Ціна", f"{row['market_price_pct']:.2f}%")
                hdr_cols[5].metric("Score", f"{row['recommendation_score']:.1f}/10")
                hdr_cols[6].metric("Suggested", f"{row['suggested_lots']} лотів")

                det_cols = st.columns([2, 1, 1, 1])
                det_cols[0].caption(f"💡 {row['reasons']}")
                det_cols[1].caption(f"Breakeven: {row['breakeven_chg_bp']:.0f} б.п.")
                det_cols[2].caption(f"Real yield: {row['real_yield_pct']:.2f}%")
                det_cols[3].caption(f"Спред НБУ: {row['spread_vs_kr_bp']:.0f} б.п.")
                st.divider()

        # Таблиця-резюме
        with st.expander("📋 Зведена таблиця рекомендацій"):
            st.dataframe(
                buy_df[[
                    "priority", "series_code", "currency", "maturity_date",
                    "ytm_gross_pct", "ytm_net_pct", "modified_duration_yrs",
                    "market_price_pct", "real_yield_pct",
                    "breakeven_chg_bp", "volume_mln_uah", "recommendation_score",
                ]].rename(columns={
                    "priority": "#", "series_code": "Серія",
                    "currency": "Валюта", "maturity_date": "Погашення",
                    "ytm_gross_pct": "YTM brut%", "ytm_net_pct": "YTM net%",
                    "modified_duration_yrs": "Mod.Dur",
                    "market_price_pct": "Ціна %",
                    "real_yield_pct": "Real Yield%",
                    "breakeven_chg_bp": "Breakeven б.п.",
                    "volume_mln_uah": "Обсяг млн",
                    "recommendation_score": "Score",
                }),
                use_container_width=True,
            )

    # Сценарний аналіз
    st.divider()
    st.subheader("📉 Сценарний аналіз: вплив зміни ставок")
    if not an_df.empty:
        scenarios = {
            "-200 б.п.": -0.02, "-100 б.п.": -0.01,
            "Базовий":   0.00,
            "+100 б.п.": +0.01, "+200 б.п.": +0.02,
        }
        total_mv = an_df["market_value_uah"].sum()
        scen_rows = []
        for label, dr in scenarios.items():
            approx_chg = -an_df["modified_duration_yrs"] * dr * an_df["market_value_uah"]
            convex_adj = 0.5 * an_df["convexity"] * dr**2 * an_df["market_value_uah"]
            total_chg  = (approx_chg + convex_adj).sum()
            scen_rows.append({
                "Сценарій": label,
                "Зміна ставок": f"{dr*10000:+.0f} б.п.",
                "Зміна вартості UAH": round(total_chg, 0),
                "Зміна, %": round(total_chg / total_mv * 100, 2),
            })

        scen_df = pd.DataFrame(scen_rows)
        st.dataframe(
            scen_df.style.map(
                lambda v: "color:green" if isinstance(v, (int,float)) and v > 0
                          else ("color:red" if isinstance(v,(int,float)) and v < 0 else ""),
                subset=["Зміна вартості UAH", "Зміна, %"],
            ).format({"Зміна вартості UAH": "₴{:+,.0f}", "Зміна, %": "{:+.2f}%"}),
            use_container_width=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ТАБ 5: НАЛАШТУВАННЯ
# ══════════════════════════════════════════════════════════════════════════════

with tab_settings:
    st.header("⚙️ Налаштування та завантаження даних")

    # Interactive Brokers — завантажує і реєстр, і портфель одночасно
    with st.expander("🏦 Interactive Brokers — Activity Statement", expanded=False):
        st.caption(
            "Завантажте CSV-звіт з IB (Reports → Activity Statement або Flex Query). "
            "Автоматично заповнює реєстр ОВДП **і** портфель з секцій «Open Positions» та «Trades»."
        )
        ib_file = st.file_uploader("Activity Statement CSV", type=["csv", "txt"], key="ib_upload")
        if ib_file and st.button("📥 Імпортувати з IB", type="primary"):
            with st.spinner("Парсинг IB Statement..."):
                raw_bytes = ib_file.read()
                bonds_list, port_df = parse_ib_statement(raw_bytes)
            if not bonds_list:
                st.error(
                    "Не знайдено облігацій (Bonds) у секції «Open Positions». "
                    "Перевірте що файл — Activity Statement, а не інший звіт."
                )
            else:
                S["bonds_df"]   = parse_bond_registry_from_dict(bonds_list)
                if not port_df.empty:
                    S["portfolio_df"] = port_df
                st.success(
                    f"✅ IB: {len(S['bonds_df'])} паперів, "
                    f"{len(S['portfolio_df'])} позицій портфеля"
                )
                if S["bonds_df"].empty:
                    st.warning("Реєстр порожній — можливо, купон або дата погашення не розпізнані з Description.")

    st.divider()

    # Реєстр облігацій
    with st.expander("📋 Реєстр облігацій (dim_bonds)", expanded=True):
        reg_method = st.radio(
            "Джерело даних",
            ["🌐 Завантажити з Мінфіну", "📊 NBU Excel", "💬 ICU котировки",
             "Ввести вручну / JSON", "Завантажити CSV"],
            horizontal=True,
        )

        if reg_method == "🌐 Завантажити з Мінфіну":
            st.caption("Завантажує актуальні ОВДП з API Мінфіну (результати аукціонів). Потребує інтернет.")
            col_lim, _ = st.columns([1, 3])
            limit = col_lim.number_input("Кількість аукціонів", min_value=20, max_value=500, value=100, step=20)
            if st.button("🔄 Завантажити з Мінфіну", type="primary"):
                with st.spinner("Підключення до minfin.gov.ua..."):
                    raw_list = load_minfin_bonds(limit=int(limit))
                if raw_list.empty:
                    st.error("Мінфін API недоступний або повернув порожній список. Спробуйте ввести дані вручну або завантажте CSV.")
                else:
                    S["bonds_df"] = parse_bond_registry_from_dict(raw_list.to_dict("records"))
                    st.success(f"✅ Завантажено {len(S['bonds_df'])} активних ОВДП з Мінфіну")

        elif reg_method == "📊 NBU Excel":
            st.caption(
                "Excel-експорт з реєстру ОВДП НБУ "
                "(bank.gov.ua/ua/markets/ovdp/search → Завантажити)."
            )
            up_nbu = st.file_uploader("Excel-файл NBU", type=["xlsx", "xls"], key="nbu_xlsx")
            if up_nbu and st.button("📥 Імпортувати NBU Excel", type="primary"):
                with st.spinner("Парсинг NBU Excel..."):
                    new_df = parse_nbu_ovdp_excel(up_nbu.read())
                if new_df.empty:
                    st.error("Не вдалося розпізнати таблицю. Перевірте формат файлу.")
                else:
                    S["bonds_df"] = new_df
                    st.success(f"✅ Імпортовано {len(new_df)} ОВДП з NBU Excel")

        elif reg_method == "💬 ICU котировки":
            st.caption(
                "Вставте повідомлення з Telegram-бота ICU. Котировки злиються "
                "з поточним реєстром за ISIN (потрібен попередньо завантажений реєстр)."
            )
            icu_text = st.text_area(
                "Текст повідомлення з бота ICU:",
                height=300,
                placeholder="ISIN: /UA4000231559\nНазва: Джарилгач\nДата погашення: 10.06.2026\n"
                            "ICU продає: 1065,53₴ | 13,50% SIM\nICU купує: 1064,66₴ | 14,50% SIM\n---\n...",
            )
            side = st.radio(
                "Сторона котировки",
                ["ask (ICU продає)", "bid (ICU купує)"],
                horizontal=True,
                help="ask — за якою ви купуєте; bid — за якою продаєте.",
            )
            if st.button("📥 Імпортувати ICU котировки", type="primary"):
                if S["bonds_df"].empty:
                    st.warning("Спочатку завантажте реєстр (Мінфін / NBU Excel / JSON).")
                elif not icu_text.strip():
                    st.warning("Вставте текст повідомлення.")
                else:
                    icu_df = parse_icu_telegram(icu_text)
                    if icu_df.empty:
                        st.error("У тексті не знайдено жодного ISIN. Перевірте формат.")
                    else:
                        side_key = "ask" if side.startswith("ask") else "bid"
                        S["bonds_df"] = merge_icu_into_registry(
                            S["bonds_df"], icu_df, side=side_key,
                        )
                        n_quoted = (S["bonds_df"]["market_ytm_pct"] > 0).sum()
                        st.success(
                            f"✅ ICU: {len(icu_df)} котировок розпізнано, "
                            f"{n_quoted} паперів реєстру оновлено."
                        )

        elif reg_method == "Ввести вручну / JSON":
            default_json = """[
  {"isin":"UA4000228894","currency":"UAH","face_value":1000,"coupon_rate_pct":14.5,
   "coupon_freq":4,"day_count":"ACT/ACT","issue_date":"2023-10-15","maturity_date":"2025-10-15",
   "market_price_pct":99.20,"market_ytm_pct":14.9,"volume_mln_uah":320}
]"""
            json_input = st.text_area("JSON-список паперів:", default_json, height=200)
            if st.button("📥 Завантажити реєстр", type="primary"):
                import json as _json
                try:
                    bond_list = _json.loads(json_input)
                    S["bonds_df"] = parse_bond_registry_from_dict(bond_list)
                    st.success(f"✅ Завантажено {len(S['bonds_df'])} паперів")
                except Exception as e:
                    st.error(f"Помилка парсингу JSON: {e}")

        else:  # Завантажити CSV
            st.caption("CSV повинен містити колонки: isin, currency, face_value, coupon_rate_pct, coupon_freq, issue_date, maturity_date, market_price_pct")
            up = st.file_uploader("CSV з параметрами облігацій", type=["csv"])
            if up and st.button("📥 Обробити"):
                df_raw = pd.read_csv(up)
                S["bonds_df"] = parse_bond_registry_from_dict(df_raw.to_dict("records"))
                st.success(f"✅ {len(S['bonds_df'])} паперів")

    # Поточний реєстр
    if not S["bonds_df"].empty:
        with st.expander("👁 Переглянути поточний реєстр"):
            st.dataframe(
                S["bonds_df"][[
                    "isin","series_code","currency","face_value",
                    "coupon_rate_pct","coupon_freq","maturity_date",
                    "market_price_pct","market_ytm_pct","volume_mln_uah",
                ]],
                use_container_width=True,
            )

    st.divider()
    st.caption("ℹ️ Дані НБУ API: bank.gov.ua | Реєстр ОВДП: minfin.gov.ua")
    st.caption("⚠️ Військовий збір 1.5% враховано у всіх розрахунках YTM. ПДФО = 0% (ОВДП звільнені)")