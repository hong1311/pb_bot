"""Causal, session-scoped PB narrative. No order or risk decisions in this module.

Timestamps identify the closed input 1m bar (by its opening timestamp), not
an earlier time at which the information was available.
"""
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Dict, List, Optional, Sequence

from pb_bot.core.liquidity import DOL_PRIORITY, LiquidityPool
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG
from pb_bot.core.sessions import DailySessionState, to_ny


class NarrativePhase(str, Enum):
    WAITING_CONDITION = "WAITING_CONDITION"
    WAITING_RESPECT = "WAITING_RESPECT"
    ACTIVE = "ACTIVE"
    TARGET_REACHED = "TARGET_REACHED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True)
class DeliverySource:
    label: str
    direction: Direction
    price: float
    observed_at: datetime
    fvg: Optional[FVG] = field(default=None, compare=False)


@dataclass
class MarketNarrative:
    id: int
    direction: Direction
    source: DeliverySource
    phase: NarrativePhase = NarrativePhase.WAITING_CONDITION
    condition: Optional[FVG] = None
    opposing_inversion: bool = False
    primary_dol: Optional[LiquidityPool] = None
    alternative_dol: Optional[LiquidityPool] = None
    locked_at: Optional[datetime] = None
    condition_selected_at: Optional[datetime] = None
    condition_respected: bool = False

    @property
    def terminal(self) -> bool:
        return self.phase in (NarrativePhase.TARGET_REACHED, NarrativePhase.INVALIDATED)

    @property
    def invalidation_price(self) -> Optional[float]:
        if self.condition is None:
            return None
        return self.condition.bottom if self.direction == Direction.BULLISH else self.condition.top

    def can_enter(self, direction: Direction, now: datetime) -> bool:
        return (self.phase == NarrativePhase.ACTIVE and self.direction == direction
                and self.primary_dol is not None and self.locked_at is not None
                and self.locked_at < now)

    def snapshot(self) -> dict:
        def target(p):
            return None if p is None else {"label": p.label, "price": p.price, "timeframe": p.timeframe,
                                           "es_price": p.correlated_price}
        return {"narrative_id": self.id, "direction": self.direction.value, "phase": self.phase.value,
                "source": self.source.label, "source_price": self.source.price,
                "source_at": self.source.observed_at.isoformat(),
                "condition_id": self.condition.id if self.condition else None,
                "condition_tf": self.condition.timeframe if self.condition else None,
                "invalidation_price": self.invalidation_price,
                "condition_respected": self.condition_respected,
                "opposing_inversion": self.opposing_inversion,
                "primary_dol": target(self.primary_dol), "alternative_dol": target(self.alternative_dol),
                "locked_at": self.locked_at.isoformat() if self.locked_at else None}


class MarketNarrativeEngine:
    """Pins a target before the trigger; terminal narratives require a fresh source.

    Initial research scope: first taps of 15m+ FVGs, completed session/previous-day
    sweeps, subsequent 5m/15m conditions, and unfilled FVG or finalized session
    targets. No inferred SMT-source equivalence or unverified swing pools.
    """
    SOURCE_TFS = ("15m", "30m", "1H", "4H", "1D")
    TARGET_TFS = ("5m", "15m", "1H", "4H", "1D")

    def __init__(self):
        self.current: Optional[MarketNarrative] = None
        self.events: List[dict] = []
        self._day = None
        self._sequence = 0
        self._seen_fvgs: Dict[str, FVGState] = {}
        self._consumed = set()
        self._swept = set()

    @staticmethod
    def _hit(direction, price, bar):
        return bar.high >= price if direction == Direction.BULLISH else bar.low <= price

    @staticmethod
    def _levels(session: DailySessionState, now: datetime):
        """Only completed session ranges. Prior day high/low known at rollover."""
        t = to_ny(now).time()
        yield "PDH", session.pdh, Direction.BULLISH, "1D", False
        yield "PDL", session.pdl, Direction.BEARISH, "1D", False
        if t < time(18):
            yield "ASIA_H", session.asia.high, Direction.BULLISH, "SESSION", session.asia.high_swept
            yield "ASIA_L", session.asia.low, Direction.BEARISH, "SESSION", session.asia.low_swept
            if t >= time(5):
                yield "LONDON_H", session.london.high, Direction.BULLISH, "SESSION", session.london.high_swept
                yield "LONDON_L", session.london.low, Direction.BEARISH, "SESSION", session.london.low_swept
            if t >= time(8, 45):
                yield "DATA_H", session.data_830_high, Direction.BULLISH, "SESSION", False
                yield "DATA_L", session.data_830_low, Direction.BEARISH, "SESSION", False

    def _emit(self, now, reason):
        self.events.append({"bar_timestamp": now.isoformat(), "event": reason,
                            **(self.current.snapshot() if self.current else {})})

    def _finish(self, now, phase, reason):
        self.current.phase = phase
        self._emit(now, reason)

    def _scan_levels(self, bar, session, market):
        sources = []
        for kind, price, side, tf, already_swept in self._levels(session, bar.timestamp):
            if price is None:
                continue
            key = (market, kind, price)
            hit = self._hit(side, price, bar)
            # A strict through-level sweep, not merely a touch or gap past it.
            swept = (bar.open <= price < bar.high if side == Direction.BULLISH
                     else bar.open >= price > bar.low)
            # Already-swept flags may have been set by the current bar; only use
            # a sweep observed on this input bar, never a historical flag alone.
            if hit or already_swept:
                self._consumed.add(key)
            if swept and key not in self._swept and market == "NQ":
                sources.append(DeliverySource(kind, side.opposite, price, bar.timestamp))
            if already_swept or (bar.high > price if side == Direction.BULLISH else bar.low < price):
                self._swept.add(key)
        return sources

    def _targets(self, direction, bar, es_bar, session, es_session, fvgs):
        pools = []
        es_levels = {kind: (price, swept) for kind, price, _, _, swept in self._levels(es_session, bar.timestamp)
                     if price is not None}
        for kind, price, side, tf, swept in self._levels(session, bar.timestamp):
            if (side != direction or price is None or swept or ("NQ", kind, price) in self._consumed
                    or self._hit(side, price, bar)):
                continue
            es_price, es_swept = es_levels.get(kind, (None, False))
            if es_price is not None and (es_swept or ("ES", kind, es_price) in self._consumed
                                         or self._hit(side, es_price, es_bar)):
                # Counterpart objective already delivered: do not target stale paired liquidity.
                continue
            pools.append(LiquidityPool(kind, price, side, tf, DOL_PRIORITY[kind], kind,
                                       correlated_price=es_price))
        for tf in self.TARGET_TFS:
            for f in fvgs.get(tf, ()):
                if f.state != FVGState.UNFILLED_TARGET or f.direction != direction.opposite:
                    continue
                price = f.bottom if direction == Direction.BULLISH else f.top
                if not self._hit(direction, price, bar):
                    pools.append(LiquidityPool("UNFILLED_FVG", price, direction, tf,
                                               DOL_PRIORITY["UNFILLED_FVG"], f.id))
        # Conservative local DOL: nearest untouched eligible level. No RR input.
        return sorted(pools, key=lambda p: (abs(p.price - bar.close), p.priority, p.label))

    def update(self, bar: Bar, es_bar: Bar, bias: Direction, session: DailySessionState,
               es_session: DailySessionState, fvgs: Dict[str, List[FVG]],
               created: Sequence[FVG], inverted: Dict[str, List[IFVG]],
               closed: Dict[str, List[Bar]], blocked: bool = False):
        now = to_ny(bar.timestamp)
        if self._day != session.trading_day:
            if self.current and not self.current.terminal:
                self._finish(now, NarrativePhase.INVALIDATED, "DAY_ROLLOVER")
            self._day = session.trading_day
            self.current = None
            self._consumed.clear()
            self._swept.clear()
        sources = self._scan_levels(bar, session, "NQ")
        self._scan_levels(es_bar, es_session, "ES")
        # Inspect detector state transitions rather than HTF bar-open timestamps.
        seen = {}
        for tf in self.SOURCE_TFS:
            for f in fvgs.get(tf, ()):
                seen[f.id] = f.state
                if (f.state == FVGState.ACTIVE_CONDITION
                        and self._seen_fvgs.get(f.id) == FVGState.UNFILLED_TARGET):
                    sources.append(DeliverySource(f.id, f.direction,
                        f.top if f.direction == Direction.BULLISH else f.bottom, now, f))
        self._seen_fvgs = seen

        n = self.current
        eligible = [s for s in sources if s.direction == bias]
        if n is not None and not n.terminal:
            if blocked or bias != n.direction:
                self._finish(now, NarrativePhase.INVALIDATED, "REGIME_OR_BIAS_CHANGED")
                return
            if n.source.fvg is not None and n.source.fvg.state == FVGState.INVERTED:
                self._finish(now, NarrativePhase.INVALIDATED, "SOURCE_INVERTED")
                return
            if n.condition is not None and n.condition.state == FVGState.INVERTED:
                self._finish(now, NarrativePhase.INVALIDATED, "CONDITION_INVERTED")
                return
            if any(not any(f.id == anchor.id for f in fvgs.get(anchor.timeframe, ()))
                   for anchor in (n.source.fvg, n.condition) if anchor is not None):
                self._finish(now, NarrativePhase.INVALIDATED, "ANCHOR_NO_LONGER_TRACKED")
                return
            p = n.primary_dol
            if p is not None and self._hit(n.direction, p.price, bar):
                self._finish(now, NarrativePhase.TARGET_REACHED, "NQ_TARGET_REACHED")
                return
            if p is not None and p.correlated_price is not None and self._hit(n.direction, p.correlated_price, es_bar):
                self._finish(now, NarrativePhase.TARGET_REACHED, "ES_TARGET_REACHED")
                return
            if p is not None and p.kind == "UNFILLED_FVG" and not any(
                f.id == p.label and f.state == FVGState.UNFILLED_TARGET for f in fvgs.get(p.timeframe, ())
            ):
                self._finish(now, NarrativePhase.INVALIDATED, "TARGET_NO_LONGER_TRACKED")
                return
            # A newer delivery event supersedes an unconfirmed source; an already
            # selected condition or locked objective is never silently replaced.
            if n.condition is None and eligible:
                self._finish(now, NarrativePhase.INVALIDATED, "SOURCE_SUPERSEDED")

        if blocked or bias == Direction.NEUTRAL:
            return
        if n is None or n.terminal:
            if not eligible:
                return
            self._sequence += 1
            n = self.current = MarketNarrative(self._sequence, bias, eligible[-1])
            self._emit(now, "SOURCE_OBSERVED")
            return  # Same-bar sequence is unknowable with 1m OHLC.

        # Opposing PD-array disrespect must be observed after delivery source.
        if any(i.direction == n.direction for tf in ("5m", "15m") for i in inverted.get(tf, ())):
            if not n.opposing_inversion:
                n.opposing_inversion = True
                self._emit(now, "OPPOSING_ARRAY_INVERTED")
        if n.condition is None:
            choices = [f for f in created if f.timeframe in ("5m", "15m") and f.direction == n.direction]
            if choices:
                # Prefer 15m if both close together; pin until invalidation.
                n.condition = max(choices, key=lambda f: int(f.timeframe[:-1]))
                n.condition_selected_at = now
                n.phase = NarrativePhase.WAITING_RESPECT
                self._emit(now, "CONDITION_SELECTED")
        if n.condition is None:
            return
        # Respect requires a later exact-TF closed candle overlapping the gap.
        f = n.condition
        if not n.condition_respected and now > n.condition_selected_at and f.state == FVGState.ACTIVE_CONDITION:
            if any(b.timestamp > f.created_at and f.overlapped_by(b)
                   and (b.close >= f.bottom if n.direction == Direction.BULLISH else b.close <= f.top)
                   for b in closed.get(f.timeframe, ())):
                n.condition_respected = True
                self._emit(now, "CONDITION_RESPECTED")
        if not n.opposing_inversion:
            return
        if n.primary_dol is None:
            targets = self._targets(n.direction, bar, es_bar, session, es_session, fvgs)
            if targets:
                n.primary_dol = targets[0]
                alternatives = self._targets(n.direction.opposite, bar, es_bar, session, es_session, fvgs)
                n.alternative_dol = alternatives[0] if alternatives else None
                n.locked_at = now
                self._emit(now, "DOL_LOCKED")
        if n.condition_respected and n.primary_dol is not None and n.phase != NarrativePhase.ACTIVE:
            n.phase = NarrativePhase.ACTIVE
            self._emit(now, "NARRATIVE_ACTIVE")

    def accepts_pending(self, narrative_id, direction, now):
        n = self.current
        return n is not None and n.id == narrative_id and n.can_enter(direction, now)
