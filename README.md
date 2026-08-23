# M03 Four-Way CONF65 PAPER Bot v4.0 — V5 Dynamic Hedge

One process compares four independent PAPER strategies on the same BTC 5-minute Polymarket order-book snapshots and the same Binance CONF65 feature snapshot.

## Strategies

### A — M03_V3_NOSW90 + CONF65
- entry move: 0.03
- pyramid step: 0.08
- lookback: 2
- no side switching
- max 5 buys on a side
- all new buys stop after second 90

### B — M03_V2_LOCK + CONF65
- entry move: 0.03
- pyramid step: 0.08
- lookback: 2
- no side switching
- max 6 buys
- first-entry price band: 0.55–0.75
- momentum cap: 0.30

### C — M03_V5_DYNAMIC + CONF65
- entry move: 0.03
- pyramid step: 0.08
- lookback: 2
- switch move: 0.03
- max 5 buys per side
- original V5 dynamic switch rules preserved

### E — M03_V5_DYNAMIC_HEDGE + CONF65
E has the same normal V5 base rules and CONF65 filtering as C. Its only extra component is a separate risk-management hedge layer.

Default hedge rules:
- first actual non-HEDGE PAPER fill defines the protected/primary side;
- hedge becomes eligible once the primary side reaches 20 actual shares;
- existing opposite-side V5 SWITCH/PYRAMID shares already count as protection;
- the bot buys only the missing opposite-side shares needed to target `PnL >= -$10` if the primary side loses;
- hedge sizing walks the captured opposite-side order-book asks and includes the same taker-fee formula used by the simulator;
- hedge spending is capped so that the projected PnL if the primary side wins stays at least `+$2`;
- HEDGE orders do not require Binance CONF65 because they are risk management, not a new directional signal;
- HEDGE orders do not modify V5 `buys`, `last_buy`, `started_sides`, or shadow acceptance state;
- if a hedge was partial because of book depth or the upside cap, E can top it up on a later decision tick when possible.

Environment controls:

```text
HEDGE_START_SHARES=20
HEDGE_MAX_LOSS=10
HEDGE_MIN_UPSIDE=2
HEDGE_MIN_ORDER_SHARES=0.05
```

`HEDGE_MAX_LOSS=10` means the target floor is `-$10`, not a guaranteed stop. If the opposite outcome is too expensive, liquidity is insufficient, or preserving `HEDGE_MIN_UPSIDE` prevents enough hedge spending, E buys only the protection that fits those constraints.

## Fair A/B/C/E timing

All four variants run in one process with:
- one ~3-second scheduler;
- one captured Polymarket book snapshot per market/tick;
- one shared Binance core-feature snapshot per market/tick;
- independent base states;
- independent CONF65 shadow states;
- independent PAPER balances.

E's normal V5 decision is evaluated on the same tick as C. Only after the normal strategy evaluations does E's independent hedge risk-manager run against that same captured book.

## PAPER balances and database

Each strategy starts from its own `$500` by default. Balances are not pooled.

This version intentionally uses a new database so A/B/C/E begin a fresh fair comparison together:

`/var/data/m03_fourway_conf65_hedge.db`

The previous three-way database is not overwritten.

## Telegram

- `BALANCE` shows four independent accounts.
- `STATISTICS` shows W/L, fees, average win/loss, worst market, realized PnL and equity.
- E statistics additionally show hedge count and total hedge cost.
- `POSITIONS` shows each strategy's open outcome positions.
- `TRADES` shows the last 10 trades per strategy; E protection orders are labeled `HEDGE`.
- Settlement reports show each strategy independently.
- `START` / `STOP` control all four accounts together.

## LIVE

This build remains PAPER-only. LIVE is intentionally disabled while the four virtual strategies are being compared.

## Render deployment

1. Replace the repository files with this package.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python main.py`
4. Persistent disk: `/var/data`
5. Keep your existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` environment values.
6. Add the four hedge environment values above, or leave them unset to use the defaults.
7. Keep `CONF_MIN=65`.
8. Deploy and press `START` in Telegram.

Startup log should contain:

`4.0-paper-abce-m03-conf65-v5-hedge started | PAPER ONLY | CONF>=65.0`

## Verification

Run:

`python test_fourway.py`

Expected final line:

`four-way CONF65 + V5 hedge regression: OK`
