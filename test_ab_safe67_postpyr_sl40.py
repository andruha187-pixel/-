import os
import tempfile
import time
import asyncio
import importlib.util
import zipfile
from pathlib import Path

# Isolated DB for regression.
tmp = tempfile.mkdtemp(prefix='safe67_ab_postpyr_sl40_')
os.environ['DATA_DIR'] = tmp
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_CHAT_ID'] = ''
os.environ['PAPER_START_BALANCE'] = '500'
os.environ['ENTRY_ORDER_SIZE'] = '5'
os.environ['PYRAMID_ORDER_SIZE'] = '10'
os.environ['SAFE_ENTRY_PRICE_MIN'] = '0.67'
os.environ['SAFE_ENTRY_PRICE_MAX'] = '0.75'
os.environ['SAFE_ENTRY_MOM_MIN'] = '0.05'
os.environ['SAFE_ENTRY_MOM_MAX'] = '0.10'
os.environ['STOP_LOSS_PRICE'] = '0.40'
os.environ['STOP_CHECK_INTERVAL'] = '0.20'

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bot', here / 'main.py')
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.init_db()

# Clean-loop contract: no pre-decision REST refresh.
source = (here / 'main.py').read_text(encoding='utf-8')
loop_start = source.index('async def strategy_loop():')
loop_end = source.index('# ============================================================\n# RESOLUTION', loop_start)
loop_src = source[loop_start:loop_end]
assert 'await ensure_book(asset)' not in loop_src
assert 'ask = best_ask(asset)' in loop_src

# Execution can refresh before a PAPER fill.
exec_src = source[source.index('async def execute_paper'):source.index('def _first_v2_eligible_candidates')]
assert 'age = await ensure_book(asset)' in exec_src

# Stop loop can refresh only after stop is armed.
stop_src = source[source.index('async def stop_loss_loop'):source.index('def record_position_trajectory')]
assert 'stop_armed' in stop_src
assert 'await ensure_book(pos["primary_asset"])' in stop_src

A = bot.STRATEGY_BY_NAME['A_SAFE67']
B = bot.STRATEGY_BY_NAME['B_SAFE67_POSTPYR_SL40']
assert A['stop_loss_price'] is None
assert A['stop_after_pyramid'] is False
assert abs(B['stop_loss_price'] - 0.40) < 1e-12
assert B['stop_after_pyramid'] is True
assert abs(bot.SAFE_ENTRY_PRICE_MIN - 0.67) < 1e-12
assert bot.paper_cash(A['name']) == 500
assert bot.paper_cash(B['name']) == 500
assert bot.trading_enabled() is False
bot.state_set('trading_enabled', '1')

now = int(time.time())
slot = (now // 300) * 300
market = {
    'condition_id': 'cid-pass',
    'question': 'Bitcoin Up or Down Test',
    'slug': f'btc-updown-5m-{slot}',
    'start_ts': slot,
    'end_ts': slot + 300,
    'up_asset': 'UP',
    'down_asset': 'DOWN',
}
bot.markets[market['condition_id']] = market
bot.persist_market(market)

# SAFE67 PASS: first V2-eligible signal .68, momentum .07.
ms = bot.now_ms()
bot.books['UP'] = {'bids': {0.67: 100}, 'asks': {0.68: 100}, 'received_ms': ms, 'source': 'test'}
bot.books['DOWN'] = {'bids': {0.31: 100}, 'asks': {0.32: 100}, 'received_ms': ms, 'source': 'test'}
bot.price_history['cid-pass']['UP'].extend([(ms - 6000, 0.61), (ms - 3000, 0.64), (ms, 0.68)])
bot.price_history['cid-pass']['DOWN'].extend([(ms - 6000, 0.39), (ms - 3000, 0.36), (ms, 0.32)])

asyncio.run(bot.evaluate_variant(market, A, 30.0))
asyncio.run(bot.evaluate_variant(market, B, 30.0))

for v in (A, B):
    pos = bot.position_totals('cid-pass', v['name'])
    assert abs(pos['bought'] - 5.0) < 1e-9
    assert abs(pos['remaining'] - 5.0) < 1e-9
    assert pos['has_pyramid'] is False

# Critical new rule: B may fall far below .40 while it only has ENTRY 5sh.
bot.books['UP']['bids'] = {0.20: 100}
bot.books['UP']['received_ms'] = bot.now_ms()
assert bot.process_stop_loss(market, B) is None
assert bot.stop_triggered('cid-pass', B['name']) is False
assert abs(bot.position_totals('cid-pass', B['name'])['remaining'] - 5.0) < 1e-9

# Confirmation/PYRAMID at .76 -> 15sh. Stop becomes armed only now.
ms = bot.now_ms()
bot.books['UP']['asks'] = {0.76: 100}
bot.books['UP']['bids'] = {0.75: 100}
bot.books['UP']['received_ms'] = ms
bot.price_history['cid-pass']['UP'].extend([(ms - 3000, 0.70), (ms, 0.76)])
asyncio.run(bot.evaluate_variant(market, A, 60.0))
asyncio.run(bot.evaluate_variant(market, B, 60.0))

for v in (A, B):
    pos = bot.position_totals('cid-pass', v['name'])
    assert abs(pos['bought'] - 15.0) < 1e-9
    assert pos['has_pyramid'] is True
    assert pos['pyramid_trades'] == 1

# No stop at .41 after pyramid.
bot.books['UP']['bids'] = {0.41: 100}
bot.books['UP']['received_ms'] = bot.now_ms()
assert bot.process_stop_loss(market, B) is None
assert bot.stop_triggered('cid-pass', B['name']) is False

# At .40 after pyramid, B exits all visible 15sh; A remains untouched.
bot.books['UP']['bids'] = {0.40: 100}
bot.books['UP']['received_ms'] = bot.now_ms()
stop = bot.process_stop_loss(market, B)
assert stop is not None
assert abs(stop['filled'] - 15.0) < 1e-9
assert abs(stop['avg'] - 0.40) < 1e-9
assert bot.stop_triggered('cid-pass', B['name']) is True
assert abs(bot.position_totals('cid-pass', B['name'])['remaining']) < 1e-9
assert abs(bot.position_totals('cid-pass', A['name'])['remaining'] - 15.0) < 1e-9

# Persistent stop event and exit records.
with bot.db() as c:
    assert c.execute('SELECT COUNT(*) c FROM stop_events WHERE variant=?', (B['name'],)).fetchone()['c'] == 1
    assert c.execute('SELECT COUNT(*) c FROM paper_exits WHERE variant=?', (B['name'],)).fetchone()['c'] == 1

# Trajectory remains separate for A/B and shows B stopped.
bot.record_position_trajectory(market, A, 90.0)
bot.record_position_trajectory(market, B, 90.0)
with bot.db() as c:
    ta = c.execute('SELECT * FROM position_trajectory WHERE variant=? ORDER BY id DESC LIMIT 1', (A['name'],)).fetchone()
    tb = c.execute('SELECT * FROM position_trajectory WHERE variant=? ORDER BY id DESC LIMIT 1', (B['name'],)).fetchone()
assert ta['stop_triggered'] == 0
assert tb['stop_triggered'] == 1
assert abs(tb['remaining_shares']) < 1e-9

# Settle Up. A still owns 15; B sold all before settlement.
asyncio.run(bot.settle_market('cid-pass', 'UP', 'Up'))
with bot.db() as c:
    ra = c.execute("SELECT * FROM market_results WHERE condition_id='cid-pass' AND variant=?", (A['name'],)).fetchone()
    rb = c.execute("SELECT * FROM market_results WHERE condition_id='cid-pass' AND variant=?", (B['name'],)).fetchone()
assert abs(ra['payout'] - 15.0) < 1e-9
assert abs(rb['payout']) < 1e-9
assert rb['stopped_out'] == 1
assert ra['stopped_out'] == 0
assert abs(bot.paper_cash(A['name']) - (500 + ra['pnl'])) < 1e-7
assert abs(bot.paper_cash(B['name']) - (500 + rb['pnl'])) < 1e-7

# Raw M03 below V2 price floor does not decide anything.
market2 = {
    'condition_id': 'cid-skip',
    'question': 'Bitcoin Up or Down Test 2',
    'slug': f'btc-updown-5m-{slot + 300}',
    'start_ts': slot + 300,
    'end_ts': slot + 600,
    'up_asset': 'UP2',
    'down_asset': 'DOWN2',
}
bot.markets[market2['condition_id']] = market2
bot.persist_market(market2)
ms = bot.now_ms()
bot.books['UP2'] = {'bids': {0.49: 100}, 'asks': {0.50: 100}, 'received_ms': ms, 'source': 'test'}
bot.books['DOWN2'] = {'bids': {0.49: 100}, 'asks': {0.50: 100}, 'received_ms': ms, 'source': 'test'}
bot.price_history['cid-skip']['UP2'].extend([(ms - 6000, .44), (ms - 3000, .46), (ms, .50)])
bot.price_history['cid-skip']['DOWN2'].extend([(ms - 6000, .56), (ms - 3000, .54), (ms, .50)])
for v in (A, B):
    asyncio.run(bot.evaluate_variant(market2, v, 20.0))
with bot.db() as c:
    assert c.execute("SELECT COUNT(*) c FROM gate_decisions WHERE condition_id='cid-skip'").fetchone()['c'] == 0

# First V2-eligible signal at .65/mom .05 is below SAFE67 -> both skip forever.
ms = bot.now_ms()
bot.books['UP2']['asks'] = {0.65: 100}
bot.books['UP2']['bids'] = {0.64: 100}
bot.books['UP2']['received_ms'] = ms
bot.price_history['cid-skip']['UP2'].extend([(ms - 3000, .60), (ms, .65)])
for v in (A, B):
    asyncio.run(bot.evaluate_variant(market2, v, 30.0))
with bot.db() as c:
    gates = c.execute("SELECT * FROM gate_decisions WHERE condition_id='cid-skip' ORDER BY variant").fetchall()
assert len(gates) == 2
assert all(r['passed'] == 0 and r['reason'] == 'SAFE_PRICE_LOW' for r in gates)

# Hourly report preserves one ZIP and separate variant folders.
hour_start = slot - (slot % 3600)
path, summaries = bot.make_report(hour_start, hour_start + 3600)
assert len(summaries) == 2
with zipfile.ZipFile(path, 'r') as z:
    names = set(z.namelist())
required = {
    'variants_summary.csv', 'report.txt', 'markets.csv',
    'A_safe67_no_stop/summary.csv',
    'A_safe67_no_stop/paper_trades.csv',
    'A_safe67_no_stop/paper_exits.csv',
    'A_safe67_no_stop/market_results.csv',
    'A_safe67_no_stop/position_trajectory.csv',
    'B_safe67_postpyr_stop_040/summary.csv',
    'B_safe67_postpyr_stop_040/paper_trades.csv',
    'B_safe67_postpyr_stop_040/paper_exits.csv',
    'B_safe67_postpyr_stop_040/stop_events.csv',
    'B_safe67_postpyr_stop_040/market_results.csv',
    'B_safe67_postpyr_stop_040/position_trajectory.csv',
}
assert required.issubset(names), required - names

print('SAFE67 A/B + POST-PYRAMID STOP 0.40 regression: OK')
