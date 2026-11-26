from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from pb_bot.config import BotConfig
from pb_bot.core.calendar import EconomicCalendar
from pb_bot.core.models import Bar, BrokerAdapter, Direction, OrderRequest
from pb_bot.data.feed import sync_dual_feed
from pb_bot.engine.risk_manager import ClosedTrade, OpenPosition
from pb_bot.engine.strategy import PBStrategyEngine, RejectionRecord


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    trades: List[ClosedTrade] = field(default_factory=list)
    rejections: List[RejectionRecord] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)


class BacktestBroker(BrokerAdapter):
    """Bar-by-bar broker simulator (slippage, commissions, conservative SL-before-TP)."""

    def __init__(
        self,
        config: BotConfig,
        slippage_ticks: int = 1,
        commission_per_side: float = 0.62,  # Standard MNQ all-in fee per side
    ) -> None:
        self.config = config
        self.slippage_pts = slippage_ticks * config.instrument.tick_size
        self.commission_per_side = commission_per_side
        self.balance: float = config.account_balance
        self.position: Optional[OpenPosition] = None
        self.pending_order: Optional[Tuple[str, OrderRequest]] = None
        self.trades: List[ClosedTrade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []
        self._order_seq = 0
        self._just_opened_on_bar_ts: Optional[datetime] = None

    def submit_order(self, order: OrderRequest) -> str:
        self._order_seq += 1
        order_id = f"SIM_{self._order_seq:04d}"

        if order.order_type == "MARKET":
            fill = (
                order.entry_price + self.slippage_pts
                if order.direction == Direction.BULLISH
                else order.entry_price - self.slippage_pts
            )
            self.position = OpenPosition(
                order_id=order_id,
                symbol=order.symbol,
                direction=order.direction,
                quantity=order.quantity,
                entry_price=fill,
                initial_stop_loss=order.stop_loss,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                opened_at=order.timestamp,
                be_trigger_price=order.metadata.get("be_trigger_price"),
                es_target_price=order.metadata.get("es_target_price"),
                reason=order.reason,
                metadata=order.metadata,
            )
            self._just_opened_on_bar_ts = order.timestamp
        else:
            self.pending_order = (order_id, order)

        return order_id

    def modify_stop_loss(self, order_id: str, new_stop: float, reason: str) -> None:
        if self.position is not None and self.position.order_id == order_id:
            self.position.stop_loss = new_stop
            if reason.startswith("BE_"):
                self.position.be_moved = True

    def close_position_market(self, symbol: str, reason: str) -> None:
        if self.position is not None and self.position.symbol == symbol:
            # Close recorded on latest mark via force_close_at_bar
            self.position.metadata["pending_market_close_reason"] = reason

    def process_bar(self, nq_bar: Bar) -> Optional[ClosedTrade]:
        # 1. Check pending limit order fill
        if self.position is None and self.pending_order is not None:
            order_id, req = self.pending_order
            risk_pts = abs(req.entry_price - req.stop_loss)
            # Expire pending limit if price ran 2R in trade direction without filling (PB CISD rule)
            ran_away = (
                (req.direction == Direction.BULLISH and nq_bar.high >= req.entry_price + 2.0 * risk_pts)
                or (req.direction == Direction.BEARISH and nq_bar.low <= req.entry_price - 2.0 * risk_pts)
            )
            if ran_away:
                self.pending_order = None
            else:
                filled = (
                    (req.direction == Direction.BULLISH and nq_bar.low <= req.entry_price)
                    or (req.direction == Direction.BEARISH and nq_bar.high >= req.entry_price)
                )
                if filled:
                    self.position = OpenPosition(
                        order_id=order_id,
                        symbol=req.symbol,
                        direction=req.direction,
                        quantity=req.quantity,
                        entry_price=req.entry_price,
                        initial_stop_loss=req.stop_loss,
                        stop_loss=req.stop_loss,
                        take_profit=req.take_profit,
                        opened_at=nq_bar.timestamp,
                        be_trigger_price=req.metadata.get("be_trigger_price"),
                        es_target_price=req.metadata.get("es_target_price"),
                        reason=req.reason,
                        metadata=req.metadata,
                    )
                    self.pending_order = None
                    self._just_opened_on_bar_ts = nq_bar.timestamp

        # 2. Evaluate open position (never stop out on the exact bar it opened at bar close)
        if self.position is None or self._just_opened_on_bar_ts == nq_bar.timestamp:
            return None

        pos = self.position
        exit_price: Optional[float] = None
        exit_reason: Optional[str] = None

        # Forced market close (e.g., Target SMT exit)
        if "pending_market_close_reason" in pos.metadata:
            exit_reason = pos.metadata["pending_market_close_reason"]
            exit_price = (
                nq_bar.close - self.slippage_pts
                if pos.direction == Direction.BULLISH
                else nq_bar.close + self.slippage_pts
            )
        elif pos.direction == Direction.BULLISH:
            if nq_bar.low <= pos.stop_loss:
                exit_price = pos.stop_loss - self.slippage_pts
                exit_reason = "BREAK_EVEN" if pos.be_moved and abs(pos.stop_loss - pos.entry_price) <= self.config.instrument.tick_size else "STOP_LOSS"
            elif nq_bar.high >= pos.take_profit:
                exit_price = pos.take_profit
                exit_reason = "TAKE_PROFIT"
        else:
            if nq_bar.high >= pos.stop_loss:
                exit_price = pos.stop_loss + self.slippage_pts
                exit_reason = "BREAK_EVEN" if pos.be_moved and abs(pos.stop_loss - pos.entry_price) <= self.config.instrument.tick_size else "STOP_LOSS"
            elif nq_bar.low <= pos.take_profit:
                exit_price = pos.take_profit
                exit_reason = "TAKE_PROFIT"

        if exit_price is None or exit_reason is None:
            return None

        pnl_pts = (
            (exit_price - pos.entry_price)
            if pos.direction == Direction.BULLISH
            else (pos.entry_price - exit_price)
        )
        gross_dollars = pnl_pts * self.config.instrument.point_value * pos.quantity
        fees = 2.0 * self.commission_per_side * pos.quantity
        net_dollars = gross_dollars - fees
        r_mult = pnl_pts / pos.initial_risk_pts if pos.initial_risk_pts > 0 else 0.0

        self.balance += net_dollars
        trade = ClosedTrade(
            order_id=pos.order_id,
            symbol=pos.symbol,
            direction=pos.direction,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            initial_stop_loss=pos.initial_stop_loss,
            take_profit=pos.take_profit,
            opened_at=pos.opened_at,
            closed_at=nq_bar.timestamp,
            pnl_points=pnl_pts,
            pnl_dollars=net_dollars,
            r_multiple=r_mult,
            exit_reason=exit_reason,
            setup_reason=pos.reason,
        )
        self.trades.append(trade)
        self.equity_curve.append((nq_bar.timestamp, self.balance))
        self.position = None
        return trade


def run_backtest(
    nq_bars: List[Bar],
    es_bars: List[Bar],
    config: Optional[BotConfig] = None,
    calendar: Optional[EconomicCalendar] = None,
) -> BacktestResult:
    cfg = config or BotConfig()
    broker = BacktestBroker(cfg)
    engine = PBStrategyEngine(cfg, broker, calendar=calendar)

    for nq_bar, es_bar in sync_dual_feed(nq_bars, es_bars):
        # First check fills/exits on incoming bar
        closed = broker.process_bar(nq_bar)
        if closed is not None:
            engine.on_trade_closed(closed)
            engine.active_position = None
            engine.risk.equity = broker.balance
        elif broker.position is not None and engine.active_position is None:
            engine.active_position = broker.position

        # Run strategy logic on closed 1m bar pair
        engine.on_1m_pair(nq_bar, es_bar)

        # Keep engine position synced to broker's actual post-slippage fill price
        if broker.position is not None and engine.active_position is not None:
            engine.active_position.entry_price = broker.position.entry_price

        # If strategy triggered an immediate Target SMT market exit, settle it on this bar
        if broker.position is not None and "pending_market_close_reason" in broker.position.metadata:
            closed = broker.process_bar(nq_bar)
            if closed is not None:
                engine.on_trade_closed(closed)
                engine.active_position = None
                engine.risk.equity = broker.balance

    return BacktestResult(
        initial_balance=cfg.account_balance,
        final_balance=broker.balance,
        trades=broker.trades,
        rejections=engine.rejections,
        equity_curve=broker.equity_curve,
    )
