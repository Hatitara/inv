"""
data_parser.py — Парсинг ICU-тексту та OVDP.xlsx
"""
import re
import pandas as pd
from datetime import datetime, date


ICU_SAMPLE = """Дякую за ваш запит! Ось перелік ОВДП які доступні в ICU на 09.05.2026:

ISIN: /UA4000231559
Назва: Джарилгач
Дата погашення: 10.06.2026
ICU продає: 1065,53₴ | 13,50% SIM
ICU купує: 1064,66₴ | 14,50% SIM

---
ISIN: /UA4000234215
Назва: Соледар
Дата погашення: 24.06.2026
ICU продає: 1058,09₴ | 13,65% SIM
ICU купує: 1057,02₴ | 14,50% SIM"""


# Ціна може містити нерозривний пробіл як роздільник тисяч: "1 065,53₴".
_PRICE = r'([\d\s ]+(?:[,.]\d+)?)'
_RATE  = r'([\d\s ]+(?:[,.]\d+)?)'
ICU_SELL_RE = re.compile(
    rf'ICU\s+продає:\s*{_PRICE}\s*₴\s*\|\s*{_RATE}\s*%\s*(\w+)',
    re.IGNORECASE,
)
ICU_BUY_RE = re.compile(
    rf'ICU\s+купує:\s*{_PRICE}\s*₴\s*\|\s*{_RATE}\s*%\s*(\w+)',
    re.IGNORECASE,
)


def _icu_num(s: str) -> float:
    """'1 065,53' / '1065.53' / '1 065,53' → 1065.53."""
    return float(s.replace(' ', '').replace(' ', '').replace(',', '.'))


def parse_icu_text(text: str) -> pd.DataFrame:
    """
    Parse raw ICU bot message into a DataFrame.
    Returns columns: isin, name, maturity, sell_price, sell_rate, sell_type,
                     buy_price, buy_rate, buy_type, is_flexible_fix
    """
    # Нормалізуємо CRLF та CR (Telegram desktop / Windows clipboard).
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    # Розбиваємо по `---` (з будь-яким пробільним матеріалом навколо).
    # Якщо `---` відсутній — fallback на split за `ISIN:` lookahead.
    if re.search(r'\n\s*---\s*\n', text):
        blocks = re.split(r'\n\s*---\s*\n', text)
    else:
        blocks = re.split(r'(?=^ISIN:)', text, flags=re.MULTILINE)

    records = []
    for block in blocks:
        if 'ISIN' not in block:
            continue
        rec = {}
        isin_m = re.search(r'ISIN:\s*/?(UA\d{10})', block)
        if not isin_m:
            continue
        rec['isin'] = isin_m.group(1)

        name_m = re.search(r'Назва:\s*([^\n]+)', block)
        raw_name = name_m.group(1).strip() if name_m else ''
        rec['is_flexible_fix'] = '❗Гнучкий ФІКС❗' in raw_name or '❗Гнучкий ФІКС❗' in block
        rec['name'] = raw_name.replace('❗Гнучкий ФІКС❗', '').strip() or None

        mat_m = re.search(r'Дата погашення:\s*(\d{2}\.\d{2}\.\d{4})', block)
        rec['maturity'] = datetime.strptime(mat_m.group(1), '%d.%m.%Y').date() if mat_m else None

        sell_m = ICU_SELL_RE.search(block)
        if sell_m:
            rec['sell_price'] = _icu_num(sell_m.group(1))
            rec['sell_rate']  = _icu_num(sell_m.group(2))
            rec['sell_type']  = sell_m.group(3)
        else:
            rec['sell_price'] = rec['sell_rate'] = rec['sell_type'] = None

        buy_m = ICU_BUY_RE.search(block)
        if buy_m:
            rec['buy_price'] = _icu_num(buy_m.group(1))
            rec['buy_rate']  = _icu_num(buy_m.group(2))
            rec['buy_type']  = buy_m.group(3)
        else:
            rec['buy_price'] = rec['buy_rate'] = rec['buy_type'] = None

        records.append(rec)

    return pd.DataFrame(records).drop_duplicates('isin').reset_index(drop=True)


def parse_ovdp_xlsx(file) -> pd.DataFrame:
    """
    Parse Ministry of Finance OVDP registry from xlsx.
    file — path string or BytesIO.
    """
    df = pd.read_excel(file, sheet_name=0)
    df.columns = [
        'isin', 'bond_type', 'nominal', 'currency',
        'issue_date', 'maturity_date', 'outstanding', 'coupon_rate_pct', 'coupon_days'
    ]

    def parse_date(v):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return datetime.strptime(str(v), '%d.%m.%Y').date()
        except Exception:
            return None

    df['issue_date'] = df['issue_date'].apply(parse_date)
    df['maturity_date'] = df['maturity_date'].apply(parse_date)
    df['nominal'] = pd.to_numeric(df['nominal'], errors='coerce').fillna(1000)
    df['coupon_rate_pct'] = pd.to_numeric(df['coupon_rate_pct'], errors='coerce')
    df['coupon_days'] = pd.to_numeric(df['coupon_days'], errors='coerce').fillna(182).astype(int)
    df['outstanding'] = pd.to_numeric(df['outstanding'], errors='coerce')
    return df


def merge_icu_ovdp(icu_df: pd.DataFrame, ovdp_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge ICU market data with OVDP registry data on ISIN.
    Якщо в ICU немає `maturity` — підтягуємо з OVDP (`maturity_date`).
    """
    merged = icu_df.merge(
        ovdp_df[['isin', 'bond_type', 'nominal', 'currency',
                 'issue_date', 'maturity_date',
                 'coupon_rate_pct', 'coupon_days', 'outstanding']],
        on='isin',
        how='left',
    )
    if 'maturity' in merged.columns and 'maturity_date' in merged.columns:
        merged['maturity'] = merged['maturity'].fillna(merged['maturity_date'])
    return merged
