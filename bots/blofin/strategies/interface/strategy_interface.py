from abc import ABC, abstractmethod
from typing import Optional


class BlofinStrategy(ABC):
    """
    Abstract base class for all Blofin trading strategies.

    The engine handles: signal parsing, balance fetching, volume calculation,
    order placement, monitoring loops, and position tracking.

    Strategies only define: TP selection, fill handling, per-tick logic,
    and optional breakeven/update support.
    """

    name: str = "BlofinBot"

    @abstractmethod
    def validate_signal(self, signal_data: dict) -> Optional[str]:
        """Extra validation beyond standard TP/SL check. Return None=valid, string=error."""
        pass

    @abstractmethod
    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        """
        Pick TP/SL from signal data. Return dict with keys like:
        - Simple: {'tp': price, 'sl': price}
        - Scaled: {'tp1': p, 'tp2': p, 'tp3': p, 'sl': p, 'mode': 'scaled'}
        """
        pass

    @abstractmethod
    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        """Order filled - set up TPSL, add to tracking."""
        pass

    @abstractmethod
    async def on_tick(self, engine):
        """Called each monitor cycle. Custom monitoring (e.g., scaled TP checks)."""
        pass

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """Handle 'MOVE SL TO ENTRY'. Return result msg or None if not supported."""
        return None

    async def on_position_closed(self, symbol: str, pos_info: dict, close_reason, engine):
        """Custom close handling. Default does nothing (engine provides default handler)."""
        pass

    @property
    def supports_updates(self) -> bool:
        """Whether this strategy supports UPDATE signals (change TP/SL)."""
        return False

    def get_state(self) -> dict:
        """Return serializable state for persistence."""
        return {}

    def load_state(self, data: dict):
        """Restore state from disk."""
        pass
