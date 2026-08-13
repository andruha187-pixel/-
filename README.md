# Powerwinner Wallet Observer v2 — Render Free

This build is adapted for **Render Free**.

Target wallet:

`0xf3531b23b504cf0aed4ff21325232b2a2d496685`

## What changed for the free version

Render Free has no persistent disk. The SQLite database is therefore treated as a working cache, not as the permanent archive.

The permanent archive is the **hourly ZIP sent to Telegram**.

After a restart the bot re-fetches the last 6 hours of public Polymarket trades. This lets it reconstruct the current hour's trade history. Historical order-book snapshots cannot be reconstructed, so a backfilled trade may have `execution_type=UNKNOWN`.

## Files sent every hour

- `trades.csv`
- `markets_summary.csv`
- `book_events.csv`
- `positions.csv`
- `report.txt`
- `metadata.json`

Upload the ZIP directly to ChatGPT later for strategy analysis.

## 1. GitHub

Create a repository and upload:

- `main.py`
- `requirements.txt`
- `render.yaml`
- `.gitignore`
- `.env.example`
- `README.md`

Do **not** upload your real Telegram token.

## 2. Render Free

Create a Web Service from the GitHub repository.

Build command:

`pip install -r requirements.txt`

Start command:

`python main.py`

Environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional:

- `WALLET=0xf3531b23b504cf0aed4ff21325232b2a2d496685`
- `POLL_INTERVAL=5`
- `POSITIONS_INTERVAL=300`
- `BOOTSTRAP_LOOKBACK_HOURS=6`
- `BOOK_RETENTION_HOURS=24`
- `DATA_DIR=./data`

Do not add a persistent disk: Render Free does not support it.

## 3. Prevent idle spin-down with UptimeRobot

When Render gives you a URL such as:

`https://powerwinner-wallet-observer.onrender.com`

create a free HTTP(s) monitor in UptimeRobot.

Monitor URL:

`https://YOUR-RENDER-URL.onrender.com/health`

Monitoring interval:

**5 minutes**

The bot's `/health` endpoint returns HTTP 200 JSON. Regular inbound checks keep the free web service from reaching Render's 15-minute no-inbound-traffic idle window.

## 4. Telegram

At startup the bot sends a short startup message.

Every UTC hour it sends one ZIP file.

If Render restarts during the hour, the bot re-fetches recent trades from the public Polymarket Data API and the next report begins from the start of the current UTC hour.

## Important maker/taker limitation

The bot cannot see Powerwinner's private outstanding limit orders.

It estimates:

- `MAKER_LIKELY`
- `TAKER_LIKELY`
- `UNKNOWN`

from the public CLOB order book near execution time.

If a trade is recovered after a Render restart and the live order-book history no longer exists, it is left `UNKNOWN` rather than guessed.

## Health page

Open:

`https://YOUR-RENDER-URL.onrender.com/health`

You should see JSON containing:

- `ok: true`
- wallet
- number of stored trades
- last trade time
- number of subscribed WebSocket assets
