# Strategy Simulator v1.8 — Binance shadow filters

Исходные 12 стратегий НЕ изменены.

Добавлен Binance USD-M Futures поток BTCUSDT:
- aggTrade
- depth20@100ms

Для каждого уже совершённого Polymarket-сигнала сохраняются:
- BTC return 3s
- BTC return 10s
- EMA9 / EMA21 / EMA bias
- RSI14
- aggressive volume delta 10s
- aggressive volume delta 30s
- top-10 order-book imbalance
- large-trade delta 30s
- общий score

Пять shadow-фильтров:
- B1_MOM
- B2_FLOW
- B3_BOOK
- B4_COMBO
- B5_SCORE

Shadow-фильтры не меняют исходные 12 стратегий.
Они только принимают или пропускают уже возникшие сделки и считают свой PnL.

Новые файлы в часовом ZIP:
- binance_signal_features.csv
- binance_shadow_trades.csv
- binance_shadow_results.csv
- binance_shadow_summary.csv

В report.txt выводятся 15 лучших комбинаций стратегия + Binance-фильтр.
