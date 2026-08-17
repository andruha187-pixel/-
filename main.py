import os
import io
import csv
import json
import time
import math
import sqlite3
import asyncio
import zipfile
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from aiohttp import web
import websockets
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

PORT = int(os.getenv("PORT", "8080"))

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DISCOVERY_INTERVAL = float(os.getenv("DISCOVERY_INTERVAL", "5"))
STRATEGY_INTERVAL = float(os.getenv("STRATEGY_INTERVAL", "3.0"))
MAX_BOOK_AGE_MS = int(os.getenv("MAX_BOOK_AGE_MS", "900"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CRYPTO_FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))

# Original M03 parameters.
ENTRY_MOVE = 0.03
PYRAMID_STEP = 0.08
LOOKBACK_TICKS = 2
SWITCH_MOVE = 0.03
MAX_BUYS_SIDE = 5

# A $1000 account corresponds to the original simulator's 10 shares per buy.
BASE_CAPITAL = float(os.getenv("BASE_CAPITAL", "1000"))
BASE_SHARES = float(os.getenv("BASE_SHARES", "10"))

# Independent paper accounts.
CAPITALS = [
    float(x.strip())
    for x in os.getenv("PAPER_CAPITALS", "100,250,500,1000,2500").split(",")
    if x.strip()
]

REPORT_DELAY_SECONDS = int(os.getenv("REPORT_DELAY_SECONDS", "300"))
REPORT_CHECK_INTERVAL = int(os.getenv("REPORT_CHECK_INTERVAL", "30"))

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / ".write_test"
    p.write_text("ok")
    p.unlink()
except Exception:
    DATA_DIR = Path("./data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "m03_paper_money.db"
REPORT_DIR = DATA_DIR / "m03_paper_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("m03-paper-money")

session: Optional[aiohttp.ClientSession] = None

markets = {}
asset_to_market = {}
books = {}
subscribed_assets = set()
ws_send_queue: asyncio.Queue = asyncio.Queue()

# condition_id -> per-market original M03 state
strategy_state = {}
price_history = {}

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

def slot_start_from_slug(slug):
    try:
        return int(str(slug).rstrip("/").split("-")[-1])
    except Exception:
        return None

def account_id(capital):
    return f"CAP_{capital:g}"

def shares_per_buy(capital):
    return BASE_SHARES * (capital / BASE_CAPITAL)

def fee_for(shares, price):
    fee = shares * CRYPTO_FEE_RATE * price * (1.0 - price)
    return round(fee, 6) if fee >= 0.0000005 else 0.0

# ============================================================
# DB
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
        CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY,
            slug TEXT,
            title TEXT,
            start_ts INTEGER,
            end_ts INTEGER,
            up_asset TEXT,
            down_asset TEXT,
            resolved INTEGER DEFAULT 0,
            winning_asset TEXT,
            winning_outcome TEXT,
            discovered_ms INTEGER
        );

        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            initial_capital REAL,
            cash REAL,
            realized_pnl REAL DEFAULT 0,
            created_ms INTEGER
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT,
            ts_ms INTEGER,
            elapsed_sec REAL,
            signal_type TEXT,
            asset TEXT,
            outcome TEXT,
            ask REAL,
            reference REAL,
            momentum REAL
        );

        CREATE TABLE IF NOT EXISTS paper_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            condition_id TEXT,
            ts_ms INTEGER,
            elapsed_sec REAL,
            signal_type TEXT,
            asset TEXT,
            outcome TEXT,
            requested_shares REAL,
            filled_shares REAL,
            avg_price REAL,
            gross_cost REAL,
            fee REAL,
            total_cost REAL,
            status TEXT,
            UNIQUE(account_id, condition_id, ts_ms, signal_type, asset)
        );

        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            condition_id TEXT,
            winning_asset TEXT,
            winning_outcome TEXT,
            payout REAL,
            market_cost REAL,
            pnl REAL,
            settled_ms INTEGER,
            UNIQUE(account_id, condition_id)
        );

        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            ts_ms INTEGER,
            cash REAL,
            open_value REAL,
            equity REAL,
            realized_pnl REAL
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_orders_market ON paper_orders(condition_id);
        CREATE INDEX IF NOT EXISTS idx_orders_account ON paper_orders(account_id);
        CREATE INDEX IF NOT EXISTS idx_results_account ON results(account_id);
        """)

        for capital in CAPITALS:
            aid = account_id(capital)
            conn.execute("""
                INSERT OR IGNORE INTO accounts(
                    account_id, initial_capital, cash, realized_pnl, created_ms
                ) VALUES (?,?,?,?,?)
            """, (aid, capital, capital, 0.0, now_ms()))
        conn.commit()

def state_get(key, default=None):
    with db() as conn:
        r = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

def state_set(key, value):
    with db() as conn:
        conn.execute("""
            INSERT INTO state(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        conn.commit()

# ============================================================
# HTTP / MARKET DISCOVERY
# ============================================================

async def get_json(url, params=None):
    for attempt in range(3):
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as r:
                txt = await r.text()
                if r.status == 200:
                    return json.loads(txt)
                if r.status == 429:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                log.warning("HTTP %s %s -> %s", r.status, url, txt[:220])
        except Exception as e:
            log.warning("GET %s failed: %s", url, e)
        await asyncio.sleep(0.2 * (attempt + 1))
    return None

async def fetch_event_by_slug(slug):
    for url, params in (
        (f"{GAMMA_API}/events/slug/{slug}", None),
        (f"{GAMMA_API}/events", {"slug": slug}),
    ):
        data = await get_json(url, params)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    return None

def parse_market_from_event(event, expected_slug):
    if not isinstance(event, dict):
        return None
    raw_markets = event.get("markets")
    if not isinstance(raw_markets, list):
        return None

    for raw in raw_markets:
        if not isinstance(raw, dict):
            continue

        cid = str(raw.get("conditionId") or "")
        if not cid:
            continue

        outcomes = [str(x).strip().upper() for x in parse_jsonish(raw.get("outcomes"))]
        tokens = [str(x) for x in parse_jsonish(raw.get("clobTokenIds"))]
        if len(tokens) < 2:
            continue

        up_asset = None
        down_asset = None
        for i, out in enumerate(outcomes):
            if i >= len(tokens):
                break
            if out in {"UP", "YES"}:
                up_asset = tokens[i]
            elif out in {"DOWN", "NO"}:
                down_asset = tokens[i]

        up_asset = up_asset or tokens[0]
        down_asset = down_asset or tokens[1]

        slug = str(raw.get("slug") or event.get("slug") or expected_slug)
        st = slot_start_from_slug(slug) or slot_start_from_slug(expected_slug)
        if not st:
            continue

        return {
            "condition_id": cid,
            "slug": slug,
            "title": str(raw.get("question") or event.get("title") or slug),
            "start_ts": int(st),
            "end_ts": int(st) + 300,
            "up_asset": up_asset,
            "down_asset": down_asset,
        }

    return None

async def subscribe_asset(asset):
    if not asset or asset in subscribed_assets:
        return
    subscribed_assets.add(asset)
    await ws_send_queue.put({"operation": "subscribe", "assets_ids": [asset]})

async def add_market(m):
    cid = m["condition_id"]
    if cid in markets:
        return

    markets[cid] = m
    asset_to_market[m["up_asset"]] = cid
    asset_to_market[m["down_asset"]] = cid

    with db() as conn:
        conn.execute("""
            INSERT INTO markets(
                condition_id, slug, title, start_ts, end_ts,
                up_asset, down_asset, discovered_ms
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                slug=excluded.slug,
                title=excluded.title,
                start_ts=excluded.start_ts,
                end_ts=excluded.end_ts,
                up_asset=excluded.up_asset,
                down_asset=excluded.down_asset
        """, (
            cid, m["slug"], m["title"], m["start_ts"], m["end_ts"],
            m["up_asset"], m["down_asset"], now_ms(),
        ))
        conn.commit()

    strategy_state.setdefault(cid, {
        "started_sides": set(),
        "buys": {m["up_asset"]: 0, m["down_asset"]: 0},
        "last_buy": {},
    })
    price_history.setdefault(cid, {
        m["up_asset"]: [],
        m["down_asset"]: [],
    })

    await subscribe_asset(m["up_asset"])
    await subscribe_asset(m["down_asset"])

    log.info("MARKET %s | %s -> %s", m["slug"], utc_iso(m["start_ts"]), utc_iso(m["end_ts"]))

async def discovery_loop():
    while True:
        try:
            now = now_ts()
            current = (now // 300) * 300
            for slot in (current - 300, current, current + 300):
                slug = f"btc-updown-5m-{slot}"
                event = await fetch_event_by_slug(slug)
                if not event:
                    continue
                m = parse_market_from_event(event, slug)
                if m:
                    await add_market(m)
        except Exception:
            log.exception("Discovery failed")
        await asyncio.sleep(DISCOVERY_INTERVAL)

# ============================================================
# ORDER BOOK
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

def apply_book(asset, payload):
    books[asset] = {
        "bids": level_map(payload.get("bids")),
        "asks": level_map(payload.get("asks")),
        "received_ms": now_ms(),
    }

def apply_price_change(payload):
    recv = now_ms()
    changes = payload.get("price_changes") or payload.get("priceChanges") or []
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        asset = str(ch.get("asset_id") or ch.get("token_id") or ch.get("tokenId") or "")
        if not asset:
            continue
        b = books.setdefault(asset, {"bids": {}, "asks": {}, "received_ms": recv})
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

def best_ask(asset):
    b = books.get(asset)
    if not b or not b["asks"]:
        return None
    return min(b["asks"])

def best_bid(asset):
    b = books.get(asset)
    if not b or not b["bids"]:
        return None
    return max(b["bids"])

async def refresh_book(asset):
    data = await get_json(f"{CLOB_API}/book", {"token_id": asset})
    if isinstance(data, dict):
        apply_book(asset, data)
        return True
    return False

async def ensure_fresh_book(asset):
    b = books.get(asset)
    if b and (b["asks"] or b["bids"]):
        if now_ms() - b["received_ms"] <= MAX_BOOK_AGE_MS:
            return True
    return await refresh_book(asset)

# ============================================================
# WS
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
                await asyncio.sleep(0.5)
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
                            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
                            if et == "book":
                                asset = str(payload.get("asset_id") or payload.get("token_id") or "")
                                if asset:
                                    apply_book(asset, payload)
                            elif et == "price_change":
                                apply_price_change(payload)
                finally:
                    sender.cancel()
                    ping.cancel()
        except Exception as e:
            log.warning("WS reconnect: %s", e)
            await asyncio.sleep(1)

# ============================================================
# ORIGINAL M03 SIGNAL ENGINE
# ============================================================

def record_prices(m):
    cid = m["condition_id"]
    hist = price_history.setdefault(cid, {m["up_asset"]: [], m["down_asset"]: []})
    for asset in (m["up_asset"], m["down_asset"]):
        ask = best_ask(asset)
        if ask is not None:
            arr = hist.setdefault(asset, [])
            arr.append((now_ms(), ask))
            if len(arr) > 100:
                del arr[:-100]

def momentum_for(cid, asset):
    arr = price_history.get(cid, {}).get(asset, [])
    if len(arr) <= LOOKBACK_TICKS:
        return None, None
    current = arr[-1][1]
    ref = arr[-1 - LOOKBACK_TICKS][1]
    return current - ref, ref

def store_signal(cid, elapsed, signal_type, asset, outcome, ask, ref, mom):
    with db() as conn:
        conn.execute("""
            INSERT INTO signals(
                condition_id, ts_ms, elapsed_sec, signal_type,
                asset, outcome, ask, reference, momentum
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (cid, now_ms(), elapsed, signal_type, asset, outcome, ask, ref, mom))
        conn.commit()

async def fill_market_buy(asset, shares):
    await ensure_fresh_book(asset)
    b = books.get(asset)
    if not b or not b["asks"]:
        return 0.0, None, 0.0, 0.0

    remain = shares
    gross = 0.0
    fee = 0.0
    filled = 0.0

    for price in sorted(b["asks"]):
        qty = b["asks"][price]
        if qty <= 0:
            continue
        take = min(remain, qty)
        if take <= 0:
            break
        gross += take * price
        fee += fee_for(take, price)
        filled += take
        remain -= take
        if remain <= 1e-10:
            break

    avg = gross / filled if filled > 0 else None
    return filled, avg, gross, fee

async def execute_for_accounts(cid, elapsed, signal_type, asset, outcome):
    for capital in CAPITALS:
        aid = account_id(capital)
        requested = shares_per_buy(capital)

        with db() as conn:
            acc = conn.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (aid,),
            ).fetchone()
        if not acc:
            continue

        cash = sf(acc["cash"])
        if cash <= 0:
            continue

        filled, avg, gross, fee = await fill_market_buy(asset, requested)
        total = gross + fee

        # Paper account cannot spend more than its available cash.
        if total > cash and avg is not None and total > 0:
            ratio = max(0.0, cash / total)
            filled *= ratio
            gross *= ratio
            fee *= ratio
            total = gross + fee

        if filled <= 1e-10:
            status = "NO_FILL"
        elif filled + 1e-9 < requested:
            status = "PARTIAL"
        else:
            status = "FULL"

        with db() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO paper_orders(
                    account_id, condition_id, ts_ms, elapsed_sec,
                    signal_type, asset, outcome, requested_shares,
                    filled_shares, avg_price, gross_cost, fee,
                    total_cost, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                aid, cid, now_ms(), elapsed, signal_type,
                asset, outcome, requested, filled, avg,
                gross, fee, total, status,
            ))
            if filled > 0:
                conn.execute(
                    "UPDATE accounts SET cash=cash-? WHERE account_id=?",
                    (total, aid),
                )
            conn.commit()

        if filled > 0:
            log.info(
                "%s | %s %s | %.4fsh @ %s | cost=%.2f | cash=%.2f",
                aid, signal_type, outcome, filled,
                f"{avg:.4f}" if avg is not None else "-",
                total, max(0.0, cash - total),
            )

async def evaluate_m03(m):
    cid = m["condition_id"]
    elapsed = now_ts() - m["start_ts"]

    if elapsed < 0 or elapsed >= 300:
        return

    st = strategy_state.setdefault(cid, {
        "started_sides": set(),
        "buys": {m["up_asset"]: 0, m["down_asset"]: 0},
        "last_buy": {},
    })

    candidates = []

    for asset, outcome in ((m["up_asset"], "Up"), (m["down_asset"], "Down")):
        ask = best_ask(asset)
        if ask is None or ask <= 0.01 or ask >= 0.99:
            continue

        mom, ref = momentum_for(cid, asset)
        if mom is None:
            continue

        buys = st["buys"].get(asset, 0)
        signal = None

        if buys == 0:
            if not st["started_sides"]:
                if mom >= ENTRY_MOVE:
                    signal = "ENTRY"
            else:
                if mom >= SWITCH_MOVE:
                    signal = "SWITCH"
        else:
            last_buy = st["last_buy"].get(asset)
            if (
                last_buy is not None
                and ask >= last_buy + PYRAMID_STEP
                and mom > 0
                and buys < MAX_BUYS_SIDE
            ):
                signal = "PYRAMID"

        if signal:
            candidates.append((mom, asset, outcome, ask, ref, signal))

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    mom, asset, outcome, ask, ref, signal = candidates[0]

    store_signal(cid, elapsed, signal, asset, outcome, ask, ref, mom)
    await execute_for_accounts(cid, elapsed, signal, asset, outcome)

    st["started_sides"].add(asset)
    st["buys"][asset] = st["buys"].get(asset, 0) + 1
    st["last_buy"][asset] = ask

async def strategy_loop():
    log.info(
        "M03 Paper Money started | capitals=%s | base %.0f -> %.1f shares | cycle=%.1fs",
        CAPITALS, BASE_CAPITAL, BASE_SHARES, STRATEGY_INTERVAL,
    )

    while True:
        try:
            now = now_ts()
            for m in list(markets.values()):
                if m["start_ts"] <= now < m["end_ts"]:
                    record_prices(m)
                    await evaluate_m03(m)
        except Exception:
            log.exception("Strategy loop failed")

        await asyncio.sleep(STRATEGY_INTERVAL)

# ============================================================
# RESOLUTION
# ============================================================

def resolved_winner_from_market(raw):
    if not isinstance(raw, dict):
        return None, None

    token_objs = raw.get("tokens")
    if isinstance(token_objs, list):
        for tok in token_objs:
            if isinstance(tok, dict) and bool(tok.get("winner", False)):
                asset = str(tok.get("token_id") or tok.get("tokenId") or tok.get("id") or "")
                outcome = str(tok.get("outcome") or tok.get("name") or "")
                if asset:
                    return asset, outcome

    outcomes = [str(x) for x in parse_jsonish(raw.get("outcomes"))]
    tokens = [str(x) for x in parse_jsonish(raw.get("clobTokenIds"))]
    prices_raw = parse_jsonish(raw.get("outcomePrices"))
    if len(outcomes) >= 2 and len(tokens) >= 2 and len(prices_raw) >= 2:
        prices = [sf(x, -1) for x in prices_raw]
        n = min(len(outcomes), len(tokens), len(prices))
        idx = max(range(n), key=lambda i: prices[i])
        others = [prices[i] for i in range(n) if i != idx]
        if prices[idx] >= 0.999 and (max(others) if others else -1) <= 0.001:
            return tokens[idx], outcomes[idx]

    return None, None

async def resolve_market_row(row):
    event = await fetch_event_by_slug(str(row["slug"]))
    if not isinstance(event, dict):
        return None, None

    rms = event.get("markets")
    if not isinstance(rms, list):
        return None, None

    raw = None
    for x in rms:
        if isinstance(x, dict) and str(x.get("conditionId") or "") == str(row["condition_id"]):
            raw = x
            break
    if raw is None and len(rms) == 1 and isinstance(rms[0], dict):
        raw = rms[0]

    return resolved_winner_from_market(raw)

async def settle_market(cid, winning_asset, winning_outcome):
    with db() as conn:
        for capital in CAPITALS:
            aid = account_id(capital)
            existing = conn.execute(
                "SELECT 1 FROM results WHERE account_id=? AND condition_id=?",
                (aid, cid),
            ).fetchone()
            if existing:
                continue

            orders = conn.execute("""
                SELECT * FROM paper_orders
                WHERE account_id=? AND condition_id=?
                ORDER BY id
            """, (aid, cid)).fetchall()

            if not orders:
                # No trade in this market for that account.
                continue

            total_cost = sum(sf(o["total_cost"]) for o in orders)
            payout = sum(
                sf(o["filled_shares"])
                for o in orders
                if str(o["asset"]) == str(winning_asset)
            )
            pnl = payout - total_cost

            conn.execute("""
                INSERT INTO results(
                    account_id, condition_id, winning_asset, winning_outcome,
                    payout, market_cost, pnl, settled_ms
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (
                aid, cid, winning_asset, winning_outcome,
                payout, total_cost, pnl, now_ms(),
            ))
            conn.execute("""
                UPDATE accounts
                SET cash=cash+?,
                    realized_pnl=realized_pnl+?
                WHERE account_id=?
            """, (payout, pnl, aid))

        conn.execute("""
            UPDATE markets
            SET resolved=1, winning_asset=?, winning_outcome=?
            WHERE condition_id=?
        """, (winning_asset, winning_outcome, cid))
        conn.commit()

    log.info("RESOLVED %s | winner=%s", cid[-8:], winning_outcome)

async def resolution_loop():
    while True:
        try:
            cutoff = now_ts() - 10
            with db() as conn:
                rows = conn.execute("""
                    SELECT * FROM markets
                    WHERE resolved=0 AND end_ts < ?
                    ORDER BY end_ts
                    LIMIT 100
                """, (cutoff,)).fetchall()

            for row in rows:
                wa, wo = await resolve_market_row(row)
                if wa:
                    await settle_market(str(row["condition_id"]), wa, wo)
        except Exception:
            log.exception("Resolution failed")

        await asyncio.sleep(10)

# ============================================================
# EQUITY / REPORTS
# ============================================================

async def account_metrics(aid):
    with db() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()
        if not acc:
            return None

        rows = conn.execute("""
            SELECT po.asset, SUM(po.filled_shares) AS shares
            FROM paper_orders po
            JOIN markets m ON m.condition_id=po.condition_id
            WHERE po.account_id=? AND m.resolved=0
            GROUP BY po.asset
        """, (aid,)).fetchall()

    open_value = 0.0
    for r in rows:
        asset = str(r["asset"])
        shares = sf(r["shares"])
        if shares <= 0:
            continue
        await ensure_fresh_book(asset)
        bid = best_bid(asset)
        if bid is not None:
            open_value += shares * bid

    cash = sf(acc["cash"])
    initial = sf(acc["initial_capital"])
    realized = sf(acc["realized_pnl"])
    equity = cash + open_value

    return {
        "account_id": aid,
        "initial_capital": initial,
        "cash": cash,
        "open_value": open_value,
        "equity": equity,
        "realized_pnl": realized,
        "total_return": equity - initial,
        "return_pct": ((equity - initial) / initial * 100.0) if initial > 0 else 0.0,
    }

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

async def make_report(start_ts, end_ts):
    sm, em = start_ts * 1000, end_ts * 1000

    metrics = []
    for capital in CAPITALS:
        m = await account_metrics(account_id(capital))
        if m:
            metrics.append(m)

    with db() as conn:
        signals = conn.execute("""
            SELECT * FROM signals
            WHERE ts_ms>=? AND ts_ms<?
            ORDER BY ts_ms
        """, (sm, em)).fetchall()

        orders = conn.execute("""
            SELECT * FROM paper_orders
            WHERE ts_ms>=? AND ts_ms<?
            ORDER BY ts_ms, account_id
        """, (sm, em)).fetchall()

        results = conn.execute("""
            SELECT r.*, m.slug, m.title, m.end_ts
            FROM results r
            JOIN markets m ON m.condition_id=r.condition_id
            WHERE (m.end_ts*1000)>=? AND (m.end_ts*1000)<?
            ORDER BY m.end_ts, r.account_id
        """, (sm, em)).fetchall()

    lines = [
        "M03 PAPER MONEY BOT",
        "=" * 68,
        f"Period UTC: {utc_iso(start_ts)} -> {utc_iso(end_ts)}",
        f"Original M03: entry={ENTRY_MOVE}, pyramid={PYRAMID_STEP}, lookback={LOOKBACK_TICKS}, switch={SWITCH_MOVE}",
        f"Capital accounts: {CAPITALS}",
        "",
        f"M03 signals this hour: {len(signals)}",
        f"Paper orders this hour: {len(orders)}",
        f"Settled account-results attributed to this hour: {len(results)}",
        "",
        "ACCOUNT STATUS",
    ]

    for m in metrics:
        lines.append(
            f"{m['account_id']}: start=${m['initial_capital']:.2f} | "
            f"cash=${m['cash']:.2f} | open=${m['open_value']:.2f} | "
            f"equity=${m['equity']:.2f} | return=${m['total_return']:+.2f} "
            f"({m['return_pct']:+.2f}%) | realized=${m['realized_pnl']:+.2f}"
        )

    d1 = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    d2 = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    path = REPORT_DIR / f"m03_paper_{d1:%Y-%m-%d_%H-%M}_{d2:%H-%M}_UTC.zip"

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("signals.csv", csv_bytes(signals))
        z.writestr("orders.csv", csv_bytes(orders))
        z.writestr("results.csv", csv_bytes(results))
        z.writestr("accounts.csv", csv_bytes(metrics))
        z.writestr("report.txt", "\n".join(lines).encode("utf-8"))

    return path, metrics

async def tg_file(path, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured; report at %s", path)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    form = aiohttp.FormData()
    form.add_field("chat_id", TELEGRAM_CHAT_ID)
    form.add_field("caption", caption[:1024])
    form.add_field(
        "document", path.read_bytes(),
        filename=path.name,
        content_type="application/zip",
    )

    try:
        async with session.post(
            url, data=form,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as r:
            if r.status != 200:
                log.warning("Telegram error: %s", await r.text())
                return False
            return True
    except Exception:
        log.exception("Telegram send failed")
        return False

async def reporter():
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

                path, metrics = await make_report(start, end)
                best = max(metrics, key=lambda x: x["return_pct"]) if metrics else None

                caption = (
                    "📊 M03 Paper Money\n"
                    f"{utc_iso(start)} → {utc_iso(end)}\n"
                    + (
                        f"Best: {best['account_id']} | "
                        f"equity ${best['equity']:.2f} | "
                        f"{best['return_pct']:+.2f}%"
                        if best else "No account metrics"
                    )
                )

                ok = await tg_file(path, caption)
                if not ok:
                    break

                last_end = end
                state_set("last_report_end", last_end)

        except Exception:
            log.exception("Reporter failed")

        await asyncio.sleep(REPORT_CHECK_INTERVAL)

# ============================================================
# HEALTH
# ============================================================

async def health(request):
    metrics = []
    for capital in CAPITALS:
        m = await account_metrics(account_id(capital))
        if m:
            metrics.append(m)

    return web.json_response({
        "ok": True,
        "version": "1.0-m03-paper-money",
        "paper_only": True,
        "strategy": "M03_P08_L2",
        "capitals": CAPITALS,
        "base_capital": BASE_CAPITAL,
        "base_shares": BASE_SHARES,
        "accounts": metrics,
        "markets_tracked": len(markets),
        "ws_assets": len(subscribed_assets),
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
        "User-Agent": "M03PaperMoneyBot/1.0",
        "Accept": "application/json",
    })

    tasks = [
        asyncio.create_task(web_server()),
        asyncio.create_task(discovery_loop()),
        asyncio.create_task(ws_loop()),
        asyncio.create_task(strategy_loop()),
        asyncio.create_task(resolution_loop()),
        asyncio.create_task(reporter()),
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
