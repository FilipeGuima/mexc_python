from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Any

class OrderSide(Enum):
    Buy = "buy"
    Sell = "sell"

class PositionSide(Enum):
    Long = "long"
    Short = "short"
    Net = "net"

class OrderType(Enum):
    Market = "market"
    Limit = "limit"
    PostOnly = "post_only"
    Fok = "fok"
    Ioc = "ioc"

class MarginMode(Enum):
    Cross = "cross"
    Isolated = "isolated"

@dataclass
class BlofinOrderRequest:
    instId: str
    side: OrderSide
    orderType: OrderType
    marginMode: MarginMode
    size: str
    positionSide: Optional[PositionSide] = None
    price: Optional[str] = None
    reduceOnly: Optional[str] = "false"
    clientOid: Optional[str] = None

class CloseReason(Enum):
    """Reason why a position was closed"""
    TP = "take_profit"
    SL = "stop_loss"
    MANUAL = "manual"
    LIQUIDATION = "liquidation"
    UNKNOWN = "unknown"


@dataclass
class PositionInfo:
    """Standardized Position Info for the bot"""
    positionId: str
    symbol: str
    holdVol: float
    positionType: str  # "long", "short", "net"
    openAvgPrice: float
    liquidatePrice: float
    unrealized: float
    unrealizedPnlRatio: float  # PnL as percentage
    leverage: int
    marginMode: str
    marginRatio: float  # How close to liquidation
    margin: float  # Initial/maintenance margin
    markPrice: float  # Current mark price
    createTime: str
    updateTime: str

@dataclass
class AssetInfo:
    currency: str
    equity: float
    availableBalance: float
    unrealized: float