"""V2.2 causal model evidence and independent PB setup evaluators.

The 45-minute leg boundary comes from V2.0. Model classification is a
research approximation, not a claim that discretionary rules are solved.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from pb_bot.config import PlaybookModel
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG
from pb_bot.core.sessions import to_ny
from pb_bot.engine.narrative import MarketNarrative, MarketNarrativeEngine

HTF = ("15m", "30m", "1H", "4H", "1D")
CONDITIONS = ("5m", "15m")
EXTERNAL = {"PDH", "PDL", "PWH", "PWL", "ASIA_H", "ASIA_L", "LONDON_H", "LONDON_L", "DATA_H", "DATA_L", "BSL", "SSL"}


@dataclass(frozen=True)
class ManipulationLeg:
    start: datetime
    extreme_at: datetime
    extreme: float


@dataclass(frozen=True)
class Accumulation:
    start: datetime
    end: datetime
    low: float
    high: float

    @property
    def midpoint(self):
        return (self.high + self.low) / 2


@dataclass(frozen=True)
class Evidence:
    direction: Direction
    observed_at: datetime
    extreme: float
    kind: str
    label: str
    fvg: Optional[FVG] = None
    pivot_price: Optional[float] = None
    accumulation: Optional[Accumulation] = None

    def snapshot(self):
        return {"kind": self.kind, "label": self.label, "observed_at": self.observed_at.isoformat(),
                "extreme": self.extreme, "key_level_id": self.fvg.id if self.fvg else None,
                "key_level_tf": self.fvg.timeframe if self.fvg else None, "pivot_price": self.pivot_price,
                "accumulation": None if self.accumulation is None else {
                    "start": self.accumulation.start.isoformat(), "end": self.accumulation.end.isoformat(),
                    "low": self.accumulation.low, "high": self.accumulation.high}}


@dataclass(frozen=True)
class ModelDecision:
    model: PlaybookModel
    evidence: Optional[Evidence]
    reason: str

    @property
    def eligible(self):
        return self.evidence is not None

    def snapshot(self):
        return {"model": self.model.value, "eligible": self.eligible, "reason": self.reason,
                "evidence": self.evidence.snapshot() if self.evidence else None}


def _extreme(direction, bar):
    return bar.low if direction == Direction.BULLISH else bar.high


def _through(direction, price, bar):
    return bar.open >= price > bar.low if direction == Direction.BULLISH else bar.open <= price < bar.high


def _ahead(direction, price, bar):
    return price > bar.high if direction == Direction.BULLISH else price < bar.low


def accumulation_before(bars, now, count, max_efficiency):
    """Only prior contiguous closed minutes; compression + oscillation proxy."""
    if len(bars) < count:
        return None
    window = list(bars)[-count:]
    if window[-1].timestamp + timedelta(minutes=1) != now or any(
        b.timestamp - a.timestamp != timedelta(minutes=1) for a, b in zip(window, window[1:])
    ):
        return None
    high, low = max(b.high for b in window), min(b.low for b in window)
    traveled = sum(b.high - b.low for b in window)
    if traveled <= 0 or high <= low or (high - low) / traveled > max_efficiency:
        return None
    mid = (high + low) / 2
    sides = [1 if b.close > mid else -1 for b in window if b.close != mid]
    if sum(a != b for a, b in zip(sides, sides[1:])) < 2:
        return None
    return Accumulation(window[0].timestamp, window[-1].timestamp, low, high)


class PlaybookEngine:
    """Collects evidence before looking at an IFVG; evaluators do not place orders."""
    def __init__(self, config):
        self.config = config
        self.events = []
        self._day = None
        self._bars = deque(maxlen=config.po3_accumulation_minutes)
        self._known = {}
        self._first_touch = {}
        self._pivots = {}
        self._levels = {}
        self._swept = set()
        self._used_pivots = set()
        self._evidence = []
        self._active_fvgs = {}
        self._consumed_setups = set()

    def update(self, bar, session, fvgs, swings):
        now = to_ny(bar.timestamp)
        if session.trading_day != self._day:
            self._day = session.trading_day
            self._bars.clear()
            self._levels.clear()
            self._swept.clear()
            self._evidence.clear()
            self._consumed_setups.clear()
        if self._bars and now - self._bars[-1].timestamp != timedelta(minutes=1):
            # Missing input makes the chronology of an existing setup unknowable.
            self._evidence.clear()
            self._bars.clear()
            self._pivots.clear()
            self._levels.clear()
        cutoff = now - timedelta(minutes=self.config.model_evidence_minutes)
        self._evidence = [e for e in self._evidence if e.observed_at >= cutoff]
        prior_range = accumulation_before(self._bars, now, self.config.po3_accumulation_minutes,
                                          self.config.po3_max_range_efficiency)
        self._active_fvgs = {f.id: f for tf in (*HTF, "5m") for f in fvgs.get(tf, ())
                             if f.state != FVGState.INVERTED}
        fresh = []
        for f in self._active_fvgs.values():
            if f.id not in self._known or not f.overlapped_by(bar):
                continue  # The gap must already have been observable before this minute.
            direction = f.direction
            if self._known[f.id] == FVGState.UNFILLED_TARGET:
                self._first_touch[f.id] = now
                if f.timeframe in HTF:
                    fresh.append(Evidence(direction, now, _extreme(direction, bar), "HTF_FIRST_TAP", f.id, f))
            first = self._first_touch.get(f.id)
            if first is None:
                continue
            # Confirmed 5m+ fractal inside the FVG, known BEFORE this bar, after first tap.
            pivots = [p for p in self._pivots.get(direction, ()) if p.timestamp >= first
                      and f.contains_price(p.price)]
            if not pivots:
                continue
            pivot = max(pivots, key=lambda p: p.timestamp)
            key = (f.id, pivot.timeframe, pivot.timestamp, pivot.price)
            if key not in self._used_pivots:
                if _through(direction, pivot.price, bar):
                    fresh.append(Evidence(direction, now, _extreme(direction, bar), "INTERNAL_SWEEP", f.id, f, pivot.price))
                if (bar.low < pivot.price if direction == Direction.BULLISH else bar.high > pivot.price):
                    self._used_pivots.add(key)
        # Prior completed external levels; a flag from earlier bars is not a fresh sweep.
        levels = {kind: (price, side, swept) for kind, price, side, _, swept in
                  MarketNarrativeEngine._levels(session, now) if price is not None}
        for kind, (price, side, swept) in levels.items():
            key = (kind, price)
            previous = self._levels.get(kind)
            if previous and previous[0] == price and not previous[2] and key not in self._swept and _through(side.opposite, price, bar):
                fresh.append(Evidence(side.opposite, now, _extreme(side.opposite, bar), "EXTERNAL_SWEEP", kind))
            if swept or (bar.high > price if side == Direction.BULLISH else bar.low < price):
                self._swept.add(key)
        self._levels = levels
        # A dual-sided event is ambiguous; don't turn it into a directional setup.
        if len({e.direction for e in fresh}) > 1:
            fresh = []
        self._evidence.extend(fresh)
        if prior_range and prior_range.low <= bar.open <= prior_range.high:
            for e in fresh:
                if e.fvg is None or e.fvg.timeframe not in HTF:
                    continue
                raid = (bar.low < prior_range.low and bar.high < prior_range.high if e.direction == Direction.BULLISH
                        else bar.high > prior_range.high and bar.low > prior_range.low)
                if raid:
                    self._evidence.append(Evidence(e.direction, now, e.extreme, "PO3_RAID", e.label,
                                                   e.fvg, e.pivot_price, prior_range))
        self._known = {f.id: f.state for f in self._active_fvgs.values()}
        self._first_touch = {k: v for k, v in self._first_touch.items() if k in self._active_fvgs}
        self._used_pivots = {k for k in self._used_pivots if k[0] in self._active_fvgs}
        self._pivots = {
            Direction.BULLISH: tuple(p for tf in ("5m", "15m", "1H") if tf in swings for p in swings[tf].lows),
            Direction.BEARISH: tuple(p for tf in ("5m", "15m", "1H") if tf in swings for p in swings[tf].highs),
        }
        self._bars.append(bar)

    def _common(self, model, direction, now, bar, leg, narrative):
        if leg is None or not leg.start <= leg.extreme_at < now:
            return ModelDecision(model, None, "NO_CAUSAL_LEG")
        if narrative is None or not narrative.can_enter(direction, now):
            return ModelDecision(model, None, "NARRATIVE_NOT_ACTIVE")
        if not _ahead(direction, narrative.primary_dol.price, bar):
            return ModelDecision(model, None, "TARGET_NOT_AHEAD")
        return None

    def _candidates(self, direction, now, leg):
        return [e for e in self._evidence if e.direction == direction
                and leg.start <= e.observed_at <= leg.extreme_at and e.observed_at < now
                and now - e.observed_at <= timedelta(minutes=self.config.model_evidence_minutes)
                and (e.fvg is None or e.fvg.id in self._active_fvgs)
                # A later, deeper unsourced low/high is not the recorded manipulation.
                and e.extreme == leg.extreme
                and (e.direction, e.observed_at, e.extreme) not in self._consumed_setups]

    def evaluate_mac(self, direction, now, bar, leg, narrative):
        model = PlaybookModel.MAC
        blocked = self._common(model, direction, now, bar, leg, narrative)
        if blocked:
            return blocked
        choices = [e for e in self._candidates(direction, now, leg) if e.kind == "EXTERNAL_SWEEP"
                   or (e.fvg is not None and e.fvg.timeframe in HTF
                       and e.fvg.id != (narrative.condition.id if narrative.condition else None)
                       and e.kind in ("HTF_FIRST_TAP", "INTERNAL_SWEEP"))]
        return ModelDecision(model, choices[-1] if choices else None,
                             "FRESH_REVERSAL_SOURCE" if choices else "NO_FRESH_15M_PLUS_OR_EXTERNAL_SWEEP")

    def evaluate_continuation(self, direction, now, bar, leg, narrative):
        model = PlaybookModel.CONTINUATION
        blocked = self._common(model, direction, now, bar, leg, narrative)
        if blocked:
            return blocked
        if narrative.primary_dol.kind not in EXTERNAL:
            return ModelDecision(model, None, "DOL_NOT_EXTERNAL")
        if narrative.condition is None or narrative.condition.timeframe not in CONDITIONS:
            return ModelDecision(model, None, "NO_5M_15M_CONDITION")
        choices = [e for e in self._candidates(direction, now, leg) if e.kind == "INTERNAL_SWEEP"
                   and e.fvg is not None and e.fvg.id == narrative.condition.id
                   and narrative.locked_at < e.observed_at]
        return ModelDecision(model, choices[-1] if choices else None,
                             "CONDITION_PIVOT_SWEPT" if choices else "NO_FRESH_CONFIRMED_INTERNAL_SWEEP")

    def evaluate_po3(self, direction, now, bar, leg, narrative):
        model = PlaybookModel.PO3
        blocked = self._common(model, direction, now, bar, leg, narrative)
        if blocked:
            return blocked
        choices = [e for e in self._candidates(direction, now, leg) if e.kind == "PO3_RAID"
                   and (bar.close > e.accumulation.midpoint if direction == Direction.BULLISH
                        else bar.close < e.accumulation.midpoint)
                   and _ahead(direction, narrative.primary_dol.price, bar)
                   and (narrative.primary_dol.price > e.accumulation.high if direction == Direction.BULLISH
                        else narrative.primary_dol.price < e.accumulation.low)]
        return ModelDecision(model, choices[-1] if choices else None,
                             "ACCUMULATION_RAID_DISTRIBUTION" if choices else "NO_CONFIRMED_PO3_SEQUENCE")

    def evaluate(self, direction, now, bar, leg, narrative, ifvg: IFVG, profile):
        decisions = [self.evaluate_po3(direction, now, bar, leg, narrative),
                     self.evaluate_continuation(direction, now, bar, leg, narrative),
                     self.evaluate_mac(direction, now, bar, leg, narrative)]
        selected = next((d for d in decisions if d.eligible and d.model in self.config.enabled_models), None)
        self.events.append({"bar_timestamp": now.isoformat(), "direction": direction.value,
                            "ifvg_id": ifvg.source_fvg.id, "ifvg_tf": ifvg.source_fvg.timeframe,
                            "profile": profile.value, "narrative_id": narrative.id if narrative else None,
                            "selected": selected.model.value if selected else None,
                            "decisions": [d.snapshot() for d in decisions]})
        return selected

    def consume(self, evidence):
        self._consumed_setups.add((evidence.direction, evidence.observed_at, evidence.extreme))

    def accepts_pending(self, key_level_id):
        return key_level_id is None or key_level_id in self._active_fvgs
