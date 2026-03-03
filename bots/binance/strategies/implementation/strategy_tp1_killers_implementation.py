"""
Binance TP1 Killers Strategy
- Same TP1 logic as BinanceTP1Strategy
- Uses BinanceKillersParser for Binance Killers signal format (COIN/Direction)
"""
from common.parser import BaseSignalParser, BinanceKillersParser
from bots.binance.strategies.implementation.strategy_tp1_implementation import BinanceTP1Strategy


class BinanceTP1KillersStrategy(BinanceTP1Strategy):
    name = "BINANCE TP1 KILLERS BOT"

    @property
    def parser(self) -> BaseSignalParser:
        return BinanceKillersParser()
