"""
Blofin TP1-Trail Bot Entry Point
- Enters with TP=TP3, SL=original SL
- Monitors mark_price for TP1 cross
- On TP1 cross: trails SL to TP2, keeps TP=TP3
- Closes at TP3 (full win) or TP2 (trailing SL)

Account config in .env:
  BLOFIN_TP1_TRAIL_API_KEY, BLOFIN_TP1_TRAIL_SECRET_KEY, BLOFIN_TP1_TRAIL_PASSPHRASE
  (falls back to BLOFIN_API_KEY, etc. if not set)
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from bots.listeners.telegram_listener_implementation import TelegramListenerImplementation
from bots.blofin.blofin_bot_engine import BlofinBotEngine
from bots.blofin.strategies.implementation.strategy_tp1_trail_implementation import Tp1TrailStrategy
from mexcpy.config import (
    API_ID, API_HASH, BLOFIN_TP1_TRAIL_CHATS, SESSION_BLOFIN_TP1_TRAIL,
    BLOFIN_TP1_TRAIL_API_KEY, BLOFIN_TP1_TRAIL_SECRET_KEY, BLOFIN_TP1_TRAIL_PASSPHRASE, BLOFIN_TP1_TRAIL_TESTNET
)

if __name__ == "__main__":
    listener = TelegramListenerImplementation(
        session_name=str(SESSION_BLOFIN_TP1_TRAIL),
        api_id=API_ID,
        api_hash=API_HASH,
        target_chats=BLOFIN_TP1_TRAIL_CHATS
    )
    strategy = Tp1TrailStrategy()
    engine = BlofinBotEngine(
        listener=listener,
        strategy=strategy,
        api_key=BLOFIN_TP1_TRAIL_API_KEY,
        secret_key=BLOFIN_TP1_TRAIL_SECRET_KEY,
        passphrase=BLOFIN_TP1_TRAIL_PASSPHRASE,
        testnet=BLOFIN_TP1_TRAIL_TESTNET
    )
    engine.run()
