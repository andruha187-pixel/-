# M03 Exact Paper Money Bot v1.9

Исправление отчётов.

Стратегия M03 и все её параметры НЕ менялись.

Каждый час ZIP теперь ОБЯЗАТЕЛЬНО содержит:
- variants_summary.csv
- paper_trades.csv
- signals.csv
- market_results.csv
- markets.csv
- m03_accounts.csv
- m03_account_trades.csv
- m03_account_results.csv
- report.txt

`m03_accounts.csv` показывает накопительный результат каждого виртуального счёта:
$100 / $250 / $500 / $1000 / $2500.

В логах при формировании отчёта появится:
REPORT ACCOUNT FILES | accounts=5 | trades=... | results=...

Если `accounts=5`, файлы виртуальных капиталов точно добавлены в ZIP.
