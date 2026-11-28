from dataclasses import dataclass
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple

from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode, NarrativeMode, PlaybookMode, StructureMode, DiscretionMode, PlaybookModel, RegimeMode
from pb_bot.core.calendar import EconomicCalendar
from pb_bot.core.liquidity import LiquidityTracker
from pb_bot.core.models import (
    Bar,
    BrokerAdapter,
    Direction,
    FVG,
    FVGState,
    IFVG,
    OrderRequest,
)
from pb_bot.core.primitives import (
    BreakerBlockDetector,
    CISDDetector,
    DevilsMarkDetector,
    FVGDetector,
    is_in_premium,
    oversized_wick_eq,
)
from pb_bot.core.sessions import SessionTracker, to_ny
from pb_bot.data.resampler import MultiTimeframeResampler, SUPPORTED_TIMEFRAMES
from pb_bot.engine.bias_profile import BiasAndProfileEngine, DailyProfile
from pb_bot.engine.risk_manager import OpenPosition, RiskManager
from pb_bot.engine.narrative import MarketNarrativeEngine
from pb_bot.engine.playbook import ManipulationLeg, PlaybookEngine
from pb_bot.engine.structure import StructureEngine
from pb_bot.engine.discretion import DiscretionEngine
from pb_bot.engine.regime import RegimeEngine


@dataclass(frozen=True)
class RejectionRecord:
    timestamp: datetime
    code: str
    detail: str


class PBStrategyEngine:
    """Full bar-by-bar PB Trading execution engine across synchronized NQ + ES streams."""

    def __init__(self, config: BotConfig, broker: BrokerAdapter, calendar: Optional[EconomicCalendar] = None) -> None:
        self.config = config
        self.broker = broker
        self.calendar = calendar

        self.nq_resampler = MultiTimeframeResampler(config.primary_symbol, SUPPORTED_TIMEFRAMES)
        self.es_resampler = MultiTimeframeResampler(config.correlated_symbol, SUPPORTED_TIMEFRAMES)
        self.nq_sessions = SessionTracker()
        self.es_sessions = SessionTracker()

        self.nq_fvgs: Dict[str, FVGDetector] = {
            tf: FVGDetector(config.primary_symbol, tf) for tf in SUPPORTED_TIMEFRAMES
        }
        self.es_fvgs: Dict[str, FVGDetector] = {
            tf: FVGDetector(config.correlated_symbol, tf) for tf in ("5m", "15m", "1H", "4H")
        }
        self.nq_cisds: Dict[str, CISDDetector] = {
            tf: CISDDetector(config.primary_symbol, tf) for tf in ("1m", "5m", "15m", "1H")
        }
        self.nq_breakers: Dict[str, BreakerBlockDetector] = {
            tf: BreakerBlockDetector(config.primary_symbol, tf) for tf in ("2m", "5m")
        }
        self.devils_marks = DevilsMarkDetector(config.primary_symbol, "15m")

        self.liquidity = LiquidityTracker()
        self.bias_engine = BiasAndProfileEngine(config)
        self.risk = RiskManager(config)
        self.narrative = MarketNarrativeEngine()
        self.playbook = PlaybookEngine(config)
        self.structure = StructureEngine(config)
        self.discretion = DiscretionEngine(config)
        self.regime = RegimeEngine(config, calendar)
        self._structural_decision = None

        # Daily runtime state
        self._current_day = None
        self.trades_today: int = 0
        self.losses_today: int = 0
        self.wins_today: int = 0
        self.am_5m_fvg_count: int = 0
        self.judas_manipulation_seen: bool = False
        self.day_invalidated_immediate_delivery: bool = False
        self.active_position: Optional[OpenPosition] = None
        self.rejections: List[RejectionRecord] = []
        self.submitted_orders: List[OrderRequest] = []
        self._selected_leg_extreme: Optional[float] = None
        self._selected_leg: Optional[ManipulationLeg] = None

    def _log_rejection(self, ts: datetime, code: str, detail: str) -> None:
        self.rejections.append(RejectionRecord(ts, code, detail))

    @staticmethod
    def _valid_es_target(
        direction: Direction, es_target: Optional[float], es_bar: Bar
    ) -> Optional[float]:
        if es_target is None:
            return None
        if direction == Direction.BULLISH and es_target > es_bar.high:
            return es_target
        if direction == Direction.BEARISH and es_target < es_bar.low:
            return es_target
        return None

    def on_trade_closed(self, closed) -> None:
        pnl = getattr(closed, 'pnl_dollars', 0.0)
        if pnl < 0:
            self.losses_today += 1
        elif pnl > 0:
            self.wins_today += 1

    def on_1m_pair(self, nq_bar: Bar, es_bar: Bar, allow_entry: bool = True) -> Optional[OrderRequest]:
        ts = to_ny(nq_bar.timestamp)
        nq_sess = self.nq_sessions.update(nq_bar)
        es_sess = self.es_sessions.update(es_bar)

        # Daily reset on CME session rollover
        if self._current_day != nq_sess.trading_day:
            self._current_day = nq_sess.trading_day
            self.trades_today = 0
            self.losses_today = 0
            self.wins_today = 0
            self.am_5m_fvg_count = 0
            self.judas_manipulation_seen = False
            self.day_invalidated_immediate_delivery = False
            self.bias_engine.reset_for_new_day(nq_sess.trading_day)

        # 1. Update resamplers & all exact-TF primitive detectors
        nq_closed = self.nq_resampler.update(nq_bar)
        es_closed = self.es_resampler.update(es_bar)

        newly_confirmed_ifvgs: Dict[str, List[IFVG]] = {}
        created_fvgs: List[FVG] = []
        for tf, bars in nq_closed.items():
            for b in bars:
                created_fvg, new_ifvgs = self.nq_fvgs[tf].update(b)
                if created_fvg is not None:
                    created_fvgs.append(created_fvg)
                if tf == '5m' and created_fvg is not None and time(9, 30) <= to_ny(b.timestamp).time() < time(11, 0):
                    self.am_5m_fvg_count += 1
                if new_ifvgs:
                    newly_confirmed_ifvgs.setdefault(tf, []).extend(new_ifvgs)
                if tf in self.nq_cisds:
                    cisd = self.nq_cisds[tf].update(b)
                    if cisd is not None and tf == "15m":
                        self.bias_engine.on_15m_cisd(cisd, nq_sess, self._all_fvgs_by_tf())
                if tf in self.nq_breakers:
                    self.nq_breakers[tf].update(b)
                if tf == "15m":
                    self.devils_marks.update(b)
                if tf in ("4H", "1H", "15m"):
                    self.bias_engine.on_htf_fvg_update(
                        tf, b, self._all_fvgs_by_tf(), [i.direction for i in new_ifvgs]
                    )

        for tf, bars in es_closed.items():
            for b in bars:
                if tf in self.es_fvgs:
                    self.es_fvgs[tf].update(b)

        # Real-time 1m tap visibility for HTF FVGs (inversions still require exact-TF close)
        for tf in ("5m", "15m", "30m", "1H", "4H", "1D"):
            if tf not in nq_closed:
                self.nq_fvgs[tf].register_intraday_tap(nq_bar)
        for tf in ("5m", "15m", "1H", "4H"):
            if tf not in es_closed:
                self.es_fvgs[tf].register_intraday_tap(es_bar)

        # Update synchronized NQ/ES swings & SMT on key timeframes
        for tf in ("1m", "5m", "15m", "1H"):
            nq_b = nq_closed[tf][-1] if tf in nq_closed else None
            es_b = es_closed[tf][-1] if tf in es_closed else None
            if nq_b is not None or es_b is not None:
                self.liquidity.update_bars(tf, nq_b, es_b)

        # Regime observes every closed minute, including warmup and open positions.
        self.playbook.update(nq_bar, nq_sess, self._all_fvgs_by_tf(), self.liquidity.nq_swings)
        self.regime.update(nq_bar, nq_sess, nq_closed, created_fvgs, self.liquidity.smt_events,
                           self.playbook.evidence_at(ts), self.narrative.current)
        strict_regime = self.config.regime_mode == RegimeMode.STRICT
        # Replace only the legacy Seek & Destroy classification in strict mode.
        bias_state = self.bias_engine.evaluate_daily_profile(
            ts, nq_sess, self._all_fvgs_by_tf(), current_price=nq_bar.close,
            seek_override=self.regime.seek_active if strict_regime else None,
        )

        # Advance narrative even during warmup, outside execution hours, or in a trade.
        self.narrative.update(
            nq_bar, es_bar, bias_state.bias, nq_sess, es_sess,
            self._all_fvgs_by_tf(), created_fvgs, newly_confirmed_ifvgs, nq_closed,
            blocked=bias_state.seek_and_destroy_active or bias_state.profile == DailyProfile.SEEK_AND_DESTROY,
        )

        self.structure.update(nq_bar, nq_sess.trading_day, nq_closed, created_fvgs,
                              newly_confirmed_ifvgs, self.playbook.evidence_at(ts))

        self.discretion.update(nq_bar, nq_sess.trading_day, nq_closed)

        # Track 09:30 Judas manipulation away from 09:30 open
        if nq_sess.open_930 is not None and ts.time() >= time(9, 30):
            if bias_state.bias == Direction.BULLISH and nq_bar.low < nq_sess.open_930:
                self.judas_manipulation_seen = True
            elif bias_state.bias == Direction.BEARISH and nq_bar.high > nq_sess.open_930:
                self.judas_manipulation_seen = True

        # 2. Manage open position (Target SMT exit -> Break-Even -> Breaker trailing)
        if self.active_position is not None:
            smt_exit = self.risk.check_target_smt_exit(self.active_position, es_bar)
            if smt_exit is not None:
                self.broker.close_position_market(self.active_position.symbol, smt_exit)
                self.active_position = None
                return None

            stop_update = self.risk.evaluate_stop_management(
                self.active_position,
                nq_bar,
                self.nq_breakers["2m"].breakers + self.nq_breakers["5m"].breakers,
            )
            if stop_update is not None:
                new_sl, sl_reason = stop_update
                self.broker.modify_stop_loss(self.active_position.order_id, new_sl, sl_reason)
                self.active_position.stop_loss = new_sl
            return None

        if not allow_entry:
            return None

        # 3. Hard Session, Trade Cap, & Regime Filters
        if not SessionTracker.in_trading_window(
            ts,
            self.config.am_start,
            self.config.am_end,
            self.config.allow_pm_session,
            self.config.pm_start,
            self.config.pm_end,
        ):
            return None

        if self.trades_today >= self.config.max_trades_per_day:
            return None
        if self.config.max_wins_per_day is not None and self.wins_today >= self.config.max_wins_per_day:
            if newly_confirmed_ifvgs:
                self._log_rejection(ts, 'MAX_DAILY_WINS', f'Reached {self.wins_today} win(s) today')
            return None
        if self.losses_today >= self.config.max_losses_per_day:
            if newly_confirmed_ifvgs:
                self._log_rejection(ts, 'MAX_DAILY_LOSSES', f'Reached {self.losses_today} loss(es) today')
            return None

        # 10:00 AM ET USD red-folder macro buffer (Ep. 6 Time)
        if self.config.enable_10am_macro_pause and time(9, 55) <= ts.time() <= time(10, 10):
            if newly_confirmed_ifvgs:
                self._log_rejection(ts, 'MACRO_10AM_PAUSE', '09:55-10:10 NY macro buffer')
            return None

        # Lunch accumulation hard block (12:00-13:00 NY)
        if self.config.enable_lunch_block and time(12, 0) <= ts.time() < time(13, 0):
            if newly_confirmed_ifvgs:
                self._log_rejection(ts, "NY_LUNCH_ACCUMULATION", "12:00-13:00 NY lunch stand-aside")
            return None

        if strict_regime:
            if self.regime.blocked:
                if newly_confirmed_ifvgs:
                    for reason in self.regime.reasons:
                        self._log_rejection(ts, reason, "V2.5 market regime gate")
                return None
        else:
            # Forex Factory Red Folder calendar gates (Yellow ignored)
            if self.calendar is not None:
                pm_red = self.calendar.has_pm_red_news(nq_sess.trading_day)
                if pm_red is not None and ts.time() < time(12, 0):
                    if newly_confirmed_ifvgs:
                        self._log_rejection(ts, "PM_RED_NEWS_SUPPRESSION", f"AM suppressed by PM {pm_red.title}")
                    return None
                pre_event = self.calendar.is_day_before_nfp_or_cpi(nq_sess.trading_day)
                if pre_event is not None:
                    if newly_confirmed_ifvgs:
                        self._log_rejection(ts, "PRE_NFP_CPI_CHOP", f"Day before {pre_event.title}")
                    return None
                freeze_ev = self.calendar.active_red_news_freeze(ts)
                if freeze_ev is not None:
                    if newly_confirmed_ifvgs:
                        self._log_rejection(ts, "RED_NEWS_FREEZE", f"Frozen around {freeze_ev.title}")
                    return None

            if bias_state.seek_and_destroy_active or bias_state.profile == DailyProfile.SEEK_AND_DESTROY:
                if newly_confirmed_ifvgs:
                    self._log_rejection(ts, "SEEK_AND_DESTROY", bias_state.notes)
                return None

            # Condition #1: Dual-sided SMT divergence at key levels
            if self.config.enable_dual_smt_chop_filter and self.liquidity.has_dual_sided_smt(ts, current_price=nq_bar.close):
                if newly_confirmed_ifvgs:
                    self._log_rejection(ts, "DUAL_SMT_CHOP", "Concurrent Bullish & Bearish key-level SMT")
                return None

            # Condition #2: Overnight exhaustion (>300 pts + HTF daily objective swept)
            if self.bias_engine.is_overnight_exhausted(nq_sess, current_price=nq_bar.close) and ts.time() < time(11, 0):
                if newly_confirmed_ifvgs:
                    self._log_rejection(
                        ts,
                        "OVERNIGHT_EXHAUSTION_300PTS",
                        f"Overnight range {nq_sess.overnight_range:.1f}pts + HTF objective hit",
                    )
                return None

            # Volatility check: zero 5m FVGs printed between 09:30 and 10:00 -> stand aside in 10:00 hour
            if (
                self.config.enable_am_5m_fvg_volatility_filter
                and time(10, 0) <= ts.time() < time(11, 0)
                and self.am_5m_fvg_count == 0
            ):
                if newly_confirmed_ifvgs:
                    self._log_rejection(ts, "NO_5M_FVG_VOLATILITY", "Zero 5m FVGs printed since 09:30 NY")
                return None

        direction = bias_state.bias
        if direction == Direction.NEUTRAL:
            if ts.time() < time(10, 0) and newly_confirmed_ifvgs:
                self._log_rejection(ts, "NEUTRAL_BIAS_PRE_10AM", "Standing aside until 10:00 structure")
            return None

        # Only scan for entries when an LTF IFVG confirmed on this bar
        if not any(tf in newly_confirmed_ifvgs for tf in self.config.ifvg_scan_ladder):
            return None

        narrative = self.narrative.current
        strict_narrative = self.config.narrative_mode == NarrativeMode.STRICT
        # 4. Highest-Timeframe IFVG Selection (5m -> 4m -> 3m -> 2m -> 1m)
        chosen_ifvg, is_judas_override = self._select_highest_tf_ifvg(
            direction, newly_confirmed_ifvgs, ts, nq_bar
        )
        if chosen_ifvg is None:
            return None

        entry_regime_reason = self.regime.audit_entry(ts, chosen_ifvg, self._selected_leg)
        if strict_regime and entry_regime_reason:
            self._log_rejection(ts, entry_regime_reason, "Repeated overlapping reversal candles in IFVG")
            return None

        # Slow/Choppy LRLR Inversion Filter (DOL 2.0 [40:24])
        if chosen_ifvg.candles_to_invert > self.config.max_candles_for_clean_inversion:
            self._log_rejection(
                ts,
                "SLOW_CHOPPY_INVERSION",
                f"{chosen_ifvg.source_fvg.timeframe} took {chosen_ifvg.candles_to_invert} candles",
            )
            return None

        # Model-specific evidence is collected before the IFVG; in observe mode it is diagnostic only.
        model_decision = self.playbook.evaluate(direction, ts, nq_bar, self._selected_leg,
                                                narrative, chosen_ifvg, bias_state.profile)
        strict_models = self.config.model_mode == PlaybookMode.STRICT
        if strict_narrative and (narrative is None or not narrative.can_enter(direction, ts)):
            self._log_rejection(ts, "NARRATIVE_NOT_READY", narrative.phase.value if narrative else "NO_DELIVERY_SOURCE")
            return None
        if strict_models:
            if model_decision is None or is_judas_override:
                self._log_rejection(ts, "NO_ELIGIBLE_PB_MODEL", "No enabled model with fresh causal evidence")
                return None
            evidence = model_decision.evidence
            key_level_ok, setup_type, key_detail, matched_fvg = True, model_decision.model.value, evidence.label, evidence.fvg
            if (self.config.discretion_mode != DiscretionMode.STRICT and matched_fvg is not None
                    and self._is_disqualified_stacked_fvg(matched_fvg, direction, nq_sess)):
                self._log_rejection(ts, "STACKED_FVG_PREMIUM_DISQUALIFIED", matched_fvg.id)
                return None
        else:
            key_level_ok, setup_type, key_detail, matched_fvg = self._check_key_level_gate(
                direction, ts, nq_bar, nq_sess
            )
        if not key_level_ok and not is_judas_override:
            code = "STACKED_FVG_PREMIUM_DISQUALIFIED" if key_detail.startswith("STACKED_FVG_") else "NO_VALID_KEY_LEVEL"
            self._log_rejection(ts, code, key_detail)
            return None

        # Time Alignment Matrix (TIME AND STRUCTURE): 1H/4H/1D PDRA cannot jump directly to 1m IFVG
        # Requires Path A (5m/15m IFVG) OR Path B (structural re-sweep of initial reaction extreme)
        if (
            self.config.enable_time_alignment_matrix
            and not is_judas_override
            and matched_fvg is not None
            and matched_fvg.timeframe in ("1H", "4H", "1D")
            and not (model_decision.evidence.pivot_price is not None if strict_models else matched_fvg.internal_extreme_swept)
            and not self._has_aligned_confirmation(matched_fvg, direction, ts)
        ):
            self._log_rejection(
                ts,
                "TIME_ALIGNMENT_VIOLATION",
                f"Direct {matched_fvg.timeframe}->{chosen_ifvg.source_fvg.timeframe} entry blocked without aligned flip or re-sweep",
            )
            return None

        # 6. Target-to-Condition Blacklist (Building Discretion [05:00]):
        # Never enter directly into or inside an un-inverted opposing 5m/15m FVG
        opposing_blocker = self._is_inside_opposing_5m_15m_fvg(direction, nq_bar)
        if opposing_blocker is not None:
            self._log_rejection(
                ts,
                "INSIDE_OPPOSING_5M_15M_FVG",
                f"Blocked by {opposing_blocker.id} [{opposing_blocker.bottom:.2f}, {opposing_blocker.top:.2f}]",
            )
            return None

        # 7. Path Obstruction Filter (Spread-apart EQH/EQL)
        eq_block = self.liquidity.has_spread_apart_obstruction(direction, nq_bar.close)
        if eq_block is not None:
            self._log_rejection(
                ts,
                "SPREAD_APART_EQ_OBSTRUCTION",
                f"Blocked by {eq_block.label} at {eq_block.price:.2f} (span={eq_block.bar_span})",
            )
            return None

        # 8. V2.4 evaluates the prior aligned impulse, not the opposing entry leg.
        # OBSERVE preserves the legacy gates and sizing; STRICT replaces them.
        entry_price, order_type = self._resolve_entry_price(chosen_ifvg, direction)
        strict_discretion = self.config.discretion_mode == DiscretionMode.STRICT
        model = model_decision.model if model_decision else (PlaybookModel.CONTINUATION if setup_type == "CONTINUATION_5M" else None)
        discretion = self.discretion.audit(
            direction, model, matched_fvg, self._selected_leg, chosen_ifvg,
            entry_price, order_type,
            self.nq_fvgs[matched_fvg.timeframe].fvgs if matched_fvg is not None else (),
            self._has_unfilled_1h_fvg_behind(direction, entry_price), ts,
        )
        if strict_discretion:
            if not discretion.allowed:
                self._log_rejection(ts, discretion.reason, "Structural discretion gate")
                return None
            is_continuation_extreme = discretion.reduced_risk
        else:
            past_eq_05 = self._is_past_equilibrium(direction, nq_bar.close, nq_sess)
            is_continuation_extreme = self._is_overextended_continuation(direction, nq_bar.close, nq_sess)
            if past_eq_05 and self._has_unfilled_1h_fvg_behind(direction, nq_bar.close):
                self._log_rejection(
                    ts, "TOO_PREMIUM_OR_DISCOUNT_WITH_1H_FVG_BEHIND",
                    f"Close={nq_bar.close:.2f} past 0.5 EQ with unfilled 1H FVG behind",
                )
                return None

        # 9. Compute stop from the actual manipulation extreme. A separately
        # validated midpoint exception is optional and never used for limits.
        manip_extreme, _ = self._get_manipulation_stop_anchor(direction, chosen_ifvg)
        if self._selected_leg_extreme is not None:
            manip_extreme = self._selected_leg_extreme
        stop_loss = self.risk.calculate_stop_loss(direction, manip_extreme,
            oversized_wick_midpoint=discretion.wick_midpoint if strict_discretion else None)

        # Ensure SL is on valid side of entry
        if (direction == Direction.BULLISH and stop_loss >= entry_price) or (
            direction == Direction.BEARISH and stop_loss <= entry_price
        ):
            self._log_rejection(ts, "INVALID_SL_GEOMETRY", f"entry={entry_price} sl={stop_loss}")
            return None

        if self.config.max_stop_pts is not None and abs(entry_price - stop_loss) > self.config.max_stop_pts:
            self._log_rejection(ts, "SL_TOO_WIDE", f"SL={abs(entry_price - stop_loss):.2f}pts > max {self.config.max_stop_pts:.1f}pts")
            return None

        if strict_narrative:
            # A known DOL must satisfy RR; never skip it to manufacture a better RR.
            dol_candidates = [narrative.primary_dol]
        else:
            dol_candidates = self.liquidity.collect_dol_targets(
                direction=direction,
                current_price=entry_price,
                now=ts,
                nq_session=nq_sess,
                es_session=es_sess,
                unfilled_fvgs=self._collect_unfilled_fvgs(("5m", "15m", "1H", "4H", "1D")),
                devils_marks=self.devils_marks.marks,
                es_fvgs=[f for tf in ("5m", "15m", "1H", "4H") for f in self.es_fvgs[tf].fvgs],
            )
        target_pool, rr = self.risk.select_target_with_rr_floor(
            direction, entry_price, stop_loss, dol_candidates
        )
        if target_pool is None:
            self._log_rejection(
                ts,
                "RR_BELOW_1_TO_1",
                f"No DOL target reached min RR {self.config.min_rr:.2f} from entry={entry_price:.2f}",
            )
            return None

        qty = self.risk.calculate_contracts(
            entry_price, stop_loss, is_continuation_extreme=is_continuation_extreme
        )
        if qty < 1:
            self._log_rejection(ts, "RISK_EXCEEDS_MIN_CONTRACT", f"SL={abs(entry_price - stop_loss):.2f}pts exceeds 0.5% risk on 1 {self.config.instrument.symbol}")
            return None

        be_trigger = self._resolve_be_trigger(direction, entry_price)
        order = OrderRequest(
            symbol=self.config.instrument.symbol,
            direction=direction,
            quantity=qty,
            order_type=order_type,
            entry_price=self.risk.round_tick(entry_price),
            stop_loss=stop_loss,
            take_profit=self.risk.round_tick(target_pool.price),
            reason=f"{setup_type}|{chosen_ifvg.source_fvg.timeframe}_IFVG|DOL={target_pool.label}|RR={rr:.2f}",
            timestamp=ts,
            metadata={
                "profile": bias_state.profile.value,
                "be_trigger_price": be_trigger,
                "es_target_price": self._valid_es_target(direction, target_pool.correlated_price, es_bar),
                "ifvg_tf": chosen_ifvg.source_fvg.timeframe,
                "key_level": key_detail,
                "narrative_mode": self.config.narrative_mode.value,
                "narrative_id": narrative.id if strict_narrative else None,
                "narrative": narrative.snapshot() if narrative else None,
                "model_mode": self.config.model_mode.value,
                "structure_mode": self.config.structure_mode.value,
                "discretion_mode": self.config.discretion_mode.value,
                "regime_mode": self.config.regime_mode.value,
                "regime": self.regime.snapshot(),
                "discretion": discretion.snapshot(),
                "structural_leg": self._structural_decision.leg.snapshot() if self._structural_decision and self._structural_decision.leg else None,
                "playbook_model": model_decision.model.value if model_decision else None,
                "model_evidence": model_decision.evidence.snapshot() if model_decision else None,
                "model_key_level_id": matched_fvg.id if strict_models and matched_fvg else None,
            },
        )
        order_id = self.broker.submit_order(order)
        if strict_models:
            self.playbook.consume(model_decision.evidence)
        self.trades_today += 1
        self.submitted_orders.append(order)

        if order_type == "MARKET":
            self.active_position = OpenPosition(
                order_id=order_id,
                symbol=order.symbol,
                direction=order.direction,
                quantity=order.quantity,
                entry_price=order.entry_price,
                initial_stop_loss=order.stop_loss,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                opened_at=ts,
                be_trigger_price=be_trigger,
                es_target_price=self._valid_es_target(direction, target_pool.correlated_price, es_bar),
                reason=order.reason,
                metadata=order.metadata,
            )

        return order

    def _has_aligned_confirmation(self, fvg: FVG, direction: Direction, now: datetime) -> bool:
        tf = self.config.htf_to_ltf_alignment.get(fvg.timeframe)
        detector = self.nq_fvgs.get(tf)
        if detector is None or fvg.first_tap_at is None:
            return False
        return any(i.direction == direction and fvg.first_tap_at <= i.confirmed_at <= now
                   for i in detector.ifvgs[-20:])

    def _all_fvgs_by_tf(self) -> Dict[str, List[FVG]]:
        return {tf: det.fvgs for tf, det in self.nq_fvgs.items()}

    def _collect_unfilled_fvgs(self, tfs: Tuple[str, ...]) -> List[FVG]:
        out: List[FVG] = []
        for tf in tfs:
            out.extend(self.nq_fvgs[tf].fvgs)
        return out

    def _select_highest_tf_ifvg(
        self,
        direction: Direction,
        newly_confirmed_ifvgs: Dict[str, List[IFVG]],
        ts: datetime,
        nq_bar: Bar,
    ) -> Tuple[Optional[IFVG], bool]:
        self._selected_leg_extreme = None
        self._selected_leg = None
        self._structural_decision = self.structure.audit(direction, ts, newly_confirmed_ifvgs)
        if self.config.structure_mode == StructureMode.STRICT:
            decision = self._structural_decision
            if decision.ifvg is None:
                self._log_rejection(ts, "STRUCTURAL_LEG_REQUIRED", decision.reason)
                return None, False
            self._selected_leg = decision.leg.leg
            self._selected_leg_extreme = decision.leg.extreme
            return decision.ifvg, False
        # 09:30 AM Judas 1m override (Building Discretion [26:45])
        if (
            self.config.enable_0930_judas_1m_override
            and time(9, 30) <= ts.time() <= time(9, 36)
            and self.judas_manipulation_seen
            and "1m" in newly_confirmed_ifvgs
        ):
            aligned_1m = [i for i in newly_confirmed_ifvgs["1m"] if i.direction == direction]
            if aligned_1m:
                return aligned_1m[-1], True

        # Isolate the active manipulation leg (from local origin swing to sweep extreme)
        recent_1m = self.nq_resampler.history["1m"][-45:]
        if not recent_1m:
            return None, False
        if direction == Direction.BULLISH:
            ext_idx = min(range(len(recent_1m)), key=lambda i: recent_1m[i].low)
            leg_prefix = recent_1m[: ext_idx + 1] or recent_1m
            origin_bar = max(leg_prefix, key=lambda b: b.high)
            leg_start_ts, leg_low, leg_high = origin_bar.timestamp, recent_1m[ext_idx].low, origin_bar.high
        else:
            ext_idx = max(range(len(recent_1m)), key=lambda i: recent_1m[i].high)
            leg_prefix = recent_1m[: ext_idx + 1] or recent_1m
            origin_bar = min(leg_prefix, key=lambda b: b.low)
            leg_start_ts, leg_low, leg_high = origin_bar.timestamp, origin_bar.low, recent_1m[ext_idx].high

        # Select the outermost opposing gap on the highest timeframe inside this leg.
        # The 45-minute leg window remains a heuristic pending annotated swing fixtures.
        for tf in self.config.ifvg_scan_ladder:
            candidates = [f for f in self.nq_fvgs[tf].fvgs
                          if f.direction == direction.opposite
                          and leg_start_ts <= f.created_at <= recent_1m[ext_idx].timestamp
                          and f.bottom >= leg_low and f.top <= leg_high]
            if not candidates:
                continue
            outer = FVGDetector.select_outermost_opposing_fvg(candidates, direction)
            if any(f.state != FVGState.INVERTED for f in candidates):
                self._log_rejection(ts, "WAITING_HIGHER_TF_IFVG", f"{tf} opposing gap remains in manipulation leg")
                return None, False
            if int(tf[:-1]) < self.config.min_ifvg_tf_minutes:
                return None, False
            aligned = [i for i in newly_confirmed_ifvgs.get(tf, [])
                       if i.direction == direction and i.source_fvg.id == outer.id]
            if aligned:
                self._selected_leg_extreme = leg_low if direction == Direction.BULLISH else leg_high
                self._selected_leg = ManipulationLeg(leg_start_ts, recent_1m[ext_idx].timestamp,
                                                     self._selected_leg_extreme)
                return aligned[-1], False
            return None, False
        return None, False

    def _is_disqualified_stacked_fvg(
        self, fvg: FVG, direction: Direction, nq_sess
    ) -> bool:
        """Stacked FVG 0.50 rule (Recognizing Market Conditions Part 1):
        When 2+ same-direction FVGs are stacked on a leg, disqualify FVGs entirely in Premium
        (for longs) or Discount (for shorts) when a lower/higher discounted FVG exists.
        """
        if not self.config.enable_stacked_fvg_eq_filter or not nq_sess.full_day.is_valid:
            return False
        siblings = [
            x for x in self.nq_fvgs[fvg.timeframe].fvgs[-5:]
            if x.direction == direction
            and x.state in (FVGState.UNFILLED_TARGET, FVGState.ACTIVE_CONDITION)
        ]
        if len(siblings) < 2:
            return False
        eq_05 = (nq_sess.full_day.low + nq_sess.full_day.high) / 2.0
        if direction == Direction.BULLISH and fvg.bottom > eq_05:
            return any(x.bottom <= eq_05 for x in siblings)
        if direction == Direction.BEARISH and fvg.top < eq_05:
            return any(x.top >= eq_05 for x in siblings)
        return False

    def _check_key_level_gate(
        self,
        direction: Direction,
        ts: datetime,
        nq_bar: Bar,
        nq_sess,
    ) -> Tuple[bool, str, str, Optional[FVG]]:
        # 1. Check 15m+ FVG Condition (prioritize 15m/30m before 1H/4H so ITF alignment is seen first)
        for tf in ("15m", "30m", "1H", "4H"):
            for fvg in reversed(self.nq_fvgs[tf].fvgs[-6:]):
                if fvg.direction != direction or fvg.state != FVGState.ACTIVE_CONDITION:
                    continue
                if fvg.first_tap_at and (ts - fvg.first_tap_at).total_seconds() <= 3600:
                    if fvg.re_tapped_after_bounce and not fvg.internal_extreme_swept:
                        return False, "", f"UNSWEPT_IML_IMH_INSIDE_{fvg.id}", fvg
                    if self._is_disqualified_stacked_fvg(fvg, direction, nq_sess):
                        return False, "", f"STACKED_FVG_OUTSIDE_05_EQ_{fvg.id}", fvg
                    return True, "MAC_OR_HTF_CONDITION", f"{tf}_FVG_TAPPED", fvg

        # 2. Check 5m FVG Condition (Continuation: requires 5m IML/IMH sweep OR ES/NQ SMT OR session sweep)
        for fvg in reversed(self.nq_fvgs["5m"].fvgs[-6:]):
            if fvg.direction != direction or fvg.state != FVGState.ACTIVE_CONDITION:
                continue
            if fvg.first_tap_at and (ts - fvg.first_tap_at).total_seconds() <= 2700:
                if fvg.re_tapped_after_bounce and not fvg.internal_extreme_swept:
                    return False, "", f"UNSWEPT_5M_IML_IMH_INSIDE_{fvg.id}", fvg
                if self._is_disqualified_stacked_fvg(fvg, direction, nq_sess):
                    return False, "", f"STACKED_FVG_OUTSIDE_05_EQ_{fvg.id}", fvg
                has_smt = self.liquidity.has_recent_smt(direction, ts, max_age_minutes=60)
                if fvg.internal_extreme_swept or has_smt or self.judas_manipulation_seen:
                    return True, "CONTINUATION_5M", "5M_FVG_WITH_CONFLUENCE", fvg

        # 3. ES Key-Level SMT tap satisfies NQ condition (Building Discretion [67:10])
        for tf in ("15m", "5m", "1H", "4H"):
            for es_fvg in reversed(self.es_fvgs[tf].fvgs[-4:]):
                if es_fvg.direction == direction and es_fvg.state == FVGState.ACTIVE_CONDITION:
                    if es_fvg.first_tap_at and (ts - es_fvg.first_tap_at).total_seconds() <= 1800:
                        if self.liquidity.has_recent_smt(direction, ts, max_age_minutes=60):
                            return True, "SMT_KEY_LEVEL", f"ES_{tf}_FVG_SMT_SATISFIED", es_fvg

        # 4. External Liquidity Sweep (PDL/PDH, London, Asia)
        if direction == Direction.BULLISH and (nq_sess.london.low_swept or nq_sess.asia.low_swept):
            return True, "LIQUIDITY_SWEEP_REVERSAL", "SESSION_LOW_SWEPT", None
        if direction == Direction.BEARISH and (nq_sess.london.high_swept or nq_sess.asia.high_swept):
            return True, "LIQUIDITY_SWEEP_REVERSAL", "SESSION_HIGH_SWEPT", None

        return False, "", "No 15m+ key level, 5m condition, SMT tap, or session sweep", None

    def _is_inside_opposing_5m_15m_fvg(self, direction: Direction, nq_bar: Bar) -> Optional[FVG]:
        opposing = direction.opposite
        for tf in ("15m", "5m"):
            for fvg in reversed(self.nq_fvgs[tf].fvgs[-6:]):
                if fvg.direction == opposing and fvg.state in (
                    FVGState.UNFILLED_TARGET,
                    FVGState.ACTIVE_CONDITION,
                ):
                    if fvg.overlapped_by(nq_bar):
                        return fvg
        return None

    def _is_past_equilibrium(
        self, direction: Direction, price: float, nq_sess
    ) -> bool:
        if not nq_sess.full_day.is_valid:
            return False
        if direction == Direction.BULLISH:
            return is_in_premium(price, nq_sess.full_day.low, nq_sess.full_day.high)
        return not is_in_premium(price, nq_sess.full_day.low, nq_sess.full_day.high)

    def _is_overextended_continuation(
        self, direction: Direction, price: float, nq_sess
    ) -> bool:
        if not nq_sess.full_day.is_valid:
            return False
        span = nq_sess.full_day.high - nq_sess.full_day.low
        if span <= 0:
            return False
        pos = (price - nq_sess.full_day.low) / span
        if direction == Direction.BULLISH:
            return pos >= self.config.continuation_extreme_threshold
        return pos <= (1.0 - self.config.continuation_extreme_threshold)

    def _has_unfilled_1h_fvg_behind(self, direction: Direction, price: float) -> bool:
        for fvg in self.nq_fvgs["1H"].fvgs[-6:]:
            if fvg.state != FVGState.UNFILLED_TARGET or fvg.direction != direction:
                continue
            if direction == Direction.BULLISH and fvg.top < price:
                return True
            if direction == Direction.BEARISH and fvg.bottom > price:
                return True
        return False

    def _resolve_entry_price(self, ifvg: IFVG, direction: Direction) -> Tuple[float, str]:
        if self.config.entry_mode == EntryOrderMode.LIMIT_IFVG_EDGE:
            edge = ifvg.source_fvg.top if direction == Direction.BULLISH else ifvg.source_fvg.bottom
            return edge, "LIMIT"
        if self.config.entry_mode == EntryOrderMode.LIMIT_CISD_OPEN:
            recent_cisds = [
                c for c in self.nq_cisds["1m"].cisds[-3:] if c.direction == direction
            ]
            if recent_cisds:
                return recent_cisds[-1].cisd_level, "LIMIT"
        return ifvg.confirm_bar.close, "MARKET"

    def _get_manipulation_stop_anchor(
        self, direction: Direction, chosen_ifvg: Optional[IFVG] = None
    ) -> Tuple[float, Optional[float]]:
        recent_1m = self.nq_resampler.history["1m"][-15:]
        if chosen_ifvg is not None:
            since_fvg = [b for b in recent_1m if b.timestamp >= chosen_ifvg.source_fvg.created_at]
            if len(since_fvg) >= 2:
                recent_1m = since_fvg
        if direction == Direction.BULLISH:
            extreme_bar = min(recent_1m, key=lambda b: b.low)
            body = max(0.25, abs(extreme_bar.close - extreme_bar.open))
            wick_eq = (
                oversized_wick_eq(extreme_bar, direction)
                if extreme_bar.lower_wick > body * 2.5
                else None
            )
            return extreme_bar.low, wick_eq

        extreme_bar = max(recent_1m, key=lambda b: b.high)
        body = max(0.25, abs(extreme_bar.close - extreme_bar.open))
        wick_eq = (
            oversized_wick_eq(extreme_bar, direction)
            if extreme_bar.upper_wick > body * 2.5
            else None
        )
        return extreme_bar.high, wick_eq

    def _resolve_be_trigger(self, direction: Direction, entry_price: float) -> Optional[float]:
        if self.config.be_mode == BreakEvenMode.OFF:
            return None
        tf = "1m" if self.config.be_mode == BreakEvenMode.STRICT_INTERNAL else "5m"
        tracker = self.liquidity.nq_swings.get(tf)
        if not tracker:
            return None
        if direction == Direction.BULLISH:
            above = [s.price for s in tracker.highs[-5:] if s.price > entry_price]
            return min(above) if above else None
        below = [s.price for s in tracker.lows[-5:] if s.price < entry_price]
        return max(below) if below else None
