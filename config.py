"""Инфраструктурные настройки (env). Торговые параметры — в settings.py, меняются из Telegram."""
import os

ASSETS = [a.strip().lower() for a in os.getenv("DATA_ASSETS", "btc,eth").split(",") if a.strip()]
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH = os.getenv("DB_PATH", "data/maker.db")
OUT_DIR = os.getenv("OUT_DIR", "data/reports")
MODE = os.getenv("MODE", "DRY_RUN")  # LIVE пока не реализован намеренно

GAMMA = "https://gamma-api.polymarket.com"
PM_WS = os.getenv("PM_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
RTDS_WS = os.getenv("RTDS_WS", "wss://ws-live-data.polymarket.com")
BINANCE_REST = os.getenv("BINANCE_REST", "https://api.binance.com")
BINANCE_WS = os.getenv("BINANCE_WS", "wss://stream.binance.com:9443")
BINANCE_SYM = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT",
               "xrp": "XRPUSDT", "bnb": "BNBUSDT", "hype": "HYPEUSDT"}
