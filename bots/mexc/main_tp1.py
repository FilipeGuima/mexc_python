"""
MEXC TP1 Bot Entry Point
- Uses smart entry logic (market if price favorable, limit otherwise)
- Supports UPDATE signals (change TP/SL)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.mexc.mexc_bot_engine import MexcBotEngine
from bots.mexc.strategies.strategy_tp1_implementation import MexcTP1Strategy
from mexcpy.config import (
    API_ID, API_HASH, TARGET_CHATS, SESSION_TP1,
    TP1_TOKEN, MEXC_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_TP1),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=TARGET_CHATS
    )
    strategy = MexcTP1Strategy()
    engine = MexcBotEngine(
        listener=listener,
        strategy=strategy,
        token=TP1_TOKEN,
        testnet=MEXC_TESTNET
    )
    engine.run()
