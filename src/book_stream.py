"""
Живой стакан Polymarket по WebSocket.

Ключевой принцип этой версии: подписка содержит ТОЛЬКО токены текущих
активных рынков. Старые токены удаляются через operation=unsubscribe.
Это предотвращает бесконечное накопление подписок и disconnect 1013
"slow consumer: send buffer full" после нескольких часов работы.

После любого reconnect подписка строится заново из актуального набора
_desired_assets. Межсоединительная очередь команд намеренно не используется:
старые subscribe/unsubscribe не могут "протечь" в новый сокет.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

log = logging.getLogger("book_stream")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_BOOK_AGE_MS = 3000
RECONNECT_BACKOFF_SEC = 2

_books: dict[str, dict] = {}
_desired_assets: set[str] = set()
_subscription_changed = asyncio.Event()
_ws_ready = asyncio.Event()


def now_ms() -> int:
    return int(time.time() * 1000)


def _level_map(levels) -> dict[float, float]:
    out = {}
    for lvl in levels or []:
        try:
            price = float(lvl.get("price"))
            size = float(lvl.get("size"))
        except (TypeError, ValueError, AttributeError):
            continue
        if size > 0:
            out[price] = size
    return out


def _apply_snapshot(asset: str, msg: dict) -> None:
    tick = msg.get("tick_size") or msg.get("tickSize") or _books.get(asset, {}).get("tick_size") or 0.01
    _books[asset] = {
        "bids": _level_map(msg.get("bids")),
        "asks": _level_map(msg.get("asks")),
        "received_ms": now_ms(),
        "tick_size": float(tick),
    }


def _apply_delta(msg: dict) -> None:
    for ch in msg.get("price_changes", []):
        asset = str(ch.get("asset_id") or "")
        if not asset or asset not in _desired_assets:
            continue
        book = _books.setdefault(asset, {"bids": {}, "asks": {}, "received_ms": now_ms(), "tick_size": 0.01})
        try:
            price = float(ch.get("price"))
            size = float(ch.get("size"))
        except (TypeError, ValueError):
            continue
        side = str(ch.get("side", "")).upper()
        target = book["bids"] if side == "BUY" else book["asks"]
        if size <= 0:
            target.pop(price, None)
        else:
            target[price] = size
        book["received_ms"] = now_ms()


def replace_subscriptions(asset_ids: list[str] | set[str] | tuple[str, ...]) -> None:
    """Заменяет желаемый набор подписок целиком.

    Функция синхронная и безопасна для частых вызовов из main.py. Реальные
    subscribe/unsubscribe отправит текущий WS-сеанс. При reconnect новый
    сокет сразу получает полный актуальный набор.
    """
    global _desired_assets
    target = {str(a) for a in asset_ids if a}
    if target == _desired_assets:
        return

    removed = _desired_assets - target
    _desired_assets = target

    # Старые стаканы больше не должны использоваться торговой логикой.
    for asset in removed:
        _books.pop(asset, None)

    _subscription_changed.set()


def subscribe(asset_ids: list[str]) -> None:
    """Обратная совместимость: добавить токены к текущему набору."""
    replace_subscriptions(_desired_assets | {str(a) for a in asset_ids if a})


def get_book(asset: str) -> dict | None:
    if asset not in _desired_assets:
        return None
    return _books.get(asset)


def best_ask(asset: str) -> float | None:
    b = get_book(asset)
    # Статичный стакан может законно не меняться дольше 3 секунд. Поэтому
    # для торговли требуем валидный snapshot текущего соединения (received_ms
    # > 0), а не искусственную "свежесть" по таймеру. При disconnect ниже
    # received_ms принудительно обнуляется до получения нового snapshot.
    if not b or not b.get("asks") or not b.get("received_ms"):
        return None
    return min(b["asks"])


def best_bid(asset: str) -> float | None:
    b = get_book(asset)
    if not b or not b.get("bids") or not b.get("received_ms"):
        return None
    return max(b["bids"])


def ask_liquidity_usdc(asset: str, depth_levels: int = 5) -> float:
    b = get_book(asset)
    if not b or not b.get("asks") or not b.get("received_ms"):
        return 0.0
    top = sorted(b["asks"].items())[:depth_levels]
    return sum(price * size for price, size in top)


def book_imbalance(asset: str, depth_levels: int = 10) -> float | None:
    b = get_book(asset)
    if not b or not b.get("received_ms") or (not b.get("bids") and not b.get("asks")):
        return None
    bid_vol = sum(size for _, size in sorted(b.get("bids", {}).items(), reverse=True)[:depth_levels])
    ask_vol = sum(size for _, size in sorted(b.get("asks", {}).items())[:depth_levels])
    total = bid_vol + ask_vol
    if total <= 0:
        return None
    return bid_vol / total


def tick_size(asset: str) -> float:
    b = get_book(asset)
    return float(b["tick_size"]) if b and b.get("tick_size") else 0.01


def is_fresh(asset: str, max_age_ms: int = MAX_BOOK_AGE_MS) -> bool:
    if asset not in _desired_assets:
        return False
    b = _books.get(asset)
    if not b or not b.get("received_ms"):
        return False
    return (now_ms() - b["received_ms"]) <= max_age_ms


def desired_count() -> int:
    return len(_desired_assets)


def _handle_message(msg: dict) -> None:
    event_type = msg.get("event_type")
    if event_type == "book":
        asset = str(msg.get("asset_id") or "")
        if asset and asset in _desired_assets:
            _apply_snapshot(asset, msg)
    elif event_type == "price_change":
        _apply_delta(msg)
    elif event_type == "tick_size_change":
        asset = str(msg.get("asset_id") or "")
        new_tick = msg.get("new_tick_size")
        if asset in _desired_assets and asset in _books and new_tick:
            _books[asset]["tick_size"] = float(new_tick)


async def _subscription_sync_loop(ws, connected_assets: set[str]) -> None:
    """Синхронизирует один живой сокет с _desired_assets.

    connected_assets принадлежит только текущему соединению и никогда не
    переживает reconnect, поэтому старые команды не могут попасть в новый WS.
    """
    while True:
        await _subscription_changed.wait()
        _subscription_changed.clear()

        target = set(_desired_assets)
        to_add = sorted(target - connected_assets)
        to_remove = sorted(connected_assets - target)

        if to_remove:
            await ws.send(json.dumps({
                "assets_ids": to_remove,
                "operation": "unsubscribe",
            }))
            connected_assets.difference_update(to_remove)
            log.info("Book stream unsubscribe | removed=%d active=%d", len(to_remove), len(connected_assets))

        if to_add:
            await ws.send(json.dumps({
                "assets_ids": to_add,
                "operation": "subscribe",
            }))
            connected_assets.update(to_add)
            log.info("Book stream subscribe | added=%d active=%d", len(to_add), len(connected_assets))


async def run_forever() -> None:
    while True:
        sync_task = None
        try:
            # max_queue ограничивает локальный receive-buffer: лучше получить
            # controlled reconnect, чем бесконечно копить сообщения в памяти.
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
                max_queue=1024,
                close_timeout=5,
            ) as ws:
                # Сбрасываем событие ДО снимка desired, чтобы изменение
                # подписки во время initial send не потерялось.
                _subscription_changed.clear()
                connected_assets = set(_desired_assets)
                log.info("Book stream connected | subscribed=%d", len(connected_assets))

                if connected_assets:
                    await ws.send(json.dumps({
                        "type": "market",
                        "assets_ids": sorted(connected_assets),
                        "custom_feature_enabled": True,
                    }))

                _ws_ready.set()
                sync_task = asyncio.create_task(_subscription_sync_loop(ws, connected_assets))

                async for raw in ws:
                    if not raw or raw == "PONG":
                        continue
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, ValueError) as exc:
                        log.debug("Пропускаю нераспарсенное сообщение стакана (%s): %r", exc, raw[:200])
                        continue

                    messages = parsed if isinstance(parsed, list) else [parsed]
                    for msg in messages:
                        if isinstance(msg, dict):
                            _handle_message(msg)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Book stream disconnected (%s), reconnecting in %ss | desired=%d",
                exc,
                RECONNECT_BACKOFF_SEC,
                len(_desired_assets),
            )
            _ws_ready.clear()
            # Данные старого сокета после обрыва не считаем пригодными для входа.
            for asset in list(_desired_assets):
                if asset in _books:
                    _books[asset]["received_ms"] = 0
            await asyncio.sleep(RECONNECT_BACKOFF_SEC)
        finally:
            _ws_ready.clear()
            if sync_task is not None:
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass


async def wait_ready(timeout: float = 5.0) -> bool:
    try:
        await asyncio.wait_for(_ws_ready.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
