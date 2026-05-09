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


def parse_icu_text(text: str) -> pd.DataFrame:
    """
    Parse raw ICU bot message into a DataFrame.
    Returns columns: isin, name, maturity, sell_price, sell_rate, sell_type,
                     buy_price, buy_rate, buy_type, is_flexible_fix
    """
    blocks = re.split(r'\n---\n', text.strip())
    records = []
    for block in blocks:
        if 'ISIN' not in block:
            continue
        rec = {}
        isin_m = re.search(r'ISIN:\s*/?(UA\w+)', block)
        rec['isin'] = isin_m.group(1) if isin_m else None

        name_m = re.search(r'Назва:\s*([^\n]+)', block)
        raw_name = name_m.group(1).strip() if name_m else ''
        rec['is_flexible_fix'] = '❗Гнучкий ФІКС❗' in raw_name or '❗Гнучкий ФІКС❗' in block
        rec['name'] = raw_name.replace('❗Гнучкий ФІКС❗', '').strip() or None

        mat_m = re.search(r'Дата погашення:\s*(\d{2}\.\d{2}\.\d{4})', block)
        rec['maturity'] = datetime.strptime(mat_m.group(1), '%d.%m.%Y').date() if mat_m else None

        sell_m = re.search(r'ICU продає:\s*([\d,]+)₴\s*\|\s*([\d,]+)%\s*(\w+)', block)
        if sell_m:
            rec['sell_price'] = float(sell_m.group(1).replace(',', '.'))
            rec['sell_rate'] = float(sell_m.group(2).replace(',', '.'))
            rec['sell_type'] = sell_m.group(3)
        else:
            rec['sell_price'] = None
            rec['sell_rate'] = None
            rec['sell_type'] = None

        buy_m = re.search(r'ICU купує:\s*([\d,]+)₴\s*\|\s*([\d,]+)%\s*(\w+)', block)
        if buy_m:
            rec['buy_price'] = float(buy_m.group(1).replace(',', '.'))
            rec['buy_rate'] = float(buy_m.group(2).replace(',', '.'))
            rec['buy_type'] = buy_m.group(3)
        else:
            rec['buy_price'] = None
            rec['buy_rate'] = None
            rec['buy_type'] = None

        if rec['isin']:
            records.append(rec)

    df = pd.DataFrame(records)
    return df


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
    """
    merged = icu_df.merge(
        ovdp_df[['isin', 'bond_type', 'nominal', 'currency',
                 'issue_date', 'coupon_rate_pct', 'coupon_days', 'outstanding']],
        on='isin',
        how='left'
    )
    # fallback for maturity from ovdp if needed
    merged['maturity'] = merged['maturity'].fillna(merged.get('maturity_date'))
    return merged
