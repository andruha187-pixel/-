# M03 Five-Way CONF65 PAPER Bot v5.0 — E Loss-Floor + F Pair Hedge

Один процесс одновременно сравнивает пять независимых PAPER-стратегий на одних и тех же BTC 5-minute Polymarket стаканах и одном Binance CONF65 snapshot.

## Стратегии

### A — M03_V3_NOSW90 + CONF65
Без изменений.

### B — M03_V2_LOCK + CONF65
Без изменений.

### C — M03_V5_DYNAMIC + CONF65
Чистый V5 Dynamic — контрольная версия.

### E — M03_V5_DYNAMIC_HEDGE + CONF65
Старая версия защиты:
- включается после 20 фактически купленных shares основной стороны;
- пытается удержать проигрыш рынка около `-$10`;
- старается сохранить минимум `+$2`, если исходная сторона победит.

### F — M03_V5_DYNAMIC_PAIR_HEDGE + CONF65
Новая версия. Обычная направленная логика F полностью совпадает с C / V5 Dynamic.

После КАЖДОЙ фактической обычной покупки F создаётся отдельная 1:1 hedge-цель на противоположную сторону.

Пример:

```text
BUY 10 Up @ 0.60
=> создаётся цель BUY 10 Down LIMIT
```

Цена лимита рассчитывается максимально высокой, при которой полностью собранная пара оставляет минимум:

```text
PAIR_LOCKED_PROFIT=0.25
```

В расчёт входит фактическая стоимость исходной покупки вместе с taker fee. Для resting hedge limit PAPER-модель использует maker fee = 0.

Для равных объёмов `q`:

```text
hedge_budget = q - base_total_cost - PAIR_LOCKED_PROFIT
max_maker_price = hedge_budget / q
```

Цена округляется ВНИЗ по текущему `tick_size`.

Пример: 10 Up @ 0.60 обходятся примерно в `$6.168` с taker fee. При цели `+$0.25`:

```text
max maker Down price = (10 - 6.168 - 0.25) / 10
                     = 0.3582
```

При `tick_size=0.01` PAPER limit:

```text
10 Down LIMIT @ 0.35
```

Если он полностью исполнится как maker:

```text
10 - 6.168 - 3.50 = +0.332
```

То есть любая сторона settlement даёт около `+$0.33` для этой пары.

## Если цена уже дошла до hedge-уровня

Если post-only limit сразу пересёк бы текущий ask, F сначала пробует полный FOK-style PAPER hedge:

1. нужен весь объём сразу;
2. используется текущая глубина ask;
3. taker fee учитывается полностью;
4. сделка разрешается только если сохраняется `PAIR_LOCKED_PROFIT`.

Если полный FOK не проходит, создаётся resting PAPER limit ниже текущего ask.

F не догоняет противоположную сторону дороже цены, совместимой с locked-profit целью.

## Быстрый resting-limit checker

Pending hedge limits проверяются отдельным циклом:

```text
PAIR_HEDGE_CHECK_INTERVAL=0.20
```

Он использует уже получаемый WebSocket-стакан и не ждёт 3-секундного основного V5 decision loop.

По умолчанию:

```text
PAIR_LIMIT_FILL_REQUIRE_VISIBLE_SIZE=1
```

PAPER-limit получает fill только при наблюдаемой crossing liquidity. Если видно только часть объёма, hedge исполняется частично и остаток продолжает стоять.

Одна и та же отображаемая ask-liquidity не используется сразу несколькими PAPER-ордерами в одном проходе.

Важно: это симуляция. Публичный стакан не показывает реальную queue position будущего maker-ордера, поэтому live fill-rate может отличаться. Модель специально не считает ордер полностью исполненным только от одного касания цены.

## Min order size и tick size

Код не использует фиксированный минимум в долларах.

Из CLOB book сохраняются:
- `min_order_size`;
- `tick_size`.

F создаёт pair hedge только если фактический размер покупки не меньше текущего `min_order_size`.

## Комиссии

- обычные V5 PAPER-покупки: taker fee;
- E HEDGE: taker fee;
- F `PAIR_HEDGE_LIMIT`: maker fee = 0;
- F `PAIR_HEDGE_FOK`: taker fee.

Maker rebates намеренно не добавляются — это консервативнее.

## Резервирование cash

Pending F limits резервируют виртуальный cash. F не может продолжать открывать V5-позиции так, будто эти деньги всё ещё свободны.

После partial fill резерв уменьшается. После полного fill исчезает.

## Новая база

```text
/var/data/m03_fiveway_conf65_pairhedge.db
```

Все A/B/C/E/F начинают новый тест одновременно с одинакового стартового баланса. Старая v4 база не перезаписывается.

## Telegram

`BALANCE`
- 5 независимых счетов;
- у F дополнительно показывает cash, зарезервированный под pending pair limits.

`STATISTICS`
- W/L, fees, avg win/loss, worst market, PnL;
- E: количество и стоимость старых HEDGE;
- F: pair orders, filled, pending, LIMIT/FOK fills и сумму locked PnL полностью закрытых пар.

`POSITIONS`
- открытые позиции;
- у F дополнительно список pending pair limits.

`TRADES`
- F hedge fills:
  - `PAIR_HEDGE_LIMIT`
  - `PAIR_HEDGE_FOK`

## Render

1. Заменить файлы репозитория содержимым архива.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python main.py`
4. Persistent disk: `/var/data`
5. Оставить текущие `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CONF_MIN=65`.
6. Новые F-параметры можно не добавлять — defaults встроены.

## Проверка

```text
python test_fiveway.py
```

Ожидаемая последняя строка:

```text
five-way CONF65 + E loss-floor + F pair-hedge regression: OK
```

## LIVE

Версия намеренно остаётся PAPER-only. Она не отправляет реальные hedge orders в Polymarket.

Сначала стоит собрать статистику C vs E vs F: сколько pair limits реально исполняется, средний locked PnL, комиссии, worst market и итоговый PnL.
