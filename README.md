# M03 Six-Way CONF65 PAPER Bot v6.0 — G / V5 SMART65

Эта версия запускает шесть независимых PAPER-счетов на одном и том же BTC 5-minute Polymarket стакане и одном Binance CONF65 snapshot:

- A — V3 NOSW90
- B — V2 LOCK
- C — V5 DYNAMIC
- E — V5 DYNAMIC HEDGE
- F — V5 PAIR HEDGE
- G — V5 SMART65

Каждый счет стартует со своих `$500`. LIVE намеренно отключен.

## Зачем добавлен G

По результатам F полный 1:1 hedge сильно поднял winrate, но слишком сильно урезал средний выигрыш. G оставляет ту же направленную логику V5, но меняет управление деньгами.

## 1. Динамический размер основной покупки G

Обычный C/F продолжает работать по 10 shares. Только G реально покупает:

| Цена фактического V5 fill | G покупает |
|---|---:|
| `<= 0.62` | `10 shares` |
| `0.63–0.70` | `8 shares` |
| `0.71–0.78` | `6 shares` |
| `0.79–0.82` | `4 shares` |
| `> 0.82` | `SKIP` |

То есть дорогие поздние PYRAMID становятся меньше, а выше 0.82 G вообще не рискует капиталом.

Важно: внутренний BASE/shadow V5 остается тем же, что у C/F. Меняется только фактический PAPER-size G, чтобы сравнение сигналов оставалось максимально близким.

## 2. 65% hedge вместо полного 100%

После каждой фактической покупки G создается hedge на противоположную сторону:

```text
hedge shares = фактически купленные G shares × 0.65
```

Например:

```text
10 Up -> target 6.5 Down
8 Up  -> target 5.2 Down
6 Up  -> target 3.9 Down
4 Up  -> target 2.6 Down
```

Если рассчитанный размер меньше реального `min_order_size` текущего рынка, такой отдельный hedge-order не симулируется как валидный ордер. Это специально оставлено реалистично.

## 3. Цена hedge остается такой же выгодной, как у F

G НЕ хеджируется сразу по дорогой противоположной стороне.

Цена лимита считается по той же границе, при которой полный F-style 1:1 pair смог бы зафиксировать минимум:

```text
PAIR_LOCKED_PROFIT=0.25
```

То есть G ждет такого же сильного движения в нашу пользу, как F, но при достижении уровня покупает только 65% hedge.

Пример:

```text
10 Up @ 0.60
фактическая стоимость с taker fee ≈ $6.168
полный F threshold -> Down LIMIT 0.35
G ставит не 10 Down, а 6.5 Down LIMIT 0.35
```

После полного G hedge этого лота примерно:

```text
если Up победит:   +$1.56
если Down победит: -$1.94
```

То есть прибыль не превращается в +$0.25–0.30, как у полного F hedge, но проигрыш конкретного лота существенно уменьшается.

## 4. LIMIT/FOK логика

- Основной вариант — resting maker-style PAPER LIMIT.
- Pending limits проверяются каждые `0.20 sec` по live WebSocket book.
- Если target уже доступен в ask при создании hedge, бот пробует полный FOK-style fill.
- FOK учитывает taker fee.
- Maker PAPER fill имеет fee `0`.
- Частичные fills поддерживаются.
- Внутри одного PAPER-счета одна и та же видимая ликвидность не используется двумя ордерами одновременно.
- F и G являются альтернативными тестами, поэтому получают независимую копию одного и того же видимого стакана.

## 5. G market risk cap около -$6

Дополнительно G следит за реальной суммарной позицией рынка:

```text
PnL if Up = Up shares - total paid
PnL if Down = Down shares - total paid
worst PnL = min(PnL Up, PnL Down)
```

После того как G накопил минимум:

```text
SMART_RISK_START_SHARES=16
```

и worst-case становится хуже:

```text
-SMART_MAX_LOSS = -$6
```

бот может сделать `SMART_RISK_HEDGE` текущей противоположной стороны, чтобы приблизить worst-case обратно к `-$6`.

При этом он:

- учитывает текущий ask и taker fee;
- не тратит зарезервированные деньги других pair limits;
- старается сохранить минимум `SMART_MIN_UPSIDE=+$1` по лучшему исходу;
- соблюдает `min_order_size`;
- после emergency hedge уменьшает/отменяет соответствующие pending pair limits, чтобы потом не перехеджироваться.

Поэтому `-$6` — **мягкая аварийная цель, а не математическая гарантия**. Если минимальный размер ордера, плохая цена, глубина стакана или ограничение по upside не позволяют корректно сделать hedge, бот не будет насильно фиксировать еще худшую конструкцию.

## Новая база

```text
/var/data/m03_sixway_conf65_smart65.db
```

A/B/C/E/F/G начинают новый тест одновременно. Старая v5 база не перезаписывается.

## Telegram

`BALANCE` показывает 6 независимых счетов. У F/G также виден cash, зарезервированный под pending pair limits.

`STATISTICS` для G дополнительно показывает:

- Pair orders / Filled / Pending
- LIMIT/FOK fills
- Pair worst-PnL sum
- Avg normal lot
- Risk hedges и их стоимость
- Worst market
- Avg win/loss
- Realized PnL / Equity

`POSITIONS` показывает и pending pair limits F/G.

`TRADES` помечает защитные сделки:

- `PAIR_HEDGE_LIMIT`
- `PAIR_HEDGE_FOK`
- `SMART_RISK_HEDGE`

## Render

1. Заменить файлы репозитория содержимым архива.
2. Build command:

```text
pip install -r requirements.txt
```

3. Start command:

```text
python main.py
```

4. Persistent disk:

```text
/var/data
```

5. Оставить текущие `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CONF_MIN=65`.
6. Новые параметры G можно не добавлять в Render — defaults уже встроены.

## Тест

```text
python test_sixway.py
```

Ожидаемая последняя строка:

```text
six-way CONF65 + G SMART65 regression: OK
```

## Что сравнивать

Главное теперь смотреть не на winrate отдельно, а одновременно на:

- Realized PnL
- Avg win
- Avg loss
- Worst market
- G Avg normal lot
- сколько G pair limits реально исполнилось
- сколько раз сработал SMART_RISK_HEDGE

Цель G — сохранить значительную часть хороших выигрышей C, но уменьшить хвост крупных проигрышей заметнее, чем чистый C.
