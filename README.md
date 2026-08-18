# M03 Exact Paper Money Bot

Это НЕ переписанная версия M03.

Основа — исходный Strategy Simulator. Его M03_P08_L2 оставлен с теми же
параметрами и тем же движком принятия решений:

- entry_move = 0.03
- pyramid_step = 0.08
- lookback = 2
- switch_move = 0.04
- max_buys_side = 6
- TRADE_WINDOW_SECONDS = 180
- DECISION_INTERVAL = 3.0
- MIN_PRICE = 0.08
- MAX_PRICE = 0.95

Чтобы максимально сохранить поведение исходника, все варианты Strategy
Simulator остаются внутри процесса. Виртуальные счета НИЧЕГО не меняют
в strategy_state M03.

После каждого успешно исполненного оригинального M03-сигнала фиксируется
снимок стакана, и отдельно считаются виртуальные счета:

- $100 -> 1 share
- $250 -> 2.5 shares
- $500 -> 5 shares
- $1000 -> 10 shares
- $2500 -> 25 shares

$1000 является контрольным счётом: его размер ордера совпадает с оригинальным
M03 (10 shares). Сигналы M03 и строка M03 в обычном paper_trades.csv должны
совпадать с исходным Strategy Simulator.

В ZIP каждый час дополнительно:
- m03_accounts.csv
- m03_account_trades.csv
- m03_account_results.csv

Используйте отдельный Render service и Persistent Disk /var/data.
