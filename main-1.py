import asyncio
import logging
import time

import report
import store
from config import ASSETS, MODE
from engine import Engine
from mkt import MarketCache
from settings import Settings
from streams import BinanceStream, Chainlink, PMBook
from tg import TG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

HELP = ("Команды:\n/set имя значение — изменить параметр (например /set min_edge 0.04)\n"
        "/settings — все параметры\n/reset — обнулить симуляцию (депозит = bankroll)\n"
        "/report [часы] — отчёт (без числа — с последнего /reset)\n/status — состояние")


async def main():
    if MODE != "DRY_RUN":
        raise SystemExit("LIVE-режим намеренно не включён: сначала проверяем стратегию в DRY_RUN.")
    S = Settings()
    tg = TG()
    markets, pm, bn, cl = MarketCache(), PMBook(), BinanceStream(), Chainlink()
    eng = Engine(S, markets, pm, bn, cl, tg)
    if store.get("since") is None:
        store.put("since", time.time())
        store.put("cash", S.bankroll)
        eng.cash = S.bankroll
    st = {"next": time.time() + S.report_hours * 3600, "busy": False}

    async def send_report(hours=None):
        if st["busy"]:
            return await tg.send("Отчёт уже собирается…")
        st["busy"] = True
        try:
            path, cap = await asyncio.to_thread(report.build, S, eng, hours)
            await tg.send_file(path, cap)
        except Exception as e:
            log.exception("report")
            await tg.send(f"⚠️ Ошибка отчёта: {e}")
        finally:
            st["busy"] = False
            st["next"] = time.time() + S.report_hours * 3600

    async def status():
        now = time.time()
        age = lambda t: f"{now - t:.0f}с" if t else "нет"
        lines = [f"{'▶️ Котирует' if S.running else '⏸ Пауза'} | активы: {S.assets} | DRY RUN",
                 f"Кэш ${eng.cash:,.2f}, в окнах ${eng.locked():,.2f}, в заявках ${eng.reserved():,.2f}",
                 f"Потоки: Binance {age(bn.last_msg)}, Chainlink {age(cl.last_msg)}, стакан PM {age(pm.last_msg)}"]
        for a, r in eng.last_reason.items():
            lines.append(f"{a}: {r}")
        for (a, side), o in sorted(eng.orders.items()):
            lines.append(f"Заявка {a} {side}: {o.left:g} шт @ {o.price:.2f}, очередь {o.queue:.0f}")
        for w in eng.win.values():
            lines.append(f"Окно {w['slug']}: Up {w['qty_up']:g} (${w['cost_up']:.2f}) / Down {w['qty_dn']:g} (${w['cost_dn']:.2f})")
        n, pnl = store.q("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM windows WHERE settled_ts IS NOT NULL AND (qty_up>0 OR qty_dn>0)")[0]
        reb = store.q("SELECT COALESCE(SUM(rebate),0) FROM fills")[0][0]
        lines.append(f"Закрыто окон {n}, торговля ${pnl:+,.2f}, ребейты ~${reb:,.2f}")
        lines.append(f"Рывков Binance: {eng.stats['kills']}; следующий отчёт через {(st['next'] - now) / 60:.0f} мин")
        await tg.send("\n".join(lines), kb=True)

    async def handle(text):
        t = text.lower()
        try:
            if t.startswith("/set"):
                p = text.split()
                if len(p) != 3:
                    return await tg.send("Формат: /set имя значение")
                v = S.set(p[1], p[2])
                await tg.send(f"✅ {p[1]} = {v}" + ("\nНовый депозит применится после /reset" if p[1] == "bankroll" else ""))
            elif t in ("/settings", "⚙️ настройки"):
                await tg.send(S.text() + "\n\n" + HELP, kb=True)
            elif t in ("/start", "▶️ старт"):
                S.set_running(True)
                await tg.send("▶️ Котирование включено", kb=True)
            elif t in ("/stop", "⏸ стоп"):
                S.set_running(False)
                await tg.send("⏸ Новые заявки не ставятся (открытые окна досчитаются)", kb=True)
            elif t in ("/status", "📊 статус"):
                await status()
            elif t in ("📄 отчёт 4ч",):
                await send_report(4)
            elif t.startswith("/report") or t == "📄 отчёт":
                p = t.split()
                await send_report(float(p[1]) if len(p) > 1 else None)
            elif t == "/reset":
                await tg.send("Точно обнулить статистику симуляции? Отправь /reset_yes")
            elif t == "/reset_yes":
                eng.reset()
                await tg.send(f"♻️ Обнулено. Депозит ${S.bankroll:,.2f}", kb=True)
            elif t == "/help":
                await tg.send(HELP, kb=True)
        except Exception as e:
            await tg.send(f"⚠️ {e}")

    async def fast_loop():
        while True:
            t0 = time.time()
            try:
                eng.tick(t0)
            except Exception:
                log.exception("tick")
            await asyncio.sleep(max(0.05, 0.5 - (time.time() - t0)))

    async def reporter():
        while True:
            await asyncio.sleep(30)
            if time.time() >= st["next"]:
                await send_report(S.report_hours)

    await tg.send(f"🤖 Мейкер-бот запущен (DRY RUN)\nАктивы: {S.assets}, депозит симуляции ${S.bankroll:,.0f}\n" + HELP, kb=True)
    await asyncio.gather(bn.run(), cl.run(), pm.run(), markets.loop(pm), eng.sigma_loop(), fast_loop(),
                         eng.settle_loop(), reporter(), tg.loop(handle))


if __name__ == "__main__":
    asyncio.run(main())
