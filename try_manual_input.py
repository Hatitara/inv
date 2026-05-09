"""
try_manual_input.py
===================
Демо: парсимо NBU Excel + ICU Telegram, формуємо реєстр з котировками,
потім ручний портфель з автопідтягуванням ціни.

Запуск:  python3 try_manual_input.py
"""

from pathlib import Path

from manual_input import (
    parse_nbu_ovdp_excel, parse_icu_telegram,
    merge_icu_into_registry, parse_manual_portfolio,
)

HERE = Path(__file__).parent


def main() -> None:
    # ── 1. NBU Excel → реєстр ОВДП ────────────────────────────────────────
    print("=" * 70)
    print("1. NBU OVDP Excel (bank.gov.ua)")
    print("=" * 70)
    with open(HERE / "ovdp.xlsx", "rb") as f:
        nbu_df = parse_nbu_ovdp_excel(f.read())
    print(f"Розпарсено: {len(nbu_df)} ОВДП")
    print(nbu_df[["isin", "currency", "coupon_rate_pct", "coupon_freq",
                  "issue_date", "maturity_date"]].head(8).to_string(index=False))

    # ── 2. ICU Telegram → котировки ───────────────────────────────────────
    print("\n" + "=" * 70)
    print("2. ICU Telegram bot")
    print("=" * 70)
    icu_text = (HERE / "icu_sample.txt").read_text(encoding="utf-8")
    icu_df = parse_icu_telegram(icu_text)
    print(f"Розпарсено: {len(icu_df)} котировок")
    print(icu_df[["isin", "name", "maturity_date",
                  "ask_price_uah", "ask_yield_pct", "ask_yield_type",
                  "bid_yield_pct"]].head(8).to_string(index=False))

    # ── 3. Об'єднаний реєстр з ринковими цінами ───────────────────────────
    print("\n" + "=" * 70)
    print("3. Реєстр + ICU котировки (side='ask' — за якою ви купуєте)")
    print("=" * 70)
    registry = merge_icu_into_registry(nbu_df, icu_df, side="ask")
    with_quotes = registry[registry["market_ytm_pct"] > 0]
    print(f"Паперів з котировками: {len(with_quotes)}/{len(registry)}")
    print(with_quotes[["isin", "icu_name", "currency", "coupon_rate_pct",
                       "maturity_date", "market_price_pct", "market_ytm_pct",
                       "yield_type"]].head(10).to_string(index=False))

    # ── 4. Ручний ввід портфеля ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("4. Ручний портфель (тільки ISIN + кількість)")
    print("=" * 70)
    my_holdings = [
        {"isin": "UA4000231559", "amount_lots": 60},
        {"isin": "UA4000234215", "amount_lots": 100},
        {"isin": "UA4000237416", "amount_lots": 50, "avg_buy_price_pct": 105.0},
        {"isin": "UA4000238992", "amount_lots": 25},
    ]
    portfolio = parse_manual_portfolio(my_holdings, registry_df=registry)
    print(portfolio.to_string(index=False))


if __name__ == "__main__":
    main()
