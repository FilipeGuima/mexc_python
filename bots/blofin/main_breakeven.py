"""
Blofin Breakeven Bot Entry Point
- Uses TP1 as take profit target
- Supports BREAKEVEN signals (move SL to entry)
- Supports UPDATE signals (change TP/SL)

Account config in .env:
  BLOFIN_BREAKEVEN_API_KEY, BLOFIN_BREAKEVEN_SECRET_KEY, BLOFIN_BREAKEVEN_PASSPHRASE
  (falls back to BLOFIN_API_KEY, etc. if not set)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.blofin.blofin_bot_engine import BlofinBotEngine
from bots.blofin.strategies.implementation.strategy_breakeven_implementation import BreakevenStrategy
from mexcpy.config import (
    API_ID, API_HASH, TARGET_CHATS, SESSION_BLOFIN_BREAKEVEN,
    BLOFIN_BREAKEVEN_API_KEY, BLOFIN_BREAKEVEN_SECRET_KEY, BLOFIN_BREAKEVEN_PASSPHRASE, BLOFIN_BREAKEVEN_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BLOFIN_BREAKEVEN),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=TARGET_CHATS
    )
    strategy = BreakevenStrategy()
    engine = BlofinBotEngine(
        listener=listener,
        strategy=strategy,
        api_key=BLOFIN_BREAKEVEN_API_KEY,
        secret_key=BLOFIN_BREAKEVEN_SECRET_KEY,
        passphrase=BLOFIN_BREAKEVEN_PASSPHRASE,
        testnet=BLOFIN_BREAKEVEN_TESTNET
    )
    engine.run()
