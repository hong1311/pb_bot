from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

    @property
    def opposite(self) -> "Direction":
        if self == Direction.BULLISH:
            return Direction.BEARISH
        if self == Direction.BEARISH:
            return Direction.BULLISH
        return Direction.NEUTRAL


class FVGState(str, Enum):
    UNFILLED_TARGET = "UNFILLED_TARGET"    # Acts as DOL target prior to touch
    ACTIVE_CONDITION = "ACTIVE_CONDITION"  # Tapped; disqualifies entries into it
    INVERTED = "INVERTED"                  # Body-closed past far edge on exact TF


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe: str
    timestamp: datetime  # Bar open timestamp in America/New_York
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def upper_wick(self) -> float:
        return self.high - self.body_high

    @property
    def lower_wick(self) -> float:
        return self.body_low - self.low


@dataclass
class FVG:
    id: str
    symbol: str
    timeframe: str
    direction: Direction
    created_at: datetime
    bottom: float
    top: float
    state: FVGState = FVGState.UNFILLED_TARGET
    first_tap_at: Optional[datetime] = None
    # Tracks 5m+ IML/IMH formed inside FVG after first tap (Charting Session rule)
    internal_extreme: Optional[float] = None
    has_bounced_after_tap: bool = False
    re_tapped_after_bounce: bool = False
    internal_extreme_swept: bool = False
    bars_since_first_tap: int = 0
    bars_overlapping_fvg: int = 0

    @property
    def midpoint(self) -> float:
        return (self.bottom + self.top) / 2.0

    def contains_price(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlapped_by(self, bar: Bar) -> bool:
        return bar.low <= self.top and bar.high >= self.bottom


@dataclass(frozen=True)
class IFVG:
    source_fvg: FVG
    direction: Direction         # New tradable direction (opposite of source_fvg)
    confirmed_at: datetime
    confirm_bar: Bar
    candles_to_invert: int


@dataclass
class OrderBlock:
    id: str
    symbol: str
    timeframe: str
    direction: Direction
    confirmed_at: datetime
    open_price: float            # Primary sensitive level (CISD line)
    high: float
    low: float
    sweep_extreme: float         # Wick extreme of liquidity sweep / tap
    invalidated: bool = False
    converted_to_breaker: bool = False


@dataclass(frozen=True)
class CISD:
    symbol: str
    timeframe: str
    direction: Direction
    confirmed_at: datetime
    cisd_level: float            # Open price of first candle in opposing run
    sweep_extreme: float
    orderblock: OrderBlock


@dataclass
class BreakerBlock:
    id: str
    symbol: str
    timeframe: str
    direction: Direction
    confirmed_at: datetime
    bottom: float                # Full candle wick-to-wick low
    top: float                   # Full candle wick-to-wick high
    trigger_swing_price: float   # Prior High (bullish) or Low (bearish) body-closed past
    active: bool = True
    tapped: bool = False


@dataclass(frozen=True)
class DevilsMark:
    symbol: str
    timeframe: str
    timestamp: datetime
    direction: Direction
    open_price: float            # Strong DOL magnet (zero opposing wick at open)


@dataclass(frozen=True)
class SwingPoint:
    index: int
    timestamp: datetime
    price: float
    kind: str                    # "HIGH" or "LOW"
    timeframe: str


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    direction: Direction
    quantity: int
    order_type: str              # "MARKET" or "LIMIT"
    entry_price: float
    stop_loss: float
    take_profit: float
    reason: str
    timestamp: datetime
    metadata: dict = field(default_factory=dict)


class BrokerAdapter(ABC):
    """Broker-agnostic execution interface (Tradovate / TopstepX / IBKR / Simulator)."""

    @abstractmethod
    def submit_order(self, order: OrderRequest) -> str:
        raise NotImplementedError

    @abstractmethod
    def modify_stop_loss(self, order_id: str, new_stop: float, reason: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def close_position_market(self, symbol: str, reason: str) -> None:
        raise NotImplementedError
