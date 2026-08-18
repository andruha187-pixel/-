# Original Strategy Simulator + Virtual Deposits

Исходный Strategy Simulator сохранён.

Никакой второй M03-стратегии нет.

`M03_P08_L2` принимает решение один раз своим исходным кодом.
Только ПОСЛЕ успешного оригинального исполнения это же решение получает
единый `signal_id` и отправляется виртуальным депозитам:

- $100
- $250
- $500
- $1000
- $2500

Контроль:
- `CAP_1000` использует 10 shares — как исходный M03.
- Направление и тип сигнала у CAP не могут отличаться от M03.
- CAP не рассчитывает momentum/ENTRY/SWITCH/PYRAMID самостоятельно.
- CAP не меняет `strategy_state`, `last_buy` или `price_history`.
- Все размеры исполняются из ОДНОГО снимка стакана исходного M03-сигнала.

В логах:
`SIG#123 SOURCE M03 | SWITCH Up ...`
затем:
`SIG#123 CAP_100 ...`
`SIG#123 CAP_250 ...`
`SIG#123 CAP_500 ...`
`SIG#123 CAP_1000 ...`
`SIG#123 CAP_2500 ...`

В часовом ZIP добавлены:
- deposit_accounts.csv
- deposit_trades.csv
- deposit_results.csv

Оригинальные файлы Strategy Simulator остаются без изменений.
