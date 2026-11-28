import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from pb_bot.config import BotConfig, BreakEvenMode
from pb_bot.core.liquidity import LiquidityPool
from pb_bot.core.models import Bar, BreakerBlock, Direction


@dataclass
class OpenPosition:
    order_id: str
    symbol: str
    direction: Direction
    quantity: int
    entry_price: float
    initial_stop_loss: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    be_trigger_price: Optional[float] = None
    be_moved: bool = False
    es_target_price: Optional[float] = None
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def initial_risk_pts(self) -> float:
        return abs(self.entry_price - self.initial_stop_loss)


@dataclass(frozen=True)
class ClosedTrade:
    order_id: str
    symbol: str
    direction: Direction
    quantity: int
    entry_price: float
    exit_price: float
    initial_stop_loss: float
    take_profit: float
    opened_at: datetime
    closed_at: datetime
    pnl_points: float
    pnl_dollars: float
    r_multiple: float
    exit_reason: str
    setup_reason: str


class RiskManager:
    """Enforces 0.5% risk sizing, >=1:1 RR floor, manipulation SL, BE, Breaker trailing, & Target SMT exit."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.equity: float = config.account_balance

    def round_tick(self, price: float) -> float:
        tick = self.config.instrument.tick_size
        return round(price / tick) * tick

    def calculate_stop_loss(
        self,
        direction: Direction,
        manipulation_extreme: float,
        oversized_wick_midpoint: Optional[float] = None,
    ) -> float:
        """Anchors SL to manipulation leg extreme (+ tick buffer) or 50% oversized-wick EQ."""
        anchor = oversized_wick_midpoint if oversized_wick_midpoint is not None else manipulation_extreme
        buffer_pts = self.config.sl_tick_buffer * self.config.instrument.tick_size
        if direction == Direction.BULLISH:
            return self.round_tick(anchor - buffer_pts)
        return self.round_tick(anchor + buffer_pts)

    def select_target_with_rr_floor(
        self,
        direction: Direction,
        entry_price: float,
        stop_loss: float,
        dol_candidates: List[LiquidityPool],
    ) -> Tuple[Optional[LiquidityPool], float]:
        """Selects nearest DOL meeting >= min_rr (extends to next DOL if nearest < 1:1)."""
        risk_pts = abs(entry_price - stop_loss)
        if risk_pts <= 0:
            return None, 0.0

        fallback_pool = None
        fallback_rr = 0.0
        for pool in dol_candidates:
            reward_pts = (
                (pool.price - entry_price)
                if direction == Direction.BULLISH
                else (entry_price - pool.price)
            )
            rr = reward_pts / risk_pts
            if rr >= self.config.min_rr:
                if rr <= self.config.max_target_rr:
                    return pool, rr
                if fallback_pool is None:
                    fallback_pool = pool
                    fallback_rr = rr
        if fallback_pool is not None and not self.config.strict_max_rr:
            return fallback_pool, fallback_rr

        return None, 0.0

    def calculate_contracts(
        self,
        entry_price: float,
        stop_loss: float,
        is_continuation_extreme: bool = False,
    ) -> int:
        """Scales contracts strictly to fixed 0.5% account risk (never tightens SL)."""
        risk_pts = abs(entry_price - stop_loss)
        if risk_pts <= 0:
            return 0
        base_risk_dollars = self.equity * self.config.risk_per_trade_pct
        dollar_risk_per_contract = risk_pts * self.config.instrument.point_value
        if dollar_risk_per_contract <= 0:
            return 0
        base_qty = math.floor(base_risk_dollars / dollar_risk_per_contract)
        if base_qty < 1:
            return 0
        if is_continuation_extreme:
            return max(0, math.floor((base_risk_dollars * self.config.continuation_size_mult) / dollar_risk_per_contract))
        return base_qty

    def check_target_smt_exit(
        self, position: OpenPosition, es_bar_1m: Bar
    ) -> Optional[str]:
        """Building Discretion [67:10]: Close NQ immediately at market if ES hits primary target first."""
        if not self.config.enable_target_smt_exit or position.es_target_price is None:
            return None
        if position.direction == Direction.BULLISH and es_bar_1m.high >= position.es_target_price:
            return f"TARGET_SMT_EXIT_ES_HIT_{position.es_target_price:.2f}"
        if position.direction == Direction.BEARISH and es_bar_1m.low <= position.es_target_price:
            return f"TARGET_SMT_EXIT_ES_HIT_{position.es_target_price:.2f}"
        return None

    def evaluate_stop_management(
        self,
        position: OpenPosition,
        nq_bar_1m: Bar,
        active_breakers: List[BreakerBlock],
    ) -> Optional[Tuple[float, str]]:
        """Computes Break-Even trigger and sequential Breaker Block stop trailing."""
        # 1. Mandatory Break-Even trigger (fires on wick or body touch)
        if (
            self.config.be_mode != BreakEvenMode.OFF
            and not position.be_moved
            and position.be_trigger_price is not None
        ):
            hit_be = (
                (position.direction == Direction.BULLISH and nq_bar_1m.high >= position.be_trigger_price)
                or (position.direction == Direction.BEARISH and nq_bar_1m.low <= position.be_trigger_price)
            )
            if hit_be:
                position.be_moved = True
                return self.round_tick(position.entry_price), f"BE_TRIGGERED_{self.config.be_mode.value}"

        # 2. Sequential Breaker Block trailing stop (after BE is active)
        if position.be_moved and active_breakers:
            buffer_pts = self.config.sl_tick_buffer * self.config.instrument.tick_size
            for bb in reversed(active_breakers):
                if not bb.active or not bb.tapped or bb.direction != position.direction:
                    continue
                if position.direction == Direction.BULLISH:
                    candidate = self.round_tick(bb.bottom - buffer_pts)
                    if candidate > position.stop_loss and candidate < nq_bar_1m.close:
                        return candidate, f"TRAIL_BREAKER_{bb.id}"
                else:
                    candidate = self.round_tick(bb.top + buffer_pts)
                    if candidate < position.stop_loss and candidate > nq_bar_1m.close:
                        return candidate, f"TRAIL_BREAKER_{bb.id}"

        return None
