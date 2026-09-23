"""Разбор стратегии кошелька. Запускается в отдельном потоке.
Выход: zip (report.md + CSV) и короткая подпись для Telegram."""
import json
import os
import sqlite3
import time
import zipfile

import numpy as np
import pandas as pd

from config import ANALYSIS_MAX_CONTROLS, DB_PATH, DECISION_LAG_SEC, OUT_DIR, WALLET

SIGNED = ["delta_bps", "z", "ret5s_bps", "ret15s_bps", "ret30s_bps", "ret60s_bps", "ret180s_bps",
          "cvd15s", "cvd60s", "m1_px_ema9", "m1_px_ema21", "m1_px_ema50", "m1_ema9_21", "m1_macdh_bps",
          "m1_ret1_bps", "m5_px_ema9", "m5_px_ema21", "m5_px_ema50", "m5_ema9_21", "m5_macdh_bps",
          "m5_ret1_bps", "bn_book_imb", "cl_bn_basis_bps", "by_basis_bps", "cb_basis_bps"]
FLIP100 = ["m1_rsi", "m5_rsi"]
FLIP1 = ["m1_bb", "m5_bb", "buy_ratio15s", "buy_ratio60s"]
UNSIGNED = ["sec_left", "sec_into", "hour", "sigma1m_bps", "m1_atr_bps", "m5_atr_bps", "m1_vol_z", "m5_vol_z",
            "rv60s_bps", "usd60s", "n60s", "pm_sum_ask", "pm_sum_bid", "bn_funding", "oi_chg5m_pct", "pm_n30"]
PM_FIELDS = ["ask", "bid", "mid", "spread", "bsz5", "asz5", "imb5", "bsz1", "asz1"]


# ---------------- загрузка ----------------
def _expand(df):
    if df.empty:
        return df
    feats = [json.loads(x) if isinstance(x, str) and x else {} for x in df["feat"]]
    f = pd.DataFrame(feats, index=df.index)
    f = f.drop(columns=[c for c in f.columns if c in df.columns], errors="ignore")
    return pd.concat([df.drop(columns=["feat"]), f], axis=1)


BASE_COLS = "ts,type,asset,slug,outcome,side,price,size,usdc,role"
CAT = ["type", "asset", "slug", "outcome", "side", "role"]


def _compact(df):
    for c in CAT:
        if c in df:
            df[c] = df[c].astype("category")
    for c in ("price", "size", "usdc"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    return df


def con_path():
    return DB_PATH


def load():
    """Экономно по памяти: сырые сделки без JSON-признаков, признаки — только там, где нужны."""
    con = sqlite3.connect(DB_PATH)
    # первый вход в каждую сторону каждого окна — с признаками
    first = _expand(pd.read_sql(
        "SELECT t.ts,t.type,t.asset,t.slug,t.outcome,t.side,t.price,t.size,t.usdc,t.role,t.feat FROM trades t "
        "JOIN (SELECT slug, outcome, MIN(ts) m FROM trades WHERE type='TRADE' AND side='BUY' GROUP BY slug, outcome) f "
        "ON t.slug=f.slug AND t.outcome=f.outcome AND t.ts=f.m WHERE t.type='TRADE' AND t.side='BUY' "
        "GROUP BY t.slug, t.outcome", con))
    # выборка всех покупок с признаками (для раздела «направление»)
    n = con.execute("SELECT COUNT(*) FROM trades WHERE type='TRADE' AND side='BUY' AND length(feat)>40").fetchone()[0]
    k = max(1, int(np.ceil(n / 25000)))
    buys_f = _expand(pd.read_sql(
        "SELECT ts,slug,outcome,price,feat FROM trades WHERE type='TRADE' AND side='BUY' AND length(feat)>40 "
        "AND abs(random()) % ? = 0", con, params=(k,)))
    # свежие сделки для CSV
    recent = _expand(pd.read_sql(
        f"SELECT {BASE_COLS},tx,feat FROM trades WHERE ts > ? ORDER BY ts DESC LIMIT 60000",
        con, params=(time.time() - 3 * 86400,)))
    wn = pd.read_sql("SELECT slug, asset, start, winner FROM windows", con)
    parts = []
    for src in ("live", "hist"):
        n = con.execute("SELECT COUNT(*) FROM snapshots WHERE src=?", (src,)).fetchone()[0]
        k = max(1, int(np.ceil(n / ANALYSIS_MAX_CONTROLS)))
        parts.append(pd.read_sql("SELECT * FROM snapshots WHERE src=? AND abs(random()) % ? = 0",
                                 con, params=(src, k)))
    con.close()
    sn = _expand(pd.concat(parts, ignore_index=True))
    if "ts" in sn:
        sn["ts"] = pd.to_numeric(sn["ts"], errors="coerce")
    for d in (first, buys_f, recent):
        for c in ("price", "size", "usdc", "ts"):
            if c in d:
                d[c] = pd.to_numeric(d[c], errors="coerce")
    return con_path(), first, buys_f, recent, wn, sn


# ---------------- нормализация «в сторону выбранного исхода» ----------------
def _g(df, c):
    return pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(np.nan, index=df.index)


def sidefy(df, is_up):
    up = np.asarray(is_up, dtype=bool)
    s = np.where(up, 1.0, -1.0)
    o = pd.DataFrame(index=df.index)
    for c in SIGNED:
        if c in df:
            o["my_" + c] = _g(df, c) * s
    for c in FLIP100:
        if c in df:
            o["my_" + c] = np.where(up, _g(df, c), 100 - _g(df, c))
    for c in FLIP1:
        if c in df:
            o["my_" + c] = np.where(up, _g(df, c), 1 - _g(df, c))
    if "fair_up" in df:
        o["my_fair"] = np.where(up, _g(df, "fair_up"), 1 - _g(df, "fair_up"))
    for x in PM_FIELDS:
        a, b = f"pm_up_{x}", f"pm_dn_{x}"
        if a in df or b in df:
            o["my_" + x] = np.where(up, _g(df, a), _g(df, b))
            o["opp_" + x] = np.where(up, _g(df, b), _g(df, a))
    if "pmh_up" in df or "pmh_dn" in df:
        o["my_pmh"] = np.where(up, _g(df, "pmh_up"), _g(df, "pmh_dn"))
    if "pm_up_flow30" in df:
        o["my_flow30"] = np.where(up, _g(df, "pm_up_flow30"), _g(df, "pm_dn_flow30"))
        o["opp_flow30"] = np.where(up, _g(df, "pm_dn_flow30"), _g(df, "pm_up_flow30"))
    if "pm_up_mid_chg5s" in df:
        o["my_pm_chg5s"] = _g(df, "pm_up_mid_chg5s") * s
    if "my_fair" in o and "my_ask" in o:
        o["my_edge"] = o["my_fair"] - o["my_ask"]
    if "my_fair" in o and "my_pmh" in o:
        o["my_edge_h"] = o["my_fair"] - o["my_pmh"]
    for c in UNSIGNED:
        if c in df:
            o[c] = _g(df, c)
    return o


# ---------------- PnL по окнам ----------------
UD = "slug LIKE '%-updown-15m-%'"


def window_pnl(con, wn):
    """Агрегация прямо в SQLite — Python не держит миллион строк в памяти."""
    pw = pd.read_sql(f"""
      SELECT slug,
        SUM(CASE WHEN type='TRADE' AND side='BUY' THEN -usdc WHEN type='TRADE' AND side='SELL' THEN usdc
                 WHEN type='MERGE' THEN usdc WHEN type='SPLIT' THEN -usdc ELSE 0 END) cash,
        SUM(CASE WHEN type='TRADE' AND outcome='up' THEN (CASE side WHEN 'BUY' THEN size WHEN 'SELL' THEN -size ELSE 0 END)
                 WHEN type='MERGE' THEN -size WHEN type='SPLIT' THEN size ELSE 0 END) sh_up,
        SUM(CASE WHEN type='TRADE' AND outcome='down' THEN (CASE side WHEN 'BUY' THEN size WHEN 'SELL' THEN -size ELSE 0 END)
                 WHEN type='MERGE' THEN -size WHEN type='SPLIT' THEN size ELSE 0 END) sh_down,
        SUM(CASE WHEN type='TRADE' AND side='BUY' THEN usdc ELSE 0 END) buy_usd,
        SUM(type='TRADE' AND side='BUY') n_buys, SUM(type='TRADE' AND side='SELL') n_sells,
        SUM(CASE WHEN type='TRADE' AND side='BUY' AND outcome='up' THEN size ELSE 0 END) qty_up,
        SUM(CASE WHEN type='TRADE' AND side='BUY' AND outcome='down' THEN size ELSE 0 END) qty_dn,
        SUM(CASE WHEN type='TRADE' AND side='BUY' AND outcome='up' THEN usdc ELSE 0 END) cost_up,
        SUM(CASE WHEN type='TRADE' AND side='BUY' AND outcome='down' THEN usdc ELSE 0 END) cost_dn,
        SUM(type='MERGE') merged
      FROM trades WHERE {UD} GROUP BY slug""", con)
    if pw.empty:
        return pw
    pw["avg_up"] = pw["cost_up"] / pw["qty_up"].replace(0, np.nan)
    pw["avg_dn"] = pw["cost_dn"] / pw["qty_dn"].replace(0, np.nan)
    pw = pw.merge(wn[["slug", "asset", "start", "winner"]], on="slug", how="left")
    pw["payout"] = np.where(pw["winner"] == "up", pw["sh_up"],
                            np.where(pw["winner"] == "down", pw["sh_down"], np.nan))
    pw["pnl"] = pw["cash"] + pw["payout"]
    pw["both_sides"] = (pw["qty_up"] > 0) & (pw["qty_dn"] > 0)
    pw["pair_cost"] = pw["avg_up"] + pw["avg_dn"]
    return pw


def wq(vals, weights, q):
    """Квантиль по гистограмме (значение, количество)."""
    v, w = np.asarray(vals, float), np.asarray(weights, float)
    if not len(v) or w.sum() == 0:
        return np.nan
    o = np.argsort(v)
    c = np.cumsum(w[o]) / w.sum()
    return float(v[o][np.searchsorted(c, q)])


# ---------------- статистика ----------------
def auc(pos, neg):
    pos, neg = pos.dropna(), neg.dropna()
    if len(pos) < 10 or len(neg) < 10:
        return np.nan
    r = pd.concat([pos, neg], ignore_index=True).rank()
    rp = r.iloc[:len(pos)].sum()
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def feature_table(E, C):
    rows = []
    for c in E.columns:
        if c not in C.columns:
            continue
        e, k = E[c].dropna(), C[c].dropna()
        if len(e) < 10 or len(k) < 10:
            continue
        a = auc(e, k)
        rows.append({"feature": c, "auc": a, "sep": abs(a - 0.5), "n_entry": len(e),
                     "entry_p10": e.quantile(.1), "entry_med": e.median(), "entry_p90": e.quantile(.9),
                     "ctrl_p10": k.quantile(.1), "ctrl_med": k.median(), "ctrl_p90": k.quantile(.9)})
    return pd.DataFrame(rows).sort_values("sep", ascending=False) if rows else pd.DataFrame()


def _leaf_rules(dt, cols):
    t = dt.tree_
    out = {}

    def walk(n, conds):
        if t.children_left[n] == -1:
            out[n] = conds
            return
        f, th = cols[t.feature[n]], t.threshold[n]
        walk(t.children_left[n], conds + [f"{f} <= {th:.4g}"])
        walk(t.children_right[n], conds + [f"{f} > {th:.4g}"])

    walk(0, [])
    return out


def tree_rules(E, C, title):
    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.tree import DecisionTreeClassifier
    except ImportError:
        return f"### {title}\nsklearn не установлен\n", pd.DataFrame()
    if len(E) < 30 or len(C) < 100:
        return f"### {title}\nМало данных: входов {len(E)}, контролей {len(C)} — нужно ≥30 и ≥100.\n", pd.DataFrame()
    cols = [c for c in E.columns if c in C.columns and E[c].notna().mean() > 0.6 and C[c].notna().mean() > 0.6
            and c not in ("hour",)]
    if not cols:
        return f"### {title}\nНет общих признаков.\n", pd.DataFrame()
    X = pd.concat([E[cols], C[cols]], ignore_index=True).astype(float)
    y = np.r_[np.ones(len(E)), np.zeros(len(C))]
    X = X.fillna(X.median())
    base = y.mean()
    lines = [f"### {title}", f"Входов: {len(E)}, контрольных моментов: {len(C)}, базовая доля {base:.4f}", ""]
    dt = DecisionTreeClassifier(max_depth=4, min_samples_leaf=max(10, len(E) // 30),
                                class_weight="balanced", random_state=0).fit(X, y)
    leaves = dt.apply(X)
    st = pd.DataFrame({"leaf": leaves, "y": y}).groupby("leaf")["y"].agg(["sum", "count"])
    st["rate"] = st["sum"] / st["count"]
    st["lift"] = st["rate"] / base
    st["recall"] = st["sum"] / y.sum()
    rules = _leaf_rules(dt, cols)
    st = st.sort_values("lift", ascending=False)
    lines.append("**Правила-кандидаты (листья дерева, отсортированы по lift):**")
    rr = []
    for leaf, r in st.iterrows():
        if r["recall"] < 0.03:
            continue
        cond = " И ".join(rules.get(leaf, []))
        lines.append(f"- lift ×{r['lift']:.1f}, покрывает {r['recall']*100:.0f}% его входов: {cond}")
        rr.append({"lift": r["lift"], "recall": r["recall"], "entries": r["sum"], "moments": r["count"], "rule": cond})
    rf = RandomForestClassifier(n_estimators=200, max_depth=7, min_samples_leaf=10, class_weight="balanced",
                                n_jobs=1, random_state=0).fit(X, y)
    imp = sorted(zip(rf.feature_importances_, cols), reverse=True)[:15]
    lines.append("\n**Важность признаков (случайный лес):** " + ", ".join(f"{c} {v:.3f}" for v, c in imp))
    return "\n".join(lines) + "\n", pd.DataFrame(rr)


def entries_controls(first, sn, src):
    """first — первый вход в каждую сторону окна (с признаками)."""
    if first.empty or "src" not in first:
        return pd.DataFrame(), pd.DataFrame(), first.iloc[:0]
    e = first[(first["src"] == src) & first["outcome"].isin(["up", "down"])]
    E = sidefy(e, e["outcome"] == "up")
    c = sn[sn["src"] == src] if "src" in sn else sn.iloc[:0]
    if c.empty:
        return E, pd.DataFrame(), e
    fmap = first.set_index(["slug", "outcome"])["ts"].to_dict()
    Cs = []
    for side in ("up", "down"):
        fe = pd.Series([fmap.get((s, side), np.nan) for s in c["slug"]], index=c.index, dtype=float)
        keep = fe.isna() | (pd.to_numeric(c["ts"]) < fe - DECISION_LAG_SEC - 10)
        cc = c[keep]
        Cs.append(sidefy(cc, np.full(len(cc), side == "up")))
    return E, pd.concat(Cs, ignore_index=True), e


def _pct(x):
    return f"{x*100:.1f}%" if pd.notna(x) else "—"


# ---------------- отчёт ----------------
def build_report():
    t0 = time.time()
    _, first, buys_f, recent, wn, sn = load()
    con = sqlite3.connect(DB_PATH)
    q = lambda sql, *p: con.execute(sql, p).fetchall()
    L = [f"# Профиль кошелька {WALLET}", f"Сгенерировано: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", ""]
    csvs = {}
    n_all = q("SELECT COUNT(*) FROM trades")[0][0]
    if not n_all:
        con.close()
        return _write(L + ["Сделок пока нет."], csvs, "Сделок пока нет.")
    t_min, t_max = q("SELECT MIN(ts), MAX(ts) FROM trades")[0]
    fmt = lambda x: time.strftime("%Y-%m-%d", time.gmtime(x))
    L += ["## 1. Обзор", f"Период: {fmt(t_min)} → {fmt(t_max)}",
          "События по типам: " + ", ".join(f"{k}={v}" for k, v in q("SELECT type, COUNT(*) FROM trades GROUP BY type ORDER BY 2 DESC")), ""]
    n_ud = q(f"SELECT COUNT(*) FROM trades WHERE type='TRADE' AND {UD}")[0][0]
    other = q(f"SELECT slug, COUNT(*) FROM trades WHERE type='TRADE' AND NOT ({UD}) GROUP BY slug ORDER BY 2 DESC LIMIT 8")
    n_oth = q(f"SELECT COUNT(*) FROM trades WHERE type='TRADE' AND NOT ({UD})")[0][0]
    L.append(f"Сделок в 15m Up/Down: {n_ud}; в других рынках: {n_oth}")
    if other:
        L.append("Другие рынки (топ): " + ", ".join(f"{k}({v})" for k, v in other))
    L.append("По активам: " + ", ".join(f"{k}={v}" for k, v in q(f"SELECT asset, COUNT(*) FROM trades WHERE type='TRADE' AND {UD} GROUP BY asset")))
    mv = q(f"SELECT strftime('%Y-%m', ts, 'unixepoch'), COUNT(*), ROUND(SUM(usdc)) FROM trades WHERE type='TRADE' AND {UD} GROUP BY 1")
    L.append("Оборот по месяцам: " + ", ".join(f"{m}: {n} сделок/${v:,.0f}" for m, n, v in mv))

    # --- PnL
    pw = window_pnl(con, wn)
    res = pw.dropna(subset=["pnl"]) if not pw.empty else pw
    L += ["", "## 2. Результат (пересчёт по окнам, до резолва)"]
    if not res.empty:
        L.append(f"Окон с резолвом: {len(res)} из {len(pw)}. PnL: ${res['pnl'].sum():,.0f}. "
                 f"Плюсовых окон: {_pct((res['pnl'] > 0).mean())}. Оборот покупок: ${res['buy_usd'].sum():,.0f}. "
                 f"ROI на оборот: {_pct(res['pnl'].sum() / max(res['buy_usd'].sum(), 1))}")
        by = res.groupby("asset")["pnl"].agg(["sum", "count", lambda s: (s > 0).mean()])
        for a_, r in by.iterrows():
            L.append(f"- {a_}: ${r['sum']:,.0f} за {int(r['count'])} окон, плюсовых {_pct(r.iloc[2])}")
        res2 = res.copy()
        res2["month"] = pd.to_datetime(res2["start"], unit="s").dt.strftime("%Y-%m")
        L.append("PnL по месяцам: " + ", ".join(f"{m}: ${v:,.0f}" for m, v in res2.groupby("month")["pnl"].sum().items()))
        # чем заработаны деньги: гарантированная часть (пары) против направленной
        pr = res.copy()
        pr["pairs"] = np.minimum(pr["qty_up"], pr["qty_dn"])
        pr["locked"] = pr["pairs"] * (1 - pr["pair_cost"].fillna(1))
        L.append(f"Из PnL: «запертая» маржа пар Up+Down ≈ ${pr['locked'].sum():,.0f}, "
                 f"остальное — направленная часть (перекос позиции) ≈ ${res['pnl'].sum() - pr['locked'].sum():,.0f}")
    else:
        L.append(f"Победители окон ещё не подтянуты (окон со сделками: {len(pw)}). "
                 "PnL появится после этапа «Восстанавливаю рыночный контекст».")
    if not pw.empty:
        csvs["windows_pnl.csv"] = pw

    # --- Почерк
    L += ["", "## 3. Почерк (как он торгует технически)"]
    verdict = []
    if not pw.empty:
        bs = pw["both_sides"].mean()
        pc = pw.loc[pw["both_sides"], "pair_cost"]
        L.append(f"Окон, где покупал ОБЕ стороны: {_pct(bs)}; средняя стоимость пары Up+Down: "
                 f"{pc.mean():.3f} (медиана {pc.median():.3f}); пар дороже $1: {_pct((pc > 1).mean())}"
                 if len(pc) else f"Окон с обеими сторонами: {_pct(bs)}")
        imb = (pw["qty_up"] - pw["qty_dn"]).abs() / (pw["qty_up"] + pw["qty_dn"]).replace(0, np.nan)
        L.append(f"Перекос позиции в окне |Up−Down|/(Up+Down): медиана {_pct(imb.median())}, p90 {_pct(imb.quantile(.9))}")
        L.append(f"MERGE: {int(pw['merged'].sum())}, REDEEM: {q('SELECT COUNT(*) FROM trades WHERE type=?', 'REDEEM')[0][0]}, "
                 f"окон с продажами до конца: {_pct((pw['n_sells'] > 0).mean())}")
        if bs > 0.5 and len(pc) and pc.median() < 1:
            verdict.append("НАБОР ОБЕИХ СТОРОН ДЕШЕВЛЕ $1 (арбитраж/мейкинг вокруг 0.50)")
        fpw = pw["n_buys"] + pw["n_sells"]
        L.append(f"Филлов на окно: медиана {fpw.median():.0f}, p90 {fpw.quantile(.9):.0f}, макс {fpw.max():.0f}")
    rv = q("SELECT role, COUNT(*) FROM trades WHERE type='TRADE' AND role IS NOT NULL GROUP BY role")
    if rv:
        tot = sum(v for _, v in rv)
        L.append("Роль в сделках (on-chain): " + ", ".join(f"{k} {_pct(v / tot)}" for k, v in rv))
        mk = sum(v for k, v in rv if k == "maker") / tot
        if mk > 0.6:
            verdict.append("МАРКЕТ-МЕЙКЕР: в основном исполняются его лимитки")
    gh = q(f"SELECT g, COUNT(*) FROM (SELECT ts - LAG(ts) OVER (PARTITION BY slug ORDER BY ts) g FROM trades "
           f"WHERE type='TRADE' AND {UD}) WHERE g IS NOT NULL GROUP BY g")
    if gh:
        gv, gw = zip(*gh)
        L.append(f"Интервал между филлами внутри окна: медиана {wq(gv, gw, .5):.0f}с; "
                 f"в ту же секунду: {_pct(sum(w for v, w in gh if v == 0) / sum(gw))}")
    sz = q(f"SELECT ROUND(size,2), COUNT(*) FROM trades WHERE type='TRADE' AND {UD} GROUP BY 1 ORDER BY 2 DESC LIMIT 6")
    if sz:
        L.append("Самые частые размеры (шт): " + ", ".join(f"{k:g} ({_pct(v / n_ud)})" for k, v in sz))
    uh = q(f"SELECT ROUND(usdc,1), COUNT(*) FROM trades WHERE type='TRADE' AND {UD} GROUP BY 1")
    if uh:
        uv, uw = zip(*uh)
        L.append(f"Сумма филла: медиана ${wq(uv, uw, .5):.2f}, p90 ${wq(uv, uw, .9):.2f}, макс ${max(uv):.0f}")
    g = pd.read_sql(f"""SELECT MIN(CAST(t.price*20 AS INT), 19) b, COUNT(*) n, SUM(t.usdc) usd, AVG(t.price) avgp,
          AVG(CASE WHEN w.winner IS NULL THEN NULL WHEN t.outcome=w.winner THEN 1.0 ELSE 0.0 END) win
        FROM trades t LEFT JOIN windows w ON t.slug=w.slug
        WHERE t.type='TRADE' AND t.side='BUY' AND t.{UD} GROUP BY 1 ORDER BY 1""", con)
    if len(g):
        g["edge"] = g["win"] - g["avgp"]
        L += ["", "**Цена покупки → винрейт филла и реальный edge (винрейт − средняя цена):**"]
        for r in g.itertuples():
            rng = f"{r.b/20:.2f}-{(r.b+1)/20:.2f}"
            L.append(f"- {rng}: {r.n} филлов, ${r.usd:,.0f}, винрейт {_pct(r.win)}, ср.цена {r.avgp:.3f}, "
                     f"edge {r.edge*100:+.1f}пп" if pd.notna(r.win) else f"- {rng}: {r.n} филлов, ${r.usd:,.0f}")
        csvs["price_buckets.csv"] = g
    tl = q(f"""SELECT CASE WHEN sl<=0 THEN 'после конца' WHEN sl<=60 THEN '0-60' WHEN sl<=180 THEN '60-180'
               WHEN sl<=300 THEN '180-300' WHEN sl<=600 THEN '300-600' WHEN sl<=900 THEN '600-900' ELSE 'до старта' END,
               COUNT(*) FROM (SELECT CAST(substr(slug, -10) AS INTEGER) + 900 - ts sl FROM trades
               WHERE type='TRADE' AND side='BUY' AND {UD}) GROUP BY 1""")
    if tl:
        tot = sum(v for _, v in tl)
        order = ["до старта", "600-900", "300-600", "180-300", "60-180", "0-60", "после конца"]
        d_ = dict(tl)
        L.append("Когда покупает (сек до конца окна): " + ", ".join(f"{k}: {_pct(d_[k] / tot)}" for k in order if k in d_))
    con.close()

    # --- Направление
    L += ["", "## 4. Логика направления (какую сторону берёт)"]
    for src in ("live", "hist"):
        bs_ = buys_f[buys_f["src"] == src] if "src" in buys_f else buys_f.iloc[:0]
        if len(bs_) < 10:
            continue
        S = sidefy(bs_, bs_["outcome"] == "up")
        parts = [f"[{src}, {len(bs_)} покупок]"]
        for c, name in (("my_delta_bps", "сторона, которая СЕЙЧАС выигрывает"),
                        ("my_ret5s_bps", "по импульсу 5с"), ("my_ret30s_bps", "по импульсу 30с"),
                        ("my_ret180s_bps", "по импульсу 3м"), ("my_m1_ema9_21", "по тренду EMA 1m"),
                        ("my_m5_macdh_bps", "по MACD 5m"), ("my_cvd60s", "по потоку CVD 60с")):
            if c in S and S[c].notna().sum() > 10:
                parts.append(f"{name}: {_pct((S[c] > 0).mean())}")
        if "my_edge" in S and S["my_edge"].notna().sum() > 10:
            parts.append(f"edge модели (fair−ask) медиана {S['my_edge'].median()*100:+.1f}пп, >0 в {_pct((S['my_edge'] > 0).mean())}")
        if "my_edge_h" in S and S["my_edge_h"].notna().sum() > 10:
            parts.append(f"edge к цене PM медиана {S['my_edge_h'].median()*100:+.1f}пп")
        L.append("- " + "; ".join(parts))
        if "my_delta_bps" in S and not any(v.startswith("ПОЗДНИЙ") for v in verdict):
            al = (S["my_delta_bps"] > 0).mean()
            if al > 0.85 and bs_["price"].median() > 0.75:
                verdict.append("ПОЗДНИЙ ФАВОРИТ: докупает уже выигрывающую сторону по высокой цене")
            m5 = (S.get("my_ret5s_bps", pd.Series(dtype=float)) > 0).mean()
            if m5 > 0.7 and not any(v.startswith("ЛАТЕНТ") for v in verdict):
                verdict.append("ЛАТЕНТНЫЙ АРБИТРАЖ: входит сразу после рывка Binance, пока стакан PM не догнал")

    # --- Признаки и правила
    L += ["", "## 5. Чем моменты его входов отличаются от всех остальных"]
    L.append("AUC: 0.5 = не отличается, >0.7 или <0.3 = сильный фильтр. Признаки my_* развёрнуты в сторону "
             "выбранного исхода (my_delta_bps>0 = цена на стороне его ставки).")
    for src, title in (("live", "Живые данные (полный набор: стакан PM, Chainlink, биржи)"),
                       ("hist", "История (Binance 1s + цены PM)")):
        E, C, _ = entries_controls(first, sn, src)
        if E.empty or C.empty:
            L.append(f"\n### {title}\nПока нет данных.")
            continue
        ft = feature_table(E, C)
        csvs[f"features_{src}.csv"] = ft
        L.append(f"\n### {title}: топ-15 отличающих признаков")
        for r in ft.head(15).itertuples():
            L.append(f"- {r.feature}: AUC {r.auc:.2f} | у него медиана {r.entry_med:.4g} "
                     f"[{r.entry_p10:.4g}…{r.entry_p90:.4g}] | обычно {r.ctrl_med:.4g}")
        txt, rules = tree_rules(E, C, f"Дерево решений — {title}")
        L += ["", txt]
        if not rules.empty:
            csvs[f"rules_{src}.csv"] = rules

    # --- Вывод
    L += ["", "## 6. Гипотеза стиля (автоматически)"]
    L += [f"- {v}" for v in verdict] or ["- Однозначного паттерна нет — нужна ручная разборка CSV."]

    # --- CSV: свежие сделки (3 дня) с признаками + первые входы за всю историю
    def _enrich(df):
        if df.empty:
            return df
        df = df.copy()
        df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        bm = (df["type"] == "TRADE") & (df["side"] == "BUY") & df["outcome"].isin(["up", "down"])
        if bm.any():
            sd = sidefy(df[bm], df.loc[bm, "outcome"] == "up")
            df = df.join(sd[[c for c in sd.columns if c not in df.columns]], how="left")
        return df.sort_values("ts")
    rec = _enrich(recent)
    csvs["trades_last_3d.csv"] = rec
    csvs["trades_last_4h.csv"] = rec[rec["ts"] > time.time() - 4 * 3600] if len(rec) else rec
    csvs["first_entries_all.csv"] = _enrich(first)
    L.append(f"\n_Анализ занял {time.time()-t0:.0f}с_")

    head = [f"🧬 Профиль {WALLET[:8]}…", L[4] if len(L) > 4 else ""]
    if not res.empty:
        head.append(f"PnL по окнам: ${res['pnl'].sum():,.0f}, окон {len(res)}, плюсовых {_pct((res['pnl'] > 0).mean())}")
    head += [f"• {v}" for v in verdict] or ["• Стиль пока не определён"]
    head.append(f"Сделок за 4ч: {len(csvs['trades_last_4h.csv'])}, всего событий: {n_all}")
    return _write(L, csvs, "\n".join(head))


def _write(lines, csvs, caption):
    os.makedirs(OUT_DIR, exist_ok=True)
    name = time.strftime("profile_%Y%m%d_%H%M", time.gmtime())
    path = os.path.join(OUT_DIR, name + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.md", "\n".join(lines))
        for n, df in csvs.items():
            z.writestr(n, df.to_csv(index=False))
    return path, caption, "\n".join(lines)
