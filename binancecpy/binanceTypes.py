from dataclasses import dataclass
from enum import Enum
from typing import Optional


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class OrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class MarginMode(Enum):
    ISOLATED = "ISOLATED"
    CROSSED = "CROSSED"


class TimeInForce(Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class CloseReason(Enum):
    TP = "take_profit"
    SL = "stop_loss"
    MANUAL = "manual"
    LIQUIDATION = "liquidation"
    UNKNOWN = "unknown"


@dataclass
class PositionInfo:
    symbol: str
    positionAmt: float
    entryPrice: float
    markPrice: float
    unRealizedProfit: float
    liquidationPrice: float
    leverage: int
    marginType: str
    positionSide: str
    updateTime: int


@dataclass
class AssetInfo:
    asset: str
    availableBalance: float
    balance: float
    crossUnPnl: float
