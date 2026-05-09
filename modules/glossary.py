"""
glossary.py — Бібліотека термінів для ОВДП-аналітики
"""

GLOSSARY = [
    {
        "term": "ОВДП",
        "en": "Government Bonds (OVDPs)",
        "definition": "Облігації внутрішньої державної позики — боргові цінні папери, які випускає Міністерство фінансів України. Погашення та купони виплачуються в гривні (або валюті, якщо валютний випуск).",
        "formula": None,
    },
    {
        "term": "ISIN",
        "en": "International Securities Identification Number",
        "definition": "Унікальний 12-символьний код цінного паперу. Для українських ОВДП починається з UA40.",
        "formula": None,
    },
    {
        "term": "Номінал (Face Value)",
        "en": "Face Value / Par Value",
        "definition": "Сума, яку емітент (МінФін) зобов'язується повернути власнику на дату погашення. Зазвичай 1 000 ₴.",
        "formula": "N = 1 000 ₴ (стандарт)",
    },
    {
        "term": "Купон (Coupon)",
        "en": "Coupon Payment",
        "definition": "Регулярна відсоткова виплата власнику облігації. Розраховується від номіналу.",
        "formula": "C = N × r_coupon × (days_in_period / 365)",
    },
    {
        "term": "НКД — Накопичений Купонний Дохід",
        "en": "Accrued Interest (AI)",
        "definition": "Відсотковий дохід, що накопичився з моменту останньої купонної виплати до дати розрахунку. При купівлі облігації покупець сплачує НКД продавцю.",
        "formula": "AI = N × r_coupon × (days_since_last_coupon / 365)",
    },
    {
        "term": "Чиста ціна (Clean Price)",
        "en": "Clean Price",
        "definition": "Ціна облігації без урахування НКД. Саме цю ціну зазвичай публікують у котируваннях.",
        "formula": "Clean Price = Dirty Price − AI",
    },
    {
        "term": "Брудна ціна (Dirty Price)",
        "en": "Dirty Price / Full Price",
        "definition": "Повна ціна облігації, яку реально сплачує покупець. Включає НКД.",
        "formula": "Dirty Price = Clean Price + AI",
    },
    {
        "term": "YTM — Дохідність до погашення",
        "en": "Yield to Maturity (YTM)",
        "definition": "Ефективна річна ставка дохідності, за якої поточна вартість усіх майбутніх грошових потоків (купони + номінал) дорівнює брудній ціні облігації. Розраховується ітераційно (метод Брента).",
        "formula": "Dirty Price = Σ CF_t / (1 + YTM)^t,  де t — час у роках",
    },
    {
        "term": "SIM — Проста ставка до погашення",
        "en": "Simple Interest to Maturity (SIM)",
        "definition": "Варіант дохідності, де дисконтування відбувається за простою відсотковою ставкою. Використовується для коротких ОВДП (до 1 року). ICU публікує SIM для коротких паперів.",
        "formula": "Dirty Price = Σ CF_t / (1 + SIM × t)",
    },
    {
        "term": "Поточна дохідність",
        "en": "Current Yield",
        "definition": "Відношення річного купону до поточної брудної ціни. Проста метрика, не враховує зміну ціни до погашення.",
        "formula": "CY = Annual Coupon / Dirty Price",
    },
    {
        "term": "Дюрація Макколея",
        "en": "Macaulay Duration",
        "definition": "Середньозважений термін до погашення всіх грошових потоків облігації (у роках). Показує, через скільки років інвестор 'відбиває' вкладені кошти з урахуванням часової вартості.",
        "formula": "D_mac = Σ [t × PV(CF_t)] / Dirty Price",
    },
    {
        "term": "Модифікована дюрація",
        "en": "Modified Duration",
        "definition": "Показує процентну зміну ціни облігації при зміні YTM на 1%. Ключова міра відсоткового ризику. Чим вище — тим більш чутлива ціна до ставок.",
        "formula": "D_mod = D_mac / (1 + YTM)",
    },
    {
        "term": "DV01",
        "en": "Dollar Value of 1 basis point",
        "definition": "Зміна ціни (у гривнях) при зсуві YTM на 1 базисний пункт (0,01%). Зручна міра для оцінки грошового ризику однієї або всього портфеля.",
        "formula": "DV01 = Dirty Price × D_mod × 0.0001",
    },
    {
        "term": "Опуклість (Convexity)",
        "en": "Convexity",
        "definition": "Міра кривизни залежності ціни від дохідності. Завдяки опуклості ціна зростає більше при падінні ставок, ніж падає при їх рості — це 'безкоштовна' вигода для власника. Покращує точність оцінки зміни ціни.",
        "formula": "Convexity = Σ [t(t+1) × PV(CF_t)] / [Dirty Price × (1+YTM)²]",
    },
    {
        "term": "Спред (bid-ask spread)",
        "en": "Bid-Ask Spread",
        "definition": "Різниця між ціною, за якою ICU продає (ask) і купує (bid) облігацію. Визначає транзакційні витрати. Спред у % дохідності — різниця між buy_rate і sell_rate.",
        "formula": "Spread_price = Sell Price − Buy Price\nSpread_yield = Buy Rate − Sell Rate",
    },
    {
        "term": "До погашення (Time to Maturity)",
        "en": "Time to Maturity (TTM)",
        "definition": "Кількість днів або років від дати розрахунку до дати погашення облігації.",
        "formula": "TTM = (Maturity Date − Settlement Date) / 365",
    },
    {
        "term": "Гнучкий ФІКС",
        "en": "Flexible Fixed Rate",
        "definition": "Особливий тип ОВДП, де ставка купону може змінюватися (НБУ або МінФін переглядають). На відміну від звичайних фіксованих ОВДП, несе додатковий процентний ризик.",
        "formula": None,
    },
    {
        "term": "Weighted Average YTM",
        "en": "Portfolio Weighted Average Yield",
        "definition": "Середньозважена дохідність портфеля. Розраховується як сума добутків YTM кожного паперу на його частку в ринковій вартості портфеля.",
        "formula": "WAY = Σ (YTM_i × MV_i) / Σ MV_i",
    },
    {
        "term": "Нереалізований P&L",
        "en": "Unrealized Profit & Loss",
        "definition": "Різниця між поточною ринковою вартістю позиції та ціною купівлі. Не є фактичним прибутком/збитком, поки позицію не закрито.",
        "formula": "P&L = (Market Price − Avg Buy Price) × Quantity",
    },
]
