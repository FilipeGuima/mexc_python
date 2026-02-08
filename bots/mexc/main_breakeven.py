"""
MEXC Breakeven Bot Entry Point
- Uses market orders (no smart entry)
- Supports BREAKEVEN signals (move SL to entry)
- Resumes monitoring on startup
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.mexc.mexc_bot_engine import MexcBotEngine
from bots.mexc.strategies.strategy_breakeven_implementation import MexcBreakevenStrategy
from mexcpy.config import (
    API_ID, API_HASH, TARGET_CHATS, SESSION_BREAKEVEN,
    BREAKEVEN_TOKEN, MEXC_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BREAKEVEN),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=TARGET_CHATS
    )
    strategy = MexcBreakevenStrategy()
    engine = MexcBotEngine(
        listener=listener,
        strategy=strategy,
        token=BREAKEVEN_TOKEN,
        testnet=MEXC_TESTNET
    )
    engine.run()
