import os
import time
import tempfile
import importlib.util
from pathlib import Path

# Isolate regression data from Render/local data.
tmp = tempfile.mkdtemp(prefix="m03_abcef_test_")
os.environ["DATA_DIR"] = tmp
os.environ["PAPER_START_BALANCE"] = "500"
os.environ["CONF_MIN"] = "65"

# E hedge defaults.
os.environ["HEDGE_START_SHARES"] = "20"
os.environ["HEDGE_MAX_LOSS"] = "10"
os.environ["HEDGE_MIN_UPSIDE"] = "2"
os.environ["HEDGE_MIN_ORDER_SHARES"] = "0.05"

# F pair-hedge defaults.
os.environ["PAIR_LOCKED_PROFIT"] = "0.25"
os.environ["PAIR_HEDGE_CHECK_INTERVAL"] = "0.20"
os.environ["PAIR_DEFAULT_TICK_SIZE"] = "0.01"
os.environ["PAIR_DEFAULT_MIN_ORDER_SIZE"] = "1"
os.environ["PAIR_LIMIT_FILL_REQUIRE_VISIBLE_SIZE"] = "1"

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bot", HERE / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

names = [s["name"] for s in bot.STRATEGIES]
assert names == [
    "M03_V3_NOSW90",
    "M03_V2_LOCK",
    "M03_V5_DYNAMIC",
    "M03_V5_DYNAMIC_HEDGE",
    "M03_V5_DYNAMIC_PAIR_HEDGE",
]
for name in names:
    assert abs(bot.paper_cash(name) - 500.0) < 1e-9

v3 = bot.STRATEGY_BY_NAME["M03_V3_NOSW90"]
v2 = bot.STRATEGY_BY_NAME["M03_V2_LOCK"]
v5 = bot.STRATEGY_BY_NAME["M03_V5_DYNAMIC"]
e = bot.STRATEGY_BY_NAME["M03_V5_DYNAMIC_HEDGE"]
f = bot.STRATEGY_BY_NAME["M03_V5_DYNAMIC_PAIR_HEDGE"]

# C/E/F preserve exactly the normal V5 directional parameters.
for variant in (e, f):
    for key in (
        "entry_move", "pyramid_step", "lookback", "switch_move",
        "max_buys_side", "allow_switch", "dynamic_switch_v5"
    ):
        assert variant[key] == v5[key]
assert e["risk_hedge"] is True
assert f["pair_hedge"] is True

# Existing shadow logic still works.
cid = "shadow-test"
asset = "UP"
f64 = {"data_age_ms": 10, "confidence": 64.0}
f66 = {"data_age_ms": 10, "confidence": 66.0}
ok, _ = bot.exact_shadow_decision(cid, v3["name"], asset, "ENTRY", f64)
assert ok is False
ok, reason = bot.exact_shadow_decision(cid, v3["name"], asset, "PYRAMID", f66)
assert ok is False and reason == "no_shadow_position"
ok, _ = bot.exact_shadow_decision(cid, v2["name"], asset, "ENTRY", f66)
assert ok is True

# ---------------------------------------------------------------------------
# E regression: old loss-floor hedge remains available and isolated.
# ---------------------------------------------------------------------------
market_e = {
    "condition_id": "hedge-market",
    "up_asset": "UP_E",
    "down_asset": "DOWN_E",
    "end_ts": int(time.time()) + 300,
}
entry_book = {
    "asks": {0.57: 100}, "bids": {},
    "received_ms": 1000, "captured_ms": 1000,
    "min_order_size": 5, "tick_size": 0.01,
}
entry = bot.execute_baseline_from_snapshot(
    "hedge-market", e, "UP_E", "Up", "ENTRY", entry_book
)
trade1 = bot.paper_execute_from_baseline(
    e, "hedge-market", "UP_E", "Up", "ENTRY", entry
)
assert trade1 and abs(trade1["filled"] - 10.0) < 1e-9

hedge_tick = {
    "books": {
        "UP_E": {
            "asks": {0.71: 100}, "bids": {},
            "received_ms": 1000, "captured_ms": 1000,
            "min_order_size": 5, "tick_size": 0.01,
        },
        "DOWN_E": {
            "asks": {0.30: 100}, "bids": {},
            "received_ms": 1000, "captured_ms": 1000,
            "min_order_size": 5, "tick_size": 0.01,
        },
    }
}
assert bot.maybe_execute_hedge(market_e, e, hedge_tick) is None

pyr_book = {
    "asks": {0.71: 100}, "bids": {},
    "received_ms": 1000, "captured_ms": 1000,
    "min_order_size": 5, "tick_size": 0.01,
}
pyr = bot.execute_baseline_from_snapshot(
    "hedge-market", e, "UP_E", "Up", "PYRAMID", pyr_book
)
trade2 = bot.paper_execute_from_baseline(
    e, "hedge-market", "UP_E", "Up", "PYRAMID", pyr
)
assert trade2
hr = bot.maybe_execute_hedge(market_e, e, hedge_tick)
assert hr is not None
assert 4.4 < hr["filled"] < 4.7
assert -10.01 <= hr["pnl_if_primary_loses_after"] <= -9.99
assert bot.paper_has_asset_position(e["name"], "hedge-market", "DOWN_E") is False

# ---------------------------------------------------------------------------
# F LIMIT regression.
# Buy 10 Up @ .60. Down is .41, so no immediate pair is possible.
# F must place an equal 10-share resting hedge at the highest tick that still
# locks at least +$0.25 if it later fills as maker.
# ---------------------------------------------------------------------------
market_f = {
    "condition_id": "pair-limit-market",
    "up_asset": "UP_F",
    "down_asset": "DOWN_F",
    "end_ts": int(time.time()) + 300,
}
up_snap = {
    "asks": {0.60: 100}, "bids": {},
    "received_ms": bot.now_ms(), "captured_ms": bot.now_ms(),
    "min_order_size": 5, "tick_size": 0.01,
}
down_snap = {
    "asks": {0.41: 100}, "bids": {},
    "received_ms": bot.now_ms(), "captured_ms": bot.now_ms(),
    "min_order_size": 5, "tick_size": 0.01,
}
tick_f = {
    "books": {"UP_F": up_snap, "DOWN_F": down_snap},
    "sides": [("UP_F", "Up"), ("DOWN_F", "Down")],
}

base_f = bot.execute_baseline_from_snapshot(
    "pair-limit-market", f, "UP_F", "Up", "ENTRY", up_snap
)
paper_f = bot.paper_execute_from_baseline(
    f, "pair-limit-market", "UP_F", "Up", "ENTRY", base_f
)
assert paper_f and abs(paper_f["filled"] - 10.0) < 1e-9

order = bot.create_pair_hedge_order(market_f, f, paper_f, tick_f)
assert order and order["status"] == "PENDING"
assert abs(order["requested_shares"] - 10.0) < 1e-9
# 10 Up @ .60 has total taker cost 6.168; target +.25 => max maker
# price 0.3582, rounded DOWN to the 0.01 tick => 0.35.
assert abs(order["limit_price"] - 0.35) < 1e-9
assert order["locked_if_filled"] >= 0.25
assert abs(bot._pair_order_reserved_cash(f["name"]) - 3.50) < 1e-8

# At 0.36 the resting 0.35 bid must not fill.
bot.books["DOWN_F"] = {
    "asks": {0.36: 20.0}, "bids": {0.35: 50.0},
    "received_ms": bot.now_ms(), "source": "test",
    "min_order_size": 5, "tick_size": 0.01,
}
assert bot.process_pair_hedges_for_market(market_f) == 0

# At/below 0.35 the paper maker proxy fills against visible crossing size.
# First 4 shares, then the remaining 6.
bot.books["DOWN_F"] = {
    "asks": {0.35: 4.0}, "bids": {},
    "received_ms": bot.now_ms(), "source": "test",
    "min_order_size": 5, "tick_size": 0.01,
}
assert bot.process_pair_hedges_for_market(market_f) == 1
with bot.db() as c:
    r = c.execute(
        "SELECT * FROM pair_hedges WHERE id=?", (order["order_id"],)
    ).fetchone()
assert r["status"] == "PARTIAL"
assert abs(r["filled_shares"] - 4.0) < 1e-8

bot.books["DOWN_F"] = {
    "asks": {0.34: 6.0}, "bids": {},
    "received_ms": bot.now_ms(), "source": "test",
    "min_order_size": 5, "tick_size": 0.01,
}
assert bot.process_pair_hedges_for_market(market_f) == 1
with bot.db() as c:
    r = c.execute(
        "SELECT * FROM pair_hedges WHERE id=?", (order["order_id"],)
    ).fetchone()
assert r["status"] == "FILLED"
assert r["fill_mode"] == "LIMIT"
assert abs(r["filled_shares"] - 10.0) < 1e-8
assert abs(r["hedge_total_cost"] - 3.50) < 1e-8
assert r["locked_pnl"] >= 0.25 - 1e-8

# Maker hedge carries zero fee and must not become a normal V5 position.
with bot.db() as c:
    pair_fee = c.execute(
        """SELECT COALESCE(SUM(fee),0) x FROM trades
           WHERE strategy=? AND condition_id=? AND signal_type='PAIR_HEDGE_LIMIT'""",
        (f["name"], "pair-limit-market"),
    ).fetchone()["x"]
assert abs(pair_fee) < 1e-12
assert bot.paper_has_asset_position(f["name"], "pair-limit-market", "DOWN_F") is False
assert abs(bot._pair_order_reserved_cash(f["name"])) < 1e-9

# Either outcome now has the same locked PnL for this fully paired lot.
with bot.db() as c:
    rows = c.execute(
        "SELECT asset,filled_shares,total_cost FROM trades WHERE strategy=? AND condition_id=?",
        (f["name"], "pair-limit-market"),
    ).fetchall()
cost = sum(float(x["total_cost"]) for x in rows)
up_sh = sum(float(x["filled_shares"]) for x in rows if x["asset"] == "UP_F")
dn_sh = sum(float(x["filled_shares"]) for x in rows if x["asset"] == "DOWN_F")
assert abs(up_sh - 10.0) < 1e-8 and abs(dn_sh - 10.0) < 1e-8
assert up_sh - cost >= 0.25 - 1e-8
assert dn_sh - cost >= 0.25 - 1e-8

# ---------------------------------------------------------------------------
# F FOK fallback regression.
# If the opposite ask is already cheap enough when the base trade fills,
# post-only would cross. F should take the whole hedge immediately only when
# visible depth + taker fee still preserve the +$0.25 target.
# ---------------------------------------------------------------------------
market_fok = {
    "condition_id": "pair-fok-market",
    "up_asset": "UP_Q",
    "down_asset": "DOWN_Q",
    "end_ts": int(time.time()) + 300,
}
up_q = {
    "asks": {0.60: 100}, "bids": {},
    "received_ms": bot.now_ms(), "captured_ms": bot.now_ms(),
    "min_order_size": 5, "tick_size": 0.01,
}
down_q = {
    "asks": {0.33: 20}, "bids": {},
    "received_ms": bot.now_ms(), "captured_ms": bot.now_ms(),
    "min_order_size": 5, "tick_size": 0.01,
}
tick_q = {
    "books": {"UP_Q": up_q, "DOWN_Q": down_q},
    "sides": [("UP_Q", "Up"), ("DOWN_Q", "Down")],
}
base_q = bot.execute_baseline_from_snapshot(
    "pair-fok-market", f, "UP_Q", "Up", "ENTRY", up_q
)
paper_q = bot.paper_execute_from_baseline(
    f, "pair-fok-market", "UP_Q", "Up", "ENTRY", base_q
)
assert paper_q
fq = bot.create_pair_hedge_order(market_fok, f, paper_q, tick_q)
assert fq and fq["status"] == "FILLED" and fq["mode"] == "FOK"
assert fq["locked_pnl"] >= 0.25 - 1e-8

with bot.db() as c:
    rr = c.execute(
        "SELECT * FROM pair_hedges WHERE strategy=? AND condition_id=?",
        (f["name"], "pair-fok-market"),
    ).fetchone()
assert rr["status"] == "FILLED" and rr["fill_mode"] == "FOK"
assert rr["hedge_total_cost"] > 0
assert rr["locked_pnl"] >= 0.25 - 1e-8

stats_f = bot.account_stats(f["name"])
assert stats_f["pair_orders"] == 2
assert stats_f["pair_filled"] == 2
assert stats_f["pair_limit"] == 1
assert stats_f["pair_fok"] == 1
assert stats_f["pair_pending"] == 0

print("five-way CONF65 + E loss-floor + F pair-hedge regression: OK")
