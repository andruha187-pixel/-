# M03 V2 GATE64 X2 — single PAPER strategy

This package is a stripped-down version of the strategy simulator that produced the user's hourly ZIP archives. All losing comparison variants and the unused Binance shadow experiment were removed. It stays PAPER-only.

## Fixed strategy

- BTC 5-minute Up/Down markets.
- Decision interval: ~3 seconds.
- Raw M03 first signal: ask momentum >= `0.03` over 2 decision ticks.
- **The first qualifying raw M03 signal decides the market.**
- If that first signal price is below `0.64` or above `0.75`, the entire market is permanently skipped. The bot does not wait for price to enter the band later.
- If the first signal price is `0.64..0.75`, the market passes the gate.
- Actual ENTRY still requires momentum <= `0.30`.
- No opposite-side switching.
- Pyramid step: `+0.08` from the previous actual buy price.
- Pyramid momentum must be positive and <= `0.30`.
- Maximum buys on the chosen side: **2 total** (`ENTRY + at most one PYRAMID`).
- Order size: 10 shares by default.
- Taker fee model is the same as the source simulator.

## Why `gate_decisions.csv` was added

Every first qualifying M03 signal is stored with:

- price;
- momentum;
- side;
- elapsed seconds;
- PASS/SKIP;
- reason (`FIRST_SIGNAL_PRICE_LOW`, `FIRST_SIGNAL_PRICE_HIGH`, `FIRST_SIGNAL_PRICE_OK`).

This makes the next batch of hourly reports much easier to analyze without guessing which markets were rejected by the new filter.

## Hourly Telegram ZIP

The bot keeps the same hourly reporting idea and sends the completed hour about 5 minutes later.

Each ZIP contains:

- `strategy_summary.csv`
- `variants_summary.csv` — compatibility alias for older analysis
- `gate_decisions.csv`
- `paper_trades.csv`
- `signals.csv`
- `market_results.csv`
- `markets.csv`
- `report.txt`

## Clean database

The test uses a new DB:

`/var/data/strategy_gate64_x2.db`

Old multi-strategy history will not contaminate this run.

## Render

Build command:

`pip install -r requirements.txt`

Start command:

`python main.py`

Persistent disk:

`/var/data`

Keep your existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. All strategy defaults are already built in; no new environment variables are required unless you intentionally want to change the test.

## Test

Run:

`python test_gate64.py`

Expected final line:

`M03_V2_GATE64_X2 regression: OK`
