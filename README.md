# MULTI7 SAFE67 — BASE vs REVERSAL DCA

PAPER-only bot for the same A/B experiment on **seven** Polymarket 5-minute crypto markets:

```text
BTC
XRP
BNB
SOL
ETH
DOGE
HYPE (Hyperliquid)
```

There are **two independent strategy accounts per token**, so 14 PAPER accounts in total. Each starts at `$500` by default. Results do not share a virtual balance.

## Strategy A — SAFE67 BASE

For every token:

```text
First V2-eligible signal:
price    0.55..0.75
momentum 0.03..0.30

SAFE67 PASS:
price    0.67..0.75
momentum 0.05..0.10

ENTRY = 5 shares
No second buy
No stop-loss
No switch
Hold to settlement
```

The signal loop is approximately every 3 seconds and uses the WebSocket-maintained book without a pre-decision REST refresh, matching our prior SAFE67 logic.

## Strategy B — SAFE67 REVERSAL DCA

First ENTRY is exactly the same as A.

After ENTRY:

```text
ask <= 0.50
elapsed <= 120 sec
```

sets:

```text
DCA ARMED
```

**No purchase is made on the arming tick.**

On a later decision tick, before/at 120 seconds:

```text
same held side
momentum over the same 2-tick lookback >= +0.05
ask <= 0.60
```

then the bot buys:

```text
DCA = 5 shares
```

Only one DCA is allowed.

Default maximum position:

```text
5 ENTRY + 5 DCA = 10 shares
```

If price keeps falling, there is no DCA. If rebound occurs only after 120 seconds, there is no DCA.

## Stop-loss

There is **no stop-loss in either strategy**.

The old post-pyramid `0.40` stop is not started in this experiment.

## Accounts

For each token:

```text
TOKEN_A_SAFE67_BASE
TOKEN_B_SAFE67_REVERSAL_DCA
```

For example:

```text
BTC_A_SAFE67_BASE
BTC_B_SAFE67_REVERSAL_DCA
XRP_A_SAFE67_BASE
XRP_B_SAFE67_REVERSAL_DCA
...
```

Each has its own `$500` virtual PAPER balance.

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

`STATISTICS` sends a separate compact comparison for each token.

For B it also shows:

```text
DCA armed/filled
```

LIVE is deliberately disabled.

## Hourly ZIP report

One combined ZIP arrives each hour.

Root:

```text
variants_summary.csv
markets.csv
report.txt
```

For every token there are two folders, for example:

```text
BTC/A_safe67_base_5sh/
BTC/B_safe67_reversal_dca_5plus5/

XRP/A_safe67_base_5sh/
XRP/B_safe67_reversal_dca_5plus5/
```

and the same structure for BNB, SOL, ETH, DOGE and HYPE.

Each strategy folder contains:

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

`dca_events.csv` tells us when the DCA was armed and whether/when it filled.

`position_trajectory.csv` remains important because later we can retest different DCA thresholds from the collected price paths.

## Market discovery

The bot uses the same multi-asset discovery functions as our previous MULTI6 bot, with BTC added:

```text
btc-updown-5m
xrp-updown-5m
bnb-updown-5m
sol-updown-5m
eth-updown-5m
doge-updown-5m
hype-updown-5m
```

`strategy_parity_check.txt` verifies:

- `_first_v2_eligible_candidates()` exactly matches the BTC BASE-vs-DCA bot;
- `evaluate_variant()` exactly matches the BTC BASE-vs-DCA bot;
- multi-asset discovery/parser functions exactly match the previous MULTI6 collector.

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

Fresh DB:

```text
/var/data/safe67_multi7_base_vs_reversal_dca.db
```

Reports:

```text
/var/data/safe67_multi7_base_vs_reversal_dca_reports
```

Trading starts OFF after a fresh database. Press `START`.

## Regression test

Run:

```text
python test_multi7_base_vs_dca.py
```

Expected:

```text
MULTI7 SAFE67 BASE vs REVERSAL DCA regression: OK
```

The test checks all seven token configs, 14 independent accounts, BTC and XRP full DCA paths, symbol-specific settlement, no third buy, no stop engine, market prefix parsing, and all per-token ZIP report folders.
