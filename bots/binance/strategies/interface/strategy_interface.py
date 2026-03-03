from abc import ABC, abstractmethod
from typing import Optional

from common.parser import BaseSignalParser, DefaultSignalParser


class BinanceStrategy(ABC):
    name: str = "BinanceBot"

    @property
    def parser(self) -> BaseSignalParser:
        """Signal parser used by this strategy. Override to use a different format."""
        return DefaultSignalParser()

    @abstractmethod
    def validate_signal(self, signal_data: dict) -> Optional[str]:
        """Extra validation beyond standard TP/SL check. Return None=valid, string=error."""
        pass

    @abstractmethod
    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        """
        Pick TP/SL from signal data. Return dict with keys like:
        - Simple: {'tp': price, 'sl': price}
        """
        pass

    @abstractmethod
    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        """Order filled - set up TP and SL as separate orders, add to tracking."""
        pass

    @abstractmethod
    async def on_tick(self, engine):
        """Called each monitor cycle. Custom monitoring logic."""
        pass

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """Handle 'MOVE SL TO ENTRY'. Return result msg or None if not supported."""
        return None

    async def on_position_closed(self, symbol: str, pos_info: dict, close_reason, engine):
        """Custom close handling. Default does nothing."""
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
