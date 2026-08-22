import os, io, csv, json, time, math, zipfile, sqlite3, asyncio, logging
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict, deque
from typing import Optional

import aiohttp
from aiohttp import web
import websockets
from dotenv import load_dotenv

load_dotenv()

VERSION = "5.0-guarded-pyramid-research"
PORT = int(os.getenv("PORT", "8080"))
SYMBOL = "BTC"
DECISION_INTERVAL = float(os.getenv("DECISION_INTERVAL", "3"))
TRADE_WINDOW_SECONDS = int(os.getenv("TRADE_WINDOW_SECONDS", "180"))
ORDER_SIZE = float(os.getenv("ORDER_SIZE", "10"))
CRYPTO_FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))
MAX_BOOK_AGE_MS = int(os.getenv("MAX_BOOK_AGE_MS", "1000"))
REPORT_DELAY_SECONDS = int(os.getenv("REPORT_DELAY_SECONDS", "300"))
REPORT_CHECK_INTERVAL = int(os.getenv("REPORT_CHECK_INTERVAL", "30"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    t = DATA_DIR / ".write_test"; t.write_text("ok"); t.unlink()
except Exception:
    DATA_DIR = Path("./data"); DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "guarded_pyramid_research.db"
REPORT_DIR = DATA_DIR / "guarded_reports"; REPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("guard-v5")
session: Optional[aiohttp.ClientSession] = None

# Base strategies. V2_LOCK gives the raw signals. V3 adds the 90-second cutoff.
BASE_VARIANTS = [
    dict(name="M03_V2_LOCK", entry_move=0.03, pyramid_step=0.08, lookback=2,
         max_buys_side=6, entry_price_min=0.55, entry_price_max=0.75,
         momentum_cap=0.30, cutoff=None),
    dict(name="M03_V3_NOSW90", entry_move=0.03, pyramid_step=0.08, lookback=2,
         max_buys_side=5, entry_price_min=None, entry_price_max=None,
         momentum_cap=None, cutoff=90),
]
MIN_PRICE=float(os.getenv("MIN_PRICE","0.08")); MAX_PRICE=float(os.getenv("MAX_PRICE","0.95"))

# Exact old V2 confidence weights (book INCLUDED).
BINANCE_SYMBOL=os.getenv("BINANCE_SYMBOL","btcusdt").lower()
BINANCE_WS=("wss://fstream.binance.com/market/stream?streams="
            f"{BINANCE_SYMBOL}@aggTrade/{BINANCE_SYMBOL}@depth20@100ms")
BINANCE_LARGE_TRADE_USD=float(os.getenv("BINANCE_LARGE_TRADE_USD","50000"))
BINANCE_SIGNAL_MAX_AGE_MS=int(os.getenv("BINANCE_SIGNAL_MAX_AGE_MS","1500"))
REGIME_WINDOW_SEC=int(os.getenv("REGIME_WINDOW_SEC","30"))
START_PRICE_CAPTURE_WINDOW_SEC=int(os.getenv("START_PRICE_CAPTURE_WINDOW_SEC","3"))
W_IMPULSE=float(os.getenv("W_IMPULSE","22")); W_FLOW=float(os.getenv("W_FLOW","18")); W_BOOK=float(os.getenv("W_BOOK","14"))
W_LARGE=float(os.getenv("W_LARGE","8")); W_TREND=float(os.getenv("W_TREND","14")); W_DISTANCE=float(os.getenv("W_DISTANCE","18")); W_POLY_PRICE=float(os.getenv("W_POLY_PRICE","6"))

# New experiment knobs.
GUARD_ENTRY_CONF=float(os.getenv("GUARD_ENTRY_CONF","60"))
GUARD_CUTOFF_SEC=float(os.getenv("GUARD_CUTOFF_SEC","90"))
GUARD_PYR2_CONF=float(os.getenv("GUARD_PYR2_CONF","58"))
GUARD_PYR3_CONF=float(os.getenv("GUARD_PYR3_CONF","62"))
GUARD_PYR4_CONF=float(os.getenv("GUARD_PYR4_CONF","65"))
GUARD_PATH_MIN_STRONG=float(os.getenv("GUARD_PATH_MIN_STRONG","0.22"))
GUARD_DANGER_PATH=float(os.getenv("GUARD_DANGER_PATH","0.18"))
GUARD_FLOW_MIN=float(os.getenv("GUARD_FLOW_MIN","0.00"))
GUARD_RET10_MIN=float(os.getenv("GUARD_RET10_MIN","0.00"))

MODES=["CONF60","GUARD90","GUARD90_STEP","GUARD90_FLOW","GUARD90_ADAPTIVE","GUARD90_STRONG"]

books={}; markets={}; subscribed_assets=set(); ws_send_queue=asyncio.Queue()
price_history=defaultdict(lambda: defaultdict(lambda: deque(maxlen=120)))
base_state={}
shadow_sides=defaultdict(set); shadow_buys=defaultdict(int)
market_binance_start_price={}
binance_trades=deque(maxlen=50000); binance_tick_prices=deque(maxlen=30000); binance_second_prices=deque(maxlen=600)
binance_depth_bids=[]; binance_depth_asks=[]; binance_last_trade_ms=0; binance_last_event_ms=0

def now_ts(): return int(time.time())
def now_ms(): return int(time.time()*1000)
def utc_iso(ts=None): return datetime.fromtimestamp(float(time.time() if ts is None else ts),tz=timezone.utc).isoformat()
def sf(v,d=0.0):
    try:return float(v)
    except (TypeError,ValueError):return d
def si(v,d=0):
    try:return int(float(v))
    except (TypeError,ValueError):return d
def jd(v): return json.dumps(v,ensure_ascii=False,separators=(",",":"))
def parse_jsonish(v):
    if isinstance(v,list):return v
    try:
        x=json.loads(v) if v is not None else []
        return x if isinstance(x,list) else []
    except Exception:return []
def fee_usdc(shares,price):
    f=shares*CRYPTO_FEE_RATE*price*(1-price)
    return round(f,5) if f>=0.000005 else 0.0

def db():
    c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;"); c.execute("PRAGMA synchronous=NORMAL;"); return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS markets(condition_id TEXT PRIMARY KEY,question TEXT,slug TEXT,start_ts INTEGER,end_ts INTEGER,up_asset TEXT,down_asset TEXT,resolved INTEGER DEFAULT 0,winning_asset TEXT,winning_outcome TEXT);
        CREATE TABLE IF NOT EXISTS features(id INTEGER PRIMARY KEY AUTOINCREMENT,signal_ms INTEGER,condition_id TEXT,variant TEXT,asset TEXT,outcome TEXT,signal_type TEXT,elapsed_sec REAL,poly_ask REAL,btc_price REAL,start_price REAL,distance_from_start_pct REAL,ret_1s REAL,ret_3s REAL,ret_10s REAL,flow_3s REAL,flow_10s REAL,flow_30s REAL,book_imbalance REAL,large_delta_10s REAL,large_delta_30s REAL,ema_bias REAL,rsi14 REAL,path_efficiency REAL,direction_changes INTEGER,realized_move REAL,regime TEXT,confidence REAL,data_age_ms INTEGER);
        CREATE TABLE IF NOT EXISTS shadow_trades(id INTEGER PRIMARY KEY AUTOINCREMENT,trade_ms INTEGER,condition_id TEXT,variant TEXT,mode TEXT,asset TEXT,outcome TEXT,signal_type TEXT,elapsed_sec REAL,buy_no INTEGER,filled_shares REAL,avg_price REAL,gross_cost REAL,fee REAL,total_cost REAL,accepted INTEGER,reason TEXT);
        CREATE TABLE IF NOT EXISTS shadow_results(condition_id TEXT,variant TEXT,mode TEXT,winning_asset TEXT,winning_outcome TEXT,total_cost REAL,payout REAL,pnl REAL,trades INTEGER,settled_ms INTEGER,PRIMARY KEY(condition_id,variant,mode));
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);
        CREATE INDEX IF NOT EXISTS idx_st_ms ON shadow_trades(trade_ms); CREATE INDEX IF NOT EXISTS idx_sr_cond ON shadow_results(condition_id);
        """)

def state_get(k,d=None):
    with db() as c:
        r=c.execute("SELECT value FROM state WHERE key=?",(k,)).fetchone(); return r["value"] if r else d
def state_set(k,v):
    with db() as c:
        c.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v))); c.commit()

async def get_json(url,params=None):
    for i in range(3):
        try:
            async with session.get(url,params=params,timeout=aiohttp.ClientTimeout(total=12)) as r:
                t=await r.text()
                if r.status==200:return json.loads(t)
        except Exception as e: log.warning("GET %s: %s",url,e)
        await asyncio.sleep(.3*(i+1))
    return None

def slot_start(slug):
    try:return int(str(slug).rstrip("/").split("-")[-1])
    except:return None
async def fetch_event(slug):
    for url,params in ((f"{GAMMA}/events/slug/{slug}",None),(f"{GAMMA}/events",{"slug":slug})):
        d=await get_json(url,params)
        if isinstance(d,dict):return d
        if isinstance(d,list) and d:return d[0]
    return None

def parse_market(raw,event):
    cid=str(raw.get("conditionId") or raw.get("condition_id") or ""); slug=str(raw.get("slug") or event.get("slug") or "")
    if not cid:return None
    toks=[str(x) for x in parse_jsonish(raw.get("clobTokenIds"))]; outs=[str(x).upper() for x in parse_jsonish(raw.get("outcomes"))]
    if len(toks)<2:return None
    up=down=None
    for i,o in enumerate(outs[:len(toks)]):
        if o in {"UP","YES"}:up=toks[i]
        elif o in {"DOWN","NO"}:down=toks[i]
    start=slot_start(slug)
    if not start:return None
    return dict(condition_id=cid,question=str(raw.get("question") or event.get("title") or ""),slug=slug,start_ts=start,end_ts=start+300,up_asset=up or toks[0],down_asset=down or toks[1])

async def discover_slot(slot):
    ev=await fetch_event(f"btc-updown-5m-{slot}")
    if not ev or not isinstance(ev.get("markets"),list):return None
    for raw in ev["markets"]:
        m=parse_market(raw,ev)
        if m:return m
    return None

def persist_market(m):
    with db() as c:
        c.execute("INSERT INTO markets(condition_id,question,slug,start_ts,end_ts,up_asset,down_asset) VALUES(?,?,?,?,?,?,?) ON CONFLICT(condition_id) DO UPDATE SET question=excluded.question,slug=excluded.slug,start_ts=excluded.start_ts,end_ts=excluded.end_ts,up_asset=excluded.up_asset,down_asset=excluded.down_asset",(m["condition_id"],m["question"],m["slug"],m["start_ts"],m["end_ts"],m["up_asset"],m["down_asset"])); c.commit()
async def subscribe(a):
    if a and a not in subscribed_assets:
        subscribed_assets.add(a); await ws_send_queue.put({"operation":"subscribe","assets_ids":[a]})
async def discovery_loop():
    while True:
        try:
            n=now_ts(); cur=(n//300)*300
            for slot in (cur-300,cur,cur+300):
                m=await discover_slot(slot)
                if m and m["condition_id"] not in markets:
                    markets[m["condition_id"]]=m; persist_market(m); await subscribe(m["up_asset"]); await subscribe(m["down_asset"])
                    log.info("MARKET %s",m["slug"])
        except Exception:log.exception("discovery")
        await asyncio.sleep(10)

def level_map(rows):
    d={}
    for x in rows or []:
        if isinstance(x,dict):
            p=sf(x.get("price"),math.nan); q=sf(x.get("size"))
            if not math.isnan(p) and q>0:d[p]=q
    return d
def apply_book(a,p):books[a]={"bids":level_map(p.get("bids")),"asks":level_map(p.get("asks")),"received_ms":now_ms()}
def apply_change(p):
    recv=now_ms()
    for ch in p.get("price_changes") or p.get("priceChanges") or []:
        a=str(ch.get("asset_id") or ch.get("token_id") or "");
        if not a:continue
        b=books.setdefault(a,{"bids":{},"asks":{},"received_ms":recv}); pr=sf(ch.get("price"),math.nan); q=sf(ch.get("size")); side=str(ch.get("side","")).upper()
        if math.isnan(pr):continue
        target=b["bids"] if side=="BUY" else b["asks"]
        if q<=0:target.pop(pr,None)
        else:target[pr]=q
        b["received_ms"]=recv
def best_ask(a):
    b=books.get(a); return min(b["asks"]) if b and b["asks"] else None
async def refresh_book(a):
    d=await get_json(f"{CLOB}/book",{"token_id":a})
    if isinstance(d,dict):apply_book(a,d);return True
    return False
async def ensure_book(a):
    b=books.get(a)
    if b and b["asks"] and now_ms()-b["received_ms"]<=MAX_BOOK_AGE_MS:return now_ms()-b["received_ms"]
    await refresh_book(a); b=books.get(a); return now_ms()-b["received_ms"] if b else None

def simulate_buy(a,wanted):
    b=books.get(a)
    if not b or not b["asks"]:return [],0
    rem=wanted; fills=[]
    for p in sorted(b["asks"]):
        take=min(rem,b["asks"][p])
        if take>0:fills.append((p,take));rem-=take
        if rem<=1e-9:break
    return fills,wanted-rem

def parse_ws(raw):
    if isinstance(raw,bytes):raw=raw.decode("utf-8","ignore")
    if raw in ("","PING","PONG"):return []
    try:
        x=json.loads(raw);return x if isinstance(x,list) else [x]
    except:return []
async def poly_sender(ws):
    while True:
        m=await ws_send_queue.get()
        try:await ws.send(jd(m))
        except:await ws_send_queue.put(m);return
async def poly_ping(ws):
    while True:
        try:await ws.send("PING")
        except:return
        await asyncio.sleep(10)
async def poly_ws_loop():
    while True:
        try:
            if not subscribed_assets:await asyncio.sleep(1);continue
            async with websockets.connect(POLY_WS,ping_interval=None,max_size=20_000_000) as ws:
                await ws.send(jd({"assets_ids":list(subscribed_assets),"type":"market","custom_feature_enabled":True}))
                log.info("POLY WS connected | assets=%d",len(subscribed_assets))
                sender=asyncio.create_task(poly_sender(ws)); ping=asyncio.create_task(poly_ping(ws))
                try:
                    async for raw in ws:
                        for ev in parse_ws(raw):
                            if not isinstance(ev,dict):continue
                            et=str(ev.get("event_type") or ev.get("type") or ""); p=ev.get("payload") if isinstance(ev.get("payload"),dict) else ev
                            if et=="book":
                                a=str(p.get("asset_id") or p.get("token_id") or "")
                                if a:apply_book(a,p)
                            elif et=="price_change":apply_change(p)
                finally:sender.cancel();ping.cancel()
        except Exception as e:log.warning("POLY reconnect: %s",e);await asyncio.sleep(1)

# Binance features

def _ema(vals,period):
    if not vals:return None
    a=2/(period+1);e=float(vals[0])
    for v in vals[1:]:e=a*float(v)+(1-a)*e
    return e
def _rsi(vals,period=14):
    if len(vals)<period+1:return None
    g=[];l=[]
    for i in range(-period,0):
        d=vals[i]-vals[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g)/period;al=sum(l)/period
    if al<=1e-12:return 100
    return 100-100/(1+ag/al)
def _latest_price():return float(binance_tick_prices[-1][1]) if binance_tick_prices else None
def _price_ago(msago):
    target=now_ms()-msago
    for ts,p in reversed(binance_tick_prices):
        if ts<=target:return float(p)
    return None
def _ret(msago):
    a=_latest_price();b=_price_ago(msago);return a/b-1 if a and b else 0.0
def _flow(sec):
    cut=now_ms()-sec*1000;bu=se=0
    for ts,p,q,s in reversed(binance_trades):
        if ts<cut:break
        if s>0:bu+=q
        else:se+=q
    t=bu+se;return (bu-se)/t if t else 0
def _large(sec):
    cut=now_ms()-sec*1000;bu=se=0
    for ts,p,q,s in reversed(binance_trades):
        if ts<cut:break
        if q<BINANCE_LARGE_TRADE_USD:continue
        if s>0:bu+=q
        else:se+=q
    t=bu+se;return (bu-se)/t if t else 0
def _book():
    b=sum(sf(x[1]) for x in binance_depth_bids[:10]);a=sum(sf(x[1]) for x in binance_depth_asks[:10]);t=a+b
    return (b-a)/t if t else 0
def _regime(sec=30):
    cut=now_ms()-sec*1000;pts=[float(p) for t,p in binance_tick_prices if t>=cut]
    if len(pts)<4:return dict(path_efficiency=0,direction_changes=0,realized_move=0,regime="UNKNOWN")
    net=pts[-1]-pts[0];path=sum(abs(pts[i]-pts[i-1]) for i in range(1,len(pts)));eff=abs(net)/path if path else 0
    signs=[]
    for i in range(1,len(pts)):
        d=pts[i]-pts[i-1]
        if abs(d)>1e-12:signs.append(1 if d>0 else -1)
    ch=sum(1 for i in range(1,len(signs)) if signs[i]!=signs[i-1]);mv=pts[-1]/pts[0]-1 if pts[0] else 0
    reg="TREND" if eff>=.55 and abs(mv)>=.0005 else ("CHOP" if eff<=.25 and ch>=6 else "MIXED")
    return dict(path_efficiency=eff,direction_changes=ch,realized_move=mv,regime=reg)
def _start_price(cid,m):
    if cid in market_binance_start_price:return market_binance_start_price[cid]
    target=m["start_ts"]*1000;best=None;bd=None
    for ts,p in binance_tick_prices:
        d=abs(ts-target)
        if d<=START_PRICE_CAPTURE_WINDOW_SEC*1000 and (bd is None or d<bd):best=float(p);bd=d
    if best is None:best=_latest_price()
    if best is not None:market_binance_start_price[cid]=best
    return best
def _confidence(outcome,poly,f):
    d=1 if outcome.lower()=="up" else -1
    impulse=max(-1,min(1,d*(.35*f["ret_250ms"]/.00020+.30*f["ret_500ms"]/.00030+.20*f["ret_1s"]/.00045+.15*f["ret_3s"]/.00080)))
    flow=max(-1,min(1,d*(.45*f["flow_1s"]+.30*f["flow_3s"]+.25*f["flow_10s"])))
    book=max(-1,min(1,d*f["book_imbalance"]));large=max(-1,min(1,d*(.65*f["large_delta_10s"]+.35*f["large_delta_30s"])))
    trend=1 if d*f["ema_bias"]>0 else -1
    if f["regime"]=="CHOP":trend*=.2
    elif f["regime"]=="MIXED":trend*=.55
    dist=max(-1,min(1,d*f["distance_from_start_pct"]/.0015));pc=0 if poly is None else max(-1,min(1,(.72-float(poly))/.22))
    w=W_IMPULSE*impulse+W_FLOW*flow+W_BOOK*book+W_LARGE*large+W_TREND*trend+W_DISTANCE*dist+W_POLY_PRICE*pc
    return max(0,min(100,50+w/2))
def snapshot(cid,m,outcome,poly):
    btc=_latest_price();start=_start_price(cid,m);dist=btc/start-1 if btc and start else 0;prices=[float(p) for _,p in binance_second_prices]
    e9=_ema(prices[-60:],9) if prices else None;e21=_ema(prices[-90:],21) if prices else None;bias=e9/e21-1 if e9 and e21 else 0;r=_regime(REGIME_WINDOW_SEC)
    f=dict(btc_price=btc,start_price=start,distance_from_start_pct=dist,ret_250ms=_ret(250),ret_500ms=_ret(500),ret_1s=_ret(1000),ret_3s=_ret(3000),ret_10s=_ret(10000),flow_1s=_flow(1),flow_3s=_flow(3),flow_10s=_flow(10),flow_30s=_flow(30),book_imbalance=_book(),large_delta_10s=_large(10),large_delta_30s=_large(30),ema_bias=bias,rsi14=_rsi(prices,14) if prices else None,data_age_ms=(now_ms()-binance_last_trade_ms if binance_last_trade_ms else 999999),**r)
    f["confidence"]=_confidence(outcome,poly,f);return f
async def binance_loop():
    global binance_depth_bids,binance_depth_asks,binance_last_trade_ms,binance_last_event_ms
    while True:
        try:
            async with websockets.connect(BINANCE_WS,ping_interval=20,ping_timeout=20,max_size=4_000_000) as ws:
                log.info("BINANCE V2 feed connected | %s",BINANCE_SYMBOL.upper())
                async for raw in ws:
                    d=json.loads(raw);p=d.get("data",d);stream=str(d.get("stream",""));binance_last_event_ms=now_ms()
                    if "aggtrade" in stream.lower() or p.get("e")=="aggTrade":
                        ts=si(p.get("T") or p.get("E") or now_ms());px=sf(p.get("p"));qty=sf(p.get("q"));q=px*qty;sg=-1 if bool(p.get("m")) else 1
                        if px>0 and qty>0:
                            binance_last_trade_ms=now_ms();binance_trades.append((ts,px,q,sg));binance_tick_prices.append((ts,px));sec=ts//1000
                            if binance_second_prices and binance_second_prices[-1][0]==sec:binance_second_prices[-1]=(sec,px)
                            else:binance_second_prices.append((sec,px))
                    elif "depth" in stream.lower():
                        binance_depth_bids=p.get("b") or [];binance_depth_asks=p.get("a") or []
        except Exception as e:log.warning("BINANCE reconnect: %s",e);await asyncio.sleep(1)

# Strategy + guard

def get_base(cid,v):
    k=(cid,v["name"])
    if k not in base_state:base_state[k]={"buys":defaultdict(int),"last_buy":{},"primary":None}
    return base_state[k]
def momentum(cid,a,lookback):
    h=price_history[cid][a]
    if len(h)<=lookback:return None,None
    return h[-1][1]-h[-1-lookback][1],h[-1-lookback][1]
def req_conf(next_buy):return GUARD_PYR2_CONF if next_buy<=2 else (GUARD_PYR3_CONF if next_buy==3 else GUARD_PYR4_CONF)
def decide(mode,f,typ,elapsed,before):
    if f["data_age_ms"]>BINANCE_SIGNAL_MAX_AGE_MS:return False,"stale"
    conf=f["confidence"];d=1 if f["_outcome"].lower()=="up" else -1;dr=d*f["ret_10s"];df=d*f["flow_10s"];path=f["path_efficiency"]
    if mode=="CONF60":return conf>=60,f"conf={conf:.1f}"
    if typ=="ENTRY":return conf>=GUARD_ENTRY_CONF,f"entry={conf:.1f}"
    if elapsed>GUARD_CUTOFF_SEC:return False,"after90"
    if before<=0:return False,"no_position"
    nb=before+1
    if mode=="GUARD90":return conf>=60,f"conf={conf:.1f}"
    rq=req_conf(nb)
    if conf<rq:return False,f"conf<{rq:.0f}"
    if mode=="GUARD90_STEP":return True,"step_ok"
    if nb>=3 and not (dr>=GUARD_RET10_MIN or df>=GUARD_FLOW_MIN):return False,"direction_bad"
    if mode=="GUARD90_FLOW":return True,"flow_ok"
    if mode=="GUARD90_ADAPTIVE":
        if nb>=3 and path<GUARD_DANGER_PATH and dr<=0 and df<=0:return False,"danger_cap20"
        return True,"adaptive_ok"
    if mode=="GUARD90_STRONG":
        if nb>=3 and path<GUARD_PATH_MIN_STRONG:return False,"weak_path"
        return True,"strong_ok"
    return False,"unknown"
def store_feature(ms,cid,v,a,outcome,typ,elapsed,poly,f):
    with db() as c:
        c.execute("INSERT INTO features(signal_ms,condition_id,variant,asset,outcome,signal_type,elapsed_sec,poly_ask,btc_price,start_price,distance_from_start_pct,ret_1s,ret_3s,ret_10s,flow_3s,flow_10s,flow_30s,book_imbalance,large_delta_10s,large_delta_30s,ema_bias,rsi14,path_efficiency,direction_changes,realized_move,regime,confidence,data_age_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(ms,cid,v["name"],a,outcome,typ,elapsed,poly,f["btc_price"],f["start_price"],f["distance_from_start_pct"],f["ret_1s"],f["ret_3s"],f["ret_10s"],f["flow_3s"],f["flow_10s"],f["flow_30s"],f["book_imbalance"],f["large_delta_10s"],f["large_delta_30s"],f["ema_bias"],f["rsi14"],f["path_efficiency"],f["direction_changes"],f["realized_move"],f["regime"],f["confidence"],f["data_age_ms"]));c.commit()
def record_trade(ms,cid,v,mode,a,outcome,typ,elapsed,buy_no,filled,avg,gross,fee,total,ok,reason):
    with db() as c:
        c.execute("INSERT INTO shadow_trades(trade_ms,condition_id,variant,mode,asset,outcome,signal_type,elapsed_sec,buy_no,filled_shares,avg_price,gross_cost,fee,total_cost,accepted,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(ms,cid,v["name"],mode,a,outcome,typ,elapsed,buy_no,filled if ok else 0,avg if ok else None,gross if ok else 0,fee if ok else 0,total if ok else 0,1 if ok else 0,reason));c.commit()
async def process_signal(m,v,a,outcome,typ,elapsed):
    age=await ensure_book(a);fills,filled=simulate_buy(a,ORDER_SIZE)
    if filled<=0:return False
    gross=sum(p*q for p,q in fills);fee=sum(fee_usdc(q,p) for p,q in fills);avg=gross/filled;total=gross+fee;ms=now_ms();f=snapshot(m["condition_id"],m,outcome,best_ask(a));f["_outcome"]=outcome
    store_feature(ms,m["condition_id"],v,a,outcome,typ,elapsed,best_ask(a),f)
    for mode in MODES:
        sk=(m["condition_id"],v["name"],mode);ck=(m["condition_id"],v["name"],mode,a);before=shadow_buys[ck]
        if typ=="PYRAMID" and a not in shadow_sides[sk]:ok=False;why="no_position"
        else:ok,why=decide(mode,f,typ,elapsed,before)
        if ok:
            if typ=="ENTRY":shadow_sides[sk].add(a)
            shadow_buys[ck]+=1
        record_trade(ms,m["condition_id"],v,mode,a,outcome,typ,elapsed,shadow_buys[ck] if ok else before,filled,avg,gross,fee,total,ok,f"{why};conf={f['confidence']:.1f};regime={f['regime']};path={f['path_efficiency']:.3f};dr10={(1 if outcome=='Up' else -1)*f['ret_10s']:.5f};df10={(1 if outcome=='Up' else -1)*f['flow_10s']:.3f}")
    st=get_base(m["condition_id"],v);st["buys"][a]+=1;st["last_buy"][a]=avg
    if typ=="ENTRY":st["primary"]=a
    return True
async def evaluate(m,v,elapsed):
    if v["cutoff"] is not None and elapsed>v["cutoff"]:return
    st=get_base(m["condition_id"],v);cands=[]
    for a,outcome in ((m["up_asset"],"Up"),(m["down_asset"],"Down")):
        ask=best_ask(a)
        if ask is None or not(MIN_PRICE<=ask<=MAX_PRICE):continue
        mom,ref=momentum(m["condition_id"],a,v["lookback"])
        if mom is None:continue
        buys=st["buys"][a];typ=None
        if buys==0:
            if st["primary"] is not None:continue
            if v["entry_price_min"] is not None and ask<v["entry_price_min"]:continue
            if v["entry_price_max"] is not None and ask>v["entry_price_max"]:continue
            if v["momentum_cap"] is not None and mom>v["momentum_cap"]:continue
            if mom>=v["entry_move"]:typ="ENTRY"
        else:
            if a!=st["primary"]:continue
            if v["momentum_cap"] is not None and mom>v["momentum_cap"]:continue
            last=st["last_buy"].get(a)
            if last is not None and ask>=last+v["pyramid_step"] and mom>0 and buys<v["max_buys_side"]:typ="PYRAMID"
        if typ:cands.append((mom,a,outcome,typ))
    if cands:
        cands.sort(reverse=True);_,a,outcome,typ=cands[0];await process_signal(m,v,a,outcome,typ,elapsed)
async def strategy_loop():
    while True:
        st=time.monotonic();n=time.time()
        try:
            for cid,m in list(markets.items()):
                elapsed=n-m["start_ts"]
                if -30<=elapsed<=310:
                    for a in (m["up_asset"],m["down_asset"]):
                        ask=best_ask(a)
                        if ask is not None:price_history[cid][a].append((now_ms(),ask))
                if elapsed<0 or elapsed>TRADE_WINDOW_SECONDS:continue
                if best_ask(m["up_asset"]) is None or best_ask(m["down_asset"]) is None:continue
                for v in BASE_VARIANTS:await evaluate(m,v,elapsed)
        except Exception:log.exception("strategy")
        await asyncio.sleep(max(.05,DECISION_INTERVAL-(time.monotonic()-st)))

# Resolution / reports

def resolve_winner(raw):
    outs=[str(x) for x in parse_jsonish(raw.get("outcomes"))];toks=[str(x) for x in parse_jsonish(raw.get("clobTokenIds"))];prs=[sf(x,-1) for x in parse_jsonish(raw.get("outcomePrices"))]
    if len(outs)>=2 and len(toks)>=2 and len(prs)>=2:
        i=max(range(len(prs)),key=lambda j:prs[j]);other=max([prs[j] for j in range(len(prs)) if j!=i] or [-1])
        if prs[i]>=.999 and other<=.001 and (raw.get("closed") or raw.get("resolved") or prs[i]>=.9999):return toks[i],outs[i]
    return None,None
async def settle(cid,win,out):
    with db() as c:
        for v in BASE_VARIANTS:
            for mode in MODES:
                if c.execute("SELECT 1 FROM shadow_results WHERE condition_id=? AND variant=? AND mode=?",(cid,v["name"],mode)).fetchone():continue
                rows=c.execute("SELECT * FROM shadow_trades WHERE condition_id=? AND variant=? AND mode=? AND accepted=1",(cid,v["name"],mode)).fetchall();cost=sum(sf(r["total_cost"]) for r in rows);payout=sum(sf(r["filled_shares"]) for r in rows if str(r["asset"])==win);pnl=payout-cost
                c.execute("INSERT INTO shadow_results(condition_id,variant,mode,winning_asset,winning_outcome,total_cost,payout,pnl,trades,settled_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",(cid,v["name"],mode,win,out,cost,payout,pnl,len(rows),now_ms()))
        c.execute("UPDATE markets SET resolved=1,winning_asset=?,winning_outcome=? WHERE condition_id=?",(win,out,cid));c.commit()
async def resolution_loop():
    while True:
        try:
            with db() as c:rows=c.execute("SELECT * FROM markets WHERE resolved=0 AND end_ts<? ORDER BY end_ts LIMIT 50",(now_ts()-10,)).fetchall()
            for r in rows:
                ev=await fetch_event(r["slug"])
                if not ev or not isinstance(ev.get("markets"),list):continue
                raw=next((x for x in ev["markets"] if str(x.get("conditionId") or "")==r["condition_id"]),None) or (ev["markets"][0] if len(ev["markets"])==1 else None)
                if raw:
                    w,o=resolve_winner(raw)
                    if w:await settle(r["condition_id"],w,o)
        except Exception:log.exception("resolution")
        await asyncio.sleep(10)

def csv_bytes(rows,cols=None):
    s=io.StringIO()
    if rows:
        cols=cols or list(rows[0].keys());w=csv.DictWriter(s,fieldnames=cols,extrasaction="ignore");w.writeheader();[w.writerow(dict(r)) for r in rows]
    elif cols:
        w=csv.DictWriter(s,fieldnames=cols);w.writeheader()
    return s.getvalue().encode("utf-8-sig")
def summary(sm,em):
    out=[]
    with db() as c:
        for v in BASE_VARIANTS:
            for mode in MODES:
                rows=c.execute("SELECT sr.* FROM shadow_results sr JOIN markets m ON m.condition_id=sr.condition_id WHERE sr.variant=? AND sr.mode=? AND m.end_ts*1000>=? AND m.end_ts*1000<?",(v["name"],mode,sm,em)).fetchall();pnl=sum(sf(r["pnl"]) for r in rows);cost=sum(sf(r["total_cost"]) for r in rows);wins=sum(sf(r["pnl"])>0 for r in rows);loss=sum(sf(r["pnl"])<0 for r in rows);trades=sum(si(r["trades"]) for r in rows)
                out.append(dict(variant=v["name"],mode=mode,markets=len(rows),wins=wins,losses=loss,trades=trades,cost=round(cost,5),pnl=round(pnl,5),roi_pct=round(pnl/cost*100,4) if cost else 0))
    return sorted(out,key=lambda x:x["pnl"],reverse=True)
def make_report(start,end):
    sm=start*1000;em=end*1000;su=summary(sm,em)
    with db() as c:
        tr=c.execute("SELECT * FROM shadow_trades WHERE trade_ms>=? AND trade_ms<? ORDER BY trade_ms",(sm,em)).fetchall();ft=c.execute("SELECT * FROM features WHERE signal_ms>=? AND signal_ms<? ORDER BY signal_ms",(sm,em)).fetchall();rs=c.execute("SELECT sr.* FROM shadow_results sr JOIN markets m ON m.condition_id=sr.condition_id WHERE m.end_ts*1000>=? AND m.end_ts*1000<? ORDER BY m.end_ts,sr.variant,sr.mode",(sm,em)).fetchall()
    d1=datetime.fromtimestamp(start,tz=timezone.utc);d2=datetime.fromtimestamp(end,tz=timezone.utc);path=REPORT_DIR/f"guard_v5_{d1:%Y-%m-%d_%H-%M}_{d2:%H-%M}_UTC.zip"
    lines=[VERSION,f"Period: {utc_iso(start)} -> {utc_iso(end)}","", "RANKING"]+[f"{x['variant']} + {x['mode']}: {x['pnl']:+.2f} | ROI {x['roi_pct']:+.2f}% | W/L {x['wins']}/{x['losses']} | trades {x['trades']}" for x in su]
    with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("guard_summary.csv",csv_bytes(su));z.writestr("guard_trades.csv",csv_bytes(tr));z.writestr("guard_results.csv",csv_bytes(rs));z.writestr("binance_features.csv",csv_bytes(ft));z.writestr("report.txt","\n".join(lines).encode())
    return path,su
async def tg_file(path,caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:return True
    form=aiohttp.FormData();form.add_field("chat_id",TELEGRAM_CHAT_ID);form.add_field("caption",caption[:1024]);form.add_field("document",path.read_bytes(),filename=path.name,content_type="application/zip")
    async with session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",data=form,timeout=aiohttp.ClientTimeout(total=120)) as r:return r.status==200
async def report_loop():
    last=si(state_get("last_report_end","0"))
    if last<=0:last=int(datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0).timestamp());state_set("last_report_end",last)
    while True:
        try:
            elig=((now_ts()-REPORT_DELAY_SECONDS)//3600)*3600
            while last<elig:
                path,su=make_report(last,last+3600);best=su[0] if su else None;ok=await tg_file(path,f"🧪 Guard V5\n{utc_iso(last)} → {utc_iso(last+3600)}\nBest: {best['variant']} + {best['mode']} {best['pnl']:+.2f}" if best else "Guard V5")
                if not ok:break
                last+=3600;state_set("last_report_end",last)
        except Exception:log.exception("report")
        await asyncio.sleep(REPORT_CHECK_INTERVAL)

async def health(req):
    return web.json_response(dict(ok=True,version=VERSION,markets=len(markets),assets=len(subscribed_assets),books=len(books),binance_ticks=len(binance_tick_prices),binance_trade_age_ms=(now_ms()-binance_last_trade_ms if binance_last_trade_ms else None),shadow_states=len(shadow_buys),time_utc=utc_iso()))
async def web_server():
    app=web.Application();app.router.add_get("/",health);app.router.add_get("/health",health);r=web.AppRunner(app);await r.setup();await web.TCPSite(r,"0.0.0.0",PORT).start();log.info("Health :%d",PORT)

async def main():
    global session
    init_db();session=aiohttp.ClientSession(headers={"User-Agent":VERSION,"Accept":"application/json"})
    tasks=[asyncio.create_task(x()) for x in (web_server,discovery_loop,poly_ws_loop,binance_loop,strategy_loop,resolution_loop,report_loop)]
    log.info("%s started | modes=%s",VERSION,MODES)
    try:await asyncio.gather(*tasks)
    finally:
        for t in tasks:t.cancel()
        await session.close()
if __name__=="__main__":
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
