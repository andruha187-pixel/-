# SAFE67 A/B — no stop vs post-pyramid stop 0.40

PAPER-only бот для чистого A/B теста двух вариантов одной и той же стратегии.

## Общая логика A и B

Оба варианта используют одинаковые сигналы и одинаковую логику входа:

- BTC 5-minute Up/Down Polymarket;
- цикл решения примерно каждые 3 секунды;
- без принудительного `ensure_book()` перед формированием сигнала — как в исходном GATE64 SAFE;
- сначала ждём первый **V2-eligible** сигнал:
  - цена `0.55–0.75`;
  - momentum `0.03–0.30`;
- первый V2-eligible сигнал проходит SAFE67 только если:
  - цена `0.67–0.75`;
  - momentum `0.05–0.10`;
- если первый V2-eligible сигнал не проходит SAFE67 — рынок SKIP навсегда;
- ENTRY = `5 shares`;
- один PYRAMID = `10 shares` после роста удерживаемой стороны ещё на `+0.08`;
- PYRAMID momentum: `> 0` и `<= 0.30`;
- SWITCH отключён;
- максимум `15 shares` на рынок.

`ensure_book()` сохранён перед симулированным исполнением покупки, как и в предыдущем SAFE, чтобы не исполнять PAPER-покупку по явно протухшему стакану.

## A / SAFE67 NO STOP

Никакого стоп-лосса. Позиция держится до settlement.

## B / SAFE67 POST-PYR STOP 0.40

До PYRAMID стопа **вообще нет**. Первые 5 shares могут проседать ниже 0.40 и позиция не закрывается.

Стоп становится активным только после того, как реально исполнился PYRAMID и позиция выросла до 15 shares (или до фактически исполненного объёма при неполном заполнении).

После этого отдельный stop-loop примерно каждые `0.20s`:

1. проверяет свежий стакан удерживаемого токена;
2. если `best bid <= 0.40`, создаёт STOP event;
3. продаёт оставшиеся shares по фактически видимым bid-уровням стакана;
4. учитывает taker fee;
5. если видимой ликвидности не хватило, продолжает ликвидацию на новых снимках стакана.

То есть PAPER не предполагает гарантированный выход ровно по 0.40. Если рынок перескочил с 0.41 на 0.34, выход моделируется по доступным bid-ценам около 0.34.

## Раздельные PAPER счета

- A стартует с `$500`;
- B стартует с `$500`;
- новая база: `/var/data/safe67_ab_postpyr_stop40_cleanloop.db`.

Старая статистика предыдущих версий не смешивается.

## Telegram

Сохранены кнопки:

- START
- STOP
- BALANCE
- STATISTICS
- POSITIONS
- TRADES
- PAPER
- LIVE
- EMERGENCY STOP

После нового развёртывания торговля по умолчанию OFF — нажми START.

## Часовой ZIP

Один ZIP в час, но данные разделены по двум папкам:

- `A_safe67_no_stop/`
- `B_safe67_postpyr_stop_040/`

Внутри каждого варианта сохраняются:

- summary.csv
- gate_decisions.csv
- paper_trades.csv
- paper_exits.csv
- stop_events.csv
- signals.csv
- market_results.csv
- position_trajectory.csv
- report.txt

В корне ZIP также остаются `variants_summary.csv`, `markets.csv` и общий `report.txt`.

## Render

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python main.py
```

Persistent disk:

```text
/var/data
```

Существующие `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` можно оставить без изменений.
