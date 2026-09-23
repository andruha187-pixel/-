"""Мейкер-движок: котирует лимитные BID на Up и Down, держит баланс позиции,
снимает заявки при рывках Binance. В DRY_RUN исполнение симулируется по реальному
потоку сделок и стакану Polymarket с учётом очереди перед нами.

Модель исполнения нашей заявки BID(token T, цена p):
  1) сделка по T по цене < p       → нас исполнили бы первыми: филл до размера сделки
  2) сделка по T по цене == p      → сначала съедается очередь перед нами, остаток — наш
  3) сделка по второму токену C по цене ≥ 1−p (зеркальный матчинг Up+Down) — то же самое
  4) лучший ASK по T опустился до ≤ p → нас бы «прошили» — полный филл
  Очередь = объём на нашем уровне в момент постановки × queue_mult; если уровень
  потом похудел (отмены), очередь уменьшается. Перестановка заявки = потеря очереди.
"""
import logging
import math
import time
from collections import deque

import store
from config import BINANCE_REST, BINANCE_SYM
from features import fair_block, logret_std, price_before
from mkt import gamma_market
from net import get_json

log = logging.getLogger("engine")
TICK = 0.01


def fl(p):
    return math.floor(p / TICK + 1e-9) * TICK


class Order:
    __slots__ = ("slug", "tok", "price", "left", "queue", "placed")

    def __init__(self, slug, tok, price, left, queue, placed):
        self.slug, self.tok, self.price, self.left, self.queue, self.placed = slug, tok, price, left, queue, placed


class Engine:
    def __init__(self, S, markets, pm, bn, cl, tg):
        self.S, self.m, self.pm, self.bn, self.cl, self.tg = S, markets, pm, bn, cl, tg
        self.orders = {}              # (asset, 'up'|'dn') -> Order
        self.tok_map = {}             # token -> (asset, side, slug)
        self.win = {}                 # slug -> dict состояния окна
        self.open_px = {}             # slug -> цена Chainlink на старте окна
        self.kill_until = {}
        self.sigma = {}
        self.markouts = deque()
        self.recent_t = deque(maxlen=400)  # (ts, token, size) для дедупа зеркальных сделок
        self.cash = store.get("cash", S.bankroll)
        self.stats = {"kills": 0, "ticks": 0, "quoting_ticks": 0, "skip": {}}
        for slug, a, st, qu, qd, cu, cd, k in store.q(
                "SELECT slug,asset,start,qty_up,qty_dn,cost_up,cost_dn,kills FROM windows WHERE settled_ts IS NULL"):
            self.win[slug] = {"slug": slug, "asset": a, "start": st, "qty_up": qu, "qty_dn": qd,
                              "cost_up": cu, "cost_dn": cd, "kills": k}
        pm.listeners.append(self.on_trade)

    # ---------- данные ----------
    async def sigma_loop(self):
        import asyncio
        while True:
            for a in set(self.S.asset_list()):
                if a not in BINANCE_SYM:
                    continue
                raw = await get_json(f"{BINANCE_REST}/api/v3/klines",
                                     {"symbol": BINANCE_SYM[a], "interval": "1m", "limit": 62}, quiet=True)
                if raw and len(raw) > 20:
                    s = logret_std([float(k[4]) for k in raw[:-1]])
                    if s:
                        self.sigma[a] = s
            await asyncio.sleep(30)

    def fair_up(self, a, m, now):
        st = m["start"]
        op = self.open_px.get(m["slug"])
        if op is None:
            op = self.cl.at_or_after(a, st)
            if op:
                self.open_px[m["slug"]] = op
        cl_hist = self.cl.h.get(a)
        if not (op and cl_hist and self.sigma.get(a)):
            return None
        cl_sec, cl_px = cl_hist[-1]
        # Chainlink обновляется с задержкой — доводим его до текущего Binance
        b = self.bn.buckets[a].as_list(120)
        bn_now = b[-1][1] if b else None
        bn_then = price_before(b, cl_sec + 1) if b else None
        ref = cl_px * (bn_now / bn_then) if bn_now and bn_then else cl_px
        f = fair_block(ref, op, self.sigma[a], st + 900 - now)
        return f.get("fair_up")

    def binance_move_bps(self, a, secs=5):
        b = self.bn.buckets[a].as_list(30)
        if len(b) < 2:
            return 0.0
        p0 = price_before(b, time.time() - secs)
        return (b[-1][1] / p0 - 1) * 1e4 if p0 else 0.0

    # ---------- окно ----------
    def wstate(self, m):
        w = self.win.get(m["slug"])
        if not w:
            w = {"slug": m["slug"], "asset": m["asset"], "start": m["start"], "qty_up": 0.0, "qty_dn": 0.0,
                 "cost_up": 0.0, "cost_dn": 0.0, "kills": 0}
            self.win[m["slug"]] = w
            store.x("INSERT OR IGNORE INTO windows(slug,asset,start) VALUES(?,?,?)", (m["slug"], m["asset"], m["start"]))
        return w

    def reserved(self):
        return sum(o.price * o.left for o in self.orders.values())

    def _skip(self, a, why):
        k = f"{a}:{why}"
        self.stats["skip"][k] = self.stats["skip"].get(k, 0) + 1

    # ---------- главный такт ----------
    def tick(self, now):
        self.stats["ticks"] += 1
        S = self.S
        for a in {k[0] for k in self.orders} | set(S.asset_list()):
            m = self.m.get(a)
            # заявки от прошлого окна снимаем
            for side in ("up", "dn"):
                o = self.orders.get((a, side))
                if o and (not m or o.slug != m["slug"]):
                    del self.orders[(a, side)]
            if not m or not (m["start"] <= now < m["start"] + 900):
                continue
            self.tok_map[m["up"]] = (a, "up", m["slug"])
            self.tok_map[m["dn"]] = (a, "dn", m["slug"])
            w = self.wstate(m)
            self.book_fills(a, m, w, now)
            reason = self.can_quote(a, m, now)
            fu = self.fair_up(a, m, now) if not reason else None
            if not reason and fu is None:
                reason = "нет справедливой цены"
            if not reason and not (S.fair_min <= fu <= 1 - S.fair_min):
                reason = "исход почти решён"
            if reason:
                self._skip(a, reason)
                self.orders.pop((a, "up"), None)
                self.orders.pop((a, "dn"), None)
                continue
            self.stats["quoting_ticks"] += 1
            want = self.desired(a, m, w, fu)
            for side in ("up", "dn"):
                self.manage(a, side, m, want[side], now)
        self.process_markouts(now)

    def can_quote(self, a, m, now):
        S = self.S
        if not S.running:
            return "пауза"
        if a not in S.asset_list():
            return "актив выключен"
        sec_into, sec_left = now - m["start"], m["start"] + 900 - now
        if sec_into < S.start_sec:
            return "начало окна"
        if sec_left < S.stop_sec:
            return "конец окна"
        mv = abs(self.binance_move_bps(a))
        if mv >= S.kill_bps:
            if now >= self.kill_until.get(a, 0):
                self.stats["kills"] += 1
                w = self.win.get(m["slug"])
                if w:
                    w["kills"] += 1
            self.kill_until[a] = now + S.kill_cooldown
        if now < self.kill_until.get(a, 0):
            return "рывок Binance"
        if not self.pm.top(m["up"]) or not self.pm.top(m["dn"]):
            return "нет стакана"
        return None

    def desired(self, a, m, w, fu):
        S = self.S
        bk = {"up": self.pm.top(m["up"]), "dn": self.pm.top(m["dn"])}
        net = w["qty_up"] - w["qty_dn"]
        skew = S.skew_per_share * net
        tgt = {"up": fu - S.min_edge / 2 - skew, "dn": (1 - fu) - S.min_edge / 2 + skew}
        out = {}
        spent = w["cost_up"] + w["cost_dn"]
        free = self.cash - self.reserved()
        for side, other in (("up", "dn"), ("dn", "up")):
            q_s, q_o = w[f"qty_{side}"], w[f"qty_{other}"]
            heavy = q_s - q_o
            price = tgt[side]
            hedging = False
            if heavy >= S.max_imbalance and heavy > 0:
                out[side] = None
                continue
            if -heavy >= S.order_shares and q_o > 0:          # эта сторона лёгкая — выравниваем
                avg_o = w[f"cost_{other}"] / q_o
                bound = S.hedge_max_pair - avg_o
                price = min(max(price, bk[side]["bid"] + TICK), bound)
                hedging = True
            price = min(price, bk[side]["ask"] - TICK)       # только мейкер (post-only)
            price = min(price, bk[side]["bid"] + (TICK if S.improve >= 1 else 0))
            price = fl(price)
            size = S.order_shares
            if hedging:
                size = min(size, -heavy) if -heavy >= 5 else size
            if price < TICK or price > 0.99:
                out[side] = None
            elif q_s + size > S.max_side_shares and not hedging:
                out[side] = None
            elif spent + price * size > S.max_window_cost and not hedging:
                out[side] = None
            elif price * size > free + (self.orders[(a, side)].price * self.orders[(a, side)].left
                                        if (a, side) in self.orders else 0):
                out[side] = None
            else:
                out[side] = (price, size)
        return out

    def manage(self, a, side, m, want, now):
        o = self.orders.get((a, side))
        if want is None:
            if o:
                del self.orders[(a, side)]
            return
        price, size = want
        if o and abs(o.price - price) < 1e-6:
            return
        if o and now - o.placed < self.S.requote_sec and abs(o.price - price) <= TICK + 1e-6:
            return
        tok = m[side]
        q = self.pm.level(tok, price, "b") * self.S.queue_mult
        self.orders[(a, side)] = Order(m["slug"], tok, price, size, q, now)

    # ---------- симуляция исполнения ----------
    def book_fills(self, a, m, w, now):
        for side in ("up", "dn"):
            o = self.orders.get((a, side))
            if not o:
                continue
            lvl = self.pm.level(o.tok, o.price, "b")
            if lvl < o.queue:
                o.queue = lvl
            top = self.pm.top(o.tok)
            if top and top["ask"] <= o.price + 1e-9:
                self.fill(a, side, o, o.left, now, "cross")

    def on_trade(self, tok, tside, price, size, ts):
        self.recent_t.append((ts, tok, size))
        hit = self.tok_map.get(tok)
        if hit:
            a, side, slug = hit
            self._trade_vs_order(a, side, slug, price, size, ts, "trade")
        # зеркальный матчинг: сделка по второму токену на цене ≥ 1−p
        for (a, side), o in list(self.orders.items()):
            info = self.tok_map.get(o.tok)
            if not info or o.tok == tok:
                continue
            other_tok = self._other_token(a, side)
            if tok != other_tok:
                continue
            if any(abs(t0 - ts) < 1.0 and tk == o.tok and abs(sz - size) < 1e-6 for t0, tk, sz in self.recent_t):
                continue  # эта же сделка уже пришла по нашему токену
            self._trade_vs_order(a, side, o.slug, round(1 - price, 3), size, ts, "mirror")

    def _other_token(self, a, side):
        m = self.m.get(a)
        if not m:
            return None
        return m["dn"] if side == "up" else m["up"]

    def _trade_vs_order(self, a, side, slug, price, size, ts, kind):
        o = self.orders.get((a, side))
        if not o or o.slug != slug or price > o.price + 1e-9:
            return
        if price < o.price - 1e-9:
            q = min(size, o.left)
        else:
            eat = min(size, o.queue)
            o.queue -= eat
            q = min(size - eat, o.left)
        if q > 1e-9:
            self.fill(a, side, o, q, ts, kind)

    def fill(self, a, side, o, qty, now, kind):
        m = self.m.get(a)
        w = self.win.get(o.slug)
        if not w or not m:
            return
        fu = self.fair_up(a, m, now)
        fair = (fu if side == "up" else 1 - fu) if fu is not None else None
        net_before = w["qty_up"] - w["qty_dn"]
        o.left -= qty
        if o.left <= 1e-9:
            self.orders.pop((a, side), None)
        w[f"qty_{side}"] += qty
        w[f"cost_{side}"] += qty * o.price
        self.cash -= qty * o.price
        reb = self.S.rebate_share * self.S.fee_rate * qty * o.price * (1 - o.price)
        fid = store.x("INSERT INTO fills(ts,asset,slug,side,price,size,kind,fair,sec_left,queue,net_before,rebate) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now, a, o.slug, side, o.price, qty, kind, fair, m["start"] + 900 - now, o.queue, net_before, reb))
        store.x("UPDATE windows SET qty_up=?,qty_dn=?,cost_up=?,cost_dn=?,kills=? WHERE slug=?",
                (w["qty_up"], w["qty_dn"], w["cost_up"], w["cost_dn"], w["kills"], o.slug))
        store.put("cash", self.cash)
        self.markouts.append((now + 10, fid, o.tok, "mid10", o.slug))
        self.markouts.append((now + 60, fid, o.tok, "mid60", o.slug))

    def process_markouts(self, now):
        while self.markouts and self.markouts[0][0] <= now:
            _, fid, tok, col, slug = self.markouts.popleft()
            t = self.pm.top(tok)
            if t:
                store.x(f"UPDATE fills SET {col}=? WHERE id=?", (t["mid"], fid))

    # ---------- расчёт окон ----------
    async def settle_loop(self):
        import asyncio
        while True:
            now = time.time()
            for slug, w in list(self.win.items()):
                end = w["start"] + 900
                if now < end + 30:
                    continue
                if w["qty_up"] == 0 and w["qty_dn"] == 0:
                    store.x("UPDATE windows SET settled_ts=?, pnl=0, payout=0 WHERE slug=?", (now, slug))
                    del self.win[slug]
                    continue
                g = await gamma_market(slug)
                winner = g.get("winner") if g else None
                if not winner and now > end + 900:
                    op, cp = self.open_px.get(slug), self.cl.at_or_after(w["asset"], end)
                    if op and cp:
                        winner = "up" if cp >= op else "down"
                if not winner:
                    continue
                payout = w["qty_up"] if winner == "up" else w["qty_dn"]
                pnl = payout - w["cost_up"] - w["cost_dn"]
                self.cash += payout
                store.put("cash", self.cash)
                wk = "up" if winner == "up" else "dn"
                store.x("UPDATE windows SET winner=?, payout=?, pnl=?, settled_ts=? WHERE slug=?",
                        (winner, payout, pnl, now, slug))
                store.x("UPDATE fills SET won=(side=?) WHERE slug=?", (wk, slug))
                del self.win[slug]
                self.open_px.pop(slug, None)
            await asyncio.sleep(20)

    def locked(self):
        return sum(w["cost_up"] + w["cost_dn"] for w in self.win.values())

    def reset(self):
        self.orders.clear()
        self.win.clear()
        store.x("DELETE FROM fills")
        store.x("DELETE FROM windows")
        self.cash = self.S.bankroll
        store.put("cash", self.cash)
        store.put("since", time.time())
        self.stats = {"kills": 0, "ticks": 0, "quoting_ticks": 0, "skip": {}}
