from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from pb_bot.config import BotConfig
from pb_bot.core.calendar import EconomicCalendar
from pb_bot.core.models import Bar, BrokerAdapter, Direction, OrderRequest
from pb_bot.core.sessions import SessionTracker, cme_trading_date, to_ny
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
    narrative_events: List[dict] = field(default_factory=list)


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
        if self.position is not None or self.pending_order is not None:
            raise RuntimeError("Cannot submit while another order or position is active")
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

    def expire_pending(self, nq_bar: Bar, es_bar: Optional[Bar] = None) -> None:
        if self.pending_order is None:
            return
        _, req = self.pending_order
        ts = to_ny(nq_bar.timestamp)
        cfg = self.config
        target_hit = nq_bar.open >= req.take_profit if req.direction == Direction.BULLISH else nq_bar.open <= req.take_profit
        es_target = req.metadata.get("es_target_price")
        if es_bar is not None and es_target is not None:
            target_hit |= es_bar.open >= es_target if req.direction == Direction.BULLISH else es_bar.open <= es_target
        if (cme_trading_date(ts) != cme_trading_date(req.timestamp)
            or not SessionTracker.in_trading_window(ts, cfg.am_start, cfg.am_end, cfg.allow_pm_session, cfg.pm_start, cfg.pm_end)
            or ts - req.timestamp >= timedelta(minutes=cfg.pending_order_max_minutes)
            or target_hit):
            self.pending_order = None

    def process_market_close(self, bar: Bar) -> Optional[ClosedTrade]:
        if self.position is None or "pending_market_close_reason" not in self.position.metadata:
            return None
        return self.process_bar(bar)

    def process_bar(self, nq_bar: Bar) -> Optional[ClosedTrade]:
        self.expire_pending(nq_bar)
        limit_filled = False
        # 1. Check pending limit order fill
        if self.position is None and self.pending_order is not None:
            order_id, req = self.pending_order
            risk_pts = abs(req.entry_price - req.stop_loss)
            # Expire pending limit if price ran 2R in trade direction without filling (PB CISD rule)
            ran_away = (
                (req.direction == Direction.BULLISH and nq_bar.high >= req.entry_price + 2.0 * risk_pts)
                or (req.direction == Direction.BEARISH and nq_bar.low <= req.entry_price - 2.0 * risk_pts)
            )
            filled = (
                (req.direction == Direction.BULLISH and nq_bar.low <= req.entry_price)
                or (req.direction == Direction.BEARISH and nq_bar.high >= req.entry_price)
            )
            # With OHLC-only data, a possible fill takes precedence over a possible run-away.
            target_touched = nq_bar.high >= req.take_profit if req.direction == Direction.BULLISH else nq_bar.low <= req.take_profit
            if (ran_away or target_touched) and not filled:
                self.pending_order = None
            elif filled:
                if nq_bar.timestamp > req.timestamp:
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
                    self._just_opened_on_bar_ts = None
                    limit_filled = True

        # 2. Evaluate open position (never stop out on the exact bar it opened at bar close)
        if self.position is None or (self._just_opened_on_bar_ts == nq_bar.timestamp and "pending_market_close_reason" not in self.position.metadata):
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
                exit_price = min(pos.stop_loss, nq_bar.open) - self.slippage_pts
                exit_reason = "BREAK_EVEN" if pos.be_moved and abs(pos.stop_loss - pos.entry_price) <= self.config.instrument.tick_size else "STOP_LOSS"
            elif not limit_filled and nq_bar.high >= pos.take_profit:
                exit_price = pos.take_profit
                exit_reason = "TAKE_PROFIT"
        else:
            if nq_bar.high >= pos.stop_loss:
                exit_price = max(pos.stop_loss, nq_bar.open) + self.slippage_pts
                exit_reason = "BREAK_EVEN" if pos.be_moved and abs(pos.stop_loss - pos.entry_price) <= self.config.instrument.tick_size else "STOP_LOSS"
            elif not limit_filled and nq_bar.low <= pos.take_profit:
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

    last_bar = None
    for nq_bar, es_bar in sync_dual_feed(nq_bars, es_bars):
        process_pair(engine, broker, nq_bar, es_bar)
        last_bar = nq_bar
    if last_bar is not None:
        settle_end_of_data(engine, broker, last_bar)

    return BacktestResult(
        initial_balance=cfg.account_balance,
        final_balance=broker.balance,
        trades=broker.trades,
        rejections=engine.rejections,
        equity_curve=broker.equity_curve,
        narrative_events=engine.narrative.events,
    )


def _sync_broker(engine: PBStrategyEngine, broker: BacktestBroker, closed: Optional[ClosedTrade]) -> None:
    if closed is not None:
        engine.on_trade_closed(closed)
    engine.active_position = broker.position
    engine.risk.equity = broker.balance


def process_pair(engine: PBStrategyEngine, broker: BacktestBroker, nq_bar: Bar, es_bar: Bar, warmup: bool = False) -> None:
    """Shared chronological execution path for replay and polling."""
    broker.expire_pending(nq_bar, es_bar)
    closed = broker.process_bar(nq_bar)
    _sync_broker(engine, broker, closed)
    engine.on_1m_pair(nq_bar, es_bar, allow_entry=not warmup and broker.pending_order is None)
    _sync_broker(engine, broker, broker.process_market_close(nq_bar))
    # Cancel on information observed at this bar close, before the next bar can fill.
    if broker.pending_order is not None:
        _, req = broker.pending_order
        narrative_id = req.metadata.get("narrative_id")
        if narrative_id is not None and not engine.narrative.accepts_pending(
            narrative_id, req.direction, nq_bar.timestamp
        ):
            broker.pending_order = None
            engine._log_rejection(nq_bar.timestamp, "PENDING_NARRATIVE_INVALIDATED", req.reason)


def settle_end_of_data(engine: PBStrategyEngine, broker: BacktestBroker, bar: Bar) -> None:
    broker.pending_order = None
    if broker.position is not None:
        broker.close_position_market(broker.position.symbol, "END_OF_DATA")
        _sync_broker(engine, broker, broker.process_market_close(bar))
