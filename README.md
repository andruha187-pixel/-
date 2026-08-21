# Strategy Simulator V4 — Fingerprint Research

Все 12 BASE-стратегий Polymarket остаются без изменений.

## Контроль
CONF60 — контрольный фильтр V3.

## Новые shadow-фильтры
- V4_RET10:
  CONF60 + directional BTC return за 10 секунд >= +0.01%.

- V4_LARGE_FLOW_GUARD:
  блокирует вход, если directional large-trade delta 10s > 0.34,
  но directional total flow 10s <= 0.47.

- V4_COMBO:
  одновременно RET10 + LARGE_FLOW_GUARD.
  Это главный out-of-sample кандидат.

- V4_BOOK_CONFIRM:
  CONF60 + directional top-10 book imbalance >= 0.05.

## Исправление Binance depth
В V3 в сохранённых данных book_imbalance был 0 во всех строках.
V4 использует правильный combined-stream URL:
wss://fstream.binance.com/stream?streams=...

В health добавлены:
- binance_depth_feed_ok
- binance_book_imbalance

Чтобы CONF60 оставался сравнимым с V3, исправленный book по умолчанию
НЕ входит в confidence: V4_CONF_USE_BOOK=0.
Book тестируется отдельно через V4_BOOK_CONFIRM.

Не меняй пороги в процессе одной серии тестов.
