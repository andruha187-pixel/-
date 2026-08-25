# M03 V2 GATE64 X2 — Single-Strategy PAPER Trading Bot

Эта версия собрана как один торговый PAPER-бот: все старые A/B/C/E/F/G и Binance shadow-фильтры удалены.

## Единственная стратегия

`M03_V2_GATE64_X2`

Правила зафиксированы для нового out-of-sample теста:

- Polymarket BTC 5-minute Up/Down.
- Проверка примерно каждые 3 секунды.
- Сырой M03 сигнал: рост ask минимум `+0.03` за 2 тика.
- Самый первый такой сигнал решает судьбу рынка.
- Если цена первого сигнала `0.64–0.75` — GATE PASS.
- Если первый сигнал дешевле `0.64` или дороже `0.75` — рынок пропускается навсегда.
- Не ждём, пока плохой первый сигнал позже войдёт в диапазон.
- `momentum_cap = 0.30`.
- Направление после ENTRY заблокировано: SWITCH запрещён.
- Первая покупка: 10 shares.
- Один PYRAMID после роста ещё на `+0.08`.
- Максимум 2 покупки = максимум 20 обычных shares на один рынок.
- Новые входы только первые 180 секунд рынка.
- Taker fee учитывается в PAPER PnL.

## PAPER-счёт

По умолчанию:

```text
PAPER_START_BALANCE=500
MIN_FREE_CASH=5
```

При покупке стоимость и комиссия списываются с виртуального cash. После settlement выигрышные shares возвращают payout в cash.

База новая:

```text
/var/data/gate64_x2_trading_bot.db
```

Старая статистика других ботов не смешивается с этим тестом.

## Telegram

Сохранены кнопки торгового бота:

- `START`
- `STOP`
- `BALANCE`
- `STATISTICS`
- `POSITIONS`
- `TRADES`
- `PAPER`
- `LIVE`
- `EMERGENCY STOP`

После первого запуска торговля стоит `OFF`. Нажми `START`.

`LIVE` намеренно заблокирован: эта сборка PAPER-only.

## Часовой ZIP-отчёт

Отчёт сохранён. Через 5 минут после завершения каждого UTC-часа бот отправляет ZIP в тот же Telegram.

Внутри:

- `strategy_summary.csv`
- `variants_summary.csv` — совместимое имя для старого анализа
- `gate_decisions.csv`
- `paper_trades.csv`
- `signals.csv`
- `market_results.csv`
- `markets.csv`
- `report.txt`

Особенно важен `gate_decisions.csv`: там видно, какой первый сигнал получил рынок и почему он был разрешён или пропущен.

## Render

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python main.py
```

Persistent disk:

```text
/var/data
```

Оставь существующие:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Остальные переменные можно не добавлять — defaults уже есть в коде.

## Проверка

```text
python test_gate64_trader.py
```

Ожидается:

```text
GATE64 X2 single-strategy trading bot regression: OK
```

## Важно

Это именно тест новой стратегии. Параметры `0.64–0.75`, `+0.08`, `max 2 buys` лучше сейчас не менять, чтобы следующие отчёты были настоящим out-of-sample тестом, а не новой подгонкой под уже просмотренные данные.
