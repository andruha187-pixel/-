# M03 Paper Money Bot

Отдельный paper-бот для оригинальной стратегии M03_P08_L2.

Реальных ордеров не отправляет.

Одновременно ведёт независимые виртуальные счета:
- $100
- $250
- $500
- $1000
- $2500

Размер позиции масштабируется линейно:
- $100 -> 1 share на сигнал
- $250 -> 2.5 shares
- $500 -> 5 shares
- $1000 -> 10 shares
- $2500 -> 25 shares

Логика M03 не изменена:
- ENTRY_MOVE = 0.03
- PYRAMID_STEP = 0.08
- LOOKBACK = 2
- SWITCH_MOVE = 0.03
- MAX_BUYS_SIDE = 5

Для каждого счёта считаются:
cash, open position value, equity, realized PnL, total return и return %.

Каждый час Telegram получает ZIP:
- signals.csv
- orders.csv
- results.csv
- accounts.csv
- report.txt

Для Render нужен Persistent Disk /var/data, чтобы баланс счетов не сбрасывался.
