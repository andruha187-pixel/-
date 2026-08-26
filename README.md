# GATE64 SAFE A/B — stop-loss 0.30 experiment

Один PAPER-бот одновременно запускает два независимых варианта на одних и тех же BTC 5-minute Polymarket данных.

## A / SAFE NO STOP

Полностью повторяет предыдущий `GATE64 SAFE`:

- ждём первый V2-eligible сигнал: цена `0.55–0.75`, momentum `0.03–0.30`;
- SAFE PASS только если первый eligible сигнал одновременно `0.64–0.75` и momentum `0.05–0.10`;
- ENTRY = 5 shares;
- один PYRAMID = 10 shares после +0.08;
- максимум 15 shares;
- SWITCH отсутствует;
- стоп-лосса нет.

## B / SAFE STOP 0.30

Входы и PYRAMID абсолютно такие же, как у A.

Единственное отличие: после появления позиции отдельный быстрый цикл проверяет стакан каждые `0.20 s`.

Стоп активируется, когда **best bid удерживаемого контракта <= 0.30**.

После срабатывания бот моделирует market-sell оставшихся shares по реально видимым bid уровням стакана и учитывает taker fee. Если видимой глубины не хватает, продаёт доступную часть и продолжает пытаться закрыть остаток. После первого stop trigger B больше не делает ENTRY/PYRAMID в этом рынке, даже если цена восстановилась.

Это консервативнее, чем считать, что стоп всегда исполняется ровно по 0.30: если рынок перепрыгнет с 0.31 сразу на 0.24, PAPER sell пойдёт по доступным bid около 0.24.

## Два независимых счёта

По умолчанию:

```text
A = $500
B = $500
```

Cash и PnL не смешиваются.

Новая база:

```text
/var/data/gate64_safe_ab_stop30.db
```

## Telegram

`START` / `STOP` управляют новыми входами сразу для A и B.

Важно: если нажать STOP, B всё равно продолжает контролировать stop-loss уже открытой позиции.

`BALANCE`, `STATISTICS`, `POSITIONS`, `TRADES` показывают A и B отдельно.

## Часовой ZIP

Каждый час приходит один ZIP, но внутри отчёты полностью разделены:

```text
variants_summary.csv
report.txt
markets.csv

A_no_stop/
    summary.csv
    gate_decisions.csv
    paper_trades.csv
    paper_exits.csv
    stop_events.csv
    signals.csv
    market_results.csv
    position_trajectory.csv
    report.txt

B_stop_030/
    summary.csv
    gate_decisions.csv
    paper_trades.csv
    paper_exits.csv
    stop_events.csv
    signals.csv
    market_results.csv
    position_trajectory.csv
    report.txt
```

У A `paper_exits.csv` и `stop_events.csv` обычно пустые. У B там видно каждый stop trigger и фактическое PAPER-исполнение.

## Render

Build:

```text
pip install -r requirements.txt
```

Start:

```text
python main.py
```

Persistent disk:

```text
/var/data
```

После первого запуска нажми `START`.

## LIVE

Сборка намеренно PAPER-only. Реальные Polymarket ордера не отправляются.
