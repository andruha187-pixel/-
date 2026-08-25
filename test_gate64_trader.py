import os
import time
import tempfile
import asyncio
import importlib.util
from pathlib import Path
import zipfile

tmp = tempfile.mkdtemp(prefix="gate64_trader_test_")
os.environ["DATA_DIR"] = tmp
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["PAPER_START_BALANCE"] = "500"
os.environ["MIN_FREE_CASH"] = "5"
os.environ["ORDER_SIZE"] = "10"
os.environ["FIRST_SIGNAL_PRICE_MIN"] = "0.64"
os.environ["FIRST_SIGNAL_PRICE_MAX"] = "0.75"
os.environ["MAX_BUYS_SIDE"] = "2"
os.environ["ENTRY_MOVE"] = "0.03"
os.environ["PYRAMID_STEP"] = "0.08"
os.environ["LOOKBACK_TICKS"] = "2"
os.environ["MOMENTUM_CAP"] = "0.30"

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bot", here / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

assert bot.STRATEGY_NAME == "M03_V2_GATE64_X2"
assert len(bot.VARIANTS) == 1
assert bot.MAX_BUYS_SIDE == 2
assert abs(bot.paper_cash() - 500.0) < 1e-9
assert bot.trading_enabled() is False
bot.state_set("trading_enabled", "1")
assert bot.trading_enabled() is True

now = int(time.time())
slot = (now // 300) * 300

market = {
    "condition_id": "cid-pass",
    "question": "Bitcoin Up or Down Test",
    "slug": f"btc-updown-5m-{slot}",
    "start_ts": slot,
    "end_ts": slot + 300,
    "up_asset": "UP_PASS",
    "down_asset": "DOWN_PASS",
}
bot.markets[market["condition_id"]] = market
bot.persist_market(market)

# First raw M03 signal: Up = .65 and momentum +.05 -> gate PASS + ENTRY.
bot.books["UP_PASS"] = {
    "bids": {0.64: 100.0},
    "asks": {0.65: 100.0},
    "received_ms": bot.now_ms(),
    "source": "test",
}
bot.books["DOWN_PASS"] = {
    "bids": {0.34: 100.0},
    "asks": {0.35: 100.0},
    "received_ms": bot.now_ms(),
    "source": "test",
}
bot.price_history["cid-pass"]["UP_PASS"].extend([
    (bot.now_ms()-6000, 0.60),
    (bot.now_ms()-3000, 0.62),
    (bot.now_ms(), 0.65),
])
bot.price_history["cid-pass"]["DOWN_PASS"].extend([
    (bot.now_ms()-6000, 0.40),
    (bot.now_ms()-3000, 0.38),
    (bot.now_ms(), 0.35),
])

asyncio.run(bot.evaluate_variant(market, bot.STRATEGY, 30.0))

with bot.db() as c:
    gates = c.execute("SELECT * FROM gate_decisions WHERE condition_id='cid-pass'").fetchall()
    trades = c.execute("SELECT * FROM paper_trades WHERE condition_id='cid-pass' ORDER BY id").fetchall()
assert len(gates) == 1 and gates[0]["passed"] == 1
assert len(trades) == 1 and trades[0]["signal_type"] == "ENTRY"
assert abs(trades[0]["filled_shares"] - 10.0) < 1e-9
cash_after_entry = bot.paper_cash()
assert cash_after_entry < 500.0

# +0.08 from last buy -> exactly one PYRAMID.
bot.books["UP_PASS"]["asks"] = {0.73: 100.0}
bot.books["UP_PASS"]["received_ms"] = bot.now_ms()
bot.price_history["cid-pass"]["UP_PASS"].extend([
    (bot.now_ms()-3000, 0.68),
    (bot.now_ms(), 0.73),
])
asyncio.run(bot.evaluate_variant(market, bot.STRATEGY, 60.0))

with bot.db() as c:
    trades = c.execute("SELECT * FROM paper_trades WHERE condition_id='cid-pass' ORDER BY id").fetchall()
assert len(trades) == 2
assert trades[1]["signal_type"] == "PYRAMID"

# Even if price rises again, max 2 buys prevents a third trade.
bot.books["UP_PASS"]["asks"] = {0.82: 100.0}
bot.books["UP_PASS"]["received_ms"] = bot.now_ms()
bot.price_history["cid-pass"]["UP_PASS"].extend([
    (bot.now_ms()-3000, 0.77),
    (bot.now_ms(), 0.82),
])
asyncio.run(bot.evaluate_variant(market, bot.STRATEGY, 90.0))
with bot.db() as c:
    n = c.execute("SELECT COUNT(*) c FROM paper_trades WHERE condition_id='cid-pass'").fetchone()["c"]
assert n == 2

# Settlement returns payout to the account. Since Up wins, payout = 20 shares.
asyncio.run(bot.settle_market("cid-pass", "UP_PASS", "Up"))
with bot.db() as c:
    result = c.execute("SELECT * FROM market_results WHERE condition_id='cid-pass'").fetchone()
assert result is not None
assert result["trades"] == 2
assert result["payout"] == 20.0
assert abs(bot.paper_cash() - (500.0 + float(result["pnl"]))) < 1e-7

# A first signal below .64 permanently blacklists another market.
market2 = {
    "condition_id": "cid-skip",
    "question": "Bitcoin Up or Down Test 2",
    "slug": f"btc-updown-5m-{slot+300}",
    "start_ts": slot + 300,
    "end_ts": slot + 600,
    "up_asset": "UP_SKIP",
    "down_asset": "DOWN_SKIP",
}
bot.markets[market2["condition_id"]] = market2
bot.persist_market(market2)
bot.books["UP_SKIP"] = {
    "bids": {0.59: 100.0}, "asks": {0.60: 100.0},
    "received_ms": bot.now_ms(), "source": "test",
}
bot.books["DOWN_SKIP"] = {
    "bids": {0.39: 100.0}, "asks": {0.40: 100.0},
    "received_ms": bot.now_ms(), "source": "test",
}
bot.price_history["cid-skip"]["UP_SKIP"].extend([
    (bot.now_ms()-6000, 0.55),
    (bot.now_ms()-3000, 0.57),
    (bot.now_ms(), 0.60),
])
bot.price_history["cid-skip"]["DOWN_SKIP"].extend([
    (bot.now_ms()-6000, 0.45),
    (bot.now_ms()-3000, 0.43),
    (bot.now_ms(), 0.40),
])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 30.0))
with bot.db() as c:
    gate2 = c.execute("SELECT * FROM gate_decisions WHERE condition_id='cid-skip'").fetchone()
    n2 = c.execute("SELECT COUNT(*) c FROM paper_trades WHERE condition_id='cid-skip'").fetchone()["c"]
assert gate2["passed"] == 0
assert gate2["reason"] == "FIRST_SIGNAL_PRICE_LOW"
assert n2 == 0

# Later price inside the range must NOT revive the skipped market.
bot.books["UP_SKIP"]["asks"] = {0.66: 100.0}
bot.books["UP_SKIP"]["received_ms"] = bot.now_ms()
bot.price_history["cid-skip"]["UP_SKIP"].extend([
    (bot.now_ms()-3000, 0.62),
    (bot.now_ms(), 0.66),
])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 60.0))
with bot.db() as c:
    n2 = c.execute("SELECT COUNT(*) c FROM paper_trades WHERE condition_id='cid-skip'").fetchone()["c"]
assert n2 == 0

# Hourly ZIP report still exists and includes the gate/trade/result files.
hour_start = slot - (slot % 3600)
path, summary = bot.make_report(hour_start, hour_start + 3600)
assert path.exists()
with zipfile.ZipFile(path, "r") as z:
    names = set(z.namelist())
for expected in {
    "strategy_summary.csv",
    "variants_summary.csv",
    "gate_decisions.csv",
    "paper_trades.csv",
    "signals.csv",
    "market_results.csv",
    "markets.csv",
    "report.txt",
}:
    assert expected in names

stats = bot.account_stats()
assert stats["trades"] == 2
assert stats["traded_markets"] == 1
assert stats["gate_pass"] == 1
assert stats["gate_skip"] == 1

print("GATE64 X2 single-strategy trading bot regression: OK")
