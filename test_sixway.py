import os
import time
import tempfile
import importlib.util
from pathlib import Path

TMP = tempfile.mkdtemp(prefix='m03_sixway_test_')
os.environ['DATA_DIR'] = TMP
os.environ['PAPER_START_BALANCE'] = '500'
os.environ['CONF_MIN'] = '65'
os.environ['PAIR_LOCKED_PROFIT'] = '0.25'
os.environ['SMART_HEDGE_RATIO'] = '0.65'
os.environ['SMART_MAX_LOSS'] = '6'
os.environ['SMART_RISK_START_SHARES'] = '16'
os.environ['SMART_MIN_UPSIDE'] = '1'
os.environ['PAIR_DEFAULT_MIN_ORDER_SIZE'] = '1'
os.environ['PAIR_DEFAULT_TICK_SIZE'] = '0.01'

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bot', HERE / 'main.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

names = [s['name'] for s in bot.STRATEGIES]
assert names == [
    'M03_V3_NOSW90',
    'M03_V2_LOCK',
    'M03_V5_DYNAMIC',
    'M03_V5_DYNAMIC_HEDGE',
    'M03_V5_DYNAMIC_PAIR_HEDGE',
    'M03_V5_DYNAMIC_SMART65',
]
for name in names:
    assert abs(bot.paper_cash(name) - 500.0) < 1e-9

c = bot.STRATEGY_BY_NAME['M03_V5_DYNAMIC']
f = bot.STRATEGY_BY_NAME['M03_V5_DYNAMIC_PAIR_HEDGE']
g = bot.STRATEGY_BY_NAME['M03_V5_DYNAMIC_SMART65']

# C/F/G directional engine parameters remain identical.
for variant in (f, g):
    for k in ('entry_move','pyramid_step','lookback','switch_move','max_buys_side','allow_switch','dynamic_switch_v5'):
        assert variant[k] == c[k]
assert abs(f['pair_hedge_ratio'] - 1.0) < 1e-12
assert abs(g['pair_hedge_ratio'] - 0.65) < 1e-12
assert g['smart_size'] is True and g['smart_risk_cap'] is True

# G dynamic actual lot sizes.
assert bot.smart_order_size(g, 0.55) == 10
assert bot.smart_order_size(g, 0.62) == 10
assert bot.smart_order_size(g, 0.63) == 8
assert bot.smart_order_size(g, 0.70) == 8
assert bot.smart_order_size(g, 0.71) == 6
assert bot.smart_order_size(g, 0.78) == 6
assert bot.smart_order_size(g, 0.79) == 4
assert bot.smart_order_size(g, 0.82) == 4
assert bot.smart_order_size(g, 0.83) == 0
assert bot.smart_order_size(c, 0.90) == bot.ORDER_SIZE

# ----------------------------------------------------------------------
# F regression: 10 Up @ .60 + immediately cheap Down @ .34 still uses FOK
# and preserves the old full-pair +$0.25 target.
# ----------------------------------------------------------------------
market_f = {
    'condition_id': 'f-fok', 'up_asset': 'FU', 'down_asset': 'FD',
    'end_ts': int(time.time()) + 300,
}
up60 = {
    'asks': {0.60: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 5.0, 'tick_size': 0.01,
}
dn34 = {
    'asks': {0.34: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 5.0, 'tick_size': 0.01,
}
base_f = bot.execute_baseline_from_snapshot('f-fok', f, 'FU', 'Up', 'ENTRY', up60)
trade_f = bot.paper_execute_from_baseline(f, 'f-fok', 'FU', 'Up', 'ENTRY', base_f)
assert trade_f and abs(trade_f['filled'] - 10) < 1e-9
pf = bot.create_pair_hedge_order(
    market_f, f, trade_f,
    {'books': {'FU': up60, 'FD': dn34}, 'sides': [('FU','Up'),('FD','Down')]},
)
assert pf and pf['status'] == 'FILLED' and pf['mode'] == 'FOK'
assert pf['worst_pnl'] >= 0.25 - 1e-6

# ----------------------------------------------------------------------
# G: 10 Up @ .60 creates only 65% = 6.5 Down hedge shares, but waits at
# the same favorable full-pair threshold: LIMIT .35 when ask is .41.
# ----------------------------------------------------------------------
market_g = {
    'condition_id': 'g-pair', 'up_asset': 'GU', 'down_asset': 'GD',
    'end_ts': int(time.time()) + 300,
}
up60g = dict(up60)
up60g['received_ms'] = bot.now_ms(); up60g['captured_ms'] = bot.now_ms()
dn41 = {
    'asks': {0.41: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 5.0, 'tick_size': 0.01,
}
base_g = bot.execute_baseline_from_snapshot('g-pair', g, 'GU', 'Up', 'ENTRY', up60g)
trade_g = bot.paper_execute_from_baseline(g, 'g-pair', 'GU', 'Up', 'ENTRY', base_g)
assert trade_g and abs(trade_g['filled'] - 10.0) < 1e-9
pg = bot.create_pair_hedge_order(
    market_g, g, trade_g,
    {'books': {'GU': up60g, 'GD': dn41}, 'sides': [('GU','Up'),('GD','Down')]},
)
assert pg and pg['status'] == 'PENDING'
assert abs(pg['requested_shares'] - 6.5) < 1e-9
assert abs(pg['limit_price'] - 0.35) < 1e-9

# It does not fill at .36.
bot.books['GD'] = {
    'asks': {0.36: 20.0}, 'bids': {}, 'received_ms': bot.now_ms(),
    'source': 'test', 'min_order_size': 5.0, 'tick_size': 0.01,
}
assert bot.process_pair_hedges_for_market(market_g, g['name']) == 0

# It fills all 6.5 at/below .35 with maker fee zero.
bot.books['GD'] = {
    'asks': {0.35: 20.0}, 'bids': {}, 'received_ms': bot.now_ms(),
    'source': 'test', 'min_order_size': 5.0, 'tick_size': 0.01,
}
assert bot.process_pair_hedges_for_market(market_g, g['name']) == 1
with bot.db() as db:
    row = db.execute('SELECT * FROM pair_hedges WHERE id=?', (pg['order_id'],)).fetchone()
assert row['status'] == 'FILLED'
assert abs(row['filled_shares'] - 6.5) < 1e-9
# Base lot should keep meaningful upside, while loss is much smaller than unhedged.
ex, pnl_up, pnl_down = bot._smart_market_pnls(g['name'], market_g)
assert 1.50 < pnl_up < 1.60, (pnl_up, pnl_down)
assert -2.00 < pnl_down < -1.85, (pnl_up, pnl_down)

# ----------------------------------------------------------------------
# G dynamic sizing at higher prices: .72 => only 6 actual shares.
# ----------------------------------------------------------------------
market_size = {
    'condition_id': 'g-size', 'up_asset': 'SU', 'down_asset': 'SD',
    'end_ts': int(time.time()) + 300,
}
up72 = {
    'asks': {0.72: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 1.0, 'tick_size': 0.01,
}
base_size = bot.execute_baseline_from_snapshot('g-size', g, 'SU', 'Up', 'ENTRY', up72)
trade_size = bot.paper_execute_from_baseline(g, 'g-size', 'SU', 'Up', 'ENTRY', base_size)
assert trade_size and abs(trade_size['requested'] - 6.0) < 1e-9
assert abs(trade_size['filled'] - 6.0) < 1e-9

# > .82: baseline may advance, but G PAPER capital is not committed.
up83 = {
    'asks': {0.83: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 1.0, 'tick_size': 0.01,
}
base_skip = bot.execute_baseline_from_snapshot('g-skip', g, 'XU', 'Up', 'ENTRY', up83)
assert bot.paper_execute_from_baseline(g, 'g-skip', 'XU', 'Up', 'ENTRY', base_skip) is None

# ----------------------------------------------------------------------
# G soft market risk cap. Two normal fills total 18 shares; no pair hedge here.
# At Down=.30 the risk manager can buy enough Down to pull worst settlement PnL
# from about -11.7 toward -6 while preserving at least +$1 on the better outcome.
# ----------------------------------------------------------------------
market_r = {
    'condition_id': 'g-risk', 'up_asset': 'RU', 'down_asset': 'RD',
    'end_ts': int(time.time()) + 300,
}
r60 = {
    'asks': {0.60: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 5.0, 'tick_size': 0.01,
}
r68 = {
    'asks': {0.68: 100.0}, 'bids': {},
    'received_ms': bot.now_ms(), 'captured_ms': bot.now_ms(),
    'min_order_size': 5.0, 'tick_size': 0.01,
}
b1 = bot.execute_baseline_from_snapshot('g-risk', g, 'RU', 'Up', 'ENTRY', r60)
t1 = bot.paper_execute_from_baseline(g, 'g-risk', 'RU', 'Up', 'ENTRY', b1)
assert t1 and abs(t1['filled'] - 10) < 1e-9
b2 = bot.execute_baseline_from_snapshot('g-risk', g, 'RU', 'Up', 'PYRAMID', r68)
t2 = bot.paper_execute_from_baseline(g, 'g-risk', 'RU', 'Up', 'PYRAMID', b2)
assert t2 and abs(t2['filled'] - 8) < 1e-9
_, before_up, before_down = bot._smart_market_pnls(g['name'], market_r)
assert before_down < -11.0 and before_up > 6.0
bot.books['RD'] = {
    'asks': {0.30: 100.0}, 'bids': {}, 'received_ms': bot.now_ms(),
    'source': 'test', 'min_order_size': 5.0, 'tick_size': 0.01,
}
cap = bot.maybe_execute_smart_risk_cap(market_r, g)
assert cap is not None
assert cap['filled'] >= 5.0
assert -6.01 <= cap['worst_after'] <= -5.90, cap
assert cap['best_after'] >= 1.0 - 1e-6, cap
assert bot.paper_has_asset_position(g['name'], 'g-risk', 'RD') is False

stats = bot.account_stats(g['name'])
assert stats['smart_risk_hedges'] >= 1
assert stats['normal_avg_lot'] > 0

print('six-way CONF65 + G SMART65 regression: OK')
