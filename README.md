# GATE64 SAFE A/B — clean experiment, B stop 0.30

Эта версия специально собрана для чистого сравнения:

- **A / SAFE NO STOP** — исходный `GATE64 SAFE` без стопа.
- **B / SAFE STOP 0.30** — та же стратегия, но после открытия позиции у B есть отдельный стоп по `best bid <= 0.30`.

## Главное исправление относительно предыдущей A/B версии

В старой A/B сборке перед каждым 3-секундным сигналом было дополнительное:

```python
await ensure_book(up)
await ensure_book(down)
```

В этой версии этого **нет**.

Сигнальный цикл теперь повторяет исходный одиночный `GATE64 SAFE`:

```python
for asset in (up_asset, down_asset):
    ask = best_ask(asset)
    if ask is not None:
        price_history[cid][asset].append((now_ms(), ask))
```

То есть для ENTRY/PYRAMID используется текущий стакан, который поддерживается WebSocket, без дополнительного REST-refresh перед самим сигналом.

Это нужно именно для чистоты эксперимента: **A должен вести себя как исходный SAFE**, а B должен отличаться только стопом.

## Где ensure_book() всё ещё остаётся

Он не удалён полностью.

1. **Непосредственно при PAPER-покупке** — как и в исходном SAFE. После того как сигнал уже сформирован, бот проверяет свежесть стакана перед моделированием исполнения.
2. **В отдельном stop-loss цикле B** — перед проверкой/исполнением стопа, потому что это уже отдельная функция B.

Таким образом, `ensure_book()` больше не влияет на момент, когда SAFE решает PASS/SKIP/ENTRY/PYRAMID.

## Стратегия A и B

Одинаковая до момента стопа:

```text
V2 eligible price:     0.55–0.75
V2 eligible momentum:  0.03–0.30
SAFE price:             0.64–0.75
SAFE momentum:          0.05–0.10
ENTRY:                   5 shares
PYRAMID:                10 shares
PYRAMID step:           +0.08
PYRAMID momentum cap:    0.30
Max buys:                2
Switch:                  OFF
```

### A

Никакого стопа. Позиция живёт до settlement.

### B

После открытия позиции отдельный быстрый цикл проверяет её примерно каждые `0.20` секунды.

Если:

```text
held contract best bid <= 0.30
```

B делает PAPER stop-market sell по реально видимой bid-глубине. Если рынок перескочил ниже 0.30, исполнение не рисуется по 0.30 — используются доступные bids.

После stop B больше не добавляет риск в этот рынок.

## Отдельные PAPER-счета

```text
A: $500
B: $500
```

Новая база:

```text
/var/data/gate64_safe_ab_stop30_cleanloop.db
```

Старая A/B история не смешивается с этим чистым тестом.

## Часовой ZIP

Один ZIP в Telegram, внутри отдельные данные A и B:

```text
variants_summary.csv
report.txt
markets.csv

A_no_stop/
  summary.csv
  gate_decisions.csv
  signals.csv
  paper_trades.csv
  paper_exits.csv
  market_results.csv
  position_trajectory.csv

B_stop_030/
  summary.csv
  gate_decisions.csv
  signals.csv
  paper_trades.csv
  paper_exits.csv
  stop_events.csv
  market_results.csv
  position_trajectory.csv
```

## Render

Build:

```text
pip install -r requirements.txt
```

Start:

```text
python main.py
```

Оставь существующие `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` и persistent disk `/var/data`.

После нового деплоя нажми **START**.

## Проверка

```text
python test_ab_stop30.py
```

Ожидается:

```text
GATE64 SAFE A/B CLEAN LOOP + STOP 0.30 regression: OK
```
