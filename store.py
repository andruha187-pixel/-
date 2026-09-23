import json
import os
import sqlite3
import threading

from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
_c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
_l = threading.Lock()
_c.execute("PRAGMA journal_mode=WAL")
_c.executescript("""
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, asset TEXT, slug TEXT, side TEXT,
  price REAL, size REAL, kind TEXT, fair REAL, sec_left REAL, queue REAL, net_before REAL,
  rebate REAL, mid10 REAL, mid60 REAL, won INTEGER);
CREATE TABLE IF NOT EXISTS windows(slug TEXT PRIMARY KEY, asset TEXT, start INTEGER,
  qty_up REAL DEFAULT 0, qty_dn REAL DEFAULT 0, cost_up REAL DEFAULT 0, cost_dn REAL DEFAULT 0,
  kills INTEGER DEFAULT 0, winner TEXT, payout REAL, pnl REAL, settled_ts REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
""")


def q(sql, a=()):
    with _l:
        return _c.execute(sql, a).fetchall()


def x(sql, a=()):
    with _l:
        cur = _c.execute(sql, a)
        return cur.lastrowid


def get(k, d=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r[0][0]) if r else d


def put(k, v):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
