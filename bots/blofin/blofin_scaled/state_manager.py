"""
State persistence for scaled positions.
Saves state to JSON file so bot can resume after restart.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Optional
from dataclasses import asdict

logger = logging.getLogger("StateManager")

STATE_FILE = Path(__file__).parent / "scaled_positions_state.json"


def save_state(scaled_positions: Dict) -> bool:
    """Save current positions to file."""
    try:
        # Convert to serializable format
        state = {}
        for symbol, pos in scaled_positions.items():
            state[symbol] = {
                'symbol': pos.symbol,
                'side': pos.side,
                'original_size': pos.original_size,
                'remaining_size': pos.remaining_size,
                'entry_price': pos.entry_price,
                'tp1_price': pos.tp1_price,
                'tp2_price': pos.tp2_price,
                'tp3_price': pos.tp3_price,
                'sl_price': pos.sl_price,
                'leverage': pos.leverage,
                'tp1_hit': pos.tp1_hit,
                'tp2_hit': pos.tp2_hit,
                'tp3_hit': pos.tp3_hit,
                'sl_hit': pos.sl_hit,
                'tp1_order_id': pos.tp1_order_id,
                'tp2_order_id': pos.tp2_order_id,
                'tp3_order_id': pos.tp3_order_id,
                'sl_order_id': pos.sl_order_id,
                'unrealized_pnl': getattr(pos, 'unrealized_pnl', 0.0),
                'mark_price': getattr(pos, 'mark_price', 0.0),
            }

        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)

        logger.debug(f"State saved: {len(state)} positions")
        return True
    except Exception as e:
        logger.error(f"Failed to save state: {e}")
        return False


def load_state() -> Dict:
    """Load positions from file."""
    if not STATE_FILE.exists():
        return {}

    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)

        logger.info(f"Loaded state: {len(state)} positions")
        return state
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        return {}


def clear_state():
    """Clear saved state."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        logger.info("State cleared")
