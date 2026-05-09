"""
try_ib_parser.py
================
Швидкий запуск парсера IB Activity Statement на синтетичному CSV.

Запуск:
    python3 try_ib_parser.py

Щоб перевірити на власному файлі IB — розкоментуй блок REAL_FILE нижче
та вкажи шлях до свого Activity Statement CSV.
"""

from ib_parser import parse_ib_activity_csv


SAMPLE = '''Statement,Header,Field Name,Field Value
Statement,Data,BrokerName,Interactive Brokers LLC
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Listing Exch,Multiplier,Type,Code
Financial Instrument Information,Data,Bonds,UA4000228894,UKRAINE 14.5 10/15/2025,12345678,UA4000228894,SMART,1,FIXED,
Financial Instrument Information,Data,Bonds,UA4000231245,UKRAINE 16.0 03/01/2026,12345679,UA4000231245,SMART,1,FIXED,
Financial Instrument Information,Data,Stocks,AAPL,APPLE INC,265598,US0378331005,NASDAQ,1,COMMON,
Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Bonds,UAH,UA4000228894,"2024-01-12, 10:23:01",60000,96.8,96.8,-58080,-30.0,58110,0,0,
Trades,Data,Order,Bonds,UAH,UA4000228894,"2024-02-05, 14:01:55",40000,97.5,97.5,-39000,-20.0,39020,0,0,
Trades,Data,Order,Bonds,UAH,UA4000231245,"2024-03-07, 09:00:00",100000,99.0,99.0,-99000,-45.0,99045,0,0,
Trades,Data,Order,Stocks,USD,AAPL,"2024-04-01, 13:00:00",10,180.0,180.0,-1800,-1.0,1801,0,0,
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Bonds,UAH,UA4000228894,100000,1,97.08,97080,99.20,99200,2120,
Open Positions,Data,Summary,Bonds,UAH,UA4000231245,100000,1,99.0,99000,100.50,100500,1500,
'''


def run_synthetic() -> None:
    print("=== Синтетичний IB CSV ===")
    df = parse_ib_activity_csv(SAMPLE)
    print(df.to_string() if not df.empty else "(порожньо)")


def run_real_file(path: str) -> None:
    print(f"=== Реальний IB CSV: {path} ===")
    with open(path, "rb") as f:
        df = parse_ib_activity_csv(f.read())
    print(df.to_string() if not df.empty else "(порожньо)")


if __name__ == "__main__":
    run_synthetic()

    # ── Розкоментуй для перевірки на справжньому експорті IB ──
    # run_real_file("/path/to/your_ib_activity_statement.csv")
