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
tab_market, tab_portfolio, tab_glossary = st.tabs([
    "📊 Ринок ОВДП",
    "💼 Мій Портфель",
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
#  TAB 3 — GLOSSARY / LIBRARY
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
