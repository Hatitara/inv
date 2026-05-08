"""
config.py — Константи, API-ендпоінти, налаштування
"""

# ── Податки ───────────────────────────────────────────────────────────────────
MILITARY_LEVY = 0.015        # Військовий збір 1.5% (з 01.12.2024)
INCOME_TAX    = 0.0          # ПДФО по ОВДП = 0 (звільнені)
TAX_TOTAL     = MILITARY_LEVY + INCOME_TAX

# ── API ───────────────────────────────────────────────────────────────────────
NBU_FX_TODAY  = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
NBU_FX_DATE   = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={ccy}&date={d}&json"
NBU_KEY_RATE  = "https://bank.gov.ua/NBUStatService/v1/statdirectory/NBUfixrate?json"

# Мінфін — результати аукціонів ОВДП (публічний JSON)
MINFIN_AUCTIONS_URL = "https://www.minfin.gov.ua/api/ovdp/auctions?limit=20"

# ICU Research: публічна сторінка з таблицею доходностей
# Якщо ICU заблокований — використовується ручне введення
ICU_YIELDS_URL = "https://icu.ua/api/bonds/yields"   # неофіційний endpoint

# ── Дефолтні FX (fallback якщо НБУ недоступний) ──────────────────────────────
FALLBACK_FX = {"UAH": 1.0, "USD": 41.0, "EUR": 44.5}

# ── Параметри розрахунку ──────────────────────────────────────────────────────
YTM_SOLVER_TOLERANCE = 1e-10
YTM_SOLVER_MAX_ITER  = 500
YTM_RATE_BOUNDS      = (-0.9999, 50.0)   # діапазон пошуку для brentq

# ── Скоринг рекомендацій ──────────────────────────────────────────────────────
# Ваги для фінального score (сума = 1.0)
SCORE_WEIGHTS = {
    "ytm_net":         0.40,   # Чиста доходність після ВЗ
    "duration_fit":    0.25,   # Відповідність цільовій дюрації портфеля
    "liquidity":       0.20,   # Оцінка ліквідності (обсяг торгів)
    "diversification": 0.15,   # Внесок у диверсифікацію
}

# Цільові діапазони для портфеля (можна змінити в UI)
TARGET_DURATION_YEARS = (0.5, 2.5)      # Оптимальна дюрація
TARGET_YTM_MIN_PCT    = 14.0            # Мінімально прийнятна YTM після ВЗ, %
MAX_SINGLE_ISIN_PCT   = 30.0            # Макс. концентрація в одному паері, %

# ── UI ────────────────────────────────────────────────────────────────────────
APP_TITLE   = "ОВДП Портфельний Менеджер"
PAGE_ICON   = "🇺🇦"
THEME_COLOR = "#1A5276"

# ── Колонки ICU-звіту (якщо вставляють як CSV/Excel) ─────────────────────────
ICU_COLUMN_MAP = {
    "ISIN":           "isin",
    "Купон, %":       "coupon_rate_pct",
    "Погашення":      "maturity_date",
    "YTM, %":         "ytm_icu_pct",
    "Ціна чиста, %":  "clean_price_pct",
    "НКД":            "accrued_int",
    "Обсяг, млн грн": "volume_mln",
}