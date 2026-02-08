"""
MEXC Strategy Interface

Base class for all MEXC trading strategies.
MEXC strategies have more control than Blofin strategies because the
MEXC bots have more structural differences (market-only vs smart entry,
different parsers, different close handling).
"""

from abc import ABC, abstractmethod
from typing import Optional


class MexcStrategy(ABC):
    """
    Abstract base class for all MEXC trading strategies.

    The engine provides shared helpers but strategies orchestrate more
    of the trading flow compared to Blofin strategies.
    """

    name: str = "MexcBot"

    @abstractmethod
    def parse_signal(self, text: str) -> Optional[dict]:
        """
        Parse signal text. Each strategy may use different parser.
        Return None if parsing fails or signal should be ignored.
        """
        pass

    @abstractmethod
    async def handle_signal(self, text: str, engine) -> Optional[str]:
        """
        Route parsed signal (trade, breakeven, update).
        Returns result message or None.
        """
        pass

    @abstractmethod
    async def execute_trade(self, signal_data: dict, engine) -> str:
        """
        Execute trade using engine helpers.
        Returns result message.
        """
        pass

    async def on_startup(self, engine):
        """
        Called during startup.
        Override for resume_monitoring, position loading, etc.
        """
        pass

    @property
    def supports_updates(self) -> bool:
        """Whether this strategy supports UPDATE signals."""
        return False
