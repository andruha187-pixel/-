import os
import time
import asyncio
import tempfile
import importlib.util
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gate64_test_")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bot", HERE / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

assert bot.STRATEGY_NAME == "M03_V2_GATE64_X2"
assert bot.ENTRY_MOVE == 0.03
assert bot.PYRAMID_STEP == 0.08
assert bot.LOOKBACK_TICKS == 2
assert bot.FIRST_SIGNAL_PRICE_MIN == 0.64
assert bot.FIRST_SIGNAL_PRICE_MAX == 0.75
assert bot.MOMENTUM_CAP == 0.30
assert bot.MAX_BUYS_SIDE == 2
assert bot.STRATEGY["allow_switch"] is False


def snap(asset, ask):
    bot.books[asset] = {
        "asks": {ask: 100.0},
        "bids": {},
        "received_ms": bot.now_ms(),
        "source": "test",
    }


def set_hist(cid, asset, values):
    bot.price_history[cid][asset].clear()
    base = bot.now_ms() - len(values) * 3000
    for i, value in enumerate(values):
        bot.price_history[cid][asset].append((base + i * 3000, value))


# 1) First raw signal below 0.64 => permanent blacklist.
market1 = {
    "condition_id": "low-gate",
    "question": "Bitcoin Up or Down",
    "slug": "btc-updown-5m-1",
    "start_ts": int(time.time()) - 30,
    "end_ts": int(time.time()) + 270,
    "up_asset": "UP1",
    "down_asset": "DN1",
}
bot.markets[market1["condition_id"]] = market1
snap("UP1", 0.62)
snap("DN1", 0.39)
set_hist("low-gate", "UP1", [0.58, 0.59, 0.62])
set_hist("low-gate", "DN1", [0.42, 0.41, 0.39])
asyncio.run(bot.evaluate_variant(market1, bot.STRATEGY, 30))
assert bot.get_variant_state("low-gate")["gate_decided"] is True
assert bot.get_variant_state("low-gate")["gate_passed"] is False
with bot.db() as c:
    assert c.execute("SELECT COUNT(*) c FROM paper_trades WHERE condition_id='low-gate'").fetchone()["c"] == 0
    gd = c.execute("SELECT * FROM gate_decisions WHERE condition_id='low-gate'").fetchone()
assert gd["reason"] == "FIRST_SIGNAL_PRICE_LOW" and gd["passed"] == 0

# Price later rises into band; blacklist must remain.
snap("UP1", 0.66)
set_hist("low-gate", "UP1", [0.61, 0.62, 0.66])
asyncio.run(bot.evaluate_variant(market1, bot.STRATEGY, 45))
with bot.db() as c:
    assert c.execute("SELECT COUNT(*) c FROM paper_trades WHERE condition_id='low-gate'").fetchone()["c"] == 0

# 2) First raw signal in 0.64..0.75 => entry.
market2 = {
    "condition_id": "pass-gate",
    "question": "Bitcoin Up or Down",
    "slug": "btc-updown-5m-2",
    "start_ts": int(time.time()) - 30,
    "end_ts": int(time.time()) + 270,
    "up_asset": "UP2",
    "down_asset": "DN2",
}
bot.markets[market2["condition_id"]] = market2
snap("UP2", 0.65)
snap("DN2", 0.36)
set_hist("pass-gate", "UP2", [0.60, 0.61, 0.65])
set_hist("pass-gate", "DN2", [0.41, 0.40, 0.36])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 30))
st = bot.get_variant_state("pass-gate")
assert st["gate_passed"] is True
assert st["primary_asset"] == "UP2"
assert st["buys"]["UP2"] == 1

# 3) +0.08 pyramid => second and final buy.
snap("UP2", 0.74)
set_hist("pass-gate", "UP2", [0.68, 0.70, 0.74])
set_hist("pass-gate", "DN2", [0.33, 0.31, 0.27])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 60))
assert bot.get_variant_state("pass-gate")["buys"]["UP2"] == 2

# Even a further rally cannot produce a third buy.
snap("UP2", 0.84)
set_hist("pass-gate", "UP2", [0.77, 0.79, 0.84])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 90))
assert bot.get_variant_state("pass-gate")["buys"]["UP2"] == 2

# No opposite-side switching.
snap("DN2", 0.50)
set_hist("pass-gate", "DN2", [0.42, 0.44, 0.50])
asyncio.run(bot.evaluate_variant(market2, bot.STRATEGY, 100))
with bot.db() as c:
    dn_trades = c.execute(
        "SELECT COUNT(*) c FROM paper_trades WHERE condition_id='pass-gate' AND asset='DN2'"
    ).fetchone()["c"]
assert dn_trades == 0

with bot.db() as c:
    trades = c.execute(
        "SELECT signal_type,filled_shares FROM paper_trades WHERE condition_id='pass-gate' ORDER BY id"
    ).fetchall()
assert [r["signal_type"] for r in trades] == ["ENTRY", "PYRAMID"]
assert all(abs(float(r["filled_shares"]) - 10.0) < 1e-9 for r in trades)

print("M03_V2_GATE64_X2 regression: OK")
