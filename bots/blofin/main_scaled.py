"""
Blofin Scaled Exit Bot Entry Point

Strategy:
- TP1: Close 50% of position
- TP2: Close 25% + Move SL to entry (breakeven)
- TP3: Close remaining 25%

Account config in .env:
  BLOFIN_SCALED_API_KEY, BLOFIN_SCALED_SECRET_KEY, BLOFIN_SCALED_PASSPHRASE
  (falls back to BLOFIN_API_KEY, etc. if not set)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.blofin.blofin_bot_engine import BlofinBotEngine
from bots.blofin.strategies.implementation.strategy_scaled_implementation import ScaledStrategy
from mexcpy.config import (
    API_ID, API_HASH, TARGET_CHATS, SESSION_BLOFIN_SCALED,
    BLOFIN_SCALED_API_KEY, BLOFIN_SCALED_SECRET_KEY, BLOFIN_SCALED_PASSPHRASE, BLOFIN_SCALED_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BLOFIN_SCALED),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=TARGET_CHATS
    )
    strategy = ScaledStrategy()
    engine = BlofinBotEngine(
        listener=listener,
        strategy=strategy,
        api_key=BLOFIN_SCALED_API_KEY,
        secret_key=BLOFIN_SCALED_SECRET_KEY,
        passphrase=BLOFIN_SCALED_PASSPHRASE,
        testnet=BLOFIN_SCALED_TESTNET
    )
    engine.run()
