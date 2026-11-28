"""Causal, evidence-anchored manipulation legs (V2.3 research preview).

All inputs are closed bars, using bar-open timestamps. Decisions use the
minute at which data actually becomes available, not a higher-TF open time.
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from pb_bot.core.models import Bar, Direction, FVG, IFVG
from pb_bot.core.primitives import FVGDetector
from pb_bot.core.sessions import to_ny
from pb_bot.engine.playbook import ManipulationLeg

SIDES = (Direction.BULLISH, Direction.BEARISH)
MINUTE = timedelta(minutes=1)


def _minute(ts):
    return to_ny(ts).astimezone(timezone.utc)


@dataclass
class Origin:
    bar: Bar
    confirmed_at: datetime
    low: float
    high: float


@dataclass(frozen=True)
class Formation:
    gap: FVG
    start: datetime
    completed_at: datetime  # last minute's open, not third candle's open


@dataclass(frozen=True)
class StructuralLeg:
    direction: Direction
    origin_at: datetime
    origin_confirmed_at: datetime
    origin_price: float
    extreme_at: datetime
    extreme: float
    evidence_labels: tuple
    formations: tuple

    @property
    def leg(self):
        return ManipulationLeg(self.origin_at, self.extreme_at, self.extreme)

    def snapshot(self):
        return {"direction": self.direction.value, "origin_at": self.origin_at.isoformat(),
                "origin_confirmed_at": self.origin_confirmed_at.isoformat(),
                "origin_price": self.origin_price, "extreme_at": self.extreme_at.isoformat(),
                "extreme": self.extreme, "evidence": list(self.evidence_labels),
                "gap_ids": [f.gap.id for f in self.formations]}


@dataclass(frozen=True)
class StructuralDecision:
    leg: Optional[StructuralLeg]
    ifvg: Optional[IFVG]
    reason: str
    timeframe: Optional[str] = None


class StructureEngine:
    """Track fresh evidence at an actual leg extreme, without a rolling origin window.

    A strict 3-bar pivot is observable only after its right bar closes. A
    newly confirmed pivot cannot sponsor a sweep on that same minute. A
    deeper unsourced extreme invalidates an existing leg. Full 3-candle
    formations must lie inside the leg and belong to a contiguous data run.
    """
    def __init__(self, config):
        self.config = config
        self.events = []
        self._day = None
        self._last = None
        self._run_start = None
        self._pivots = deque(maxlen=3)
        self._origins = {}
        self._legs = {}
        self._closed = {tf: deque(maxlen=3) for tf in config.ifvg_scan_ladder}
        self._formations = []
        self._inversions = {}
        self._retire_next = set()

    def _reset(self, now, day):
        self._day = day
        self._run_start = now
        self._last = None
        self._pivots.clear()
        self._origins.clear()
        self._legs.clear()
        self._formations.clear()
        self._inversions.clear()
        self._retire_next.clear()
        for bars in self._closed.values():
            bars.clear()

    def _is_complete(self, b, tf, now):
        span = timedelta(minutes=int(tf[:-1]))
        return (_minute(b.timestamp) >= _minute(self._run_start)
                and _minute(b.timestamp) + span == _minute(now) + MINUTE)

    def update(self, bar, day, closed_bars, created_fvgs, new_ifvgs, evidence):
        now = to_ny(bar.timestamp)
        if day != self._day or (self._last is not None and
                                _minute(now) - _minute(self._last) != MINUTE):
            self._reset(now, day)
        self._last = now
        for d in self._retire_next:
            self._legs.pop(d, None)
        self._retire_next.clear()

        # A flush of a stale or partial bucket is not an exact-TF close.
        for tf, history in self._closed.items():
            span = timedelta(minutes=int(tf[:-1]))
            for b in closed_bars.get(tf, ()):
                if not self._is_complete(b, tf, now):
                    history.clear()
                    continue
                if history and _minute(b.timestamp) - _minute(history[-1].timestamp) != span:
                    history.clear()
                history.append(b)
                if len(history) == 3:
                    c1, c2, c3 = history
                    for f in created_fvgs:
                        if f.timeframe != tf or f.created_at != b.timestamp:
                            continue
                        # Verify geometry against these actual completed candles.
                        valid = (f.bottom == c1.high and f.top == c3.low if f.direction == Direction.BULLISH
                                 else f.bottom == c3.high and f.top == c1.low)
                        if valid and f.bottom < f.top:
                            self._formations.append(Formation(f, c1.timestamp, now))
                for inv in new_ifvgs.get(tf, ()):
                    if inv.confirm_bar == b and inv.confirmed_at == b.timestamp:
                        f = inv.source_fvg
                        beyond = b.close > f.top if inv.direction == Direction.BULLISH else b.close < f.bottom
                        if beyond and inv.direction == f.direction.opposite:
                            self._inversions[f.id] = now

        for d, leg in list(self._legs.items()):
            broken = bar.low < leg.extreme if d == Direction.BULLISH else bar.high > leg.extreme
            if broken:
                del self._legs[d]

        # Use only origins confirmed BEFORE the evidence minute, and require
        # the evidence bar to set the actual directional extreme of that leg.
        for d in SIDES:
            origin = self._origins.get(d)
            if origin is None:
                continue
            ext = bar.low if d == Direction.BULLISH else bar.high
            extends = ext < origin.low if d == Direction.BULLISH else ext > origin.high
            intact = bar.high <= origin.bar.high if d == Direction.BULLISH else bar.low >= origin.bar.low
            fresh = [e for e in evidence if e.direction == d and e.observed_at == now and e.extreme == ext]
            if extends and intact and fresh and origin.confirmed_at < now:
                low, high = (ext, origin.bar.high) if d == Direction.BULLISH else (origin.bar.low, ext)
                formations = tuple(f for f in self._formations if f.gap.direction == d.opposite
                                   and origin.bar.timestamp <= f.start and f.completed_at <= now
                                   and low <= f.gap.bottom < f.gap.top <= high
                                   and (f.gap.id not in self._inversions or self._inversions[f.gap.id] >= now))
                self._legs[d] = StructuralLeg(d, origin.bar.timestamp, origin.confirmed_at,
                                              origin.bar.high if d == Direction.BULLISH else origin.bar.low,
                                              now, ext, tuple(e.label for e in fresh), formations)
            origin.low = min(origin.low, bar.low)
            origin.high = max(origin.high, bar.high)
            if not intact:
                self._origins.pop(d, None)

        # A leg can trigger on the origin-reclaim bar, but not on later bars.
        for d, leg in self._legs.items():
            if now > leg.extreme_at and (bar.close > leg.origin_price if d == Direction.BULLISH
                                        else bar.close < leg.origin_price):
                self._retire_next.add(d)

        self._pivots.append(bar)
        if len(self._pivots) == 3:
            a, b, c = self._pivots
            # Strict inequality on both sides: ambiguous equal pivots are not anchors.
            if b.high > max(a.high, c.high):
                self._origins[Direction.BULLISH] = Origin(b, now, min(b.low, c.low), b.high)
            if b.low < min(a.low, c.low):
                self._origins[Direction.BEARISH] = Origin(b, now, b.low, max(b.high, c.high))

    def select(self, direction, now, new_ifvgs):
        leg = self._legs.get(direction)
        if leg is None:
            return StructuralDecision(None, None, "NO_EVIDENCE_ANCHORED_LEG")
        if now != self._last or now <= leg.extreme_at:
            return StructuralDecision(leg, None, "WAITING_POST_SWEEP_CLOSE")
        for tf in self.config.ifvg_scan_ladder:
            gaps = [f.gap for f in leg.formations if f.gap.timeframe == tf]
            if not gaps:
                continue
            if any(self._inversions.get(f.id, leg.extreme_at) <= leg.extreme_at for f in gaps):
                return StructuralDecision(leg, None, "WAITING_HIGHEST_TF", tf)
            if int(tf[:-1]) < self.config.min_ifvg_tf_minutes:
                return StructuralDecision(leg, None, "BELOW_MINIMUM_TF", tf)
            outer = FVGDetector.select_outermost_opposing_fvg(gaps, direction)
            # A previously inverted outer gap is not a fresh entry signal.
            matches = [i for i in new_ifvgs.get(tf, ()) if i.source_fvg.id == outer.id
                       and i.direction == direction and self._inversions.get(outer.id) == now]
            if not matches:
                return StructuralDecision(leg, None, "NO_FRESH_OUTER_INVERSION", tf)
            return StructuralDecision(leg, matches[-1], "STRUCTURAL_IFVG", tf)
        return StructuralDecision(leg, None, "NO_COMPLETE_OPPOSING_FORMATION")

    def audit(self, direction, now, new_ifvgs):
        decision = self.select(direction, now, new_ifvgs)
        self.events.append({"bar_timestamp": now.isoformat(), "direction": direction.value,
                            "mode": self.config.structure_mode.value, "reason": decision.reason,
                            "timeframe": decision.timeframe,
                            "ifvg_id": decision.ifvg.source_fvg.id if decision.ifvg else None,
                            "leg": decision.leg.snapshot() if decision.leg else None})
        return decision
