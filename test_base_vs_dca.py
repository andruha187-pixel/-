import os
import time
import asyncio
import tempfile
import importlib.util
import zipfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="safe67_base_dca_")
os.environ["DATA_DIR"] = tmp
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["PAPER_START_BALANCE"] = "500"
os.environ["ENTRY_ORDER_SIZE"] = "5"
os.environ["DCA_ORDER_SIZE"] = "5"
os.environ["DCA_ARM_PRICE"] = "0.50"
os.environ["DCA_MAX_BUY_PRICE"] = "0.60"
os.environ["DCA_REBOUND_MOM"] = "0.05"
os.environ["DCA_DEADLINE_SEC"] = "120"

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bot", here / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

A = bot.STRATEGY_BY_NAME["A_SAFE67_BASE"]
B = bot.STRATEGY_BY_NAME["B_SAFE67_REVERSAL_DCA"]

# Exact SAFE67 entry contract remains unchanged.
assert bot.V2_ELIGIBLE_PRICE_MIN == 0.55
assert bot.V2_ELIGIBLE_PRICE_MAX == 0.75
assert bot.V2_ELIGIBLE_MOM_MIN == 0.03
assert bot.V2_ELIGIBLE_MOM_MAX == 0.30
assert bot.SAFE_ENTRY_PRICE_MIN == 0.67
assert bot.SAFE_ENTRY_PRICE_MAX == 0.75
assert bot.SAFE_ENTRY_MOM_MIN == 0.05
assert bot.SAFE_ENTRY_MOM_MAX == 0.10
assert bot.LOOKBACK_TICKS == 2
assert bot.ENTRY_ORDER_SIZE == 5
assert bot.DCA_ORDER_SIZE == 5
assert A["max_buys_side"] == 1 and not A["dca_enabled"]
assert B["max_buys_side"] == 2 and B["dca_enabled"]
assert all(v.get("stop_loss_price") is None for v in (A, B))

source = (here / "main.py").read_text(encoding="utf-8")
assert "async def stop_loss_loop" not in source
assert "STOP_LOSS_PRICE" not in source
assert "PYRAMID_STEP" not in source
assert "PYRAMID_ORDER_SIZE" not in source

def fresh_book(asset, bid, ask, size=100.0):
    bot.books[asset] = {
        "bids": {float(bid): float(size)},
        "asks": {float(ask): float(size)},
        "received_ms": bot.now_ms(),
        "source": "test",
    }

now = int(time.time())
slot = (now // 300) * 300
market = {
    "condition_id": "cid-dca-pass",
    "question": "Bitcoin Up or Down Test",
    "slug": f"btc-updown-5m-{slot}",
    "start_ts": slot,
    "end_ts": slot + 300,
    "up_asset": "UP",
    "down_asset": "DOWN",
}
bot.markets[market["condition_id"]] = market
bot.persist_market(market)

# SAFE67 entry at .68 with +.07 momentum for both variants.
ms = bot.now_ms()
fresh_book("UP", .67, .68)
fresh_book("DOWN", .31, .32)
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .61), (ms - 3000, .64), (ms, .68)
])
bot.price_history[market["condition_id"]]["DOWN"].extend([
    (ms - 6000, .39), (ms - 3000, .36), (ms, .32)
])
asyncio.run(bot.evaluate_variant(market, A, 30.0))
asyncio.run(bot.evaluate_variant(market, B, 30.0))

for v in (A, B):
    pos = bot.position_totals(market["condition_id"], v["name"])
    assert abs(pos["bought"] - 5.0) < 1e-9
    assert abs(pos["remaining"] - 5.0) < 1e-9

# A never adds, regardless of later movement.
ms = bot.now_ms()
fresh_book("UP", .75, .76)
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .68), (ms - 3000, .71), (ms, .76)
])
asyncio.run(bot.evaluate_variant(market, A, 55.0))
assert abs(bot.position_totals(market["condition_id"], A["name"])["bought"] - 5.0) < 1e-9

# B falls to .50: arm only, do NOT buy on the arm tick.
ms = bot.now_ms()
fresh_book("UP", .49, .50)
bot.price_history[market["condition_id"]]["UP"].clear()
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .58), (ms - 3000, .54), (ms, .50)
])
asyncio.run(bot.evaluate_variant(market, B, 70.0))
pos = bot.position_totals(market["condition_id"], B["name"])
assert abs(pos["bought"] - 5.0) < 1e-9
st = bot.get_variant_state(market["condition_id"], B)
assert st["dca_armed"] is True
with bot.db() as c:
    e = c.execute(
        "SELECT * FROM dca_events WHERE condition_id=? AND variant=?",
        (market["condition_id"], B["name"]),
    ).fetchone()
assert e is not None and e["filled_ms"] is None
assert abs(e["armed_ask"] - .50) < 1e-9

# Falling knife continues: no DCA while momentum is negative.
ms = bot.now_ms()
fresh_book("UP", .44, .45)
bot.price_history[market["condition_id"]]["UP"].clear()
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .53), (ms - 3000, .49), (ms, .45)
])
asyncio.run(bot.evaluate_variant(market, B, 80.0))
assert abs(bot.position_totals(market["condition_id"], B["name"])["bought"] - 5.0) < 1e-9

# Rebound: ask .55, two-tick momentum +.06 => one DCA buy of 5.
ms = bot.now_ms()
fresh_book("UP", .54, .55)
bot.price_history[market["condition_id"]]["UP"].clear()
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .49), (ms - 3000, .52), (ms, .55)
])
asyncio.run(bot.evaluate_variant(market, B, 90.0))
pos = bot.position_totals(market["condition_id"], B["name"])
assert abs(pos["bought"] - 10.0) < 1e-9
assert pos["dca_trades"] == 1
with bot.db() as c:
    e = c.execute(
        "SELECT * FROM dca_events WHERE condition_id=? AND variant=?",
        (market["condition_id"], B["name"]),
    ).fetchone()
assert e["filled_ms"] is not None
assert abs(e["filled_ask"] - .55) < 1e-9
assert abs(e["filled_momentum"] - .06) < 1e-9

# No third buy.
ms = bot.now_ms()
fresh_book("UP", .39, .40)
bot.price_history[market["condition_id"]]["UP"].extend([
    (ms - 6000, .30), (ms - 3000, .34), (ms, .40)
])
asyncio.run(bot.evaluate_variant(market, B, 100.0))
assert abs(bot.position_totals(market["condition_id"], B["name"])["bought"] - 10.0) < 1e-9

# Deadline test: arm before 120, rebound after 120 -> no DCA.
market2 = dict(market)
market2.update({
    "condition_id": "cid-dca-deadline",
    "slug": f"btc-updown-5m-{slot+300}",
    "start_ts": slot+300,
    "end_ts": slot+600,
    "up_asset": "UP2",
    "down_asset": "DOWN2",
})
bot.markets[market2["condition_id"]] = market2
bot.persist_market(market2)
ms = bot.now_ms()
fresh_book("UP2", .67, .68)
fresh_book("DOWN2", .31, .32)
bot.price_history[market2["condition_id"]]["UP2"].extend([
    (ms - 6000, .61), (ms - 3000, .64), (ms, .68)
])
bot.price_history[market2["condition_id"]]["DOWN2"].extend([
    (ms - 6000, .39), (ms - 3000, .36), (ms, .32)
])
asyncio.run(bot.evaluate_variant(market2, B, 30.0))

ms = bot.now_ms()
fresh_book("UP2", .49, .50)
bot.price_history[market2["condition_id"]]["UP2"].clear()
bot.price_history[market2["condition_id"]]["UP2"].extend([
    (ms - 6000, .58), (ms - 3000, .54), (ms, .50)
])
asyncio.run(bot.evaluate_variant(market2, B, 110.0))
assert bot.get_variant_state(market2["condition_id"], B)["dca_armed"]

ms = bot.now_ms()
fresh_book("UP2", .54, .55)
bot.price_history[market2["condition_id"]]["UP2"].clear()
bot.price_history[market2["condition_id"]]["UP2"].extend([
    (ms - 6000, .49), (ms - 3000, .52), (ms, .55)
])
asyncio.run(bot.evaluate_variant(market2, B, 121.0))
assert abs(bot.position_totals(market2["condition_id"], B["name"])["bought"] - 5.0) < 1e-9

# Settlement accounting: A owns 5 Up; B owns 10 Up.
asyncio.run(bot.settle_market(market["condition_id"], "UP", "Up"))
with bot.db() as c:
    ra = c.execute(
        "SELECT * FROM market_results WHERE condition_id=? AND variant=?",
        (market["condition_id"], A["name"]),
    ).fetchone()
    rb = c.execute(
        "SELECT * FROM market_results WHERE condition_id=? AND variant=?",
        (market["condition_id"], B["name"]),
    ).fetchone()
assert abs(ra["payout"] - 5.0) < 1e-9
assert abs(rb["payout"] - 10.0) < 1e-9
assert ra["stopped_out"] == 0 and rb["stopped_out"] == 0

# Hourly ZIP includes dedicated DCA events and trajectories.
hour_start = slot - (slot % 3600)
path, summaries = bot.make_report(hour_start, hour_start + 3600)
assert len(summaries) == 2
with zipfile.ZipFile(path, "r") as z:
    names = set(z.namelist())
required = {
    "variants_summary.csv",
    "markets.csv",
    "report.txt",
    "A_safe67_base_5sh/summary.csv",
    "A_safe67_base_5sh/paper_trades.csv",
    "A_safe67_base_5sh/dca_events.csv",
    "A_safe67_base_5sh/position_trajectory.csv",
    "B_safe67_reversal_dca_5plus5/summary.csv",
    "B_safe67_reversal_dca_5plus5/paper_trades.csv",
    "B_safe67_reversal_dca_5plus5/dca_events.csv",
    "B_safe67_reversal_dca_5plus5/position_trajectory.csv",
}
assert required.issubset(names), required - names

print("SAFE67 BASE vs REVERSAL DCA regression: OK")
