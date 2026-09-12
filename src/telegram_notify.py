"""
Телеграм-бот: пуш-уведомления о сделках/сигналах + команды управления
(/status, /pause, /resume, /pnl). Состояние паузы — просто модуль-level
флаг, которым управляет executor.
"""
from __future__ import annotations
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings
from src import storage

_app: Application | None = None
_paused = False
_state_ref = {}  # заполняется из main.py: последний сигнал/статус для /status


def is_paused() -> bool:
    return _paused


def set_state_ref(state: dict) -> None:
    global _state_ref
    _state_ref = state


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = _state_ref
    if not s:
        await update.message.reply_text("Бот запускается, данных пока нет.")
        return
    text = (
        f"Рынок: {s.get('market_slug', '—')}\n"
        f"Направление: {s.get('direction', '—')}\n"
        f"Цена BTC: {s.get('current_price', '—')} | страйк: {s.get('strike_price', '—')}\n"
        f"Осталось минут: {s.get('minutes_left', '—')}\n"
        f"Safety score: {s.get('safety_score', '—')} / порог {settings.SAFETY_SCORE_THRESHOLD}\n"
        f"Режим: {'DRY RUN' if settings.DRY_RUN else 'LIVE'} | {'ПАУЗА' if _paused else 'активен'}"
    )
    await update.message.reply_text(text)


async def _cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = True
    await update.message.reply_text("Бот на паузе — новые входы не открываются.")


async def _cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = False
    await update.message.reply_text("Бот снова активен.")


async def _cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_start = int(time.time() // 86400) * 86400
    today = storage.get_pnl_summary(today_start)
    total = storage.get_pnl_summary(0)
    await update.message.reply_text(
        f"Сегодня: {today['trades']} сделок, PnL {today['pnl_usdc']:.2f} USDC, побед {today['wins']}\n"
        f"Всего: {total['trades']} сделок, PnL {total['pnl_usdc']:.2f} USDC, побед {total['wins']}"
    )


def build_app() -> Application:
    global _app
    _app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("pause", _cmd_pause))
    _app.add_handler(CommandHandler("resume", _cmd_resume))
    _app.add_handler(CommandHandler("pnl", _cmd_pnl))
    return _app


async def notify(text: str) -> None:
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    if _app is None:
        return
    await _app.bot.send_message(chat_id=settings.TELEGRAM_CHAT_ID, text=text)
