"""
app.py — ОВДП Portfolio Manager (Streamlit)
"""
import io
import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
from datetime import date, datetime

# ── modules ────────────────────────────────────────────────────────────────
from modules.data_parser import parse_icu_text, parse_ovdp_xlsx, merge_icu_ovdp
from modules.bond_math import compute_all_metrics, today
from modules.portfolio import build_portfolio_df, portfolio_summary
from modules.glossary import GLOSSARY

# ── page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ОВДП Manager",
    page_icon="🇺🇦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stSidebar"] { background: #0d1b2a; }
    [data-testid="stSidebar"] * { color: #e8eaf6 !important; }
    .metric-card {
        background: #1e3a5f; border-radius: 10px;
        padding: 14px 18px; margin: 6px 0;
        border-left: 4px solid #4fc3f7;
    }
    .metric-card h4 { color: #90caf9; margin: 0 0 4px 0; font-size: 0.78rem; }
    .metric-card p  { color: #ffffff; margin: 0; font-size: 1.2rem; font-weight: 700; }
    .glossary-card {
        background: #1a237e11; border-radius: 8px;
        padding: 12px 16px; margin-bottom: 10px;
        border-left: 4px solid #3f51b5;
    }
    .formula-box {
        background: #0d47a1; border-radius: 6px;
        padding: 8px 12px; font-family: monospace;
        font-size: 0.85rem; color: #e3f2fd; margin-top: 6px;
    }
    .tag-sim  { background:#ff8f00; color:#fff; padding:2px 8px; border-radius:4px; font-size:0.75rem; }
    .tag-ytm  { background:#1565c0; color:#fff; padding:2px 8px; border-radius:4px; font-size:0.75rem; }
    .tag-flex { background:#6a1b9a; color:#fff; padding:2px 8px; border-radius:4px; font-size:0.75rem; }
    .positive { color: #66bb6a; font-weight: 700; }
    .negative { color: #ef5350; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  SESSION STATE
# ══════════════════════════════════════════════════════════════════════════════
if "icu_df" not in st.session_state:
    st.session_state.icu_df = pd.DataFrame()
if "ovdp_df" not in st.session_state:
    st.session_state.ovdp_df = pd.DataFrame()
if "market_df" not in st.session_state:
    st.session_state.market_df = pd.DataFrame()
if "portfolio_holdings" not in st.session_state:
    st.session_state.portfolio_holdings = []   # list of dicts


# ══════════════════════════════════════════════════════════════════════════════
#  SIDEBAR — INPUT
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## 🇺🇦 ОВДП Manager")
    st.markdown(f"**Дата розрахунку:** {today().strftime('%d.%m.%Y')}")
    st.divider()

    # ── ICU text ────────────────────────────────────────────────────────────
    st.markdown("### 📋 ICU — ринкові котирування")
    st.caption("Вставте повідомлення від ICU-бота (або залиште приклад)")
    icu_raw = st.text_area(
        "Текст від ICU:",
        height=260,
        value=open("modules/icu_default.txt", encoding="utf-8").read()
        if __import__("os").path.exists("modules/icu_default.txt")
        else "",
        placeholder="Дякую за ваш запит! Ось перелік ОВДП...",
        key="icu_text_input",
    )
    if st.button("🔄 Оновити ICU-дані", use_container_width=True):
        if icu_raw.strip():
            st.session_state.icu_df = parse_icu_text(icu_raw)
            st.success(f"Завантажено {len(st.session_state.icu_df)} ОВДП з ICU")
        else:
            st.warning("Введіть текст ICU")

    st.divider()

    # ── OVDP xlsx ────────────────────────────────────────────────────────────
    st.markdown("### 📂 МінФін — реєстр ОВДП (.xlsx)")
    xlsx_file = st.file_uploader(
        "Завантажте ovdp.xlsx", type=["xlsx"], key="xlsx_upload"
    )
    if xlsx_file:
        st.session_state.ovdp_df = parse_ovdp_xlsx(xlsx_file)
        st.success(f"Реєстр: {len(st.session_state.ovdp_df)} облігацій")


# ══════════════════════════════════════════════════════════════════════════════
#  COMPUTE MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════
def compute_market(icu_df: pd.DataFrame, ovdp_df: pd.DataFrame) -> pd.DataFrame:
    if icu_df.empty or ovdp_df.empty:
        return pd.DataFrame()
    merged = merge_icu_ovdp(icu_df, ovdp_df)
    rows = []
    for _, r in merged.iterrows():
        try:
            nominal = float(r.get("nominal") or 1000)
            coupon_rate = float(r.get("coupon_rate_pct") or 0)
            coupon_days = int(r.get("coupon_days") or 182)
            issue_date = r.get("issue_date")
            maturity = r.get("maturity")
            sell_price = r.get("sell_price")
            buy_price = r.get("buy_price")
            sell_rate = r.get("sell_rate")
            buy_rate = r.get("buy_rate")
            sell_type = str(r.get("sell_type") or "YTM")
            currency = str(r.get("currency") or "UAH")

            if not issue_date or not maturity or not coupon_rate:
                rows.append(r.to_dict())
                continue

            market_price = sell_price or buy_price or nominal
            ytm_pct = sell_rate or buy_rate or coupon_rate
            use_sim = "SIM" in sell_type.upper()

            metrics = compute_all_metrics(
                nominal=nominal,
                coupon_rate_pct=coupon_rate,
                coupon_frequency_days=coupon_days,
                issue_date=issue_date,
                maturity_date=maturity,
                market_price=market_price,
                ytm_pct=ytm_pct,
                use_sim=use_sim,
            )
            row_out = r.to_dict()
            row_out.update(metrics)
            row_out['currency'] = currency
            row_out['spread_price'] = (
                round(sell_price - buy_price, 2)
                if sell_price and buy_price else None
            )
            row_out['spread_yield'] = (
                round(buy_rate - sell_rate, 2)
                if buy_rate and sell_rate else None
            )
            rows.append(row_out)
        except Exception:
            rows.append(r.to_dict())
    return pd.DataFrame(rows)


# Recompute whenever both sources are loaded
if not st.session_state.icu_df.empty and not st.session_state.ovdp_df.empty:
    st.session_state.market_df = compute_market(
        st.session_state.icu_df, st.session_state.ovdp_df
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TABS
# ══════════════════════════════════════════════════════════════════════════════
tab_market, tab_portfolio, tab_analysis, tab_glossary = st.tabs([
    "📊 Ринок ОВДП",
    "💼 Мій Портфель",
    "🧠 Аналіз та Рекомендації",
    "📚 Бібліотека",
])


# ─────────────────────────────────────────────────────────────────────────────
#  TAB 1 — MARKET
# ─────────────────────────────────────────────────────────────────────────────
with tab_market:
    st.header("📊 Ринок ОВДП (ICU)")

    if st.session_state.market_df.empty:
        st.info(
            "👈 Вставте текст ICU та завантажте `ovdp.xlsx` у бічній панелі, "
            "потім натисніть **Оновити ICU-дані**."
        )
        st.stop()

    mdf = st.session_state.market_df.copy()
    uah_df = mdf[mdf.get('currency', 'UAH').apply(
        lambda x: str(x) == 'UAH') if 'currency' in mdf.columns else mdf.index].copy()

    # ── Filters ──────────────────────────────────────────────────────────────
    col_f1, col_f2, col_f3 = st.columns([2, 2, 2])
    with col_f1:
        if 'maturity' in uah_df.columns:
            mat_dates = uah_df['maturity'].dropna().unique()
            years = sorted(set(
                d.year for d in mat_dates if isinstance(d, date)
            ))
            sel_years = st.multiselect(
                "Рік погашення", years,
                default=years, key="mkt_year"
            )
            if sel_years:
                uah_df = uah_df[uah_df['maturity'].apply(
                    lambda d: isinstance(d, date) and d.year in sel_years
                )]
    with col_f2:
        ytm_min, ytm_max = st.slider(
            "YTM/SIM, %", 0.0, 25.0, (12.0, 20.0), 0.25, key="mkt_ytm"
        )
        if 'sell_rate' in uah_df.columns:
            uah_df = uah_df[
                uah_df['sell_rate'].fillna(0).between(ytm_min, ytm_max)
            ]
    with col_f3:
        show_all_cols = st.checkbox("Показати всі колонки", False, key="mkt_cols")

    st.divider()

    # ── Summary cards ────────────────────────────────────────────────────────
    if not uah_df.empty:
        c1, c2, c3, c4 = st.columns(4)
        metrics_cards = [
            (c1, "Облігацій на ринку", f"{len(uah_df)}"),
            (c2, "Мін YTM/SIM ICU продає", f"{uah_df['sell_rate'].min():.2f}%" if 'sell_rate' in uah_df else "—"),
            (c3, "Макс YTM/SIM ICU продає", f"{uah_df['sell_rate'].max():.2f}%" if 'sell_rate' in uah_df else "—"),
            (c4, "Серед. дюрація Маккол., рок.",
             f"{uah_df['Дюрація Макколея, рок.'].mean():.2f}" if 'Дюрація Макколея, рок.' in uah_df else "—"),
        ]
        for col, label, val in metrics_cards:
            col.markdown(f"""
            <div class='metric-card'>
              <h4>{label}</h4><p>{val}</p>
            </div>""", unsafe_allow_html=True)

    st.divider()

    # ── Main table ────────────────────────────────────────────────────────────
    DISPLAY_COLS = [
        'isin', 'name', 'maturity', 'sell_price', 'sell_rate', 'sell_type',
        'buy_price', 'buy_rate', 'spread_price', 'spread_yield',
        'НКД (Accrued Interest), ₴', 'Чиста ціна (Clean Price), ₴',
        'До погашення, рок.', 'Дюрація Макколея, рок.',
        'Модифікована дюрація', 'DV01, ₴', 'Опуклість (Convexity)',
        'Поточна дохідність, %', 'is_flexible_fix',
    ]
    show_cols = [c for c in DISPLAY_COLS if c in uah_df.columns]

    rename_map = {
        'isin': 'ISIN',
        'name': 'Назва',
        'maturity': 'Погашення',
        'sell_price': 'ICU продає, ₴',
        'sell_rate': 'Ставка продажу, %',
        'sell_type': 'Тип',
        'buy_price': 'ICU купує, ₴',
        'buy_rate': 'Ставка купівлі, %',
        'spread_price': 'Спред ціни, ₴',
        'spread_yield': 'Спред дохідності, %',
        'is_flexible_fix': 'Гнучкий ФІКС',
    }

    display_df = uah_df[show_cols].rename(columns=rename_map) if not show_all_cols else uah_df

    st.dataframe(
        display_df,
        use_container_width=True,
        height=500,
        column_config={
            "Погашення": st.column_config.DateColumn("Погашення", format="DD.MM.YYYY"),
            "ISIN": st.column_config.TextColumn("ISIN", width="medium"),
            "Назва": st.column_config.TextColumn("Назва", width="small"),
            "ICU продає, ₴": st.column_config.NumberColumn("ICU продає, ₴", format="%.2f ₴"),
            "ICU купує, ₴": st.column_config.NumberColumn("ICU купує, ₴", format="%.2f ₴"),
            "Ставка продажу, %": st.column_config.NumberColumn("Ставка продажу, %", format="%.2f%%"),
            "Ставка купівлі, %": st.column_config.NumberColumn("Ставка купівлі, %", format="%.2f%%"),
            "Спред ціни, ₴": st.column_config.NumberColumn("Спред, ₴", format="%.2f"),
            "Спред дохідності, %": st.column_config.NumberColumn("Спред YTM, %", format="%.2f%%"),
            "НКД (Accrued Interest), ₴": st.column_config.NumberColumn("НКД, ₴", format="%.2f"),
            "Чиста ціна (Clean Price), ₴": st.column_config.NumberColumn("Чиста ціна, ₴", format="%.2f"),
            "До погашення, рок.": st.column_config.NumberColumn("TTM, рок.", format="%.2f"),
            "Дюрація Макколея, рок.": st.column_config.NumberColumn("Dur (Mac), рок.", format="%.3f"),
            "Модифікована дюрація": st.column_config.NumberColumn("Mod Dur", format="%.3f"),
            "DV01, ₴": st.column_config.NumberColumn("DV01, ₴", format="%.4f"),
            "Опуклість (Convexity)": st.column_config.NumberColumn("Convexity", format="%.3f"),
            "Поточна дохідність, %": st.column_config.NumberColumn("Пот. дохідн., %", format="%.2f%%"),
            "Гнучкий ФІКС": st.column_config.CheckboxColumn("Гнучкий ФІКС"),
        },
        hide_index=True,
    )

    # ── Charts (Plotly) ──────────────────────────────────────────────────────
    if not uah_df.empty and 'maturity' in uah_df.columns and 'sell_rate' in uah_df.columns:
        st.subheader("📈 Крива дохідності (Yield Curve)")
        
        plot_df = uah_df[['До погашення, рок.', 'sell_rate', 'buy_rate', 'name', 'isin']].copy()
        for col in ['До погашення, рок.', 'sell_rate', 'buy_rate']:
            plot_df[col] = pd.to_numeric(plot_df[col], errors='coerce')
        plot_df = plot_df.dropna(subset=['До погашення, рок.', 'sell_rate']).sort_values('До погашення, рок.')

        fig_yield = px.line(
            plot_df, 
            x='До погашення, рок.', 
            y=['sell_rate', 'buy_rate'],
            labels={'value': 'Дохідність, %', 'variable': 'Тип', 'До погашення, рок.': 'До погашення (роки)'},
            hover_data=['isin', 'name'],
            markers=True,
            template="plotly_dark",
            color_discrete_map={'sell_rate': '#4fc3f7', 'buy_rate': '#ff8a65'}
        )
        fig_yield.update_layout(xaxis_title="До погашення (роки)", yaxis_title="Дохідність (%)")
        
        # Перейменування легенди
        newnames = {'sell_rate':'ICU продає', 'buy_rate': 'ICU купує'}
        fig_yield.for_each_trace(lambda t: t.update(name = newnames[t.name],
                                                    legendgroup = newnames[t.name],
                                                    hovertemplate = t.hovertemplate.replace(t.name, newnames[t.name])
                                                    )
                                )
        st.plotly_chart(fig_yield, use_container_width=True)

    # ── Duration chart (Plotly) ──────────────────────────────────────────────
    if 'Дюрація Макколея, рок.' in uah_df.columns:
        st.subheader("⏱️ Дюрація vs YTM")
        
        dur_df = uah_df[['Дюрація Макколея, рок.', 'sell_rate', 'name', 'isin']].copy()
        for col in ['Дюрація Макколея, рок.', 'sell_rate']:
            dur_df[col] = pd.to_numeric(dur_df[col], errors='coerce')
        dur_df = dur_df.dropna(subset=['Дюрація Макколея, рок.', 'sell_rate'])

        fig_dur_ytm = px.scatter(
            dur_df,
            x='Дюрація Макколея, рок.',
            y='sell_rate',
            hover_data=['isin', 'name'],
            labels={
                'sell_rate': 'YTM/SIM ICU продає (%)', 
                'Дюрація Макколея, рок.': 'Дюрація Макколея (роки)'
            },
            template="plotly_dark",
            color_discrete_sequence=['#ce93d8']
        )
        fig_dur_ytm.update_traces(marker=dict(size=12, opacity=0.8))
        st.plotly_chart(fig_dur_ytm, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
#  TAB 2 — PORTFOLIO
# ─────────────────────────────────────────────────────────────────────────────
with tab_portfolio:
    st.header("💼 Мій Портфель ОВДП")

    if st.session_state.market_df.empty:
        st.info("Спочатку завантажте ICU-дані та реєстр ОВДП на вкладці **Ринок ОВДП**.")
    else:
        mdf = st.session_state.market_df
        available_isins = mdf['isin'].dropna().tolist()
        isin_labels = {
            row['isin']: f"{row['isin']} — {row.get('name') or row.get('maturity') or ''}"
            for _, row in mdf.iterrows() if pd.notna(row.get('isin'))
        }

        st.subheader("➕ Додати / Редагувати позиції")
        st.caption(
            "Оберіть ОВДП що є у вашому портфелі, вкажіть кількість та середню ціну купівлі."
        )

        # ── Holdings editor ──────────────────────────────────────────────────
        holdings = st.session_state.portfolio_holdings

        with st.form("add_holding_form"):
            col1, col2, col3 = st.columns([3, 1, 2])
            with col1:
                sel_isin = st.selectbox(
                    "Оберіть ОВДП (ISIN)",
                    options=available_isins,
                    format_func=lambda x: isin_labels.get(x, x),
                    key="new_isin",
                )
            with col2:
                qty = st.number_input("К-сть (шт.)", min_value=1, value=10, step=1, key="new_qty")
            with col3:
                avg_price = st.number_input(
                    "Сер. ціна купівлі, ₴ (необов.)",
                    min_value=0.0, value=0.0, step=0.01,
                    key="new_price",
                    help="Якщо 0 — P&L не розраховується"
                )
            submitted = st.form_submit_button("✅ Додати позицію", use_container_width=True)
            if submitted:
                exists = next((i for i, h in enumerate(holdings) if h['isin'] == sel_isin), None)
                entry = {
                    'isin': sel_isin,
                    'quantity': qty,
                    'avg_buy_price': avg_price if avg_price > 0 else None,
                }
                if exists is not None:
                    holdings[exists] = entry
                    st.success(f"Позицію {sel_isin} оновлено")
                else:
                    holdings.append(entry)
                    st.success(f"Додано {sel_isin}")

        # ── Current holdings table ────────────────────────────────────────────
        if holdings:
            st.divider()
            st.subheader("📋 Поточні позиції")
            holdings_edit = pd.DataFrame(holdings).rename(columns={
                'isin': 'ISIN', 'quantity': 'К-сть', 'avg_buy_price': 'Сер. ціна, ₴'
            })
            st.dataframe(holdings_edit, use_container_width=True, hide_index=True)

            col_del1, col_del2 = st.columns([2, 4])
            with col_del1:
                del_isin = st.selectbox(
                    "Видалити позицію", [h['isin'] for h in holdings], key="del_isin"
                )
                if st.button("🗑️ Видалити", use_container_width=True):
                    st.session_state.portfolio_holdings = [
                        h for h in holdings if h['isin'] != del_isin
                    ]
                    st.rerun()

            st.divider()

            # ── Portfolio analytics ──────────────────────────────────────────
            portfolio_df = build_portfolio_df(
                st.session_state.portfolio_holdings, mdf
            )
            summary = portfolio_summary(portfolio_df)

            if summary:
                st.subheader("📊 Зведення портфеля")
                cols = st.columns(3)
                items = list(summary.items())
                for i, (k, v) in enumerate(items):
                    with cols[i % 3]:
                        if isinstance(v, float):
                            display_v = f"{v:,.2f}"
                        else:
                            display_v = str(v)
                        st.markdown(f"""
                        <div class='metric-card'>
                          <h4>{k}</h4><p>{display_v}</p>
                        </div>""", unsafe_allow_html=True)

            st.divider()
            st.subheader("📑 Деталі позицій")

            if not portfolio_df.empty:
                # Color P&L
                def style_pnl(val):
                    if pd.isna(val) or val == "" or val is None:
                        return ""
                    try:
                        return "color: #66bb6a" if float(val) >= 0 else "color: #ef5350"
                    except Exception:
                        return ""

                pnl_cols = ['Нереаліз. P&L, ₴', 'Нереаліз. P&L, %']
                styled = portfolio_df.style.map(
                    style_pnl, subset=[c for c in pnl_cols if c in portfolio_df.columns]
                ).format({
                    'Ринк. ціна (брудна), ₴': '{:.2f}',
                    'Ринк. вартість, ₴': '{:,.2f}',
                    'Номінальна вартість, ₴': '{:,.2f}',
                    'Сер. ціна купівлі, ₴': lambda x: f'{x:.2f}' if x else '—',
                    'Собівартість, ₴': lambda x: f'{x:,.2f}' if x else '—',
                    'НКД (накоп.), ₴': '{:.2f}',
                    'Нереаліз. P&L, ₴': lambda x: f'{x:,.2f}' if x else '—',
                    'Нереаліз. P&L, %': lambda x: f'{x:.2f}%' if x else '—',
                    'Річний купон. дохід, ₴': '{:,.2f}',
                    'DV01 (портф.), ₴': '{:.2f}',
                    'Дюрація Макколея, рок.': '{:.3f}',
                    'Мод. дюрація': '{:.3f}',
                })
                st.dataframe(styled, use_container_width=True, hide_index=True,
                             column_config={
                                 "Дата погашення": st.column_config.DateColumn(format="DD.MM.YYYY"),
                             })

            # ── Portfolio duration bar chart (Plotly) ─────────────────────────
            if not portfolio_df.empty and 'Дюрація Макколея, рок.' in portfolio_df.columns:
                st.divider()
                st.subheader("⏱️ Дюрація позицій")
                
                plot_dur_df = portfolio_df.copy()
                plot_dur_df['Дюрація Макколея, рок.'] = pd.to_numeric(plot_dur_df['Дюрація Макколея, рок.'], errors='coerce')
                plot_dur_df = plot_dur_df.dropna(subset=['Дюрація Макколея, рок.'])

                fig_port_dur = px.bar(
                    plot_dur_df,
                    x='ISIN',
                    y='Дюрація Макколея, рок.',
                    color='Дюрація Макколея, рок.',
                    hover_data=['Назва', 'YTM/SIM ринк., %'],
                    labels={'Дюрація Макколея, рок.': 'Дюрація (роки)'},
                    template="plotly_dark",
                    color_continuous_scale='Blues'
                )
                st.plotly_chart(fig_port_dur, use_container_width=True)

                # Weight pie (Plotly)
                st.subheader("🥧 Розподіл портфеля (ринкова вартість)")
                
                fig_pie = px.pie(
                    portfolio_df, 
                    values='Ринк. вартість, ₴', 
                    names='ISIN',
                    hover_data=['Назва'],
                    hole=0.4,
                    template="plotly_dark",
                    color_discrete_sequence=px.colors.qualitative.Pastel
                )
                fig_pie.update_traces(textposition='inside', textinfo='percent+label')
                st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("Додайте позиції вище, щоб побачити аналітику портфеля.")


# ─────────────────────────────────────────────────────────────────────────────
#  TAB 3 — ANALYSIS & RECOMMENDATIONS
# ─────────────────────────────────────────────────────────────────────────────
with tab_analysis:
    st.header("🧠 Аналіз портфеля та рекомендації")
    st.caption(
        "Аналізуємо ваш поточний портфель і порівнюємо з ринком ICU. "
        "Вкажіть параметри інвестиційної стратегії, щоб отримати конкретні рекомендації щодо ребалансування."
    )

    if st.session_state.market_df.empty:
        st.info("Спочатку завантажте ICU-дані та реєстр ОВДП у бічній панелі.")
        st.stop()

    holdings = st.session_state.portfolio_holdings
    mdf = st.session_state.market_df.copy()

    if not holdings:
        st.warning(
            "Ваш портфель порожній. Перейдіть на вкладку **💼 Мій Портфель** і додайте позиції — "
            "тоді тут з'являться персоналізовані рекомендації."
        )
        st.stop()

    portfolio_df = build_portfolio_df(holdings, mdf)
    summary = portfolio_summary(portfolio_df)

    if portfolio_df.empty:
        st.error("Не вдалося зіставити позиції портфеля з ринковими даними.")
        st.stop()

    # ── 1. Investor profile inputs ───────────────────────────────────────────
    st.subheader("⚙️ Профіль інвестора")
    st.caption("Ці параметри допоможуть нам точніше адаптувати рекомендації.")

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        goal = st.selectbox(
            "🎯 Головна мета",
            ["Максимальний дохід (YTM)", "Баланс дохід / ризик", "Мінімальний ризик (короткий дюрація)"],
            key="goal_select",
        )
    with col_p2:
        horizon_years = st.slider(
            "📅 Горизонт інвестування (роки)", 0.5, 5.0, 1.5, 0.25, key="horizon_slider"
        )
    with col_p3:
        risk_tolerance = st.selectbox(
            "📉 Толерантність до відсоткового ризику",
            ["Низька (короткі папери)", "Середня", "Висока (довгі папери)"],
            key="risk_select",
        )

    col_p4, col_p5 = st.columns(2)
    with col_p4:
        target_ytm = st.slider(
            "💰 Цільова дохідність (YTM/SIM), %", 12.0, 20.0, 15.5, 0.25, key="target_ytm"
        )
    with col_p5:
        avoid_flex = st.checkbox(
            "🚫 Уникати «Гнучкий ФІКС» (підвищений ризик)",
            value=True, key="avoid_flex"
        )

    st.divider()

    # ── Helper: map goal → target duration range ─────────────────────────────
    dur_targets = {
        "Максимальний дохід (YTM)":         (horizon_years * 0.7, horizon_years * 1.2),
        "Баланс дохід / ризик":             (horizon_years * 0.4, horizon_years * 0.9),
        "Мінімальний ризик (короткий дюрація)": (0.1, horizon_years * 0.5),
    }
    dur_lo, dur_hi = dur_targets[goal]

    risk_dur_max = {
        "Низька (короткі папери)": 0.8,
        "Середня": 1.8,
        "Висока (довгі папери)": 5.0,
    }
    max_dur_allowed = risk_dur_max[risk_tolerance]

    # ── 2. Portfolio snapshot ────────────────────────────────────────────────
    st.subheader("📋 Поточний стан портфеля")

    snap_cols = st.columns(4)
    snap_items = [
        ("Позицій", str(len(portfolio_df))),
        ("Ринкова вартість", f"₴{summary.get('Ринкова вартість портфеля, ₴', 0):,.0f}"),
        ("Серед. зваж. YTM", f"{summary.get('Серед. зважений YTM, %', 0):.2f}%"),
        ("Серед. зваж. Дюрація", f"{summary.get('Серед. зважена дюрація (Маккол.), рок.', 0):.2f} рок."),
    ]
    for col, (label, val) in zip(snap_cols, snap_items):
        col.markdown(f"<div class='metric-card'><h4>{label}</h4><p>{val}</p></div>", unsafe_allow_html=True)

    st.divider()

    # ── 3. Diagnose each holding ─────────────────────────────────────────────
    st.subheader("🔍 Діагностика позицій")

    # Enrich portfolio_df with market alternatives context
    mdf_clean = mdf.copy()
    for col in ['sell_rate', 'buy_rate', 'Дюрація Макколея, рок.', 'До погашення, рок.']:
        if col in mdf_clean.columns:
            mdf_clean[col] = pd.to_numeric(mdf_clean[col], errors='coerce')

    market_available = mdf_clean[mdf_clean['sell_price'].notna()].copy()
    if avoid_flex:
        market_available = market_available[~market_available.get('is_flexible_fix', pd.Series(False, index=market_available.index)).fillna(False)]

    port_ytm_avg = summary.get('Серед. зважений YTM, %', 0)
    port_dur_avg = summary.get('Серед. зважена дюрація (Маккол.), рок.', 0)

    issues = []          # list of dicts: position-level problems
    recommendations = [] # list of dicts: swap/action suggestions

    for _, row in portfolio_df.iterrows():
        isin = row['ISIN']
        name = row['Назва'] or isin
        ytm_pos = float(row.get('YTM/SIM ринк., %') or 0)
        dur_pos = float(row.get('Дюрація Макколея, рок.') or 0)
        mv = float(row.get('Ринк. вартість, ₴') or 0)
        pnl = row.get('Нереаліз. P&L, ₴')
        pnl_pct = row.get('Нереаліз. P&L, %')
        mat = row.get('Дата погашення')
        ttm = float(row.get('До погашення, рок.', dur_pos) if 'До погашення, рок.' in row.index else dur_pos)

        # Lookup market row for this isin
        mrow = mdf_clean[mdf_clean['isin'] == isin]
        is_flex = bool(mrow.iloc[0].get('is_flexible_fix', False)) if not mrow.empty else False

        diag_flags = []

        # Flag: below target yield
        if ytm_pos < target_ytm - 0.5:
            diag_flags.append(f"📉 YTM {ytm_pos:.2f}% нижче цільового ({target_ytm:.2f}%)")

        # Flag: duration out of zone
        if dur_pos > max_dur_allowed:
            diag_flags.append(f"⏳ Дюрація {dur_pos:.2f} рок. перевищує ліміт ризику ({max_dur_allowed:.1f})")
        if dur_pos > horizon_years * 1.2:
            diag_flags.append(f"📅 Дюрація {dur_pos:.2f} рок. значно перевищує горизонт інвестора ({horizon_years:.1f} рок.)")

        # Flag: maturing soon vs horizon (reinvestment risk)
        if isinstance(mat, date) and ttm < 0.25:
            diag_flags.append("⚠️ Погашення менш ніж за 3 місяці — ризик реінвестування")

        # Flag: flexible fix
        if is_flex and avoid_flex:
            diag_flags.append("🔄 Гнучкий ФІКС — ставка може змінитися")

        # Flag: significant negative P&L
        if pnl is not None and not pd.isna(pnl) and pnl < -50:
            diag_flags.append(f"🔴 Нереалізований збиток: ₴{pnl:,.0f} ({pnl_pct:.1f}%)")

        issues.append({
            'isin': isin, 'name': name, 'ytm': ytm_pos, 'dur': dur_pos,
            'mv': mv, 'flags': diag_flags, 'ttm': ttm,
            'pnl': pnl, 'is_flex': is_flex,
        })

    # ── Display diagnostics ──────────────────────────────────────────────────
    any_flag = any(len(i['flags']) > 0 for i in issues)
    if not any_flag:
        st.success("✅ Усі позиції відповідають вашому інвестиційному профілю. Ребалансування не потрібне.")
    else:
        for iss in issues:
            if iss['flags']:
                with st.expander(
                    f"⚠️ **{iss['name']}** ({iss['isin']}) — {len(iss['flags'])} проблем(и)",
                    expanded=True
                ):
                    for f in iss['flags']:
                        st.markdown(f"- {f}")
            else:
                st.markdown(f"✅ **{iss['name']}** ({iss['isin']}) — позиція в порядку")

    st.divider()

    # ── 4. Swap recommendations ──────────────────────────────────────────────
    st.subheader("🔄 Рекомендації щодо ребалансування")

    if market_available.empty:
        st.info("Немає доступних альтернатив на ринку ICU для порівняння.")
    else:
        # Filter market candidates to match investor profile
        candidates = market_available.copy()

        # Must have yield data
        candidates = candidates[candidates['sell_rate'].notna()]
        candidates['sell_rate'] = pd.to_numeric(candidates['sell_rate'], errors='coerce')
        candidates['Дюрація Макколея, рок.'] = pd.to_numeric(candidates.get('Дюрація Макколея, рок.'), errors='coerce')

        # Target yield filter (±1% band)
        candidates = candidates[candidates['sell_rate'] >= target_ytm - 1.0]

        # Duration filter
        candidates = candidates[candidates['Дюрація Макколея, рок.'] <= max_dur_allowed]

        # Exclude ISINs already in portfolio
        portfolio_isins = set(portfolio_df['ISIN'].tolist())
        candidates_not_held = candidates[~candidates['isin'].isin(portfolio_isins)]

        swap_suggestions = []

        for iss in issues:
            if not iss['flags']:
                continue
            isin = iss['isin']
            ytm_cur = iss['ytm']
            dur_cur = iss['dur']

            # Find better candidates: higher YTM, better duration fit, not same ISIN
            cands = candidates_not_held.copy()
            if cands.empty:
                continue

            # Score candidates: reward YTM above current & target, penalise duration deviation from horizon
            cands = cands.copy()
            cands['ytm_score'] = cands['sell_rate'] - ytm_cur     # positive = better yield
            target_dur = min(horizon_years * 0.8, max_dur_allowed)
            cands['dur_score'] = -abs(cands['Дюрація Макколея, рок.'].fillna(9) - target_dur)
            cands['total_score'] = cands['ytm_score'] * 0.6 + cands['dur_score'] * 0.4
            top = cands.nlargest(3, 'total_score')

            for _, alt in top.iterrows():
                alt_ytm = float(alt['sell_rate'])
                alt_dur = float(alt.get('Дюрація Макколея, рок.') or 0)
                ytm_delta = alt_ytm - ytm_cur
                swap_suggestions.append({
                    'sell_isin': isin,
                    'sell_name': iss['name'],
                    'sell_ytm': ytm_cur,
                    'sell_dur': dur_cur,
                    'sell_mv': iss['mv'],
                    'buy_isin': alt['isin'],
                    'buy_name': alt.get('name') or alt['isin'],
                    'buy_ytm': alt_ytm,
                    'buy_dur': alt_dur,
                    'buy_price': float(alt.get('sell_price') or 0),
                    'buy_maturity': alt.get('maturity'),
                    'ytm_delta': ytm_delta,
                    'flags': iss['flags'],
                })

        # De-duplicate: one best swap per sell ISIN
        seen_sells = {}
        for s in swap_suggestions:
            k = s['sell_isin']
            if k not in seen_sells or s['ytm_delta'] > seen_sells[k]['ytm_delta']:
                seen_sells[k] = s

        best_swaps = list(seen_sells.values())

        if not best_swaps:
            st.success(
                "✅ На поточному ринку ICU немає кандидатів, що суттєво покращать ваш портфель "
                "відповідно до заданих параметрів."
            )
        else:
            st.markdown(
                f"Знайдено **{len(best_swaps)}** потенційних замін. "
                "Оцінка базується на покращенні YTM та відповідності дюрації горизонту інвестора."
            )
            for s in sorted(best_swaps, key=lambda x: -x['ytm_delta']):
                ytm_delta = s['ytm_delta']
                delta_color = "#66bb6a" if ytm_delta >= 0 else "#ef5350"
                delta_sign = "+" if ytm_delta >= 0 else ""
                mat_str = s['buy_maturity'].strftime('%d.%m.%Y') if isinstance(s['buy_maturity'], date) else "—"

                with st.container():
                    st.markdown(f"""
<div style="background:#1e3a5f; border-radius:10px; padding:16px 20px; margin-bottom:14px; border-left:4px solid {delta_color};">
  <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
    <div>
      <span style="color:#90caf9; font-size:0.8rem;">ПРОДАТИ</span><br>
      <b style="font-size:1.05rem;">{s['sell_name']}</b>
      <span style="color:#aaa; font-size:0.82rem;"> / {s['sell_isin']}</span><br>
      <span style="color:#ccc; font-size:0.85rem;">YTM {s['sell_ytm']:.2f}% &nbsp;·&nbsp; Дюрація {s['sell_dur']:.2f} рок. &nbsp;·&nbsp; ₴{s['sell_mv']:,.0f}</span>
    </div>
    <div style="font-size:1.6rem; color:{delta_color}; font-weight:900; align-self:center;">→</div>
    <div>
      <span style="color:#a5d6a7; font-size:0.8rem;">КУПИТИ</span><br>
      <b style="font-size:1.05rem;">{s['buy_name']}</b>
      <span style="color:#aaa; font-size:0.82rem;"> / {s['buy_isin']}</span><br>
      <span style="color:#ccc; font-size:0.85rem;">YTM {s['buy_ytm']:.2f}% &nbsp;·&nbsp; Дюрація {s['buy_dur']:.2f} рок. &nbsp;·&nbsp; Погашення {mat_str} &nbsp;·&nbsp; Ціна ICU ₴{s['buy_price']:.2f}</span>
    </div>
    <div style="text-align:right; align-self:center;">
      <span style="color:{delta_color}; font-size:1.3rem; font-weight:700;">{delta_sign}{ytm_delta:.2f}%</span><br>
      <span style="color:#aaa; font-size:0.75rem;">Δ YTM</span>
    </div>
  </div>
  <div style="margin-top:10px; color:#b0bec5; font-size:0.82rem;">
    {'&nbsp;&nbsp;'.join([f'<span style="background:#263238; border-radius:4px; padding:2px 7px;">{f}</span>' for f in s['flags']])}
  </div>
</div>""", unsafe_allow_html=True)

        st.divider()

        # ── 5. Rebalanced portfolio preview ─────────────────────────────────
        st.subheader("📐 Прогноз портфеля після ребалансування")
        st.caption(
            "Нижче показано, як зміниться агрегований профіль портфеля, якщо ви виконаєте всі рекомендовані заміни."
        )

        if best_swaps:
            # Build simulated holdings
            sim_holdings = []
            swap_map = {s['sell_isin']: s for s in best_swaps}
            for h in holdings:
                isin = h['isin']
                if isin in swap_map:
                    sim_holdings.append({
                        'isin': swap_map[isin]['buy_isin'],
                        'quantity': h['quantity'],
                        'avg_buy_price': swap_map[isin]['buy_price'] or h.get('avg_buy_price'),
                    })
                else:
                    sim_holdings.append(h)

            sim_portfolio_df = build_portfolio_df(sim_holdings, mdf)
            sim_summary = portfolio_summary(sim_portfolio_df)

            if sim_summary:
                compare_keys = [
                    'Серед. зважений YTM, %',
                    'Серед. зважена дюрація (Маккол.), рок.',
                    'Серед. зважена мод. дюрація',
                    'DV01 портфеля, ₴',
                    'Річний купон. дохід, ₴',
                ]
                before_vals = {k: summary.get(k, 0) for k in compare_keys}
                after_vals  = {k: sim_summary.get(k, 0) for k in compare_keys}

                labels = {
                    'Серед. зважений YTM, %': 'Серед. YTM, %',
                    'Серед. зважена дюрація (Маккол.), рок.': 'Дюрація (Маккол.), рок.',
                    'Серед. зважена мод. дюрація': 'Мод. дюрація',
                    'DV01 портфеля, ₴': 'DV01, ₴',
                    'Річний купон. дохід, ₴': 'Річний купон. дохід, ₴',
                }

                cmp_cols = st.columns(len(compare_keys))
                for col, k in zip(cmp_cols, compare_keys):
                    bv = before_vals[k]
                    av = after_vals[k]
                    delta = av - bv
                    delta_str = f"{'+' if delta >= 0 else ''}{delta:.2f}"
                    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                    # For YTM up is good; for DV01/duration, depends on goal
                    is_good_up = k in ('Серед. зважений YTM, %', 'Річний купон. дохід, ₴')
                    good = (delta > 0) == is_good_up
                    clr = "#66bb6a" if (delta > 0 and is_good_up) or (delta < 0 and not is_good_up) else "#ef5350" if delta != 0 else "#aaa"
                    col.markdown(f"""
<div class='metric-card'>
  <h4>{labels[k]}</h4>
  <p>{av:.2f}</p>
  <p style="font-size:0.78rem; color:{clr}; margin:2px 0 0 0;">{arrow} {delta_str} від {bv:.2f}</p>
</div>""", unsafe_allow_html=True)

                st.divider()

                # Side-by-side bar chart: Before vs After key metrics
                import plotly.graph_objects as go

                fig_cmp = go.Figure()
                metric_labels = [labels[k] for k in compare_keys[:3]]  # first 3 for chart clarity
                before_plot = [before_vals[k] for k in compare_keys[:3]]
                after_plot  = [after_vals[k]  for k in compare_keys[:3]]

                fig_cmp.add_trace(go.Bar(
                    name='Зараз', x=metric_labels, y=before_plot,
                    marker_color='#ef5350', opacity=0.85
                ))
                fig_cmp.add_trace(go.Bar(
                    name='Після ребалансування', x=metric_labels, y=after_plot,
                    marker_color='#66bb6a', opacity=0.85
                ))
                fig_cmp.update_layout(
                    barmode='group',
                    template='plotly_dark',
                    title='Порівняння ключових метрик: До vs Після',
                    yaxis_title='Значення',
                    legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
                    margin=dict(t=60, b=30),
                )
                st.plotly_chart(fig_cmp, use_container_width=True)
        else:
            st.info("Немає рекомендованих замін — нічого симулювати.")

    st.divider()

    # ── 6. Portfolio risk stress test ─────────────────────────────────────────
    st.subheader("📉 Стрес-тест відсоткового ризику")
    st.caption(
        "Скільки коштуватиме портфель при паралельному зсуві кривої дохідності на ±X базисних пунктів."
    )

    shock_bp = st.slider("Зсув ставок (б.п.)", -300, 300, 0, 25, key="shock_slider",
                         help="Негативне значення = ставки падають (ціни ростуть)")

    if 'Мод. дюрація' in portfolio_df.columns and 'Ринк. вартість, ₴' in portfolio_df.columns:
        port_mv = float(summary.get('Ринкова вартість портфеля, ₴', 0))
        port_mod_dur = float(summary.get('Серед. зважена мод. дюрація', 0))
        port_conv = float(portfolio_df['Опуклість (Convexity)'].mean() if 'Опуклість (Convexity)' in portfolio_df.columns else 0)

        dy = shock_bp / 10000
        # ΔP ≈ -D_mod × ΔY + 0.5 × Convexity × (ΔY)²  (as fraction of price)
        delta_pct = (-port_mod_dur * dy + 0.5 * port_conv * dy ** 2) * 100
        delta_uah = port_mv * delta_pct / 100

        col_s1, col_s2, col_s3 = st.columns(3)
        col_s1.markdown(f"""<div class='metric-card'><h4>Зсув ставок</h4>
            <p>{'+' if shock_bp >= 0 else ''}{shock_bp} б.п.</p></div>""", unsafe_allow_html=True)
        pct_color = "#66bb6a" if delta_pct >= 0 else "#ef5350"
        col_s2.markdown(f"""<div class='metric-card'><h4>Зміна вартості, %</h4>
            <p style="color:{pct_color};">{'+' if delta_pct >= 0 else ''}{delta_pct:.2f}%</p></div>""",
            unsafe_allow_html=True)
        uah_color = "#66bb6a" if delta_uah >= 0 else "#ef5350"
        col_s3.markdown(f"""<div class='metric-card'><h4>Зміна вартості, ₴</h4>
            <p style="color:{uah_color};">{'+' if delta_uah >= 0 else ''}₴{delta_uah:,.0f}</p></div>""",
            unsafe_allow_html=True)

        # Show shock curve
        shocks = list(range(-300, 325, 25))
        deltas_uah = []
        for bp in shocks:
            dy_ = bp / 10000
            dp_ = (-port_mod_dur * dy_ + 0.5 * port_conv * dy_ ** 2) * port_mv
            deltas_uah.append(dp_)

        shock_df = pd.DataFrame({'Зсув (б.п.)': shocks, 'Δ Вартість портфеля, ₴': deltas_uah})
        fig_stress = px.line(
            shock_df, x='Зсув (б.п.)', y='Δ Вартість портфеля, ₴',
            template='plotly_dark',
            title='Зміна вартості портфеля при зсуві ставок',
            color_discrete_sequence=['#4fc3f7'],
            markers=True,
        )
        fig_stress.add_hline(y=0, line_dash='dash', line_color='gray')
        fig_stress.add_vline(x=shock_bp, line_dash='dot', line_color='#ffb74d',
                             annotation_text=f"{shock_bp} б.п.", annotation_position="top right")
        fig_stress.update_layout(yaxis_tickformat=',.0f', margin=dict(t=50, b=30))
        st.plotly_chart(fig_stress, use_container_width=True)

    st.divider()

    # ── 7. Reinvestment calendar ──────────────────────────────────────────────
    st.subheader("📆 Календар погашень і реінвестування")
    st.caption("Коли надходять кошти від погашень і де їх краще реінвестувати.")

    if 'Дата погашення' in portfolio_df.columns and 'Ринк. вартість, ₴' in portfolio_df.columns:
        mat_df = portfolio_df[['ISIN', 'Назва', 'Дата погашення', 'Номінальна вартість, ₴',
                                'Річний купон. дохід, ₴', 'YTM/SIM ринк., %']].copy()
        mat_df = mat_df[mat_df['Дата погашення'].notna()].sort_values('Дата погашення')
        mat_df['Дата погашення'] = pd.to_datetime(mat_df['Дата погашення'])
        mat_df['Рік-Місяць'] = mat_df['Дата погашення'].dt.to_period('M').astype(str)

        if not mat_df.empty:
            fig_cal = px.bar(
                mat_df,
                x='Рік-Місяць',
                y='Номінальна вартість, ₴',
                color='YTM/SIM ринк., %',
                hover_data=['ISIN', 'Назва', 'Річний купон. дохід, ₴'],
                text='Назва',
                template='plotly_dark',
                color_continuous_scale='Blues',
                labels={'Номінальна вартість, ₴': 'Погашення, ₴', 'Рік-Місяць': 'Місяць'},
                title='Очікувані надходження від погашень',
            )
            fig_cal.update_traces(textposition='outside')
            fig_cal.update_layout(margin=dict(t=50, b=30))
            st.plotly_chart(fig_cal, use_container_width=True)

            # Suggest reinvestment targets for upcoming maturities
            upcoming = mat_df[mat_df['Дата погашення'] <= pd.Timestamp(today()) + pd.DateOffset(months=12)]
            if not upcoming.empty:
                st.markdown("**Для погашень у наступні 12 місяців — рекомендовані об'єкти реінвестування:**")
                best_market = market_available[market_available['sell_rate'].notna()]
                best_market = best_market.nlargest(5, 'sell_rate')
                if not best_market.empty:
                    reinvest_cols = ['isin', 'name', 'sell_rate', 'sell_type', 'Дюрація Макколея, рок.', 'sell_price', 'maturity']
                    reinvest_show = best_market[[c for c in reinvest_cols if c in best_market.columns]].copy()
                    reinvest_show = reinvest_show.rename(columns={
                        'isin': 'ISIN', 'name': 'Назва', 'sell_rate': 'YTM/SIM, %',
                        'sell_type': 'Тип', 'Дюрація Макколея, рок.': 'Дюрація, рок.',
                        'sell_price': 'Ціна ICU, ₴', 'maturity': 'Погашення',
                    })
                    st.dataframe(reinvest_show, hide_index=True, use_container_width=True,
                                 column_config={
                                     "Погашення": st.column_config.DateColumn(format="DD.MM.YYYY"),
                                     "YTM/SIM, %": st.column_config.NumberColumn(format="%.2f%%"),
                                     "Дюрація, рок.": st.column_config.NumberColumn(format="%.2f"),
                                     "Ціна ICU, ₴": st.column_config.NumberColumn(format="%.2f ₴"),
                                 })


# ─────────────────────────────────────────────────────────────────────────────
#  TAB 4 — GLOSSARY / LIBRARY
# ─────────────────────────────────────────────────────────────────────────────
with tab_glossary:
    st.header("📚 Бібліотека: Термінологія ОВДП")
    st.caption("Пояснення ключових понять і формул, що використовуються в аналізі облігацій.")

    search = st.text_input("🔍 Пошук терміну...", key="glossary_search")

    terms = GLOSSARY
    if search:
        q = search.lower()
        terms = [
            t for t in terms
            if q in t['term'].lower()
            or q in t['definition'].lower()
            or q in (t['en'] or '').lower()
        ]

    for item in terms:
        has_formula = bool(item.get('formula'))
        with st.expander(f"**{item['term']}** — *{item['en']}*", expanded=False):
            st.markdown(item['definition'])
            if has_formula:
                st.markdown(
                    f"<div class='formula-box'>📐 {item['formula'].replace(chr(10), '<br>')}</div>",
                    unsafe_allow_html=True
                )

    st.divider()
    st.subheader("🔢 Зв'язок метрик")
    st.markdown("""
    ```
    Брудна ціна = Чиста ціна + НКД
    YTM: Dirty Price = Σ CF_t / (1+YTM)^t
    SIM: Dirty Price = Σ CF_t / (1+SIM×t)
    
    Дюрація Макколея  = Σ [t × PV(CF_t)] / Dirty Price
    Модифікована дюрація = D_mac / (1 + YTM)
    DV01 = Dirty Price × D_mod × 0.0001
    Convexity = Σ [t(t+1) × PV(CF_t)] / [Dirty Price × (1+YTM)²]
    
    ΔPrice ≈ −D_mod × ΔY + ½ × Convexity × (ΔY)²
    ```
    """)
    st.caption("ΔPrice — зміна ціни, ΔY — зміна дохідності (у частках, напр. 0.01 = 100 б.п.)")
