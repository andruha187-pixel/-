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
from datetime import datetime, timezone
from collections import defaultdict, deque
from typing import Optional

import aiohttp
from aiohttp import web
import websockets
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PORT = int(os.getenv("PORT", "8080"))

# We intentionally start with BTC only because our Powerwinner dataset
# is dominated by BTC 5-minute Up/Down markets.
SYMBOL = os.getenv("SYMBOL", "BTC").upper()

# The observed Powerwinner rhythm is about one strategy decision every 3 sec.
DECISION_INTERVAL = float(os.getenv("DECISION_INTERVAL", "3.0"))

# Stop opening new positions after this many seconds from market start.
TRADE_WINDOW_SECONDS = int(os.getenv("TRADE_WINDOW_SECONDS", "180"))

# Paper order size per signal. 10 shares keeps the simulation liquid and
# makes variants comparable. Scale later after finding profitable logic.
ORDER_SIZE = float(os.getenv("ORDER_SIZE", "10"))

# ============================================================
# EXACT M03 PAPER-MONEY ACCOUNTS
# ============================================================
# IMPORTANT:
# The strategy engine below is left intact. These accounts merely mirror
# successfully executed M03_P08_L2 signals from the original simulator.
#
# $1000 mirrors the original 10-share lot. Other accounts scale linearly.
PAPER_CAPITALS = [
    float(x.strip())
    for x in os.getenv(
        "PAPER_CAPITALS",
        "100,250,500,1000,2500",
    ).split(",")
    if x.strip()
]
PAPER_BASE_CAPITAL = float(os.getenv("PAPER_BASE_CAPITAL", "1000"))
PAPER_ACCOUNT_REPORTS = os.getenv("PAPER_ACCOUNT_REPORTS", "1").strip() not in {"0", "false", "False"}


# Crypto taker fee rate from current Polymarket docs.
CRYPTO_FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))

# Market discovery frequency.
DISCOVERY_INTERVAL = float(os.getenv("DISCOVERY_INTERVAL", "10"))

# Full book older than this triggers REST refresh before simulated execution.
MAX_BOOK_AGE_MS = int(os.getenv("MAX_BOOK_AGE_MS", "1000"))

# Reports are sent 5 minutes after the hour closes.
REPORT_DELAY_SECONDS = int(os.getenv("REPORT_DELAY_SECONDS", "300"))
REPORT_CHECK_INTERVAL = int(os.getenv("REPORT_CHECK_INTERVAL", "30"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / ".write_test"
    p.write_text("ok")
    p.unlink()
except Exception:
    DATA_DIR = Path("./data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "m03_exact_paper.db"
REPORT_DIR = DATA_DIR / "m03_exact_paper_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("strategy-sim")

session: Optional[aiohttp.ClientSession] = None

# ============================================================
# STRATEGY GRID
# ============================================================
#
# We do NOT pretend to know Powerwinner's exact formula yet.
# Run several candidate variants simultaneously on the same live books.
#
# entry_move:
#   minimum rise in ask price over lookback needed for first entry.
#
# pyramid_step:
#   after buying a side, it must rise this much above its previous buy price
#   before another fixed-size lot is added.
#
# lookback:
#   number of 3-sec samples used to measure momentum.
#
# switch_move:
#   minimum opposite-side momentum required to start buying the other side.
#
# max_buys_side:
#   maximum number of lots on each side per market.
#
# min_price/max_price:
#   avoid extreme contracts where movement has different behavior.
#

VARIANTS = [
    {"name": "M03_P08_L2", "entry_move": 0.03, "pyramid_step": 0.08, "lookback": 2, "switch_move": 0.04, "max_buys_side": 6},
    {"name": "M04_P08_L2", "entry_move": 0.04, "pyramid_step": 0.08, "lookback": 2, "switch_move": 0.04, "max_buys_side": 6},
    {"name": "M05_P08_L2", "entry_move": 0.05, "pyramid_step": 0.08, "lookback": 2, "switch_move": 0.05, "max_buys_side": 6},
    {"name": "M05_P10_L2", "entry_move": 0.05, "pyramid_step": 0.10, "lookback": 2, "switch_move": 0.05, "max_buys_side": 6},
    {"name": "M06_P10_L2", "entry_move": 0.06, "pyramid_step": 0.10, "lookback": 2, "switch_move": 0.06, "max_buys_side": 6},
    {"name": "M08_P10_L2", "entry_move": 0.08, "pyramid_step": 0.10, "lookback": 2, "switch_move": 0.08, "max_buys_side": 6},
    {"name": "M05_P10_L3", "entry_move": 0.05, "pyramid_step": 0.10, "lookback": 3, "switch_move": 0.05, "max_buys_side": 6},
    {"name": "M08_P12_L3", "entry_move": 0.08, "pyramid_step": 0.12, "lookback": 3, "switch_move": 0.08, "max_buys_side": 5},

    # Prospective v2 filter derived from the first M03 research sample.
    # IMPORTANT: keep these rules fixed while collecting new out-of-sample data.
    {
        "name": "M03_V2_LOCK",
        "entry_move": 0.03,
        "pyramid_step": 0.08,
        "lookback": 2,
        "switch_move": 999.0,       # effectively disabled
        "max_buys_side": 6,
        "entry_price_min": 0.55,
        "entry_price_max": 0.75,
        "momentum_cap": 0.30,
        "allow_switch": False,
    },
    # 10-й вариант: исходный M03 без переворотов, новые покупки только до 90-й секунды.
    {
        "name": "M03_V3_NOSW90",
        "entry_move": 0.03,
        "pyramid_step": 0.08,
        "lookback": 2,
        "switch_move": 999.0,
        "max_buys_side": 5,
        "allow_switch": False,
        "entry_cutoff_sec": 90,
    },

    # 11-й вариант: исходный M03, но SWITCH разрешён только пока новая сторона дешёвая.
    {
        "name": "M03_V4_SW45",
        "entry_move": 0.03,
        "pyramid_step": 0.08,
        "lookback": 2,
        "switch_move": 0.03,
        "max_buys_side": 5,
        "allow_switch": True,
        "switch_price_max": 0.45,
    },

    # 12-й вариант: динамический M03 V5.
    # Первые 60 сек: дорогие SWITCH > 0.45 блокируются.
    # После 60 сек: <=0.45 разрешены; 0.46-0.50 только при momentum < 0.10;
    # 0.51-0.70 блокируются; >0.70 оставляем как у исходного M03 для проверки.
    {
        "name": "M03_V5_DYNAMIC",
        "entry_move": 0.03,
        "pyramid_step": 0.08,
        "lookback": 2,
        "switch_move": 0.03,
        "max_buys_side": 5,
        "allow_switch": True,
        "dynamic_switch_v5": True,
    },

]

MIN_PRICE = float(os.getenv("MIN_PRICE", "0.08"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))

# ============================================================
# SHARED MARKET STATE
# ============================================================

books = {}
markets = {}
subscribed_assets = set()
ws_send_queue: asyncio.Queue = asyncio.Queue()

# price_history[condition][asset] -> deque [(timestamp_ms, ask)]
price_history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=100)))

# strategy_state[(condition, variant)] -> state dict
strategy_state = {}

# ============================================================
# HELPERS
# ============================================================

def now_ts():
    return int(time.time())

def now_ms():
    return int(time.time() * 1000)

def utc_iso(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

def sf(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def si(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

def jd(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))

def parse_jsonish(v):
    if isinstance(v, list):
        return v
    if v is None:
        return []
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except Exception:
        return []

def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None

def fee_usdc(shares, price):
    fee = shares * CRYPTO_FEE_RATE * price * (1.0 - price)
    # Polymarket rounds fees to 5 decimals.
    return round(fee, 5) if fee >= 0.000005 else 0.0

def target_market_text(m):
    return f"{m.get('question','')} {m.get('slug','')}".lower()

def is_target_market(m):
    s = target_market_text(m)

    if SYMBOL == "BTC":
        symbol_ok = ("bitcoin" in s or "btc" in s)
    elif SYMBOL == "ETH":
        symbol_ok = ("ethereum" in s or "eth" in s)
    else:
        symbol_ok = True

    updown = ("up or down" in s or "up-down" in s)
    return (
        symbol_ok
        and updown
        and bool(m.get("enableOrderBook", True))
        and not bool(m.get("closed", False))
    )

# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS discovered_markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            slug TEXT,
            start_ts INTEGER,
            end_ts INTEGER,
            up_asset TEXT,
            down_asset TEXT,
            discovered_ms INTEGER,
            resolved INTEGER DEFAULT 0,
            winning_asset TEXT,
            winning_outcome TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_ms INTEGER,
            condition_id TEXT,
            variant TEXT,
            asset TEXT,
            outcome TEXT,
            ask REAL,
            reference_ask REAL,
            momentum REAL,
            signal_type TEXT,
            elapsed_sec REAL
        );

        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_ms INTEGER,
            condition_id TEXT,
            variant TEXT,
            asset TEXT,
            outcome TEXT,
            signal_type TEXT,
            requested_shares REAL,
            filled_shares REAL,
            avg_price REAL,
            gross_cost REAL,
            fee REAL,
            total_cost REAL,
            book_age_ms INTEGER,
            fills_json TEXT
        );

        CREATE TABLE IF NOT EXISTS market_results (
            condition_id TEXT,
            variant TEXT,
            winning_asset TEXT,
            winning_outcome TEXT,
            total_cost REAL,
            payout REAL,
            pnl REAL,
            trades INTEGER,
            up_shares REAL,
            down_shares REAL,
            settled_ms INTEGER,
            PRIMARY KEY(condition_id, variant)
        );

        CREATE TABLE IF NOT EXISTS paper_accounts (
            account_id TEXT PRIMARY KEY,
            initial_capital REAL,
            cash REAL,
            realized_pnl REAL DEFAULT 0,
            created_ms INTEGER
        );

        CREATE TABLE IF NOT EXISTS paper_account_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            trade_ms INTEGER,
            condition_id TEXT,
            asset TEXT,
            outcome TEXT,
            signal_type TEXT,
            requested_shares REAL,
            filled_shares REAL,
            avg_price REAL,
            gross_cost REAL,
            fee REAL,
            total_cost REAL,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS paper_account_results (
            account_id TEXT,
            condition_id TEXT,
            winning_asset TEXT,
            winning_outcome TEXT,
            total_cost REAL,
            payout REAL,
            pnl REAL,
            settled_ms INTEGER,
            PRIMARY KEY(account_id, condition_id)
        );

        CREATE INDEX IF NOT EXISTS idx_account_trades_market
            ON paper_account_trades(condition_id);
        CREATE INDEX IF NOT EXISTS idx_account_trades_account
            ON paper_account_trades(account_id);
        CREATE INDEX IF NOT EXISTS idx_account_results_account
            ON paper_account_results(account_id);

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_ms ON paper_trades(trade_ms);
        CREATE INDEX IF NOT EXISTS idx_trades_condition ON paper_trades(condition_id);
        CREATE INDEX IF NOT EXISTS idx_signals_ms ON signals(signal_ms);
        CREATE INDEX IF NOT EXISTS idx_results_ms ON market_results(settled_ms);
        """)

        for capital in PAPER_CAPITALS:
            account_id = f"CAP_{capital:g}"
            conn.execute("""
                INSERT OR IGNORE INTO paper_accounts(
                    account_id, initial_capital, cash, realized_pnl, created_ms
                ) VALUES (?,?,?,?,?)
            """, (
                account_id,
                capital,
                capital,
                0.0,
                now_ms(),
            ))
        conn.commit()


def state_get(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def state_set(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()

# ============================================================
# HTTP
# ============================================================

async def get_json(url, params=None):
    for attempt in range(3):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                text = await r.text()
                if r.status == 200:
                    return json.loads(text)
                log.warning("HTTP %s %s %s -> %s", r.status, url, params, text[:200])
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)

        await asyncio.sleep(0.3 * (attempt + 1))

    return None

# ============================================================
# BOOK
# ============================================================

def level_map(rows):
    out = {}
    for x in rows or []:
        if not isinstance(x, dict):
            continue
        p = sf(x.get("price"), math.nan)
        q = sf(x.get("size"), 0)
        if not math.isnan(p) and q > 0:
            out[p] = q
    return out

def apply_book(asset, payload, source="ws"):
    books[asset] = {
        "bids": level_map(payload.get("bids")),
        "asks": level_map(payload.get("asks")),
        "received_ms": now_ms(),
        "source": source,
    }

def apply_price_change(payload):
    changes = payload.get("price_changes") or payload.get("priceChanges") or []
    recv = now_ms()

    for ch in changes:
        if not isinstance(ch, dict):
            continue
        asset = str(
            ch.get("asset_id")
            or ch.get("token_id")
            or ch.get("tokenId")
            or ""
        )
        if not asset:
            continue

        b = books.setdefault(asset, {
            "bids": {},
            "asks": {},
            "received_ms": recv,
            "source": "ws-delta",
        })

        p = sf(ch.get("price"), math.nan)
        q = sf(ch.get("size"), 0)
        side = str(ch.get("side", "")).upper()

        if math.isnan(p):
            continue

        target = b["bids"] if side == "BUY" else b["asks"]

        if q <= 0:
            target.pop(p, None)
        else:
            target[p] = q

        b["received_ms"] = recv
        b["source"] = "ws"

def best_ask(asset):
    b = books.get(asset)
    if not b or not b["asks"]:
        return None
    return min(b["asks"])

async def refresh_book(asset):
    data = await get_json(f"{CLOB_API}/book", params={"token_id": asset})
    if isinstance(data, dict):
        apply_book(asset, data, "rest")
        return True
    return False

async def ensure_book(asset):
    b = books.get(asset)
    if b and b["asks"]:
        age = now_ms() - b["received_ms"]
        if age <= MAX_BOOK_AGE_MS:
            return age

    await refresh_book(asset)
    b = books.get(asset)
    if not b:
        return None

    return now_ms() - b["received_ms"]

def simulate_buy(asset, wanted):
    b = books.get(asset)
    if not b or not b["asks"]:
        return [], 0.0

    remaining = wanted
    fills = []

    for p in sorted(b["asks"]):
        q = b["asks"][p]
        take = min(q, remaining)
        if take > 0:
            fills.append((p, take))
            remaining -= take
        if remaining <= 1e-12:
            break

    return fills, wanted - remaining


def slot_start_from_slug(slug):
    try:
        return int(str(slug).rstrip("/").split("-")[-1])
    except Exception:
        return None


async def fetch_event_by_slug(slug):
    for url, params in (
        (f"{GAMMA_API}/events/slug/{slug}", None),
        (f"{GAMMA_API}/events", {"slug": slug}),
    ):
        data = await get_json(url, params=params)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    return None


def parse_market_from_event(raw, event):
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("conditionId") or raw.get("condition_id") or "")
    if not cid:
        return None
    title = str(raw.get("question") or raw.get("title") or event.get("title") or event.get("question") or "Unknown")
    slug = str(raw.get("slug") or event.get("slug") or "")
    combined = f"{title} {slug}".lower()
    if SYMBOL == "BTC" and "bitcoin" not in combined and "btc" not in combined:
        return None
    if SYMBOL == "ETH" and "ethereum" not in combined and "eth" not in combined:
        return None
    outcomes = [str(x).strip().upper() for x in parse_jsonish(raw.get("outcomes"))]
    tokens = [str(x) for x in parse_jsonish(raw.get("clobTokenIds"))]
    if len(tokens) < 2:
        return None
    up_asset = None
    down_asset = None
    for i, outcome in enumerate(outcomes):
        if i >= len(tokens):
            break
        if outcome in {"UP", "YES"}:
            up_asset = tokens[i]
        elif outcome in {"DOWN", "NO"}:
            down_asset = tokens[i]
    up_asset = up_asset or tokens[0]
    down_asset = down_asset or tokens[1]
    start_ts = slot_start_from_slug(slug)
    if not start_ts:
        start_dt = parse_iso(raw.get("startDate")) or parse_iso(event.get("startDate"))
        start_ts = int(start_dt.timestamp()) if start_dt else None
    if not start_ts:
        return None
    if slot_start_from_slug(slug):
        end_ts = start_ts + 300
    else:
        end_dt = parse_iso(raw.get("endDate")) or parse_iso(event.get("endDate"))
        end_ts = int(end_dt.timestamp()) if end_dt else start_ts + 300
    return {
        "condition_id": cid,
        "question": title,
        "slug": slug,
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "up_asset": str(up_asset),
        "down_asset": str(down_asset),
        "raw": raw,
    }


async def discover_slot_market(prefix, slot_start):
    slug = f"{prefix}-{slot_start}"
    event = await fetch_event_by_slug(slug)
    if not event:
        return None
    raw_markets = event.get("markets")
    if not isinstance(raw_markets, list):
        return None
    for raw in raw_markets:
        market = parse_market_from_event(raw, event)
        if market:
            return market
    return None


# ============================================================
# MARKET DISCOVERY
# ============================================================

def persist_market(m):
    cid = m["condition_id"]
    with db() as conn:
        conn.execute("""
            INSERT INTO discovered_markets(
                condition_id, question, slug, start_ts, end_ts,
                up_asset, down_asset, discovered_ms
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                question=excluded.question,
                slug=excluded.slug,
                start_ts=excluded.start_ts,
                end_ts=excluded.end_ts,
                up_asset=excluded.up_asset,
                down_asset=excluded.down_asset
        """, (
            cid,
            m["question"],
            m["slug"],
            m["start_ts"],
            m["end_ts"],
            m["up_asset"],
            m["down_asset"],
            now_ms(),
        ))
        conn.commit()

async def subscribe_asset(asset):
    if not asset or asset in subscribed_assets:
        return

    subscribed_assets.add(asset)
    await ws_send_queue.put({
        "operation": "subscribe",
        "assets_ids": [asset],
    })

async def discovery_loop():
    prefix = "btc-updown-5m" if SYMBOL == "BTC" else "eth-updown-5m"
    last_current_slot = None
    while True:
        try:
            now = now_ts()
            current = (now // 300) * 300
            candidates = []
            for slot_start in (current, current + 300, current - 300):
                market = await discover_slot_market(prefix, slot_start)
                if market:
                    candidates.append(market)
            if candidates:
                active = [m for m in candidates if m["start_ts"] - 5 <= now <= m["end_ts"] + 5]
                chosen = min(active or candidates, key=lambda m: abs(now - m["start_ts"]))
                for market in candidates:
                    cid = market["condition_id"]
                    if cid in markets:
                        continue
                    markets[cid] = market
                    persist_market(market)
                    await subscribe_asset(market["up_asset"])
                    await subscribe_asset(market["down_asset"])
                    log.info(
                        "MARKET %s | slug=%s | start=%s | end=%s",
                        market["question"],
                        market["slug"],
                        utc_iso(market["start_ts"]),
                        utc_iso(market["end_ts"]),
                    )
                if current != last_current_slot:
                    log.info("CURRENT SLOT %s | selected=%s", utc_iso(current), chosen["slug"])
                    last_current_slot = current
            else:
                log.info("Discovery: slug market not found for slot %s; retrying", utc_iso(current))
        except Exception:
            log.exception("Discovery loop failed")
        await asyncio.sleep(DISCOVERY_INTERVAL)

# ============================================================
# WEBSOCKET
# ============================================================

def parse_ws(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")

    if raw in ("", "PING", "PONG"):
        return []

    try:
        x = json.loads(raw)
        return x if isinstance(x, list) else [x]
    except Exception:
        return []

async def ws_sender(ws):
    while True:
        msg = await ws_send_queue.get()
        try:
            await ws.send(jd(msg))
        except Exception:
            await ws_send_queue.put(msg)
            return

async def ws_ping(ws):
    while True:
        try:
            await ws.send("PING")
        except Exception:
            return
        await asyncio.sleep(10)

async def ws_loop():
    while True:
        try:
            if not subscribed_assets:
                await asyncio.sleep(1)
                continue

            async with websockets.connect(
                MARKET_WS,
                ping_interval=None,
                close_timeout=5,
                max_size=20_000_000,
            ) as ws:

                await ws.send(jd({
                    "assets_ids": list(subscribed_assets),
                    "type": "market",
                    "custom_feature_enabled": True,
                }))

                log.info("WS connected | assets=%d", len(subscribed_assets))

                sender = asyncio.create_task(ws_sender(ws))
                ping = asyncio.create_task(ws_ping(ws))

                try:
                    async for raw in ws:
                        for ev in parse_ws(raw):
                            if not isinstance(ev, dict):
                                continue

                            et = str(ev.get("event_type") or ev.get("type") or "")
                            payload = (
                                ev.get("payload")
                                if isinstance(ev.get("payload"), dict)
                                else ev
                            )

                            if et == "book":
                                asset = str(
                                    payload.get("asset_id")
                                    or payload.get("token_id")
                                    or ""
                                )
                                if asset:
                                    apply_book(asset, payload)

                            elif et == "price_change":
                                apply_price_change(payload)

                            elif et == "market_resolved":
                                await settle_from_resolution(payload)

                finally:
                    sender.cancel()
                    ping.cancel()

        except Exception as e:
            log.warning("WS reconnect: %s", e)
            await asyncio.sleep(1)


# ============================================================
# EXACT M03 ACCOUNT MIRROR
# ============================================================

def paper_account_id(capital):
    return f"CAP_{capital:g}"

def paper_account_shares(capital):
    # $1000 account == original ORDER_SIZE (10 shares by default).
    return ORDER_SIZE * (float(capital) / PAPER_BASE_CAPITAL)

def simulate_buy_snapshot(asks_snapshot, wanted):
    remaining = float(wanted)
    fills = []

    for p in sorted(asks_snapshot):
        q = sf(asks_snapshot[p], 0.0)
        if q <= 0:
            continue
        take = min(q, remaining)
        if take > 0:
            fills.append((float(p), take))
            remaining -= take
        if remaining <= 1e-12:
            break

    return fills, max(0.0, wanted - remaining)

async def mirror_m03_to_accounts(
    condition,
    asset,
    outcome,
    signal_type,
    asks_snapshot,
    signal_ms,
):
    """
    Mirrors one SUCCESSFUL original M03 execution to independent accounts.

    It does NOT write strategy_state, price_history, books, or M03 last_buy.
    Therefore virtual account fills cannot change future M03 signals.
    """
    for capital in PAPER_CAPITALS:
        account_id = paper_account_id(capital)
        requested = paper_account_shares(capital)

        with db() as conn:
            acc = conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()

        if not acc:
            continue

        available_cash = sf(acc["cash"])
        if available_cash <= 0:
            continue

        fills, filled = simulate_buy_snapshot(asks_snapshot, requested)

        gross = sum(p * q for p, q in fills)
        fee = sum(fee_usdc(q, p) for p, q in fills)
        total = gross + fee

        # A real cash account cannot spend more than available cash.
        # Scale the SAME snapshot fill proportionally if needed.
        if filled > 0 and total > available_cash and total > 0:
            ratio = max(0.0, available_cash / total)
            fills = [(p, q * ratio) for p, q in fills]
            filled = sum(q for _, q in fills)
            gross = sum(p * q for p, q in fills)
            fee = sum(fee_usdc(q, p) for p, q in fills)
            total = gross + fee

        avg = (gross / filled) if filled > 0 else None

        if filled <= 1e-12:
            status = "NO_FILL"
        elif filled + 1e-9 < requested:
            status = "PARTIAL"
        else:
            status = "FULL"

        with db() as conn:
            conn.execute("""
                INSERT INTO paper_account_trades(
                    account_id, trade_ms, condition_id, asset, outcome,
                    signal_type, requested_shares, filled_shares,
                    avg_price, gross_cost, fee, total_cost, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                account_id,
                signal_ms,
                condition,
                asset,
                outcome,
                signal_type,
                requested,
                filled,
                avg,
                gross,
                fee,
                total,
                status,
            ))

            if filled > 0:
                conn.execute(
                    "UPDATE paper_accounts SET cash=cash-? WHERE account_id=?",
                    (total, account_id),
                )

            conn.commit()

        if filled > 0:
            log.info(
                "%s | M03 %s %s | %.4fsh @ %.4f | cost=%.2f",
                account_id,
                signal_type,
                outcome,
                filled,
                avg,
                total,
            )

def settle_m03_accounts_in_conn(
    conn,
    cid,
    winning_asset,
    winning_outcome,
):
    """Called from the SAME market settlement that settles the simulator."""
    for capital in PAPER_CAPITALS:
        account_id = paper_account_id(capital)

        exists = conn.execute("""
            SELECT 1 FROM paper_account_results
            WHERE account_id=? AND condition_id=?
        """, (account_id, cid)).fetchone()

        if exists:
            continue

        rows = conn.execute("""
            SELECT * FROM paper_account_trades
            WHERE account_id=? AND condition_id=?
        """, (account_id, cid)).fetchall()

        if not rows:
            continue

        total_cost = sum(sf(r["total_cost"]) for r in rows)
        payout = sum(
            sf(r["filled_shares"])
            for r in rows
            if str(r["asset"]) == str(winning_asset)
        )
        pnl = payout - total_cost

        conn.execute("""
            INSERT INTO paper_account_results(
                account_id, condition_id, winning_asset, winning_outcome,
                total_cost, payout, pnl, settled_ms
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            account_id,
            cid,
            winning_asset,
            winning_outcome,
            total_cost,
            payout,
            pnl,
            now_ms(),
        ))

        conn.execute("""
            UPDATE paper_accounts
            SET cash=cash+?,
                realized_pnl=realized_pnl+?
            WHERE account_id=?
        """, (
            payout,
            pnl,
            account_id,
        ))

# ============================================================
# STRATEGY ENGINE
# ============================================================

def get_variant_state(condition, variant):
    key = (condition, variant["name"])

    if key not in strategy_state:
        strategy_state[key] = {
            "buys": defaultdict(int),
            "last_buy": {},
            "started_sides": set(),
            "last_signal_ms": 0,
        }

    return strategy_state[key]

def momentum_for(condition, asset, lookback):
    h = price_history[condition][asset]

    if len(h) <= lookback:
        return None, None

    current = h[-1][1]
    ref = h[-1 - lookback][1]

    return current - ref, ref

def store_signal(condition, variant, asset, outcome, ask, ref, mom, signal_type, elapsed):
    with db() as conn:
        conn.execute("""
            INSERT INTO signals(
                signal_ms, condition_id, variant, asset, outcome,
                ask, reference_ask, momentum, signal_type, elapsed_sec
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            now_ms(), condition, variant["name"], asset, outcome,
            ask, ref, mom, signal_type, elapsed,
        ))
        conn.commit()

async def execute_paper(condition, variant, asset, outcome, signal_type):
    age = await ensure_book(asset)

    # Snapshot the exact order book seen by the original strategy execution.
    # Account simulations use this copy and never mutate the live book.
    asks_snapshot = dict((books.get(asset) or {}).get("asks") or {})
    mirror_signal_ms = now_ms()

    fills, filled = simulate_buy(asset, ORDER_SIZE)

    if filled <= 0:
        return False

    gross = sum(p * q for p, q in fills)
    fee = sum(fee_usdc(q, p) for p, q in fills)
    avg = gross / filled
    total = gross + fee

    with db() as conn:
        conn.execute("""
            INSERT INTO paper_trades(
                trade_ms, condition_id, variant, asset, outcome,
                signal_type, requested_shares, filled_shares,
                avg_price, gross_cost, fee, total_cost,
                book_age_ms, fills_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now_ms(), condition, variant["name"], asset, outcome,
            signal_type, ORDER_SIZE, filled,
            avg, gross, fee, total,
            age,
            jd([{"price": p, "shares": q} for p, q in fills]),
        ))
        conn.commit()

    st = get_variant_state(condition, variant)
    st["buys"][asset] += 1
    st["last_buy"][asset] = avg
    st["started_sides"].add(asset)

    log.info(
        "%s %s %s %s %.1fsh @ %.4f fee=%.4f",
        variant["name"],
        signal_type,
        outcome,
        condition[-6:],
        filled,
        avg,
        fee,
    )

    # Mirror ONLY original M03. Run asynchronously from the captured snapshot
    # so the exact strategy engine/timing is not blocked by account accounting.
    if variant["name"] == "M03_P08_L2":
        asyncio.create_task(
            mirror_m03_to_accounts(
                condition,
                asset,
                outcome,
                signal_type,
                asks_snapshot,
                mirror_signal_ms,
            )
        )

    return True

async def evaluate_variant(market, variant, elapsed):
    cid = market["condition_id"]

    sides = [
        (market["up_asset"], "Up"),
        (market["down_asset"], "Down"),
    ]

    st = get_variant_state(cid, variant)

    candidates = []
    # Для V3 после заданной секунды рынка новые покупки не создаём.
    entry_cutoff_sec = variant.get("entry_cutoff_sec")
    if entry_cutoff_sec is not None and elapsed > float(entry_cutoff_sec):
        return


    # Optional v2 controls. Existing 8 variants keep their old behaviour.
    allow_switch = bool(variant.get("allow_switch", True))
    entry_price_min = variant.get("entry_price_min")
    entry_price_max = variant.get("entry_price_max")
    momentum_cap = variant.get("momentum_cap")

    # For locked-direction variants, remember which asset became PRIMARY.
    primary_asset = st.get("primary_asset")

    for asset, outcome in sides:
        ask = best_ask(asset)

        if ask is None or ask < MIN_PRICE or ask > MAX_PRICE:
            continue

        mom, ref = momentum_for(cid, asset, variant["lookback"])
        if mom is None:
            continue

        buys = st["buys"][asset]
        signal = None

        if buys == 0:
            if not st["started_sides"]:
                # FIRST ENTRY.
                # M03_V2_LOCK applies a prospective price band and momentum cap.
                if entry_price_min is not None and ask < float(entry_price_min):
                    continue
                if entry_price_max is not None and ask > float(entry_price_max):
                    continue
                if momentum_cap is not None and mom > float(momentum_cap):
                    continue

                if mom >= variant["entry_move"]:
                    signal = "ENTRY"

            else:
                # Opposite-side entry = SWITCH for the old variants.
                # Locked-direction v2 never buys the opposite outcome.
                if not allow_switch:
                    continue

                switch_price_max = variant.get("switch_price_max")
                if switch_price_max is not None and ask > float(switch_price_max):
                    continue

                # M03 V5: dynamic SWITCH filter derived from the 120-market sample.
                # Keep this rule fixed while collecting fresh out-of-sample data.
                if variant.get("dynamic_switch_v5"):
                    if elapsed <= 60.0 and ask > 0.45:
                        continue
                    if elapsed > 60.0:
                        if 0.45 < ask <= 0.50 and mom >= 0.10:
                            continue
                        if 0.50 < ask <= 0.70:
                            continue

                if mom >= variant["switch_move"]:
                    signal = "SWITCH"

        else:
            # Locked variant can pyramid only the original PRIMARY side.
            if not allow_switch and primary_asset is not None and asset != primary_asset:
                continue

            # If current momentum becomes extreme, M03 v2 stops adding risk.
            if momentum_cap is not None and mom > float(momentum_cap):
                continue

            last_buy = st["last_buy"].get(asset)

            if (
                last_buy is not None
                and ask >= last_buy + variant["pyramid_step"]
                and mom > 0
                and buys < variant["max_buys_side"]
            ):
                signal = "PYRAMID"

        if signal:
            candidates.append((mom, asset, outcome, ask, ref, signal))

    # One decision / one order per variant per 3-second tick.
    if not candidates:
        return

    candidates.sort(reverse=True, key=lambda x: x[0])
    mom, asset, outcome, ask, ref, signal = candidates[0]

    store_signal(
        cid, variant, asset, outcome, ask, ref, mom, signal, elapsed
    )

    filled = await execute_paper(cid, variant, asset, outcome, signal)

    # Lock the very first successfully executed direction for v2.
    if filled and not allow_switch and signal == "ENTRY":
        st["primary_asset"] = asset

async def strategy_loop():
    # Align decisions roughly to 3-second cadence rather than drift.
    while True:
        started = time.monotonic()
        now = time.time()

        try:
            for cid, market in list(markets.items()):
                elapsed = now - market["start_ts"]

                # Record prices from shortly before open through resolution.
                if -30 <= elapsed <= 310:
                    for asset in (market["up_asset"], market["down_asset"]):
                        ask = best_ask(asset)
                        if ask is not None:
                            price_history[cid][asset].append((now_ms(), ask))

                if elapsed < 0 or elapsed > TRADE_WINDOW_SECONDS:
                    continue

                # Need both books to compare / switch reliably.
                if best_ask(market["up_asset"]) is None:
                    continue
                if best_ask(market["down_asset"]) is None:
                    continue

                for variant in VARIANTS:
                    await evaluate_variant(market, variant, elapsed)

        except Exception:
            log.exception("Strategy loop failed")

        spent = time.monotonic() - started
        await asyncio.sleep(max(0.05, DECISION_INTERVAL - spent))

# ============================================================
# RESOLUTION
# ============================================================

async def settle_from_resolution(ev):
    cid = str(ev.get("market") or ev.get("condition_id") or "")
    winning_asset = str(ev.get("winning_asset_id") or ev.get("winning_asset") or "")
    winning_outcome = str(ev.get("winning_outcome") or "")

    if not cid or not winning_asset:
        return

    await settle_market(cid, winning_asset, winning_outcome)

async def settle_market(cid, winning_asset, winning_outcome):
    market = markets.get(cid)

    if not market:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM discovered_markets WHERE condition_id=?",
                (cid,),
            ).fetchone()
            if not row:
                return
            market = dict(row)

    with db() as conn:
        already = conn.execute(
            "SELECT COUNT(*) c FROM market_results WHERE condition_id=?",
            (cid,),
        ).fetchone()["c"]

        if already >= len(VARIANTS):
            return

        for variant in VARIANTS:
            exists = conn.execute(
                "SELECT 1 FROM market_results WHERE condition_id=? AND variant=?",
                (cid, variant["name"]),
            ).fetchone()

            if exists:
                continue

            rows = conn.execute("""
                SELECT * FROM paper_trades
                WHERE condition_id=? AND variant=?
            """, (cid, variant["name"])).fetchall()

            total_cost = sum(sf(r["total_cost"]) for r in rows)
            payout = sum(
                sf(r["filled_shares"])
                for r in rows
                if str(r["asset"]) == winning_asset
            )
            pnl = payout - total_cost

            up_asset = market["up_asset"]
            down_asset = market["down_asset"]

            up_shares = sum(
                sf(r["filled_shares"]) for r in rows if str(r["asset"]) == up_asset
            )
            down_shares = sum(
                sf(r["filled_shares"]) for r in rows if str(r["asset"]) == down_asset
            )

            conn.execute("""
                INSERT INTO market_results(
                    condition_id, variant, winning_asset, winning_outcome,
                    total_cost, payout, pnl, trades, up_shares,
                    down_shares, settled_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                cid, variant["name"], winning_asset, winning_outcome,
                total_cost, payout, pnl, len(rows),
                up_shares, down_shares, now_ms(),
            ))

        # Settle the independent M03 virtual cash accounts from the same
        # winner used by the original simulator.
        settle_m03_accounts_in_conn(
            conn,
            cid,
            winning_asset,
            winning_outcome,
        )

        conn.execute("""
            UPDATE discovered_markets
            SET resolved=1, winning_asset=?, winning_outcome=?
            WHERE condition_id=?
        """, (winning_asset, winning_outcome, cid))

        conn.commit()

    log.info(
        "RESOLVED %s | winner=%s",
        market.get("question", cid),
        winning_outcome or winning_asset[-8:],
    )

def resolve_winner_from_market(market_row):
    """
    Return (winning_asset, winning_outcome) when Gamma clearly exposes
    the resolved outcome, otherwise (None, None).

    Handles the formats we have seen from Gamma:
    - outcomePrices -> ["1", "0"] / ["0", "1"]
    - outcomePrices -> [1, 0]
    - outcomes + clobTokenIds
    - optional winner flags inside tokens/outcomes structures
    """
    if not isinstance(market_row, dict):
        return None, None

    outcomes = [
        str(x)
        for x in parse_jsonish(market_row.get("outcomes"))
    ]
    tokens = [
        str(x)
        for x in parse_jsonish(market_row.get("clobTokenIds"))
    ]
    prices_raw = parse_jsonish(market_row.get("outcomePrices"))

    if len(outcomes) >= 2 and len(tokens) >= 2 and len(prices_raw) >= 2:
        prices = [sf(x, -1) for x in prices_raw]

        # Require a near-certain resolved pair, not merely a live 0.99 quote.
        best_idx = max(range(len(prices)), key=lambda i: prices[i])
        best = prices[best_idx]
        others = [prices[i] for i in range(len(prices)) if i != best_idx]
        second = max(others) if others else -1

        closed = bool(market_row.get("closed", False))
        resolved_flag = bool(
            market_row.get("resolved", False)
            or market_row.get("umaResolutionStatus") == "resolved"
        )

        if (
            best >= 0.999
            and second <= 0.001
            and (closed or resolved_flag or best >= 0.9999)
        ):
            return tokens[best_idx], outcomes[best_idx]

    # Some Gamma payloads may expose token objects with winner=true.
    token_objs = market_row.get("tokens")
    if isinstance(token_objs, list):
        for tok in token_objs:
            if not isinstance(tok, dict):
                continue
            if bool(tok.get("winner", False)):
                asset = str(
                    tok.get("token_id")
                    or tok.get("tokenId")
                    or tok.get("id")
                    or ""
                )
                outcome = str(
                    tok.get("outcome")
                    or tok.get("name")
                    or ""
                )
                if asset:
                    return asset, outcome

    return None, None


async def fetch_resolved_market_by_slug(slug, condition_id):
    """
    Query the exact event slug (same proven discovery route) and return the
    embedded market matching condition_id when available.
    """
    event = await fetch_event_by_slug(slug)

    if not isinstance(event, dict):
        return None

    embedded = event.get("markets")

    if not isinstance(embedded, list):
        return None

    for m in embedded:
        if not isinstance(m, dict):
            continue

        cid = str(m.get("conditionId") or m.get("condition_id") or "")

        if cid == str(condition_id):
            return m

    # A 5m event normally has one market, so use it as a safe fallback.
    if len(embedded) == 1 and isinstance(embedded[0], dict):
        return embedded[0]

    return None


async def resolution_fallback_loop():
    """
    Reliable settlement fallback.

    WebSocket market_resolved remains the fastest route. If that event is
    missed, poll the exact Gamma event slug for every ended unresolved market.

    This uses the SAME slug method that reliably discovers BTC 5m markets,
    instead of scanning /markets by condition_ids.
    """
    while True:
        try:
            # Give Gamma a short grace period after the 5-minute close.
            cutoff = now_ts() - 10

            with db() as conn:
                rows = conn.execute("""
                    SELECT condition_id, slug, question, end_ts
                    FROM discovered_markets
                    WHERE resolved=0
                      AND end_ts < ?
                    ORDER BY end_ts
                    LIMIT 50
                """, (cutoff,)).fetchall()

            for row in rows:
                cid = str(row["condition_id"])
                slug = str(row["slug"] or "")

                if not slug:
                    continue

                m = await fetch_resolved_market_by_slug(slug, cid)

                if not m:
                    continue

                winning_asset, winning_outcome = resolve_winner_from_market(m)

                if not winning_asset:
                    continue

                log.info(
                    "RESOLUTION FALLBACK %s | winner=%s",
                    slug,
                    winning_outcome or winning_asset[-8:],
                )

                await settle_market(
                    cid,
                    winning_asset,
                    winning_outcome,
                )

        except Exception:
            log.exception("Resolution fallback failed")

        # Check frequently enough that the hourly report at +5 min contains
        # all markets from the completed hour.
        await asyncio.sleep(10)

# ============================================================
# HOURLY REPORT
# ============================================================

def csv_bytes(rows, columns=None):
    s = io.StringIO()

    if rows:
        if columns is None:
            columns = list(rows[0].keys())

        w = csv.DictWriter(s, fieldnames=columns, extrasaction="ignore")
        w.writeheader()

        for r in rows:
            w.writerow(dict(r))

    elif columns:
        w = csv.DictWriter(s, fieldnames=columns)
        w.writeheader()

    return s.getvalue().encode("utf-8-sig")

def variant_summary(start_ms, end_ms):
    out = []

    with db() as conn:
        for v in VARIANTS:
            rows = conn.execute("""
                SELECT mr.*
                FROM market_results mr
                JOIN discovered_markets dm
                  ON dm.condition_id = mr.condition_id
                WHERE mr.variant=?
                  AND (dm.end_ts * 1000) >= ?
                  AND (dm.end_ts * 1000) < ?
            """, (v["name"], start_ms, end_ms)).fetchall()

            pnl = sum(sf(r["pnl"]) for r in rows)
            cost = sum(sf(r["total_cost"]) for r in rows)
            wins = sum(1 for r in rows if sf(r["pnl"]) > 0)
            losses = sum(1 for r in rows if sf(r["pnl"]) < 0)

            trades = conn.execute("""
                SELECT COUNT(*) c
                FROM paper_trades
                WHERE variant=? AND trade_ms>=? AND trade_ms<?
            """, (v["name"], start_ms, end_ms)).fetchone()["c"]

            fees = conn.execute("""
                SELECT COALESCE(SUM(fee),0) f
                FROM paper_trades
                WHERE variant=? AND trade_ms>=? AND trade_ms<?
            """, (v["name"], start_ms, end_ms)).fetchone()["f"]

            out.append({
                "variant": v["name"],
                "entry_move": v["entry_move"],
                "pyramid_step": v["pyramid_step"],
                "lookback_ticks": v["lookback"],
                "switch_move": v["switch_move"],
                "max_buys_side": v["max_buys_side"],
                "entry_price_min": v.get("entry_price_min", ""),
                "entry_price_max": v.get("entry_price_max", ""),
                "momentum_cap": v.get("momentum_cap", ""),
                "allow_switch": v.get("allow_switch", True),
                "entry_cutoff_sec": v.get("entry_cutoff_sec", ""),
                "switch_price_max": v.get("switch_price_max", ""),
                "markets_settled": len(rows),
                "winning_markets": wins,
                "losing_markets": losses,
                "paper_trades": trades,
                "fees": round(sf(fees), 5),
                "cost": round(cost, 5),
                "pnl": round(pnl, 5),
                "roi_pct": round((pnl / cost * 100) if cost > 0 else 0, 4),
            })

    return sorted(out, key=lambda x: x["pnl"], reverse=True)


def paper_account_status_rows():
    out = []
    with db() as conn:
        accounts = conn.execute("""
            SELECT * FROM paper_accounts
            ORDER BY initial_capital
        """).fetchall()

        for a in accounts:
            account_id = str(a["account_id"])
            initial = sf(a["initial_capital"])
            cash = sf(a["cash"])
            realized = sf(a["realized_pnl"])

            # Cost and PnL figures here are settled/realized.
            rr = conn.execute("""
                SELECT
                    COUNT(*) AS markets,
                    COALESCE(SUM(total_cost),0) AS cost,
                    COALESCE(SUM(payout),0) AS payout,
                    COALESCE(SUM(pnl),0) AS pnl,
                    SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) AS losses
                FROM paper_account_results
                WHERE account_id=?
            """, (account_id,)).fetchone()

            # Open positions at cost; separate from cash so a low cash number
            # is never mistaken for account equity.
            oo = conn.execute("""
                SELECT COALESCE(SUM(t.total_cost),0) AS open_cost
                FROM paper_account_trades t
                LEFT JOIN paper_account_results r
                  ON r.account_id=t.account_id
                 AND r.condition_id=t.condition_id
                WHERE t.account_id=?
                  AND r.condition_id IS NULL
            """, (account_id,)).fetchone()

            open_cost = sf(oo["open_cost"]) if oo else 0.0
            settled_pnl = sf(rr["pnl"]) if rr else 0.0

            # "settled_equity" deliberately ignores unrealized mark-to-market:
            # initial + realized PnL. This is directly comparable across accounts.
            settled_equity = initial + settled_pnl

            out.append({
                "account_id": account_id,
                "initial_capital": round(initial, 6),
                "cash": round(cash, 6),
                "open_positions_cost": round(open_cost, 6),
                "settled_equity": round(settled_equity, 6),
                "realized_pnl": round(realized, 6),
                "return_pct": round(
                    ((settled_equity - initial) / initial * 100.0)
                    if initial > 0 else 0.0,
                    6,
                ),
                "settled_markets": si(rr["markets"]) if rr else 0,
                "wins": si(rr["wins"]) if rr else 0,
                "losses": si(rr["losses"]) if rr else 0,
                "settled_cost": round(sf(rr["cost"]), 6) if rr else 0.0,
                "settled_payout": round(sf(rr["payout"]), 6) if rr else 0.0,
            })

    return out

def make_report(start_ts, end_ts):
    sm = start_ts * 1000
    em = end_ts * 1000

    with db() as conn:
        trades = conn.execute("""
            SELECT * FROM paper_trades
            WHERE trade_ms>=? AND trade_ms<?
            ORDER BY trade_ms
        """, (sm, em)).fetchall()

        signals = conn.execute("""
            SELECT * FROM signals
            WHERE signal_ms>=? AND signal_ms<?
            ORDER BY signal_ms
        """, (sm, em)).fetchall()

        results = conn.execute("""
            SELECT mr.*
            FROM market_results mr
            JOIN discovered_markets dm
              ON dm.condition_id = mr.condition_id
            WHERE (dm.end_ts * 1000) >= ?
              AND (dm.end_ts * 1000) < ?
            ORDER BY dm.end_ts, mr.variant
        """, (sm, em)).fetchall()

        markets_rows = conn.execute("""
            SELECT * FROM discovered_markets
            WHERE discovered_ms<? AND end_ts>=?
            ORDER BY start_ts
        """, (em, start_ts - 300)).fetchall()

    summary = variant_summary(sm, em)

    lines = [
        "POWERWINNER-INSPIRED STRATEGY SIMULATOR v1",
        "=" * 70,
        f"Period UTC: {utc_iso(start_ts)} -> {utc_iso(end_ts)}",
        f"Symbol: {SYMBOL}",
        f"Decision interval: {DECISION_INTERVAL}s",
        f"Trading window: first {TRADE_WINDOW_SECONDS}s",
        f"Paper lot: {ORDER_SIZE} shares",
        "",
        "VARIANTS RANKED BY REALIZED PNL",
    ]

    for x in summary:
        lines.append(
            f"{x['variant']}: pnl=${x['pnl']:+.2f} | "
            f"ROI={x['roi_pct']:+.2f}% | markets={x['markets_settled']} | "
            f"W/L={x['winning_markets']}/{x['losing_markets']} | "
            f"trades={x['paper_trades']} | fees=${x['fees']:.2f}"
        )

    lines += [
        "",
        "IMPORTANT",
        "This is an independent paper strategy test. It does NOT copy Powerwinner.",
        "All fills use the live public order book and taker fees.",
        "No real orders are placed.",
    ]

    d1 = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    d2 = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    path = REPORT_DIR / f"strategy_sim_{d1:%Y-%m-%d_%H-%M}_{d2:%H-%M}_UTC.zip"

    summary_cols = [
        "variant", "entry_move", "pyramid_step", "lookback_ticks",
        "switch_move", "max_buys_side", "entry_price_min", "entry_price_max",
        "momentum_cap", "allow_switch", "entry_cutoff_sec", "switch_price_max", "markets_settled",
        "winning_markets", "losing_markets", "paper_trades",
        "fees", "cost", "pnl", "roi_pct"
    ]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("variants_summary.csv", csv_bytes(summary, summary_cols))
        z.writestr("paper_trades.csv", csv_bytes(trades))
        z.writestr("signals.csv", csv_bytes(signals))
        z.writestr("market_results.csv", csv_bytes(results))
        z.writestr("markets.csv", csv_bytes(markets_rows))
        account_rows = paper_account_status_rows()

        with db() as conn:
            account_trades = conn.execute("""
                SELECT * FROM paper_account_trades
                WHERE trade_ms>=? AND trade_ms<?
                ORDER BY trade_ms, account_id
            """, (sm, em)).fetchall()

            account_results = conn.execute("""
                SELECT ar.*, dm.slug, dm.question, dm.end_ts
                FROM paper_account_results ar
                JOIN discovered_markets dm
                  ON dm.condition_id=ar.condition_id
                WHERE (dm.end_ts * 1000) >= ?
                  AND (dm.end_ts * 1000) < ?
                ORDER BY dm.end_ts, ar.account_id
            """, (sm, em)).fetchall()

        z.writestr("m03_accounts.csv", csv_bytes(account_rows))
        z.writestr("m03_account_trades.csv", csv_bytes(account_trades))
        z.writestr("m03_account_results.csv", csv_bytes(account_results))

        lines += [
            "",
            "M03 EXACT PAPER-MONEY ACCOUNTS",
            "These accounts mirror ONLY successful original M03_P08_L2 signals.",
            "The M03 signal engine/state above is unchanged.",
        ]
        for a in account_rows:
            lines.append(
                f"{a['account_id']}: settled_equity=${a['settled_equity']:.2f} | "
                f"PnL=${a['realized_pnl']:+.2f} | "
                f"return={a['return_pct']:+.2f}% | "
                f"cash=${a['cash']:.2f} | open_cost=${a['open_positions_cost']:.2f}"
            )

        z.writestr("report.txt", "\n".join(lines).encode("utf-8"))

    return path, summary

async def tg_file(path, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured: %s", path)
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
            timeout=aiohttp.ClientTimeout(total=120),
        ) as r:
            if r.status != 200:
                log.warning("Telegram: %s", await r.text())
                return False
            return True

    except Exception:
        log.exception("Telegram send failed")
        return False

async def report_loop():
    saved = si(state_get("last_report_end", "0"))

    if saved <= 0:
        d = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        saved = int(d.timestamp())
        state_set("last_report_end", saved)

    last_end = saved

    while True:
        try:
            eligible = ((now_ts() - REPORT_DELAY_SECONDS) // 3600) * 3600

            while last_end < eligible:
                start = last_end
                end = start + 3600

                path, summary = make_report(start, end)

                best = summary[0] if summary else None

                if best:
                    extra = (
                        f"Best: {best['variant']} | "
                        f"PnL ${best['pnl']:+.2f} | ROI {best['roi_pct']:+.2f}%"
                    )
                else:
                    extra = "No settled markets yet"

                ok = await tg_file(
                    path,
                    (
                        "🧪 Strategy Simulator\n"
                        f"{utc_iso(start)} → {utc_iso(end)}\n"
                        f"{extra}"
                    ),
                )

                if not ok:
                    break

                last_end = end
                state_set("last_report_end", last_end)

        except Exception:
            log.exception("Report loop failed")

        await asyncio.sleep(REPORT_CHECK_INTERVAL)

# ============================================================
# HEALTH
# ============================================================

async def health(request):
    with db() as conn:
        t = conn.execute("SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
        r = conn.execute("SELECT COUNT(*) c FROM market_results").fetchone()["c"]
        p = conn.execute("SELECT COALESCE(SUM(pnl),0) p FROM market_results").fetchone()["p"]

    return web.json_response({
        "ok": True,
        "version": "1.8-m03-exact-paper",
        "symbol": SYMBOL,
        "decision_interval": DECISION_INTERVAL,
        "trade_window_seconds": TRADE_WINDOW_SECONDS,
        "order_size": ORDER_SIZE,
        "variants": len(VARIANTS),
        "markets_tracked": len(markets),
        "assets_subscribed": len(subscribed_assets),
        "books": len(books),
        "paper_trades": t,
        "settled_variant_results": r,
        "aggregate_all_variant_pnl": p,
        "m03_exact_accounts": paper_account_status_rows(),
        "time_utc": utc_iso(),
    })

async def web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    log.info("Health server on :%d", PORT)

# ============================================================
# MAIN
# ============================================================

async def main():
    global session

    init_db()

    session = aiohttp.ClientSession(headers={
        "User-Agent": "PowerwinnerInspiredStrategySimulator/1.8",
        "Accept": "application/json",
    })

    tasks = [
        asyncio.create_task(web_server()),
        asyncio.create_task(discovery_loop()),
        asyncio.create_task(ws_loop()),
        asyncio.create_task(strategy_loop()),
        asyncio.create_task(resolution_fallback_loop()),
        asyncio.create_task(report_loop()),
    ]

    log.info(
        "Strategy Simulator started | %d variants | %.1fs cycle | lot=%.1f",
        len(VARIANTS),
        DECISION_INTERVAL,
        ORDER_SIZE,
    )

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
