"""
Blofin TP3 Bot Entry Point
- Uses TP3 (or best available) as take profit target
- Supports UPDATE signals (change TP/SL)
- Does NOT support BREAKEVEN signals

Account config in .env:
  BLOFIN_TP3_API_KEY, BLOFIN_TP3_SECRET_KEY, BLOFIN_TP3_PASSPHRASE
  (falls back to BLOFIN_API_KEY, etc. if not set)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.blofin.blofin_bot_engine import BlofinBotEngine
from bots.blofin.strategies.implementation.strategy_tp3_implementation import TP3Strategy
from mexcpy.config import (
    API_ID, API_HASH, BLOFIN_TP3_CHATS, SESSION_BLOFIN_TP3,
    BLOFIN_TP3_API_KEY, BLOFIN_TP3_SECRET_KEY, BLOFIN_TP3_PASSPHRASE, BLOFIN_TP3_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BLOFIN_TP3),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=BLOFIN_TP3_CHATS
    )
    strategy = TP3Strategy()
    engine = BlofinBotEngine(
        listener=listener,
        strategy=strategy,
        api_key=BLOFIN_TP3_API_KEY,
        secret_key=BLOFIN_TP3_SECRET_KEY,
        passphrase=BLOFIN_TP3_PASSPHRASE,
        testnet=BLOFIN_TP3_TESTNET
    )
    engine.run()
