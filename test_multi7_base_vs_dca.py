import os
import time
import asyncio
import tempfile
import importlib.util
import zipfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="safe67_multi7_dca_")
os.environ["DATA_DIR"] = tmp
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["SYMBOLS"] = "BTC,XRP,BNB,SOL,ETH,DOGE,HYPE"
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

EXPECTED = ["BTC", "XRP", "BNB", "SOL", "ETH", "DOGE", "HYPE"]
assert bot.SYMBOLS == EXPECTED
assert len(bot.STRATEGIES) == 14
assert set(bot.STRATEGIES_BY_SYMBOL) == set(EXPECTED)

for symbol in EXPECTED:
    pair = bot.STRATEGIES_BY_SYMBOL[symbol]
    assert len(pair) == 2
    a, b = pair
    assert a["symbol"] == b["symbol"] == symbol
    assert a["name"] == f"{symbol}_A_SAFE67_BASE"
    assert b["name"] == f"{symbol}_B_SAFE67_REVERSAL_DCA"
    assert not a["dca_enabled"] and a["max_buys_side"] == 1
    assert b["dca_enabled"] and b["max_buys_side"] == 2
    assert a["stop_loss_price"] is None and b["stop_loss_price"] is None
    assert bot.paper_cash(a["name"]) == 500
    assert bot.paper_cash(b["name"]) == 500

# Exact strategy constants.
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
assert bot.DCA_ARM_PRICE == 0.50
assert bot.DCA_MAX_BUY_PRICE == 0.60
assert bot.DCA_REBOUND_MOM == 0.05
assert bot.DCA_DEADLINE_SEC == 120

source = (here / "main.py").read_text(encoding="utf-8")
assert "async def stop_loss_loop" not in source
assert "STOP_LOSS_PRICE" not in source
assert "PYRAMID_STEP" not in source
assert "PYRAMID_ORDER_SIZE" not in source

# Prefix / symbol parser contract.
slot = (int(time.time()) // 300) * 300
for symbol in EXPECTED:
    cfg = bot.ASSET_CONFIG[symbol]
    slug = f"{cfg['prefix']}-{slot}"
    raw = {
        "conditionId": f"cid-parse-{symbol}",
        "question": f"{cfg['label']} Up or Down",
        "slug": slug,
        "outcomes": '["Up","Down"]',
        "clobTokenIds": f'["{symbol}UP","{symbol}DN"]',
    }
    m = bot.parse_market_from_event(raw, {"title": raw["question"], "slug": slug}, symbol)
    assert m is not None
    assert m["symbol"] == symbol
    assert bot.market_symbol(m) == symbol
    assert bot.strategies_for_market(m)[0]["symbol"] == symbol


def fresh_book(asset, bid, ask, size=100.0):
    bot.books[asset] = {
        "bids": {float(bid): float(size)},
        "asks": {float(ask): float(size)},
        "received_ms": bot.now_ms(),
        "source": "test",
    }


def make_market(symbol, suffix, offset=0):
    cfg = bot.ASSET_CONFIG[symbol]
    s = slot + offset
    return {
        "condition_id": f"cid-{symbol}-{suffix}",
        "symbol": symbol,
        "question": f"{cfg['label']} Up or Down Test",
        "slug": f"{cfg['prefix']}-{s}",
        "start_ts": s,
        "end_ts": s + 300,
        "up_asset": f"{symbol}UP-{suffix}",
        "down_asset": f"{symbol}DN-{suffix}",
    }


def seed_entry(m):
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .67, .68)
    fresh_book(m["down_asset"], .31, .32)
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .61), (ms - 3000, .64), (ms, .68)
    ])
    bot.price_history[m["condition_id"]][m["down_asset"]].extend([
        (ms - 6000, .39), (ms - 3000, .36), (ms, .32)
    ])


# Test the full two-stage DCA path independently on BTC and XRP.
for n, symbol in enumerate(["BTC", "XRP"]):
    m = make_market(symbol, "dca", n * 300)
    bot.markets[m["condition_id"]] = m
    bot.persist_market(m)
    A, B = bot.STRATEGIES_BY_SYMBOL[symbol]

    seed_entry(m)
    asyncio.run(bot.evaluate_variant(m, A, 30.0))
    asyncio.run(bot.evaluate_variant(m, B, 30.0))
    assert bot.position_totals(m["condition_id"], A["name"])["bought"] == 5
    assert bot.position_totals(m["condition_id"], B["name"])["bought"] == 5

    # A never adds when price rises or falls.
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .75, .76)
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .68), (ms - 3000, .71), (ms, .76)
    ])
    asyncio.run(bot.evaluate_variant(m, A, 50.0))
    assert bot.position_totals(m["condition_id"], A["name"])["bought"] == 5

    # B: ask .50 only arms; no buy.
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .49, .50)
    bot.price_history[m["condition_id"]][m["up_asset"]].clear()
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .58), (ms - 3000, .54), (ms, .50)
    ])
    asyncio.run(bot.evaluate_variant(m, B, 70.0))
    assert bot.position_totals(m["condition_id"], B["name"])["bought"] == 5
    assert bot.get_variant_state(m["condition_id"], B)["dca_armed"]

    # Still falling -> no DCA.
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .44, .45)
    bot.price_history[m["condition_id"]][m["up_asset"]].clear()
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .53), (ms - 3000, .49), (ms, .45)
    ])
    asyncio.run(bot.evaluate_variant(m, B, 80.0))
    assert bot.position_totals(m["condition_id"], B["name"])["bought"] == 5

    # Later rebound +.06 at ask .55 -> exactly one 5sh DCA.
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .54, .55)
    bot.price_history[m["condition_id"]][m["up_asset"]].clear()
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .49), (ms - 3000, .52), (ms, .55)
    ])
    asyncio.run(bot.evaluate_variant(m, B, 90.0))
    bp = bot.position_totals(m["condition_id"], B["name"])
    assert bp["bought"] == 10 and bp["dca_trades"] == 1

    # No third buy.
    ms = bot.now_ms()
    fresh_book(m["up_asset"], .58, .59)
    bot.price_history[m["condition_id"]][m["up_asset"]].extend([
        (ms - 6000, .50), (ms - 3000, .54), (ms, .59)
    ])
    asyncio.run(bot.evaluate_variant(m, B, 105.0))
    assert bot.position_totals(m["condition_id"], B["name"])["bought"] == 10

# Settlement must update only the BTC pair, not create results for other token strategies.
btc_m = bot.markets["cid-BTC-dca"]
asyncio.run(bot.settle_market(btc_m["condition_id"], btc_m["up_asset"], "Up"))
with bot.db() as c:
    btc_results = c.execute(
        "SELECT variant FROM market_results WHERE condition_id=? ORDER BY variant",
        (btc_m["condition_id"],)
    ).fetchall()
assert {r["variant"] for r in btc_results} == {
    "BTC_A_SAFE67_BASE", "BTC_B_SAFE67_REVERSAL_DCA"
}

# Persisted market carries symbol.
with bot.db() as c:
    row = c.execute(
        "SELECT symbol FROM discovered_markets WHERE condition_id=?",
        (btc_m["condition_id"],)
    ).fetchone()
assert row["symbol"] == "BTC"

# Hourly report has two separate strategy folders for every configured token.
hour_start = slot - (slot % 3600)
path, summaries = bot.make_report(hour_start, hour_start + 3600)
assert len(summaries) == 14
with zipfile.ZipFile(path, "r") as z:
    names = set(z.namelist())

required = {"variants_summary.csv", "markets.csv", "report.txt"}
for symbol in EXPECTED:
    required.update({
        f"{symbol}/A_safe67_base_5sh/summary.csv",
        f"{symbol}/A_safe67_base_5sh/paper_trades.csv",
        f"{symbol}/A_safe67_base_5sh/dca_events.csv",
        f"{symbol}/A_safe67_base_5sh/position_trajectory.csv",
        f"{symbol}/B_safe67_reversal_dca_5plus5/summary.csv",
        f"{symbol}/B_safe67_reversal_dca_5plus5/paper_trades.csv",
        f"{symbol}/B_safe67_reversal_dca_5plus5/dca_events.csv",
        f"{symbol}/B_safe67_reversal_dca_5plus5/position_trajectory.csv",
    })
assert required.issubset(names), required - names

print("MULTI7 SAFE67 BASE vs REVERSAL DCA regression: OK")
