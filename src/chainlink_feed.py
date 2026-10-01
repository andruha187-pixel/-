"""
Цена Chainlink — та самая, по которой Polymarket решает исход Up/Down-рынков.

Зачем: основной бот меряет расстояние до страйка по Binance (BTCUSDT), а
Polymarket резолвит рынок по Chainlink BTC/USD. Между ними бывает разница в
десятки долларов. По отчётам 25.09–01.10 (1233 закрытых рынка BTC): когда
Binance за секунду до конца окна показывал +$20–30 от страйка, Polymarket
решал наоборот в 10% случаев, при +$30–50 — в 5%. То есть «расстояние»,
которое видел бот, было неточным ровно в тех пограничных случаях, где мы
и проигрывали.

Источник — публичный Real-Time Data Socket Polymarket (без ключей):
  wss://ws-live-data.polymarket.com
  подписка: {"action": "subscribe",
             "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}]}
  сообщение: {"topic": "crypto_prices_chainlink", "type": "update",
              "payload": {"symbol": "btc/usd", "timestamp": <ms>, "value": <float>}}
Фильтр по символу НЕ передаём (формат фильтра в разных версиях документации
разный) — получаем все символы и отбираем нужные сами. Сервер требует
текстовый PING каждые ~5 секунд, иначе закрывает соединение.

Храним историю за последние ~20 минут: из неё берём и текущую цену, и цену
на момент открытия окна (страйк).
"""
from __future__ import annotations
import asyncio
import bisect
import json
import logging
import time

import websockets

log = logging.getLogger("chainlink_feed")

WS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"
PING_INTERVAL_SEC = 5
RECONNECT_BACKOFF_SEC = 2
HISTORY_SEC = 20 * 60

# asset -> отсортированные по времени списки (ts_sec, value)
_hist_ts: dict[str, list[float]] = {}
_hist_val: dict[str, list[float]] = {}
_last_rx: dict[str, float] = {}      # asset -> локальное время последнего апдейта
_connected = False


def _asset_from_symbol(symbol: str) -> str | None:
    s = (symbol or "").lower().strip()
    if not s:
        return None
    # "btc/usd", "btcusd", "BTC/USD" -> "btc"
    for sep in ("/", "-", "_"):
        if sep in s:
            return s.split(sep, 1)[0]
    if s.endswith("usd"):
        return s[:-3]
    return s


def _to_sec(ts) -> float | None:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e11 else v


def _add_point(asset: str, ts_sec: float, value: float) -> None:
    tss = _hist_ts.setdefault(asset, [])
    vals = _hist_val.setdefault(asset, [])
    if tss and ts_sec <= tss[-1]:
        if ts_sec == tss[-1]:
            vals[-1] = value
            _last_rx[asset] = time.time()
            return
        i = bisect.bisect_left(tss, ts_sec)
        if i < len(tss) and tss[i] == ts_sec:
            vals[i] = value
        else:
            tss.insert(i, ts_sec)
            vals.insert(i, value)
    else:
        tss.append(ts_sec)
        vals.append(value)
    _last_rx[asset] = time.time()
    cutoff = ts_sec - HISTORY_SEC
    drop = bisect.bisect_left(tss, cutoff)
    if drop > 0:
        del tss[:drop]
        del vals[:drop]


def _handle_payload(payload) -> None:
    if isinstance(payload, list):
        for p in payload:
            _handle_payload(p)
        return
    if not isinstance(payload, dict):
        return
    asset = _asset_from_symbol(payload.get("symbol") or payload.get("feed") or "")
    # Снапшот истории при подписке может прийти как {"symbol":..., "data":[{timestamp,value},...]}
    if isinstance(payload.get("data"), list) and asset:
        for p in payload["data"]:
            if isinstance(p, dict):
                ts = _to_sec(p.get("timestamp"))
                val = p.get("value")
                if ts is not None and val is not None:
                    try:
                        _add_point(asset, ts, float(val))
                    except (TypeError, ValueError):
                        pass
        return
    ts = _to_sec(payload.get("timestamp"))
    val = payload.get("value")
    if not asset or ts is None or val is None:
        return
    try:
        _add_point(asset, ts, float(val))
    except (TypeError, ValueError):
        return


def _handle_message(msg) -> None:
    if isinstance(msg, list):
        for m in msg:
            _handle_message(m)
        return
    if not isinstance(msg, dict):
        return
    topic = msg.get("topic")
    if topic and topic != TOPIC:
        return
    if "payload" in msg:
        _handle_payload(msg["payload"])
    elif "symbol" in msg:
        _handle_payload(msg)


def latest(asset: str) -> tuple[float | None, float | None]:
    """(цена, возраст в секундах по времени оракула) или (None, None)."""
    tss = _hist_ts.get(asset.lower())
    if not tss:
        return None, None
    return _hist_val[asset.lower()][-1], max(0.0, time.time() - tss[-1])


def price_at(asset: str, ts_sec: float, tolerance_sec: float = 3.0) -> float | None:
    """Цена оракула на момент ts_sec: первая точка с временем >= ts_sec
    (не дальше tolerance_sec). Если точной точки нет, но есть точка чуть
    раньше (в пределах tolerance) — берём её. Иначе None: не гадаем."""
    a = asset.lower()
    tss = _hist_ts.get(a)
    if not tss:
        return None
    vals = _hist_val[a]
    i = bisect.bisect_left(tss, ts_sec)
    if i < len(tss) and tss[i] - ts_sec <= tolerance_sec:
        return vals[i]
    if i > 0 and ts_sec - tss[i - 1] <= tolerance_sec:
        return vals[i - 1]
    return None


def is_connected() -> bool:
    return _connected


async def _pinger(ws) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_SEC)
        await ws.send("PING")


async def run_forever() -> None:
    global _connected
    sub = {"action": "subscribe", "subscriptions": [{"topic": TOPIC, "type": "*"}]}
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_size=2**22) as ws:
                await ws.send(json.dumps(sub))
                _connected = True
                log.info("Chainlink RTDS connected")
                pinger = asyncio.create_task(_pinger(ws))
                try:
                    async for raw in ws:
                        if not raw or raw in ("PONG", "pong"):
                            continue
                        try:
                            parsed = json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        _handle_message(parsed)
                finally:
                    pinger.cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("Chainlink RTDS disconnected (%s), reconnecting in %ss", exc, RECONNECT_BACKOFF_SEC)
        _connected = False
        await asyncio.sleep(RECONNECT_BACKOFF_SEC)
