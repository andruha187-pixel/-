"""
Исполнение решений стратегии + учёт открытых позиций и их резолюции.
Управление капиталом: не больше MAX_OPEN_POSITIONS одновременно, стоп по
дневному лимиту убытков (DAILY_LOSS_LIMIT_USDC) — при достижении лимита
новые входы блокируются до следующего дня.
"""
from __future__ import annotations
import time

from config import settings
from src import storage, polymarket_client, telegram_notify, book_stream
from src.market_discovery import ActiveMarket, get_resolution
from src.strategy import Decision


def _daily_loss_exceeded() -> bool:
    today_start = int(time.time() // 86400) * 86400
    summary = storage.get_pnl_summary(today_start)
    return summary["pnl_usdc"] <= -abs(settings.DAILY_LOSS_LIMIT_USDC)


async def maybe_enter(market: ActiveMarket, decision: Decision) -> None:
    if not decision.should_enter:
        return
    if telegram_notify.is_paused():
        return
    if storage.get_open_trade_for_market(market.slug):
        return  # уже есть позиция в этом рынке
    if _daily_loss_exceeded():
        await telegram_notify.notify("⛔ Дневной лимит убытков достигнут — вход заблокирован до конца дня.")
        return

    token_id = market.up_token_id if decision.direction == "UP" else market.down_token_id
    size_shares = round(settings.TRADE_SIZE_USDC / decision.entry_price, 2)

    # Между тем, как strategy.evaluate() прочитала ask, и моментом реальной
    # отправки ордера проходит какое-то время (сеть + подпись). Даём себе
    # небольшой запас на слиппедж, но не платим больше жёсткого потолка, и
    # обязательно выравниваем по тику — иначе CLOB отклонит ордер с неверным
    # шагом цены прямо в критичный момент.
    tick = book_stream.tick_size(token_id)
    raw_cap = min(decision.entry_price + settings.LIVE_ENTRY_MAX_SLIPPAGE, settings.MAX_ENTRY_EXECUTION_PRICE)
    execution_price = polymarket_client.round_price_for_buy(raw_cap, tick)

    status = "DRY_RUN"
    order_id = "dry-run"
    if not settings.DRY_RUN:
        try:
            resp = polymarket_client.place_buy_order(token_id, execution_price, size_shares)
            order_id = resp.get("orderID") or resp.get("order_id") or str(resp)
            status = resp.get("status", "SUBMITTED")
        except Exception as exc:  # noqa: BLE001 — любая ошибка биржи не должна ронять бота
            await telegram_notify.notify(f"❌ Ошибка при выставлении ордера: {exc}")
            return

    storage.log_trade(
        market_slug=market.slug,
        condition_id=market.condition_id,
        direction=decision.direction,
        entry_price=execution_price,
        size_usdc=settings.TRADE_SIZE_USDC,
        order_id=order_id,
        status=status,
        dry_run=settings.DRY_RUN,
    )

    await telegram_notify.notify(
        f"{'🧪 [DRY RUN] ' if settings.DRY_RUN else '✅ '}Вход {decision.direction} по {market.slug}\n"
        f"Ask на сигнале: {decision.entry_price:.3f} | Потолок исполнения: {execution_price:.3f} "
        f"(тик {tick:g}) | Размер: {settings.TRADE_SIZE_USDC} USDC\n"
        f"Safety score: {decision.safety_score} | Расхождение: {decision.distance_atr} ATR\n"
        f"До конца рынка: {decision.minutes_left:.1f} мин"
    )


async def settle_resolved_trades() -> None:
    for trade_id, market_slug, condition_id, direction, entry_price, size_usdc, dry_run in storage.get_unsettled_trades():
        outcome = await get_resolution(market_slug)
        if outcome is None:
            continue

        shares = size_usdc / entry_price
        won = outcome == direction
        pnl = (shares * 1.0 - size_usdc) if won else -size_usdc
        storage.settle_trade(trade_id, outcome, pnl)

        emoji = "🟢" if won else "🔴"
        await telegram_notify.notify(
            f"{emoji} Рынок {market_slug} зарезолвился: {outcome}. "
            f"Наша ставка: {direction}. PnL: {pnl:+.2f} USDC"
            + (" (dry run)" if dry_run else "")
        )
