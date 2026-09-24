"""Торговые параметры. Хранятся в БД, меняются командой /set имя значение."""
import store

# имя: (по умолчанию, тип, мин, макс, описание)
SPEC = {
    "assets": ("btc", str, None, None, "активы для котирования через запятую (btc,eth)"),
    "bankroll": (150.0, float, 5, 100000, "стартовый депозит симуляции, $ (применяется при /reset)"),
    "order_shares": (5.0, float, 5, 500, "размер одной заявки, шт (минимум Polymarket 5)"),
    "min_edge": (0.03, float, 0.0, 0.2, "целевая маржа пары: bid_up+bid_dn ≈ 1 − min_edge"),
    "max_side_shares": (60.0, float, 5, 5000, "макс. шт одной стороны за окно"),
    "max_window_cost": (30.0, float, 1, 50000, "макс. вложено в одно окно, $ (хедж может превысить)"),
    "max_imbalance": (15.0, float, 0, 5000, "при перекосе ≥ N шт перевешивающая сторона не котируется"),
    "hedge_max_pair": (1.00, float, 0.9, 1.05, "до какой цены пары докупаем лёгкую сторону для выравнивания"),
    "skew_per_share": (0.002, float, 0, 0.05, "сдвиг цены за каждую шт перекоса (управление запасом)"),
    "start_sec": (10.0, float, 0, 600, "начинаем котировать через N сек после старта окна"),
    "stop_sec": (150.0, float, 0, 890, "прекращаем котировать за N сек до конца окна"),
    "fair_min": (0.12, float, 0.01, 0.45, "не котируем, если справедливая вероятность вне [fair_min; 1−fair_min]"),
    "kill_bps": (6.0, float, 0.5, 200, "рывок Binance за 5 сек ≥ N bps → снимаем заявки"),
    "kill_cooldown": (8.0, float, 0, 300, "сколько сек не котируем после рывка"),
    "side_guard_bps": (3.0, float, 0, 100, "BTC ушёл на N bps за side_guard_sec → снимаем бид падающей стороны (0 = выкл)"),
    "side_guard_sec": (10.0, float, 2, 60, "окно для side_guard_bps, сек"),
    "flatten": (1.0, float, 0, 1, "1 = когда котирование окна закончилось, продать лишние шт перевешивающей стороны по биду"),
    "flatten_min_imb": (5.0, float, 1, 1000, "продавать перекос, если он ≥ N шт"),
    "requote_sec": (2.0, float, 0.2, 60, "не переставлять заявку чаще, если цена сдвинулась ≤ 1 тика"),
    "improve": (1.0, float, 0, 1, "1 = можно встать на тик выше лучшего бида, 0 = только присоединяться"),
    "queue_mult": (1.0, float, 0, 5, "симуляция: множитель очереди перед нами (больше = консервативнее)"),
    "fee_rate": (0.07, float, 0, 0.2, "ставка тейкерской комиссии Polymarket для крипто"),
    "rebate_share": (0.20, float, 0, 1, "доля комиссий, возвращаемая мейкерам (оценка ребейта)"),
    "report_hours": (4.0, float, 0.5, 48, "период автоотчёта, ч"),
}


class Settings:
    def __init__(self):
        saved = store.get("settings", {})
        for k, (d, t, *_r) in SPEC.items():
            setattr(self, k, t(saved.get(k, d)))
        self.running = bool(store.get("running", True))

    def asset_list(self):
        return [a.strip() for a in str(self.assets).lower().split(",") if a.strip()]

    def set(self, k, v):
        if k not in SPEC:
            raise ValueError(f"нет параметра {k}")
        d, t, lo, hi, _ = SPEC[k]
        val = t(v.replace(",", ".")) if t is float else t(v)
        if t is float and ((lo is not None and val < lo) or (hi is not None and val > hi)):
            raise ValueError(f"{k}: допустимо {lo}…{hi}")
        setattr(self, k, val)
        store.put("settings", {kk: getattr(self, kk) for kk in SPEC})
        return val

    def set_running(self, on):
        self.running = on
        store.put("running", on)

    def dump(self):
        return {k: getattr(self, k) for k in SPEC}

    def text(self):
        return "\n".join(f"{k} = {getattr(self, k)}  — {SPEC[k][4]}" for k in SPEC)
