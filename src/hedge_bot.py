"""
Хедж-стратегия, откалиброванная на реальных momentum-отчётах (2026-09-20):
1. Как только цена стороны рынка впервые достигает HEDGE_ENTRY_PRICE
   (по умолчанию 0.70) — покупаем эту сторону.
2. Если цена продолжает расти и достигает HEDGE_TRIGGER_PRICE (0.90) до
   конца окна — докупаем ПРОТИВОПОЛОЖНУЮ сторону в таком количестве акций,
   чтобы держать РОВНО одинаковое число акций с обеих сторон. При равном
   числе акций выплата фиксирована (ровно это число акций × $1) независимо
   от исхода — значит, и прибыль (выплата минус суммарные затраты)
   одинакова в обоих исходах, то есть зафиксирована.
3. Если цена НЕ доходит до HEDGE_TRIGGER_PRICE — остаёмся с односторонней
   позицией. Это не мелочь: по нашим данным именно эти случаи почти
   гарантированно проигрывают (застрявшая сторона выигрывает в ~4.5%
   случаев) — учитывай это в размере ставки.

Порог 0.90 (не 0.70-0.85) выбран по факту бэктеста на реальных отчётах:
чем позже хеджируешь, тем больше гарантированная маржа с каждого
успешного хеджа, и это перевешивает потери от возросшего числа случаев,
где хедж вообще не срабатывает. См. обсуждение в чате от 2026-09-20 —
на четырёх отчётах (366 сессий) хедж на 0.90 дал +$29.45, на 0.70-0.85 —
убыток, несмотря на то что сам хедж каждый раз безубыточен по построению.

ВАЖНО про комиссию: pnl_usdc здесь считается БЕЗ вычета комиссии тейкера
(7% для крипторынков на КАЖДУЮ ногу) — так же, как и у основной стратегии
в этом боте. Реальный итог на кошельке будет чуть хуже, чем показывают
отчёты. См. обсуждение комиссий в README.
"""
from __future__ import annotations
import logging
import time

from config import settings
from src import book_stream, market_discovery, polymarket_client, runtime_state, storage, telegram_notify
from src.market_discovery import ActiveMarket
from src.timeframes import TimeframeProfile

log = logging.getLogger("hedge_bot")

_last_warned_config: tuple | None = None  # чтобы не спамить одним и тем же предупреждением каждый тик

# Зеркало открытых позиций В ПАМЯТИ — (market_slug, side) -> dict с полями
# position_id/entry_shares/entry_cost/status. Быстрый цикл (check_market,
# раз в HEDGE_POLL_SECONDS x 12 потоков) читает ТОЛЬКО это, ни разу не
# трогая SQLite — иначе синхронные (блокирующие) вызовы к диску на каждом
# тике останавливают весь event loop, включая чтение WS-сокета, и сервер
# отключает нас как "slow consumer" (реальный случай, 2026-09-21). SQLite
# остаётся источником истины и пишется при каждом реальном изменении
# состояния (вход/хедж/резолюция), просто не читается на каждый тик.
_open_positions: dict[tuple[str, str], dict] = {}
_positions_loaded = False

# Счётчики причин пропуска в памяти — (asset, timeframe) -> {причина: счёт}.
# НЕ пишутся в БД на каждый тик (это и вызвало проблему со "slow consumer"
# в прошлый раз) — только читаются и сбрасываются раз в REPORT_INTERVAL_HOURS
# из reporting.py, чтобы попасть в 4-часовой отчёт.
_skip_counts: dict[tuple[str, str], dict[str, int]] = {}

SKIP_REASON_LABELS = {
    "no_price": "нет цены в стакане",
    "waiting_for_entry": "ждём цену входа",
    "missed_entry_window": "цена проскочила мимо входа",
    "daily_loss_limit": "дневной лимит убытка",
    "max_open_positions": "потолок открытых позиций",
    "hedge_leg_too_small": "нога хеджа меньше минимума ордера",
    "waiting_for_hedge": "ждём цену хеджа",
    "entered": "вход выполнен",
    "hedged": "хедж выполнен",
}


def _count(asset: str, timeframe_label: str, reason: str) -> None:
    key = (asset, timeframe_label)
    bucket = _skip_counts.setdefault(key, {})
    bucket[reason] = bucket.get(reason, 0) + 1


def get_and_reset_skip_counts() -> dict[tuple[str, str], dict[str, int]]:
    """Вызывается из reporting.py раз в REPORT_INTERVAL_HOURS — забирает
    накопленное и обнуляет счётчики для следующего периода."""
    global _skip_counts
    snapshot = _skip_counts
    _skip_counts = {}
    return snapshot


def _load_open_positions_from_db() -> None:
    """Разово при старте (и один раз после падения) — восстанавливаем
    зеркало из БД на случай, если бот перезапустился с открытыми позициями."""
    global _positions_loaded
    for pos_id, market_slug, side, entry_shares, entry_cost, hedge_shares, hedge_cost, status, dry_run in \
            storage.get_unsettled_hedge_positions():
        _open_positions[(market_slug, side)] = {
            "position_id": pos_id, "entry_shares": entry_shares, "entry_cost": entry_cost, "status": status,
        }
    _positions_loaded = True


def _hedge_leg_too_small(stake: float, entry_price: float, hedge_price: float) -> bool:
    """Нога хеджа стоит stake*(1-hedge_price)/entry_price — если это ниже
    минимального ордера Polymarket, хедж физически не сможет исполниться,
    и вся позиция навсегда останется незахеджированной (ровно тот
    убыточный сценарий, которого хедж должен избегать)."""
    hedge_leg_cost = stake * (1 - hedge_price) / entry_price
    return hedge_leg_cost < settings.MIN_VIABLE_TRADE_USDC


def _daily_loss_exceeded() -> bool:
    """Полночь UTC — тот же принцип, что и у основного бота: разово в
    сутки сбрасывается счётчик, чтобы не копить убыток бесконечно."""
    midnight_ts = int(time.time() // 86400) * 86400
    pnl_today = storage.get_hedge_pnl_since(midnight_ts)
    return pnl_today <= -abs(runtime_state.get("daily_loss_limit_usdc"))


async def _execute_entry(market: ActiveMarket, side: str, token_id: str, price_hint: float,
                          timeframe: TimeframeProfile) -> None:
    dry_run = runtime_state.get("dry_run")
    stake = runtime_state.get("hedge_stake_usdc")

    book = await polymarket_client.get_orderbook_cached(token_id, depth_levels=10)
    available = book.ask_liquidity_usdc
    if available < settings.MIN_VIABLE_TRADE_USDC:
        return
    if available < stake:
        stake = round(available * 0.9, 2)

    tick = book.tick_size or book_stream.tick_size(token_id)
    reference_price = book.best_ask or price_hint
    if reference_price is None:
        return
    price_cap = polymarket_client.round_price_for_buy(
        min(reference_price + settings.LIVE_ENTRY_MAX_SLIPPAGE, 0.99), tick,
    )

    order_id = "dry-run"
    if not dry_run:
        if not settings.POLY_PRIVATE_KEY:
            await telegram_notify.notify("❌ Хедж-бот: LIVE включён, но POLY_PRIVATE_KEY не задан — вход пропущен.")
            return
        try:
            resp = await polymarket_client.place_buy_order(token_id, price_cap, stake, tick)
        except Exception as exc:  # noqa: BLE001
            await telegram_notify.notify(f"❌ Хедж-бот: ошибка входа ({market.slug}): {exc}")
            return
        order_id = polymarket_client.response_field(resp, "order_id") or str(resp)

    shares = stake / price_cap
    position_id = storage.create_hedge_position(
        market.slug, market.asset, timeframe.label, side, price_cap, shares, stake, token_id, dry_run,
    )
    _open_positions[(market.slug, side)] = {
        "position_id": position_id, "entry_shares": shares, "entry_cost": stake, "status": "open_unhedged",
    }
    await telegram_notify.notify(
        f"{'🧪 [DRY RUN] ' if dry_run else ''}🔷 Хедж-бот: вход {side} по {market.slug}\n"
        f"Цена: {price_cap:.3f} | Размер: {stake:.2f} USDC | ждём {runtime_state.get('hedge_trigger_price'):.2f} для хеджа"
    )


async def _execute_hedge(market: ActiveMarket, side: str, position_id: int, entry_shares: float,
                          opposite_token_id: str) -> None:
    dry_run = runtime_state.get("dry_run")

    opp_book = await polymarket_client.get_orderbook_cached(opposite_token_id, depth_levels=10)
    opp_price = opp_book.best_ask
    if opp_price is None:
        return  # нет стакана на другой стороне прямо сейчас — попробуем на следующем тике

    tick = opp_book.tick_size or book_stream.tick_size(opposite_token_id)
    price_cap = polymarket_client.round_price_for_buy(
        min(opp_price + settings.LIVE_ENTRY_MAX_SLIPPAGE, 0.99), tick,
    )

    # Хотим РОВНО entry_shares акций на другой стороне — тогда выплата
    # фиксирована (entry_shares x $1) независимо от исхода. Считаем нужную
    # сумму ПО ЦЕНЕ С УЧЁТОМ ПРОСКАЛЬЗЫВАНИЯ (price_cap), а не по цене ДО
    # него (opp_price) — иначе получим меньше акций, чем entry_shares, и
    # гарантия равной прибыли в обоих исходах перестаёт выполняться (баг,
    # найденный на реальных данных 2026-09-20: hedge_shares систематически
    # оказывались меньше entry_shares).
    target_cost = entry_shares * price_cap
    available = opp_book.ask_liquidity_usdc
    if available < settings.MIN_VIABLE_TRADE_USDC:
        return
    if available < target_cost:
        target_cost = round(available * 0.9, 2)  # неполный хедж лучше, чем никакого

    if not dry_run:
        if not settings.POLY_PRIVATE_KEY:
            await telegram_notify.notify("❌ Хедж-бот: LIVE включён, но POLY_PRIVATE_KEY не задан — хедж пропущен.")
            return
        try:
            resp = await polymarket_client.place_buy_order(opposite_token_id, price_cap, target_cost, tick)
        except Exception as exc:  # noqa: BLE001
            await telegram_notify.notify(f"❌ Хедж-бот: ошибка хеджа ({market.slug}): {exc}")
            return

    hedge_shares = target_cost / price_cap
    storage.mark_hedged(position_id, price_cap, hedge_shares, target_cost, opposite_token_id)
    if (market.slug, side) in _open_positions:
        _open_positions[(market.slug, side)]["status"] = "hedged"

    await telegram_notify.notify(
        f"{'🧪 [DRY RUN] ' if dry_run else ''}🔒 Хедж-бот: зафиксирован хедж по {market.slug} ({side})\n"
        f"Докупили противоположную сторону по {price_cap:.3f}, {hedge_shares:.2f} акций "
        f"(на входе было {entry_shares:.2f}) — прибыль зафиксирована независимо от исхода."
    )


async def check_market(market: ActiveMarket, timeframe: TimeframeProfile) -> None:
    global _last_warned_config, _positions_loaded
    if not runtime_state.get("hedge_bot_enabled"):
        return

    if not _positions_loaded:
        _load_open_positions_from_db()  # разово при первом тике после старта

    entry_price = runtime_state.get("hedge_entry_price")
    hedge_price = runtime_state.get("hedge_trigger_price")
    stake = runtime_state.get("hedge_stake_usdc")

    if _hedge_leg_too_small(stake, entry_price, hedge_price):
        _count(market.asset, timeframe.label, "hedge_leg_too_small")
        config_key = (stake, entry_price, hedge_price)
        if _last_warned_config != config_key:
            _last_warned_config = config_key
            min_stake = settings.MIN_VIABLE_TRADE_USDC * entry_price / (1 - hedge_price)
            await telegram_notify.notify(
                f"⚠️ Хедж-бот: при ставке {stake:.2f}, входе {entry_price:.2f} и хедже {hedge_price:.2f} "
                f"нога хеджа стоила бы меньше минимального ордера (${settings.MIN_VIABLE_TRADE_USDC:.2f}) — "
                f"хедж физически не сможет исполниться. Нужна ставка от ${min_stake:.2f}. "
                f"Новые входы приостановлены, пока не поправишь размер ставки."
            )
        return  # не входим вслепую без возможности потом захеджироваться

    sides = [
        ("UP", market.up_token_id, market.down_token_id),
        ("DOWN", market.down_token_id, market.up_token_id),
    ]
    for side, token_id, opposite_token_id in sides:
        price = book_stream.best_ask(token_id)
        if price is None:
            _count(market.asset, timeframe.label, "no_price")
            continue

        # Читаем ТОЛЬКО зеркало в памяти — ни одного обращения к SQLite на
        # этом (горячем, раз в секунду x 12 потоков) пути.
        existing = _open_positions.get((market.slug, side))
        if existing is None:
            entry_tolerance = runtime_state.get("hedge_entry_tolerance")
            if entry_price <= price <= entry_price + entry_tolerance:
                if _daily_loss_exceeded():
                    _count(market.asset, timeframe.label, "daily_loss_limit")
                    continue  # дневной лимит убытка сработал — новых входов не открываем
                if len(_open_positions) >= runtime_state.get("max_open_positions"):
                    _count(market.asset, timeframe.label, "max_open_positions")
                    continue  # общий потолок одновременно открытых позиций
                _count(market.asset, timeframe.label, "entered")
                await _execute_entry(market, side, token_id, price, timeframe)
            elif price > entry_price + entry_tolerance:
                # Цена уже проскочила мимо входа за один тик (типично на 5m) —
                # не гонимся за ней, экономика хеджа рассчитана именно на вход
                # около entry_price, не на любую цену выше него (баг, найденный
                # на реальных данных 2026-09-20: средняя цена входа была 0.839
                # вместо 0.70).
                _count(market.asset, timeframe.label, "missed_entry_window")
            else:
                _count(market.asset, timeframe.label, "waiting_for_entry")
        elif existing["status"] == "open_unhedged" and price >= hedge_price:
            _count(market.asset, timeframe.label, "hedged")
            await _execute_hedge(market, side, existing["position_id"], existing["entry_shares"], opposite_token_id)
        elif existing["status"] == "open_unhedged":
            _count(market.asset, timeframe.label, "waiting_for_hedge")


async def settle_resolved() -> None:
    """Общая фоновая задача (как executor.settle_resolved_trades) — узнаём
    исход рынков с открытыми хедж-позициями и фиксируем итоговый PnL."""
    for pos_id, market_slug, side, entry_shares, entry_cost, hedge_shares, hedge_cost, status, dry_run in \
            storage.get_unsettled_hedge_positions():
        try:
            outcome = await market_discovery.get_resolution(market_slug)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось узнать исход %s: %s", market_slug, exc)
            continue
        if not outcome:
            continue

        won = outcome == side
        if status == "hedged":
            realized_shares = entry_shares if won else hedge_shares
            total_cost = entry_cost + (hedge_cost or 0)
            pnl = realized_shares * 1.0 - total_cost
        else:  # open_unhedged — не успели захеджировать до резолюции
            pnl = (entry_shares * 1.0 - entry_cost) if won else -entry_cost

        storage.settle_hedge_position(pos_id, outcome, pnl)
        _open_positions.pop((market_slug, side), None)  # больше не открыта — убираем из зеркала в памяти
        emoji = "🟢" if pnl > 0 else "🔴"
        await telegram_notify.notify(
            f"{emoji} Хедж-бот: {market_slug} ({side}) зарезолвился {outcome}. "
            f"{'Хедж сработал' if status == 'hedged' else 'Без хеджа (не дошло до порога)'}. "
            f"PnL: {pnl:+.2f} USDC" + (" (dry run)" if dry_run else "")
        )
