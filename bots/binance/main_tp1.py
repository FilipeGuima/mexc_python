"""
Binance TP1 Killers Bot Entry Point
- Uses TP1 as take profit target
- Parses Binance Killers signal format (COIN/Direction/TARGETS)
- TP and SL placed as separate TAKE_PROFIT_MARKET / STOP_MARKET orders

Account config in .env:
  BINANCE_TP1_KILLERS_API_KEY, BINANCE_TP1_KILLERS_SECRET_KEY
  (falls back to BINANCE_TP1_API_KEY, etc. if not set)
"""
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.binance.binance_bot_engine import BinanceBotEngine
from bots.binance.strategies.implementation.strategy_tp1_killers_implementation import BinanceTP1KillersStrategy
from mexcpy.config import (
    API_ID, API_HASH, BINANCE_TP1_KILLERS_CHATS, SESSION_BINANCE_TP1_KILLERS,
    BINANCE_TP1_KILLERS_API_KEY, BINANCE_TP1_KILLERS_SECRET_KEY, BINANCE_TP1_KILLERS_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BINANCE_TP1_KILLERS),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=BINANCE_TP1_KILLERS_CHATS
    )
    strategy = BinanceTP1KillersStrategy()
    engine = BinanceBotEngine(
        listener=listener,
        strategy=strategy,
        api_key=BINANCE_TP1_KILLERS_API_KEY,
        secret_key=BINANCE_TP1_KILLERS_SECRET_KEY,
        testnet=BINANCE_TP1_KILLERS_TESTNET
    )
    engine.run()
