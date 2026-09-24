"""Отчёт мейкер-бота: zip (summary.md, fills.csv, windows.csv, settings.json) + подпись."""
import json
import os
import sqlite3
import time
import zipfile

import numpy as np
import pandas as pd

import store
from config import DB_PATH, OUT_DIR


def _p(x):
    return f"{x*100:.1f}%" if pd.notna(x) else "—"


def build(settings, engine, since_hours=None):
    con = sqlite3.connect(DB_PATH)
    fills = pd.read_sql("SELECT * FROM fills", con)
    wins = pd.read_sql("SELECT * FROM windows", con)
    con.close()
    S = settings
    since = store.get("since", None)
    t_from = time.time() - since_hours * 3600 if since_hours else (since or 0)
    L = [f"# Мейкер-бот (DRY RUN) — отчёт {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
         f"Период: {'последние %sч' % since_hours if since_hours else 'с последнего /reset'}", ""]
    f = fills[fills["ts"] >= t_from].copy()
    w = wins[(wins["start"] >= t_from - 900)].copy()
    ws = w.dropna(subset=["settled_ts"])
    ws = ws[(ws["qty_up"] > 0) | (ws["qty_dn"] > 0)]
    reb = f["rebate"].sum() if len(f) else 0.0
    tp = ws["pnl"].sum() if len(ws) else 0.0
    locked = engine.locked()
    equity = engine.cash + locked
    L += ["## Итог",
          f"Торговый PnL (закрытые окна): ${tp:,.2f}",
          f"Оценка ребейтов мейкера: ${reb:,.2f}  (20% тейкерской комиссии за наши филлы)",
          f"**Итого с ребейтами: ${tp + reb:,.2f}**",
          f"Кэш ${engine.cash:,.2f} + в открытых окнах ${locked:,.2f} = ${equity:,.2f} "
          f"(старт ${S.bankroll:,.2f}; ребейты не зачислены в кэш — их платят раз в день)", ""]
    if len(f):
        vol = (f["price"] * f["size"]).sum()
        L += ["## Активность",
              f"Филлов: {len(f)}, куплено {f['size'].sum():,.0f} шт на ${vol:,.2f}",
              f"Окон с позицией: {len(ws)} закрыто + {len(engine.win)} открыто",
              "Тип исполнения: " + ", ".join(f"{k} {_p(v)}" for k, v in f["kind"].value_counts(normalize=True).items()),
              f"Снятий заявок по рывку Binance: {engine.stats['kills']}; доля тактов с котировками: "
              f"{_p(engine.stats['quoting_ticks'] / max(engine.stats['ticks'], 1))}", ""]
        # markout: насколько цена ушла против нас после филла
        for col, lab in (("mid10", "10с"), ("mid60", "60с")):
            mo = (f[col] - f["price"]).dropna()
            if len(mo):
                L.append(f"Markout {lab}: средний {mo.mean()*100:+.2f}¢ (мед. {mo.median()*100:+.2f}¢), "
                         f"против нас в {_p((mo < 0).mean())} филлов")
        hedge = np.where(f["side"] == "up", f["net_before"] < 0, f["net_before"] > 0)
        for flag, lab in ((False, "открывающие"), (True, "выравнивающие")):
            mo = (f.loc[hedge == flag, "mid60"] - f.loc[hedge == flag, "price"]).dropna()
            if len(mo):
                L.append(f"Markout 60с, {lab} филлы: {mo.mean()*100:+.2f}¢ ({len(mo)} шт)")
        for k, g in f.groupby("kind"):
            mo = (g["mid60"] - g["price"]).dropna()
            if len(mo):
                L.append(f"Markout 60с, исполнение «{k}»: {mo.mean()*100:+.2f}¢ ({len(mo)} шт)")
        fe = (f["fair"] - f["price"]).dropna()
        if len(fe):
            L.append(f"Цена филла vs справедливая: в среднем {fe.mean()*100:+.2f}¢ в нашу пользу")
        won = f.dropna(subset=["won"])
        if len(won):
            L.append(f"Винрейт филла {_p(won['won'].mean())} при средней цене {won['price'].mean():.3f} "
                     f"→ реальный edge {(won['won'].mean() - won['price'].mean())*100:+.1f}пп")
        L.append("")
    if len(ws):
        ws["both"] = (ws["qty_up"] > 0) & (ws["qty_dn"] > 0)
        ws["pair"] = ws["cost_up"] / ws["qty_up"].replace(0, np.nan) + ws["cost_dn"] / ws["qty_dn"].replace(0, np.nan)
        ws["imb"] = (ws["qty_up"] - ws["qty_dn"]).abs() / (ws["qty_up"] + ws["qty_dn"])
        ws["locked_m"] = np.minimum(ws["qty_up"], ws["qty_dn"]) * (1 - ws["pair"].fillna(1))
        L += ["## Окна",
              f"Плюсовых: {_p((ws['pnl'] > 0).mean())}; обе стороны в {_p(ws['both'].mean())}; "
              f"средняя цена пары {ws['pair'].mean():.3f}",
              f"Маржа собранных пар: ${ws['locked_m'].sum():,.2f}; направленная часть (перекос): "
              f"${tp - ws['locked_m'].sum():,.2f}",
              f"Перекос: медиана {_p(ws['imb'].median())}, p90 {_p(ws['imb'].quantile(.9))}"]
        cut = pd.cut(ws["imb"], [-0.01, 0.05, 0.15, 0.35, 1.0], labels=["0-5%", "5-15%", "15-35%", ">35%"])
        g = ws.groupby(cut, observed=True)["pnl"].agg(["count", "sum", "mean"])
        L.append("PnL по перекосу: " + "; ".join(f"{k}: {int(r['count'])} окон, ${r['sum']:,.2f} (ср ${r['mean']:.2f})"
                                                  for k, r in g.iterrows()))
        if "flat_qty" in ws and ws["flat_qty"].notna().any():
            fw = ws[ws["flat_qty"].notna()]
            won_side = np.where(fw["winner"] == "up", "up", "dn")
            hold = (fw["flat_qty"] * (fw["flat_side"] == won_side)).sum()
            L.append(f"Сброс перекоса: {len(fw)} окон, продали {fw['flat_qty'].sum():.0f} шт за ${fw['flat_cash'].sum():.2f} "
                     f"(ср. цена {fw['flat_px'].mean():.2f}); если бы держали до конца — получили бы ${hold:.2f}. "
                     f"Эффект сброса: ${fw['flat_cash'].sum() - hold:+.2f}")
        ws["hour"] = pd.to_datetime(ws["start"], unit="s").dt.hour
        hh = ws.groupby("hour")["pnl"].sum()
        L.append("PnL по часам UTC: " + ", ".join(f"{h}:{v:+.1f}" for h, v in hh.items()))
        L.append("По активам: " + ", ".join(f"{a}: ${v:,.2f}" for a, v in ws.groupby("asset")["pnl"].sum().items()))
        L.append("")
    sk = engine.stats["skip"]
    if sk:
        tot = sum(sk.values())
        L += ["## Почему не котировали (доля тактов)",
              ", ".join(f"{k} {_p(v / tot)}" for k, v in sorted(sk.items(), key=lambda x: -x[1])[:10]), ""]
    L += ["## Подсказки"] + hints(f, ws, S) + ["", "## Настройки", "```", S.text(), "```"]
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, time.strftime("maker_%Y%m%d_%H%M.zip", time.gmtime()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.md", "\n".join(L))
        z.writestr("fills.csv", f.to_csv(index=False))
        z.writestr("windows.csv", w.to_csv(index=False))
        z.writestr("settings.json", json.dumps(S.dump(), ensure_ascii=False, indent=1))
    cap = [f"🤖 Мейкер DRY RUN — {L[1].replace('Период: ', '')}",
           f"Торговля ${tp:+,.2f} + ребейты ~${reb:,.2f} = ${tp + reb:+,.2f}",
           f"Филлов {len(f)}, окон {len(ws)}, капитал ${equity:,.2f}"]
    if len(f) and f["mid10"].notna().any():
        cap.append(f"Markout 10с: {(f['mid10'] - f['price']).mean()*100:+.2f}¢")
    return path, "\n".join(cap)


def hints(f, ws, S):
    h = []
    if len(f) < 30:
        return ["- Пока мало филлов для выводов (нужно ≥30)."]
    mo = (f["mid10"] - f["price"]).dropna()
    if len(mo) > 20 and mo.mean() < -0.01:
        h.append(f"- Нас «подбирают»: markout 10с {mo.mean()*100:+.1f}¢. Попробуй: kill_bps ниже "
                 f"(сейчас {S.kill_bps}), min_edge выше (сейчас {S.min_edge}), improve=0.")
    if len(ws) > 10:
        imb = ((ws["qty_up"] - ws["qty_dn"]).abs() / (ws["qty_up"] + ws["qty_dn"])).median()
        if imb > 0.2:
            h.append(f"- Сильный перекос (медиана {imb*100:.0f}%). Попробуй: max_imbalance ниже "
                     f"(сейчас {S.max_imbalance}), skew_per_share выше, hedge_max_pair 1.01.")
        pair = (ws["cost_up"] / ws["qty_up"].replace(0, np.nan) + ws["cost_dn"] / ws["qty_dn"].replace(0, np.nan)).mean()
        if pd.notna(pair) and pair > 1.0:
            h.append(f"- Пары в среднем дороже $1 ({pair:.3f}) — маржи нет, увеличь min_edge.")
    fr = len(f) / max(len(ws), 1)
    if fr < 2:
        h.append("- Мало филлов на окно — можно improve=1, уменьшить min_edge или queue_mult.")
    return h or ["- Явных проблем не видно — копим статистику."]
