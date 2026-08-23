import os
import tempfile
import importlib.util
from pathlib import Path

# Isolate the regression DB from Render/local real data.
tmp = tempfile.mkdtemp(prefix="m03_abce_test_")
os.environ["DATA_DIR"] = tmp
os.environ["PAPER_START_BALANCE"] = "500"
os.environ["CONF_MIN"] = "65"
os.environ["HEDGE_START_SHARES"] = "20"
os.environ["HEDGE_MAX_LOSS"] = "10"
os.environ["HEDGE_MIN_UPSIDE"] = "2"
os.environ["HEDGE_MIN_ORDER_SHARES"] = "0.05"

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
]
for name in names:
    assert abs(bot.paper_cash(name) - 500.0) < 1e-9

# Exact base parameters and E = C + independent hedge layer.
v3 = bot.STRATEGY_BY_NAME["M03_V3_NOSW90"]
v2 = bot.STRATEGY_BY_NAME["M03_V2_LOCK"]
v5 = bot.STRATEGY_BY_NAME["M03_V5_DYNAMIC"]
hedge = bot.STRATEGY_BY_NAME["M03_V5_DYNAMIC_HEDGE"]

assert (v3["entry_move"], v3["pyramid_step"], v3["lookback"], v3["max_buys_side"]) == (0.03, 0.08, 2, 5)
assert v3["entry_cutoff_sec"] == 90 and v3["allow_switch"] is False
assert v2["entry_price_min"] == 0.55 and v2["entry_price_max"] == 0.75
assert v2["momentum_cap"] == 0.30 and v2["max_buys_side"] == 6 and v2["allow_switch"] is False
assert v5["switch_move"] == 0.03 and v5["max_buys_side"] == 5 and v5["dynamic_switch_v5"] is True
for key in ("entry_move", "pyramid_step", "lookback", "switch_move", "max_buys_side", "allow_switch", "dynamic_switch_v5"):
    assert hedge[key] == v5[key]
assert hedge["risk_hedge"] is True

# Shadow state remains independent and still requires accepted ENTRY/SWITCH before PYRAMID.
cid = "test-market"
asset = "UP"
f64 = {"data_age_ms": 10, "confidence": 64.0}
f66 = {"data_age_ms": 10, "confidence": 66.0}

ok, _ = bot.exact_shadow_decision(cid, v3["name"], asset, "ENTRY", f64)
assert ok is False
ok, reason = bot.exact_shadow_decision(cid, v3["name"], asset, "PYRAMID", f66)
assert ok is False and reason == "no_shadow_position"

ok, _ = bot.exact_shadow_decision(cid, v2["name"], asset, "ENTRY", f66)
assert ok is True
assert asset in bot.shadow_accepted_sides[(cid, v2["name"])]
assert asset not in bot.shadow_accepted_sides[(cid, v3["name"])]

# A real accepted PAPER fill affects only its own account.
snap = {
    "asks": {0.60: 100.0},
    "bids": {},
    "received_ms": 1000,
    "captured_ms": 1000,
}
base = bot.execute_baseline_from_snapshot("m1", v2, asset, "Up", "ENTRY", snap)
assert base and abs(base["filled"] - 10.0) < 1e-9
assert bot.paper_execute_from_baseline(v2, "m1", asset, "Up", "ENTRY", base) is True
assert bot.paper_cash(v2["name"]) < 500.0
assert abs(bot.paper_cash(v3["name"]) - 500.0) < 1e-9
assert abs(bot.paper_cash(v5["name"]) - 500.0) < 1e-9
assert abs(bot.paper_cash(hedge["name"]) - 500.0) < 1e-9

# V3 still stops all new buys after 90 sec.
bot.price_history["cutoff"]["UP"].extend([(1, 0.50), (2, 0.52), (3, 0.56)])
bot.price_history["cutoff"]["DOWN"].extend([(1, 0.50), (2, 0.48), (3, 0.44)])
tick = {
    "sides": [("UP", "Up"), ("DOWN", "Down")],
    "books": {
        "UP": {"asks": {0.56: 100}, "bids": {}, "received_ms": 1, "captured_ms": 1},
        "DOWN": {"asks": {0.44: 100}, "bids": {}, "received_ms": 1, "captured_ms": 1},
    },
}
assert bot.candidate_for_strategy("cutoff", v3, 91.0, tick) is None

# ---------------------------------------------------------------------------
# E hedge regression using the concrete example discussed:
# 10 Up @ .57 + 10 Up @ .71, then opposite Down ask .30.
# Before 20 shares: no hedge. After 20 shares: buy ~4.55 Down shares so the
# settlement PnL if Up loses is near -$10, while Up-win PnL remains > +$2.
# ---------------------------------------------------------------------------
market = {
    "condition_id": "hedge-market",
    "up_asset": "UP_H",
    "down_asset": "DOWN_H",
}

entry_book = {"asks": {0.57: 100}, "bids": {}, "received_ms": 1000, "captured_ms": 1000}
entry = bot.execute_baseline_from_snapshot("hedge-market", hedge, "UP_H", "Up", "ENTRY", entry_book)
assert bot.paper_execute_from_baseline(hedge, "hedge-market", "UP_H", "Up", "ENTRY", entry) is True

hedge_tick = {
    "books": {
        "UP_H": {"asks": {0.71: 100}, "bids": {}, "received_ms": 1000, "captured_ms": 1000},
        "DOWN_H": {"asks": {0.30: 100}, "bids": {}, "received_ms": 1000, "captured_ms": 1000},
    }
}
assert bot.maybe_execute_hedge(market, hedge, hedge_tick) is None

pyr_book = {"asks": {0.71: 100}, "bids": {}, "received_ms": 1000, "captured_ms": 1000}
pyr = bot.execute_baseline_from_snapshot("hedge-market", hedge, "UP_H", "Up", "PYRAMID", pyr_book)
assert bot.paper_execute_from_baseline(hedge, "hedge-market", "UP_H", "Up", "PYRAMID", pyr) is True

# HEDGE must not advance V5's opposite-side baseline buy counter.
before_down_buys = bot.get_st("hedge-market", hedge["name"])["buys"]["DOWN_H"]
hr = bot.maybe_execute_hedge(market, hedge, hedge_tick)
after_down_buys = bot.get_st("hedge-market", hedge["name"])["buys"]["DOWN_H"]
assert hr is not None
assert before_down_buys == after_down_buys == 0
assert 4.4 < hr["filled"] < 4.7
assert -10.01 <= hr["pnl_if_primary_loses_after"] <= -9.99
assert hr["pnl_if_primary_wins_after"] >= 2.0 - 1e-5

ex = bot.paper_market_exposure(hedge["name"], "hedge-market")
assert abs(ex["shares"]["UP_H"] - 20.0) < 1e-9
assert 4.4 < ex["shares"]["DOWN_H"] < 4.7
with bot.db() as c:
    hedge_rows = c.execute(
        "SELECT COUNT(*) c FROM trades WHERE strategy=? AND condition_id=? AND signal_type='HEDGE'",
        (hedge["name"], "hedge-market"),
    ).fetchone()["c"]
assert hedge_rows == 1
# HEDGE shares alone must never count as a normal V5 position for later PYRAMID permission.
assert bot.paper_has_asset_position(hedge["name"], "hedge-market", "DOWN_H") is False

# Once the risk floor is reached, the same unchanged tick must not over-hedge.
assert bot.maybe_execute_hedge(market, hedge, hedge_tick) is None

# Settlement credits only the matching strategy/account.
import asyncio
asyncio.run(bot.settle_market("m1", asset, "Up"))
assert abs(bot.paper_cash(v2["name"]) - (500.0 - base["total"] + 10.0)) < 1e-6
assert abs(bot.paper_cash(v3["name"]) - 500.0) < 1e-9
assert abs(bot.paper_cash(v5["name"]) - 500.0) < 1e-9

s2 = bot.account_stats(v2["name"])
se = bot.account_stats(hedge["name"])
assert s2["traded_markets"] == 1 and s2["wins"] == 1
assert se["hedge_trades"] == 1
assert se["hedge_cost"] > 0

print("four-way CONF65 + V5 hedge regression: OK")
