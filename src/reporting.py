"""
Периодический отчёт для этого персонального бота — раз в
REPORT_INTERVAL_HOURS формирует CSV-файлы из накопленных данных и шлёт
их в Telegram. Только momentum + hedge — у этого бота нет основной
сигнальной стратегии, поэтому таблицы signals/trades всегда пусты
(намеренно, не баг) и в отчёт не включаются.

Момент последнего отчёта хранится в bot_settings (переживает рестарт) —
чтобы при перезапуске не задваивать период и не терять данные между ним.
"""
from __future__ import annotations
import asyncio
import csv
import os
import time

from config import settings
from src import storage, telegram_notify

_LAST_REPORT_KEY = "last_report_ts"


def _write_csv(path: str, columns: list[str], rows: list[tuple]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


def _get_last_report_ts() -> int:
    saved = storage.get_all_settings().get(_LAST_REPORT_KEY)
    try:
        return int(saved)
    except (TypeError, ValueError):
        return int(time.time() - settings.REPORT_INTERVAL_HOURS * 3600)


def _set_last_report_ts(ts: int) -> None:
    storage.set_setting(_LAST_REPORT_KEY, ts)


async def build_and_send_report() -> None:
    since_ts = _get_last_report_ts()
    now_ts = int(time.time())

    momentum = storage.get_momentum_since(since_ts) if settings.MOMENTUM_TRACKER_ENABLED else []
    hedge_positions = storage.get_hedge_positions_since(since_ts)

    if not momentum and not hedge_positions:
        _set_last_report_ts(now_ts)
        return

    from_label = time.strftime("%Y%m%d-%H%M", time.gmtime(since_ts))
    to_label = time.strftime("%Y%m%d-%H%M", time.gmtime(now_ts))
    base = os.path.join(settings.REPORTS_DIR, f"{from_label}_to_{to_label}")

    if momentum:
        momentum_path = f"{base}_momentum.csv"
        _write_csv(momentum_path, storage.MOMENTUM_COLUMNS, momentum)
        reached_by_cp: dict[float, int] = {}
        for row in momentum:
            cp = row[storage.MOMENTUM_COLUMNS.index("checkpoint_price")]
            reached_by_cp[cp] = reached_by_cp.get(cp, 0) + 1
        cp_lines = "\n".join(f"  {cp:.2f}: {n} раз" for cp, n in sorted(reached_by_cp.items()))
        momentum_caption = (
            f"🔬 Momentum-отчёт {from_label} → {to_label}\n"
            f"Контрольных точек зафиксировано: {len(momentum)}\n\n"
            f"По уровням:\n{cp_lines}"
        )
        await telegram_notify.send_document(momentum_path, momentum_caption)

    if hedge_positions:
        hedge_path = f"{base}_hedge.csv"
        _write_csv(hedge_path, storage.HEDGE_COLUMNS, hedge_positions)
        closed = [row for row in hedge_positions if row[storage.HEDGE_COLUMNS.index("status")] == "closed"]
        hedged_count = sum(1 for row in closed if row[storage.HEDGE_COLUMNS.index("hedge_price")] is not None)
        pnl_sum = sum(row[storage.HEDGE_COLUMNS.index("pnl_usdc")] or 0 for row in closed)
        hedge_caption = (
            f"🔒 Хедж-бот {from_label} → {to_label}\n"
            f"Позиций закрыто: {len(closed)} (захеджировано: {hedged_count}, "
            f"без хеджа: {len(closed) - hedged_count}) | PnL: {pnl_sum:+.2f} USDC (без учёта комиссии)"
        )
        await telegram_notify.send_document(hedge_path, hedge_caption)

    _set_last_report_ts(now_ts)


async def report_loop() -> None:
    while True:
        try:
            await build_and_send_report()
        except Exception:
            pass
        await asyncio.sleep(max(60, settings.REPORT_INTERVAL_HOURS * 3600))
