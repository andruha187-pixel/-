# BTC SAFE67 — BASE vs REVERSAL DCA

PAPER-only A/B bot for a clean test of whether averaging **after a dip and confirmed rebound** improves the SAFE67 strategy.

## What is unchanged

The first entry is intentionally preserved from the previous SAFE67 BTC bot:

```text
Market: BTC 5-minute Up/Down
Decision loop: ~3 seconds
Trade window: first 180 seconds

First V2-eligible signal:
price    0.55..0.75
momentum 0.03..0.30

SAFE67 PASS:
price    0.67..0.75
momentum 0.05..0.10

ENTRY = 5 shares
LOOKBACK = 2 ticks
NO SWITCH
```

There is no pre-decision `ensure_book()` REST refresh. As in the source bot, the signal uses the WebSocket-maintained book. `ensure_book()` remains immediately before a simulated fill.

`strategy_parity_check.txt` verifies that the V2 candidate function and the complete SAFE67 gate + first ENTRY block are exact matches to the supplied source bot.

## A — SAFE67 BASE

```text
ENTRY 5 shares
No second buy
No stop-loss
Hold to settlement
Maximum position: 5 shares
```

This gives the baseline PnL of the SAFE67 signal without either the old +0.08 pyramid or a stop.

## B — SAFE67 REVERSAL DCA

First ENTRY is exactly the same 5 shares as A.

After ENTRY:

### Stage 1 — arm the DCA

If the held side's **ask <= 0.50** and market elapsed time is **<= 120 seconds**:

```text
DCA ARMED
```

No order is placed on that tick.

This is important: simply falling to 0.50 does **not** trigger a buy.

### Stage 2 — require rebound

On a later ~3-second decision tick, while elapsed time is still <=120 seconds:

```text
same held side
momentum over the same 2-tick lookback >= +0.05
current ask <= 0.60
```

Only then:

```text
DCA BUY = 5 shares
```

There is only one DCA.

Maximum B position:

```text
5 ENTRY + 5 DCA = 10 shares
```

If price keeps falling without a +0.05 rebound, B does not add.

If the rebound happens after 120 seconds, B does not add.

## Stop-loss

There is **no stop-loss** in either A or B.

The old B post-pyramid `0.40` stop loop is not started and is not part of this experiment.

## Hourly report

The bot keeps hourly ZIP reports because they are useful for comparing the new rule.

ZIP root:

```text
variants_summary.csv
markets.csv
report.txt
```

Variant folders:

```text
A_safe67_base_5sh/
B_safe67_reversal_dca_5plus5/
```

Each contains:

```text
summary.csv
gate_decisions.csv
paper_trades.csv
dca_events.csv
signals.csv
market_results.csv
position_trajectory.csv
report.txt
```

`dca_events.csv` records:

```text
when DCA armed
ask at arm
elapsed time at arm
whether DCA later filled
fill ask
fill momentum
fill elapsed time
```

`position_trajectory.csv` remains available so we can later retest 0.45/0.50/0.55 arm levels, rebound +0.03/+0.05/+0.07, and different deadlines from collected paths.

## Telegram

Buttons:

```text
START
STOP
BALANCE
STATISTICS
POSITIONS
TRADES
PAPER
LIVE
EMERGENCY STOP
```

LIVE remains disabled in this experiment.

`STATISTICS` additionally shows B:

```text
DCA armed/filled
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

Persistent disk:

```text
/var/data
```

Fresh database:

```text
/var/data/safe67_base_vs_reversal_dca.db
```

Fresh report folder:

```text
/var/data/safe67_base_vs_reversal_dca_reports
```

After a new deploy, trading defaults to OFF. Press `START`.

## Regression

Run:

```text
python test_base_vs_dca.py
```

Expected:

```text
SAFE67 BASE vs REVERSAL DCA regression: OK
```

The test verifies:

- exact SAFE67 entry thresholds;
- A buys only 5 shares;
- B does not buy when merely reaching 0.50;
- B does not average a continuing falling move;
- B buys 5 shares after a later +0.05 rebound at ask <=0.60;
- B never buys a third time;
- rebound after 120 seconds is ignored;
- no stop-loss engine is present;
- settlement accounting;
- hourly ZIP contains DCA events and trajectory data.
