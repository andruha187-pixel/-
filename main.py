import os
import io
import csv
import json
import time
import math
import zipfile
import sqlite3
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import aiohttp
from aiohttp import web
import websockets
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

WALLET = os.getenv(
    "WALLET",
    "0xf3531b23b504cf0aed4ff21325232b2a2d496685"
).lower()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
POSITIONS_INTERVAL = int(os.getenv("POSITIONS_INTERVAL", "300"))
REPORT_INTERVAL = int(os.getenv("REPORT_INTERVAL", "3600"))
BOOK_RETENTION_HOURS = int(os.getenv("BOOK_RETENTION_HOURS", "48"))
PORT = int(os.getenv("PORT", "8080"))
REPORT_DELAY_SECONDS = int(os.getenv("REPORT_DELAY_SECONDS", "120"))
REPORT_CHECK_INTERVAL = int(os.getenv("REPORT_CHECK_INTERVAL", "30"))

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
TRADE_PAGE_LIMIT = 1000

# Render Free has an ephemeral filesystem, so use a local writable directory.
# Hourly Telegram ZIP files are the durable archive.
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOOTSTRAP_LOOKBACK_HOURS = int(os.getenv("BOOTSTRAP_LOOKBACK_HOURS", "6"))

DB_PATH = DATA_DIR / "powerwinner_observer.db"
REPORT_DIR = DATA_DIR / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("powerwinner")

# Shared state
ws_send_queue: asyncio.Queue = asyncio.Queue()
subscribed_assets = set()
latest_books = {}
last_report_end = None
session: aiohttp.ClientSession | None = None


# ============================================================
# HELPERS
# ============================================================

def now_ts() -> int:
    return int(time.time())

def utc_iso(ts: int | float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

def trade_uid(t: dict) -> str:
    # Data API does not expose a dedicated trade id in this public response.
    # transactionHash can repeat for multiple fills, so include trade fields.
    parts = [
        str(t.get("transactionHash", "")),
        str(t.get("timestamp", "")),
        str(t.get("asset", "")),
        str(t.get("side", "")),
        str(t.get("price", "")),
        str(t.get("size", "")),
        str(t.get("outcome", "")),
        str(t.get("conditionId", "")),
    ]
    return "|".join(parts)

def is_crypto_5m(title: str, slug: str, event_slug: str) -> bool:
    s = f"{title} {slug} {event_slug}".lower()
    crypto = any(x in s for x in ("bitcoin", "btc", "ethereum", "eth"))
    five = any(x in s for x in ("5m", "5-min", "5 min", "5 minute", "5-minute"))
    # Polymarket titles often contain a five-minute time range but not literal "5m".
    # We keep all BTC/ETH Up/Down markets and tag likely 5m ones later.
    updown = ("up or down" in s) or ("up-down" in s) or ("updown" in s)
    return crypto and (five or updown)

def classify_symbol(title: str, slug: str, event_slug: str) -> str:
    s = f"{title} {slug} {event_slug}".lower()
    if "bitcoin" in s or "btc" in s:
        return "BTC"
    if "ethereum" in s or "eth" in s:
        return "ETH"
    return "OTHER"

def estimate_execution(side: str, trade_price: float, bid: float | None, ask: float | None):
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return "UNKNOWN", None
    eps = 0.0006
    spread = ask - bid

    # For a BUY:
    # - execution at/near ask -> aggressive/taker-like
    # - execution at/near bid -> passive/maker-like
    # For a SELL, reverse.
    if side.upper() == "BUY":
        if trade_price >= ask - eps:
            return "TAKER_LIKELY", spread
        if trade_price <= bid + eps:
            return "MAKER_LIKELY", spread
    elif side.upper() == "SELL":
        if trade_price <= bid + eps:
            return "TAKER_LIKELY", spread
        if trade_price >= ask - eps:
            return "MAKER_LIKELY", spread
    return "UNKNOWN", spread


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with db_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            uid TEXT PRIMARY KEY,
            observed_at INTEGER NOT NULL,
            trade_ts INTEGER NOT NULL,
            trade_time_utc TEXT NOT NULL,
            proxy_wallet TEXT,
            side TEXT,
            asset TEXT,
            condition_id TEXT,
            size REAL,
            price REAL,
            usd_value REAL,
            title TEXT,
            slug TEXT,
            event_slug TEXT,
            outcome TEXT,
            outcome_index INTEGER,
            symbol TEXT,
            is_crypto_5m INTEGER DEFAULT 0,
            transaction_hash TEXT,
            best_bid REAL,
            best_ask REAL,
            book_ts INTEGER,
            book_age_ms INTEGER,
            execution_type TEXT DEFAULT 'UNKNOWN',
            spread REAL,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(trade_ts);
        CREATE INDEX IF NOT EXISTS idx_trades_condition ON trades(condition_id);
        CREATE INDEX IF NOT EXISTS idx_trades_asset ON trades(asset);

        CREATE TABLE IF NOT EXISTS book_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_ts_ms INTEGER NOT NULL,
            exchange_ts_ms INTEGER,
            asset TEXT NOT NULL,
            condition_id TEXT,
            best_bid REAL,
            best_ask REAL,
            bid_size REAL,
            ask_size REAL,
            last_trade_price REAL,
            event_type TEXT,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_books_asset_ts
        ON book_events(asset, received_ts_ms);

        CREATE TABLE IF NOT EXISTS positions_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_ts INTEGER NOT NULL,
            kind TEXT NOT NULL,
            condition_id TEXT,
            asset TEXT,
            title TEXT,
            outcome TEXT,
            size REAL,
            avg_price REAL,
            current_value REAL,
            cash_pnl REAL,
            realized_pnl REAL,
            total_bought REAL,
            raw_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pos_ts ON positions_snapshots(snapshot_ts);

        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

def get_state(key: str, default=None):
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_state(key: str, value):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()

def insert_trade(t: dict) -> bool:
    uid = trade_uid(t)
    asset = str(t.get("asset", ""))
    tts = safe_int(t.get("timestamp"))
    side = str(t.get("side", "")).upper()
    price = safe_float(t.get("price"))
    size = safe_float(t.get("size"))
    title = str(t.get("title", ""))
    slug = str(t.get("slug", ""))
    event_slug = str(t.get("eventSlug", ""))
    symbol = classify_symbol(title, slug, event_slug)

    # Use latest in-memory book if it is temporally plausible.
    book = latest_books.get(asset)
    bid = ask = None
    book_ts = None
    book_age_ms = None
    execution_type = "UNKNOWN"
    spread = None

    if book:
        bid = book.get("best_bid")
        ask = book.get("best_ask")
        book_ts = book.get("received_ts_ms")
        book_age_ms = abs((tts * 1000) - book_ts) if tts and book_ts else None
        # Only classify from an in-memory snapshot close to the actual trade.
        if book_age_ms is not None and book_age_ms <= 15000:
            execution_type, spread = estimate_execution(side, price, bid, ask)

    row = (
        uid, now_ts(), tts, utc_iso(tts),
        str(t.get("proxyWallet", "")).lower(),
        side, asset, str(t.get("conditionId", "")),
        size, price, size * price,
        title, slug, event_slug, str(t.get("outcome", "")),
        safe_int(t.get("outcomeIndex"), -1),
        symbol, int(is_crypto_5m(title, slug, event_slug)),
        str(t.get("transactionHash", "")),
        bid, ask, book_ts, book_age_ms, execution_type, spread,
        json_dumps(t),
    )

    with db_connect() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO trades(
                uid, observed_at, trade_ts, trade_time_utc, proxy_wallet,
                side, asset, condition_id, size, price, usd_value,
                title, slug, event_slug, outcome, outcome_index,
                symbol, is_crypto_5m, transaction_hash,
                best_bid, best_ask, book_ts, book_age_ms,
                execution_type, spread, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, row)
        conn.commit()
        return cur.rowcount > 0

def insert_book_event(asset: str, event: dict, event_type: str, payload: dict):
    recv_ms = int(time.time() * 1000)
    exch_ts = payload.get("timestamp") or event.get("timestamp")
    exch_ts = safe_int(exch_ts, 0) or None

    def top(levels, want_max):
        if not levels:
            return None, None
        parsed = []
        for level in levels:
            if isinstance(level, dict):
                p = safe_float(level.get("price"), math.nan)
                s = safe_float(level.get("size"), 0)
            else:
                continue
            if not math.isnan(p):
                parsed.append((p, s))
        if not parsed:
            return None, None
        return (max(parsed) if want_max else min(parsed))

    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    bid, bid_size = top(bids, True)
    ask, ask_size = top(asks, False)

    # price_change/best_bid_ask events can provide direct values.
    bid = safe_float(payload.get("best_bid"), bid[0] if isinstance(bid, tuple) else bid) if payload.get("best_bid") is not None else (bid[0] if isinstance(bid, tuple) else bid)
    ask = safe_float(payload.get("best_ask"), ask[0] if isinstance(ask, tuple) else ask) if payload.get("best_ask") is not None else (ask[0] if isinstance(ask, tuple) else ask)

    # Fix size extraction from tuple path.
    if isinstance(bid_size, tuple):
        bid_size = bid_size[1]
    if isinstance(ask_size, tuple):
        ask_size = ask_size[1]

    # If event is direct best_bid_ask and sizes are available.
    if payload.get("best_bid_size") is not None:
        bid_size = safe_float(payload.get("best_bid_size"))
    if payload.get("best_ask_size") is not None:
        ask_size = safe_float(payload.get("best_ask_size"))

    last_trade = payload.get("last_trade_price") or payload.get("lastTradePrice")
    last_trade = safe_float(last_trade, 0) or None

    previous = latest_books.get(asset, {})
    if bid is None:
        bid = previous.get("best_bid")
    if ask is None:
        ask = previous.get("best_ask")

    changed = (
        previous.get("best_bid") != bid
        or previous.get("best_ask") != ask
        or event_type in ("book", "best_bid_ask")
    )

    latest_books[asset] = {
        "received_ts_ms": recv_ms,
        "exchange_ts_ms": exch_ts,
        "best_bid": bid,
        "best_ask": ask,
        "condition_id": payload.get("market") or payload.get("condition_id"),
    }

    if not changed:
        return

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO book_events(
                received_ts_ms, exchange_ts_ms, asset, condition_id,
                best_bid, best_ask, bid_size, ask_size, last_trade_price,
                event_type, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            recv_ms, exch_ts, asset,
            str(payload.get("market") or payload.get("condition_id") or ""),
            bid, ask, bid_size, ask_size, last_trade,
            event_type, json_dumps(event),
        ))
        conn.commit()

def save_positions(kind: str, rows: list[dict]):
    ts = now_ts()
    with db_connect() as conn:
        for p in rows:
            conn.execute("""
                INSERT INTO positions_snapshots(
                    snapshot_ts, kind, condition_id, asset, title, outcome,
                    size, avg_price, current_value, cash_pnl, realized_pnl,
                    total_bought, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                ts, kind,
                str(p.get("conditionId") or p.get("condition_id") or ""),
                str(p.get("asset") or ""),
                str(p.get("title") or ""),
                str(p.get("outcome") or ""),
                safe_float(p.get("size")),
                safe_float(p.get("avgPrice") or p.get("avg_price")),
                safe_float(p.get("currentValue") or p.get("current_value")),
                safe_float(p.get("cashPnl") or p.get("cash_pnl")),
                safe_float(p.get("realizedPnl") or p.get("realized_pnl")),
                safe_float(p.get("totalBought") or p.get("total_bought")),
                json_dumps(p),
            ))
        conn.commit()


# ============================================================
# HTTP / TELEGRAM
# ============================================================

async def get_json(url: str, params=None):
    global session
    assert session is not None
    for attempt in range(3):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                if r.status == 200:
                    return json.loads(text)
                log.warning("HTTP %s %s -> %s: %s", r.status, url, params, text[:300])
        except Exception as e:
            log.warning("GET error %s attempt %s: %s", url, attempt + 1, e)
        await asyncio.sleep(1.5 * (attempt + 1))
    return None

async def telegram_send_text(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}
    try:
        async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                log.warning("Telegram sendMessage failed: %s", await r.text())
    except Exception as e:
        log.warning("Telegram text error: %s", e)

async def telegram_send_file(path: Path, caption: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials are missing; report saved locally: %s", path)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"

    try:
        form = aiohttp.FormData()
        form.add_field("chat_id", TELEGRAM_CHAT_ID)
        form.add_field("caption", caption[:1024])
        form.add_field(
            "document",
            path.read_bytes(),
            filename=path.name,
            content_type="application/zip",
        )

        async with session.post(
            url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=120)
        ) as r:
            if r.status != 200:
                log.warning("Telegram sendDocument failed: %s", await r.text())
                return False

            log.info("Report sent to Telegram: %s", path.name)
            return True

    except Exception as e:
        log.exception("Telegram file error: %s", e)
        return False


# ============================================================
# DATA COLLECTION
# ============================================================

async def subscribe_asset(asset: str):
    if not asset or asset in subscribed_assets:
        return
    subscribed_assets.add(asset)
    await ws_send_queue.put({"assets_ids": [asset], "operation": "subscribe"})

async def load_seed_assets():
    # Subscribe to assets seen recently so order book history exists before the next trade.
    cutoff = now_ts() - 6 * 3600
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT asset FROM trades WHERE trade_ts>=? AND asset!=''",
            (cutoff,),
        ).fetchall()
    for row in rows:
        asset = row["asset"]
        if asset:
            subscribed_assets.add(asset)
    log.info("Seeded %d recent assets for WebSocket", len(subscribed_assets))

async def fetch_all_trades(start_ts: int, end_ts: int):
    """Fetch every public trade page for the requested window."""
    all_rows = []
    offset = 0
    page_number = 1

    while True:
        params = {
            "user": WALLET,
            "limit": TRADE_PAGE_LIMIT,
            "offset": offset,
            "takerOnly": "false",
            "start": start_ts,
            "end": end_ts,
        }

        rows = await get_json(f"{DATA_API}/trades", params=params)

        if not isinstance(rows, list):
            log.warning("Trade page %d failed", page_number)
            break

        count = len(rows)

        log.info(
            "Fetched trades page %d: %d",
            page_number,
            count,
        )

        if count == 0:
            break

        all_rows.extend(rows)

        if count < TRADE_PAGE_LIMIT:
            break

        offset += TRADE_PAGE_LIMIT
        page_number += 1

        # Safety guard against an accidental infinite pagination loop.
        if page_number > 100:
            log.warning("Trade pagination safety limit reached")
            break

        await asyncio.sleep(0.10)

    log.info(
        "Fetched total trades from API: %d",
        len(all_rows),
    )

    return all_rows


async def poll_trades():
    log.info("Trade poller started for %s", WALLET)
    first_run = True

    while True:
        try:
            # Keep a 120-second overlap so delayed/out-of-order API records
            # are not missed. INSERT OR IGNORE removes duplicates.
            last_ts = safe_int(get_state("last_trade_ts", "0"))

            if last_ts <= 0:
                start = now_ts() - BOOTSTRAP_LOOKBACK_HOURS * 3600
            else:
                start = max(1, last_ts - 120)

            end = now_ts() + 5

            rows = await fetch_all_trades(start, end)
            rows = sorted(
                rows,
                key=lambda x: safe_int(x.get("timestamp")),
            )

            new_count = 0
            duplicate_count = 0
            max_ts = last_ts

            for t in rows:
                proxy_wallet = str(
                    t.get("proxyWallet", "")
                ).lower()

                if proxy_wallet not in ("", WALLET):
                    continue

                inserted = insert_trade(t)

                asset = str(t.get("asset", ""))

                if asset:
                    await subscribe_asset(asset)

                tts = safe_int(t.get("timestamp"))
                max_ts = max(max_ts, tts)

                if inserted:
                    new_count += 1
                else:
                    duplicate_count += 1

            if max_ts > 0:
                set_state("last_trade_ts", max_ts)

            if new_count > 0:
                log.info(
                    "New unique trades stored: %d | duplicates ignored: %d",
                    new_count,
                    duplicate_count,
                )
            elif first_run:
                log.info("No new unique trades in initial window")

            first_run = False

        except Exception:
            log.exception("Trade poller failure")

        await asyncio.sleep(POLL_INTERVAL)

async def positions_poller():
    await asyncio.sleep(5)
    while True:
        try:
            current = await get_json(
                f"{DATA_API}/positions",
                params={"user": WALLET, "limit": 500, "offset": 0},
            )
            if isinstance(current, list):
                save_positions("current", current)

            closed = await get_json(
                f"{DATA_API}/closed-positions",
                params={"user": WALLET, "limit": 500, "offset": 0},
            )
            if isinstance(closed, list):
                save_positions("closed", closed)
        except Exception:
            log.exception("Positions poller failure")

        await asyncio.sleep(POSITIONS_INTERVAL)

def parse_ws_message(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    if raw in ("PONG", "PING", ""):
        return []
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    return obj if isinstance(obj, list) else [obj]

async def ws_heartbeat(ws):
    while True:
        try:
            await ws.send("PING")
        except Exception:
            return
        await asyncio.sleep(10)

async def ws_sender(ws):
    while True:
        msg = await ws_send_queue.get()
        try:
            await ws.send(json_dumps(msg))
        except Exception:
            # Put it back for the next connection.
            await ws_send_queue.put(msg)
            return

async def market_ws_loop():
    await load_seed_assets()

    while True:
        try:
            async with websockets.connect(
                MARKET_WS,
                ping_interval=None,
                close_timeout=5,
                max_size=10_000_000,
            ) as ws:
                initial_assets = list(subscribed_assets)
                if initial_assets:
                    await ws.send(json_dumps({
                        "assets_ids": initial_assets,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                else:
                    # A market stream requires assets; wait until poller discovers one.
                    log.info("WebSocket waiting for first asset")
                    while not subscribed_assets:
                        await asyncio.sleep(1)
                    await ws.send(json_dumps({
                        "assets_ids": list(subscribed_assets),
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))

                log.info("Market WebSocket connected; assets=%d", len(subscribed_assets))
                hb = asyncio.create_task(ws_heartbeat(ws))
                sender = asyncio.create_task(ws_sender(ws))

                try:
                    async for raw in ws:
                        for event in parse_ws_message(raw):
                            if not isinstance(event, dict):
                                continue
                            event_type = str(
                                event.get("event_type")
                                or event.get("type")
                                or ""
                            )
                            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                            asset = str(
                                payload.get("asset_id")
                                or payload.get("asset")
                                or payload.get("token_id")
                                or payload.get("tokenId")
                                or ""
                            )
                            if not asset:
                                continue

                            if event_type in (
                                "book", "price_change", "best_bid_ask",
                                "last_trade_price"
                            ):
                                insert_book_event(asset, event, event_type, payload)
                finally:
                    hb.cancel()
                    sender.cancel()
        except Exception as e:
            log.warning("Market WebSocket disconnected: %s", e)
            await asyncio.sleep(3)

async def reconcile_trade_books():
    # For Data API trades that appeared after the actual execution, locate the nearest
    # historical top-of-book snapshot around trade time.
    while True:
        try:
            with db_connect() as conn:
                rows = conn.execute("""
                    SELECT uid, trade_ts, side, asset, price
                    FROM trades
                    WHERE execution_type='UNKNOWN'
                      AND asset!=''
                      AND trade_ts >= ?
                    ORDER BY trade_ts DESC
                    LIMIT 1000
                """, (now_ts() - BOOK_RETENTION_HOURS * 3600,)).fetchall()

                for tr in rows:
                    target = tr["trade_ts"] * 1000
                    book = conn.execute("""
                        SELECT received_ts_ms, best_bid, best_ask
                        FROM book_events
                        WHERE asset=?
                          AND received_ts_ms BETWEEN ? AND ?
                        ORDER BY ABS(received_ts_ms - ?) ASC
                        LIMIT 1
                    """, (tr["asset"], target - 10000, target + 10000, target)).fetchone()

                    if not book:
                        continue

                    bid = book["best_bid"]
                    ask = book["best_ask"]
                    ex_type, spread = estimate_execution(
                        tr["side"], tr["price"], bid, ask
                    )
                    age = abs(book["received_ts_ms"] - target)
                    conn.execute("""
                        UPDATE trades
                        SET best_bid=?, best_ask=?, book_ts=?, book_age_ms=?,
                            execution_type=?, spread=?
                        WHERE uid=?
                    """, (
                        bid, ask, book["received_ts_ms"], age,
                        ex_type, spread, tr["uid"]
                    ))
                conn.commit()
        except Exception:
            log.exception("Book reconciliation failure")
        await asyncio.sleep(30)

async def cleanup_loop():
    while True:
        try:
            cutoff_ms = int((time.time() - BOOK_RETENTION_HOURS * 3600) * 1000)
            with db_connect() as conn:
                conn.execute("DELETE FROM book_events WHERE received_ts_ms < ?", (cutoff_ms,))
                conn.commit()
        except Exception:
            log.exception("Cleanup failure")
        await asyncio.sleep(3600)


# ============================================================
# REPORTING
# ============================================================

def rows_to_csv_bytes(rows, columns=None):
    sio = io.StringIO()
    if rows:
        if columns is None:
            columns = list(rows[0].keys())
        writer = csv.DictWriter(sio, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    elif columns:
        writer = csv.DictWriter(sio, fieldnames=columns)
        writer.writeheader()
    return sio.getvalue().encode("utf-8-sig")

def build_market_summary(trades):
    grouped = defaultdict(list)
    for r in trades:
        grouped[(r["condition_id"], r["title"])].append(r)

    out = []
    for (condition_id, title), rs in grouped.items():
        buys = [r for r in rs if r["side"] == "BUY"]
        sells = [r for r in rs if r["side"] == "SELL"]

        buy_usd = sum(safe_float(r["usd_value"]) for r in buys)
        sell_usd = sum(safe_float(r["usd_value"]) for r in sells)
        buy_shares = sum(safe_float(r["size"]) for r in buys)
        sell_shares = sum(safe_float(r["size"]) for r in sells)
        avg_buy = (
            sum(safe_float(r["price"]) * safe_float(r["size"]) for r in buys) / buy_shares
            if buy_shares else 0
        )
        avg_sell = (
            sum(safe_float(r["price"]) * safe_float(r["size"]) for r in sells) / sell_shares
            if sell_shares else 0
        )

        outcomes = defaultdict(lambda: {"buy_usd": 0.0, "sell_usd": 0.0, "buy_n": 0, "sell_n": 0})
        for r in rs:
            o = r["outcome"] or "UNKNOWN"
            if r["side"] == "BUY":
                outcomes[o]["buy_usd"] += safe_float(r["usd_value"])
                outcomes[o]["buy_n"] += 1
            else:
                outcomes[o]["sell_usd"] += safe_float(r["usd_value"])
                outcomes[o]["sell_n"] += 1

        maker = sum(1 for r in rs if r["execution_type"] == "MAKER_LIKELY")
        taker = sum(1 for r in rs if r["execution_type"] == "TAKER_LIKELY")
        unknown = sum(1 for r in rs if r["execution_type"] == "UNKNOWN")

        # Behavioral label is heuristic, intentionally not a claim of exact intent.
        unique_outcomes_bought = {r["outcome"] for r in buys if r["outcome"]}
        if sells and sell_shares >= buy_shares * 0.9:
            behavior = "EARLY/FULL_EXIT_LIKELY"
        elif sells:
            behavior = "PARTIAL_EXIT_LIKELY"
        elif len(unique_outcomes_bought) >= 2:
            behavior = "BOTH_SIDES/HEDGE_LIKELY"
        elif buys:
            behavior = "ACCUMULATE/HOLD_LIKELY"
        else:
            behavior = "SELL_ONLY/UNKNOWN"

        out.append({
            "condition_id": condition_id,
            "symbol": rs[0]["symbol"],
            "title": title,
            "first_trade_utc": min(r["trade_time_utc"] for r in rs),
            "last_trade_utc": max(r["trade_time_utc"] for r in rs),
            "trade_count": len(rs),
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_usd": round(buy_usd, 6),
            "sell_usd": round(sell_usd, 6),
            "buy_shares": round(buy_shares, 6),
            "sell_shares": round(sell_shares, 6),
            "avg_buy_price": round(avg_buy, 6),
            "avg_sell_price": round(avg_sell, 6),
            "maker_likely": maker,
            "taker_likely": taker,
            "unknown_execution": unknown,
            "behavior_hint": behavior,
            "outcome_breakdown_json": json_dumps(outcomes),
        })
    return sorted(out, key=lambda x: x["first_trade_utc"])

def build_report_txt(start_ts, end_ts, trades, summaries, positions):
    total_usd = sum(safe_float(r["usd_value"]) for r in trades)
    buys = [r for r in trades if r["side"] == "BUY"]
    sells = [r for r in trades if r["side"] == "SELL"]
    maker = sum(1 for r in trades if r["execution_type"] == "MAKER_LIKELY")
    taker = sum(1 for r in trades if r["execution_type"] == "TAKER_LIKELY")
    unknown = sum(1 for r in trades if r["execution_type"] == "UNKNOWN")
    btc = sum(1 for r in trades if r["symbol"] == "BTC")
    eth = sum(1 for r in trades if r["symbol"] == "ETH")
    crypto5m = sum(1 for r in trades if r["is_crypto_5m"])

    lines = [
        "POWERWINNER WALLET OBSERVER v2",
        "=" * 60,
        f"Wallet: {WALLET}",
        f"Period UTC: {utc_iso(start_ts)} -> {utc_iso(end_ts)}",
        "",
        "HOURLY SUMMARY",
        f"Trades: {len(trades)}",
        f"BUY: {len(buys)} | SELL: {len(sells)}",
        f"Trade notional (sum size*price): ${total_usd:.2f}",
        f"BTC trades: {btc} | ETH trades: {eth}",
        f"Likely crypto 5m Up/Down trades: {crypto5m}",
        f"Markets touched: {len(summaries)}",
        "",
        "EXECUTION HEURISTIC",
        f"MAKER_LIKELY: {maker}",
        f"TAKER_LIKELY: {taker}",
        f"UNKNOWN: {unknown}",
        "",
        "IMPORTANT:",
        "MAKER_LIKELY/TAKER_LIKELY are estimates from public order-book data.",
        "They are NOT proof of the user's private active limit orders.",
        "UNKNOWN is expected when the bot had no sufficiently close book snapshot.",
        "",
        "MARKETS",
    ]

    for s in summaries:
        lines += [
            "-" * 60,
            f"{s['symbol']} | {s['title']}",
            f"Trades: {s['trade_count']} | BUY {s['buy_count']} | SELL {s['sell_count']}",
            f"BUY ${s['buy_usd']:.2f} @ avg {s['avg_buy_price']:.4f}",
            f"SELL ${s['sell_usd']:.2f} @ avg {s['avg_sell_price']:.4f}",
            f"Execution: maker~{s['maker_likely']} taker~{s['taker_likely']} unknown={s['unknown_execution']}",
            f"Behavior hint: {s['behavior_hint']}",
            f"Outcomes: {s['outcome_breakdown_json']}",
        ]

    lines += [
        "",
        "FILES IN THIS ARCHIVE",
        "trades.csv             - every observed trade",
        "markets_summary.csv    - aggregation by market",
        "book_events.csv        - public top-of-book events around tracked assets",
        "positions.csv          - current/closed position snapshots captured in period",
        "report.txt             - this summary",
        "metadata.json          - bot/report metadata",
    ]
    return "\n".join(lines)

def create_hourly_zip(start_ts: int, end_ts: int) -> Path:
    with db_connect() as conn:
        trades = conn.execute("""
            SELECT * FROM trades
            WHERE trade_ts >= ? AND trade_ts < ?
            ORDER BY trade_ts, observed_at
        """, (start_ts, end_ts)).fetchall()

        # Include book data for assets actually traded in the hour, with 30 sec margins.
        assets = sorted({r["asset"] for r in trades if r["asset"]})
        books = []
        if assets:
            placeholders = ",".join("?" for _ in assets)
            books = conn.execute(f"""
                SELECT * FROM book_events
                WHERE asset IN ({placeholders})
                  AND received_ts_ms >= ?
                  AND received_ts_ms < ?
                ORDER BY received_ts_ms
            """, (*assets, (start_ts - 30) * 1000, (end_ts + 30) * 1000)).fetchall()

        positions = conn.execute("""
            SELECT * FROM positions_snapshots
            WHERE snapshot_ts >= ? AND snapshot_ts < ?
            ORDER BY snapshot_ts
        """, (start_ts, end_ts)).fetchall()

    summaries = build_market_summary(trades)
    report_txt = build_report_txt(start_ts, end_ts, trades, summaries, positions)

    dt_start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    dt_end = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    name = f"powerwinner_{dt_start:%Y-%m-%d_%H-%M}_{dt_end:%H-%M}_UTC.zip"
    path = REPORT_DIR / name

    metadata = {
        "version": "2.2-catchup",
        "wallet": WALLET,
        "period_start_utc": utc_iso(start_ts),
        "period_end_utc": utc_iso(end_ts),
        "generated_at_utc": utc_iso(),
        "trade_count": len(trades),
        "market_count": len(summaries),
        "maker_taker_note": (
            "MAKER_LIKELY/TAKER_LIKELY are heuristics based on public order-book "
            "snapshots near execution time; they do not expose private active orders."
        ),
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("trades.csv", rows_to_csv_bytes(trades))
        z.writestr(
            "markets_summary.csv",
            rows_to_csv_bytes(summaries, list(summaries[0].keys()) if summaries else [
                "condition_id","symbol","title","first_trade_utc","last_trade_utc",
                "trade_count","buy_count","sell_count","buy_usd","sell_usd",
                "buy_shares","sell_shares","avg_buy_price","avg_sell_price",
                "maker_likely","taker_likely","unknown_execution",
                "behavior_hint","outcome_breakdown_json"
            ])
        )
        z.writestr("book_events.csv", rows_to_csv_bytes(books))
        z.writestr("positions.csv", rows_to_csv_bytes(positions))
        z.writestr("report.txt", report_txt.encode("utf-8"))
        z.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))

    return path

def next_hour_boundary(ts=None):
    if ts is None:
        ts = now_ts()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    nxt = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return int(nxt.timestamp())

async def hourly_reporter():
    """
    Reliable hourly reporter:
    - waits 2 minutes after the UTC hour closes;
    - survives Render restarts;
    - sends every completed but unsent hour;
    - advances state only after Telegram confirms the ZIP upload.
    """

    saved = safe_int(get_state("last_report_end", "0"))

    if saved > 0:
        last_report_end = saved
    else:
        dt = datetime.now(timezone.utc)
        hour_start = dt.replace(minute=0, second=0, microsecond=0)
        last_report_end = int(hour_start.timestamp())
        set_state("last_report_end", last_report_end)

        log.info(
            "Reporter initialized from %s",
            utc_iso(last_report_end)
        )

    while True:
        try:
            now = now_ts()

            # With REPORT_DELAY_SECONDS=120, the 19:00-20:00 UTC report
            # becomes eligible at about 20:02 UTC.
            eligible_end = ((now - REPORT_DELAY_SECONDS) // 3600) * 3600

            while last_report_end < eligible_end:
                start_ts = last_report_end
                end_ts = start_ts + 3600

                log.info(
                    "Preparing hourly report: %s -> %s",
                    utc_iso(start_ts),
                    utc_iso(end_ts)
                )

                path = create_hourly_zip(start_ts, end_ts)

                with db_connect() as conn:
                    count = conn.execute(
                        """
                        SELECT COUNT(*) AS c
                        FROM trades
                        WHERE trade_ts >= ?
                        AND trade_ts < ?
                        """,
                        (start_ts, end_ts),
                    ).fetchone()["c"]

                caption = (
                    "Powerwinner hourly report\n"
                    f"{utc_iso(start_ts)} → {utc_iso(end_ts)}\n"
                    f"Trades: {count}"
                )

                sent = await telegram_send_file(path, caption)

                if not sent:
                    log.warning(
                        "Report was not confirmed by Telegram; will retry later: %s -> %s",
                        utc_iso(start_ts),
                        utc_iso(end_ts)
                    )
                    break

                last_report_end = end_ts
                set_state("last_report_end", last_report_end)

                log.info(
                    "Hourly report confirmed; last_report_end=%s",
                    utc_iso(last_report_end)
                )

        except Exception:
            log.exception("Hourly report failure")

        await asyncio.sleep(REPORT_CHECK_INTERVAL)

async def startup_message():
    # Do not spam Telegram on Render restarts or redeploys.
    log.info(
        "Powerwinner Wallet Observer v2.2 started; hourly catch-up enabled"
    )


# ============================================================
# HEALTH SERVER FOR RENDER
# ============================================================

async def health(request):
    with db_connect() as conn:
        trades = conn.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
        last = conn.execute("SELECT MAX(trade_ts) m FROM trades").fetchone()["m"]
    return web.json_response({
        "ok": True,
        "wallet": WALLET,
        "db": str(DB_PATH),
        "trades": trades,
        "last_trade_utc": utc_iso(last) if last else None,
        "ws_assets": len(subscribed_assets),
        "time_utc": utc_iso(),
    })

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server listening on port %s", PORT)


# ============================================================
# MAIN
# ============================================================

async def main():
    global session

    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not set")
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID is not set")

    init_db()

    headers = {
        "User-Agent": "PowerwinnerWalletObserver/2.0",
        "Accept": "application/json",
    }
    session = aiohttp.ClientSession(headers=headers)

    tasks = [
        asyncio.create_task(run_web()),
        asyncio.create_task(poll_trades()),
        asyncio.create_task(positions_poller()),
        asyncio.create_task(market_ws_loop()),
        asyncio.create_task(reconcile_trade_books()),
        asyncio.create_task(cleanup_loop()),
        asyncio.create_task(hourly_reporter()),
        asyncio.create_task(startup_message()),
    ]

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        if session:
            await session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
