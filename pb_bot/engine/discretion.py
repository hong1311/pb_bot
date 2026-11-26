"""V2.4 causal dealing ranges and conservative discretionary entry filters.

The dealing range is the *prior aligned impulse*, never the opposing entry
leg or the session's eventual high/low. All evidence comes from closed bars.
Numerical quality thresholds are explicit research assumptions.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pb_bot.core.models import Direction, FVGState
from pb_bot.core.sessions import to_ny
from pb_bot.config import PlaybookModel


def _utc(t):
    return to_ny(t).astimezone(timezone.utc)


MINUTE = timedelta(minutes=1)


def _pivot(bars, i, high):
    a, b, c = bars[i-1:i+2]
    return b.high > max(a.high, c.high) if high else b.low < min(a.low, c.low)


@dataclass(frozen=True)
class DealingRange:
    direction: Direction
    timeframe: str
    start: datetime
    end: datetime
    confirmed_at: datetime
    low: float
    high: float
    gap_ids: tuple

    @property
    def eq(self):
        return (self.low + self.high) / 2

    def position(self, price):
        """0 at aligned impulse origin, 1 at its directional extreme."""
        p = (price - self.low) / (self.high - self.low)
        return p if self.direction == Direction.BULLISH else 1 - p

    def snapshot(self):
        return {"direction": self.direction.value, "timeframe": self.timeframe,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "confirmed_at": self.confirmed_at.isoformat(), "low": self.low,
                "high": self.high, "eq": self.eq, "gap_ids": list(self.gap_ids)}


@dataclass(frozen=True)
class DiscretionDecision:
    allowed: bool
    reason: str
    dealing_range: Optional[DealingRange] = None
    entry_position: Optional[float] = None
    retracement_position: Optional[float] = None
    reduced_risk: bool = False
    wick_midpoint: Optional[float] = None
    displacement_ratio: Optional[float] = None

    def snapshot(self):
        return {"allowed": self.allowed, "reason": self.reason,
                "range": self.dealing_range.snapshot() if self.dealing_range else None,
                "entry_position": self.entry_position, "retracement_position": self.retracement_position,
                "reduced_risk": self.reduced_risk, "wick_midpoint": self.wick_midpoint,
                "displacement_ratio": self.displacement_ratio}


class DiscretionEngine:
    def __init__(self, config):
        self.config = config
        self.events = []
        self._day = None
        self._last = None
        self._run_start = None
        self.minutes = []
        self.closed = {"5m": [], "15m": []}

    def update(self, bar, day, closed_bars):
        now = _utc(bar.timestamp)
        if day != self._day or (self._last is not None and now - self._last != MINUTE):
            self._day = day
            self._run_start = now
            self.minutes.clear()
            for bars in self.closed.values():
                bars.clear()
        self._last = now
        self.minutes.append(bar)
        for tf, history in self.closed.items():
            span = timedelta(minutes=int(tf[:-1]))
            for b in closed_bars.get(tf, ()):
                # Reject partial/stale resampler flushes and initial partial buckets.
                start = _utc(b.timestamp)
                if start < self._run_start or start + span != now + MINUTE:
                    continue
                if history and start - _utc(history[-1].timestamp) != span:
                    history.clear()
                history.append(b)

    @staticmethod
    def _formation(bars, index, gap):
        if index < 2:
            return False
        a, _, c = bars[index-2:index+1]
        return (gap.bottom < gap.top and
                (gap.bottom == a.high and gap.top == c.low if gap.direction == Direction.BULLISH
                 else gap.bottom == c.high and gap.top == a.low))

    def dealing_range(self, gap, direction, leg, fvgs):
        if gap is None or leg is None or gap.direction != direction or gap.timeframe not in self.closed:
            return None
        span = timedelta(minutes=int(gap.timeframe[:-1]))
        cutoff = _utc(leg.extreme_at)
        # Require even the right-hand pivot candle to have closed before the sweep minute.
        bars = [b for b in self.closed[gap.timeframe] if _utc(b.timestamp) + span <= cutoff]
        indexes = {_utc(b.timestamp): i for i, b in enumerate(bars)}
        gi = indexes.get(_utc(gap.created_at), -1)
        if gi < 2 or not self._formation(bars, gi, gap):
            return None
        bull = direction == Direction.BULLISH
        starts = [i for i in range(1, gi-1) if _pivot(bars, i, not bull)]
        ends = [i for i in range(max(gi, 1), len(bars)-1) if _pivot(bars, i, bull)]
        # Prefer the latest valid aligned swing ending before the sweep, with a
        # confirmed origin prior to (or at) the gap's first candle.
        for end in reversed(ends):
            for start in reversed(starts):
                impulse = bars[start:end+1]
                low, high = min(b.low for b in impulse), max(b.high for b in impulse)
                if high <= low or not (low <= gap.bottom < gap.top <= high):
                    continue
                if bull and (bars[start].low != low or bars[end].high != high):
                    continue
                if not bull and (bars[start].high != high or bars[end].low != low):
                    continue
                # A later breakout beyond the impulse bounds makes it stale;
                # do not use the eventual day's extreme as a replacement.
                after = [b for b in self.minutes if _utc(b.timestamp) >= _utc(bars[end].timestamp)+span
                         and _utc(b.timestamp) <= cutoff]
                if any(b.low < low or b.high > high for b in after):
                    continue
                if not low <= leg.extreme <= high:
                    continue
                siblings = []
                for f in fvgs:
                    fi = indexes.get(_utc(f.created_at), -1)
                    if (f.symbol == gap.symbol and f.timeframe == gap.timeframe and f.direction == direction
                        and f.state != FVGState.INVERTED and start+2 <= fi <= end
                        and self._formation(bars, fi, f) and low <= f.bottom < f.top <= high):
                        siblings.append(f.id)
                if gap.id not in siblings:
                    continue
                return DealingRange(direction, gap.timeframe, bars[start].timestamp, bars[end].timestamp,
                                    bars[end+1].timestamp + span, low, high, tuple(dict.fromkeys(siblings)))
        return None

    def wick_midpoint(self, direction, leg, now):
        """Optional stop exception: distinct post-sweep retest and rejection.

        Never derive a tighter stop from the ratio alone. The exact structural
        extreme candle must be present, and subsequent bars must not breach the
        proposed buffered stop after the qualifying retest.
        """
        if leg is None:
            return None
        bull = direction == Direction.BULLISH
        relevant = [b for b in self.minutes if _utc(leg.extreme_at) <= _utc(b.timestamp) <= _utc(now)]
        if len(relevant) < 2 or _utc(relevant[0].timestamp) != _utc(leg.extreme_at):
            return None
        anchor = relevant[0]
        if (anchor.low if bull else anchor.high) != leg.extreme:
            return None
        body = max(self.config.instrument.tick_size, abs(anchor.close - anchor.open))
        wick = anchor.lower_wick if bull else anchor.upper_wick
        if wick <= body * self.config.oversized_wick_ratio:
            return None
        mid = (anchor.low + anchor.body_low)/2 if bull else (anchor.high + anchor.body_high)/2
        tick = self.config.instrument.tick_size
        stop = round((mid + (-1 if bull else 1)*self.config.sl_tick_buffer*tick)/tick)*tick
        if any(b.low < anchor.low if bull else b.high > anchor.high for b in relevant[1:]):
            return None
        for i, b in enumerate(relevant[1:], 1):
            touched = b.low <= mid <= b.high
            rejected = (b.close >= anchor.body_low and b.is_bullish if bull
                        else b.close <= anchor.body_high and b.is_bearish)
            if not touched or not rejected:
                continue
            if not any(x.low <= stop if bull else x.high >= stop for x in relevant[i+1:]):
                return mid
        return None

    def evaluate(self, direction, model, gap, leg, ifvg, entry, order_type, fvgs, htf_behind, now):
        b = ifvg.confirm_bar
        span = b.high-b.low
        ratio = abs(b.close-b.open)/span if span > 0 else 0.0
        aligned = b.is_bullish if direction == Direction.BULLISH else b.is_bearish
        beyond = b.close > ifvg.source_fvg.top if direction == Direction.BULLISH else b.close < ifvg.source_fvg.bottom
        if not (aligned and beyond and ratio >= self.config.min_displacement_body_fraction):
            return DiscretionDecision(False, "WEAK_DISPLACEMENT", displacement_ratio=ratio)
        r = None
        ep = rp = None
        reduced = False
        if model == PlaybookModel.CONTINUATION:
            r = self.dealing_range(gap, direction, leg, fvgs)
            if r is None:
                return DiscretionDecision(False, "NO_CONFIRMED_IMPULSE_RANGE", displacement_ratio=ratio)
            ep, rp = r.position(entry), r.position(leg.extreme)
            unfavorable = r.position(gap.bottom if direction == Direction.BULLISH else gap.top) > .5
            if self.config.enable_stacked_fvg_eq_filter and len(r.gap_ids) >= 2 and unfavorable:
                return DiscretionDecision(False, "STACKED_FVG_OUTSIDE_IMPULSE_EQ", r, ep, rp, displacement_ratio=ratio)
            if ep > .5 and rp > .5 and htf_behind:
                return DiscretionDecision(False, "UNREBALANCED_IMPULSE_WITH_1H_GAP", r, ep, rp, displacement_ratio=ratio)
            if ep >= self.config.continuation_extreme_threshold and rp > .5:
                return DiscretionDecision(False, "EXTREME_UNREBALANCED_CONTINUATION", r, ep, rp, displacement_ratio=ratio)
            reduced = ep > .5
        midpoint = (self.wick_midpoint(direction, leg, now)
                    if self.config.enable_validated_wick_stop and order_type == "MARKET" else None)
        if midpoint is not None and not (entry > midpoint if direction == Direction.BULLISH else entry < midpoint):
            midpoint = None
        return DiscretionDecision(True, "ELIGIBLE", r, ep, rp, reduced, midpoint, ratio)

    def audit(self, direction, model, gap, leg, ifvg, entry, order_type, fvgs, htf_behind, now):
        result = self.evaluate(direction, model, gap, leg, ifvg, entry, order_type, fvgs, htf_behind, now)
        self.events.append({"timestamp": now.isoformat(), "mode": self.config.discretion_mode.value,
                            "direction": direction.value, "model": model.value if model else None,
                            "condition": gap.id if gap else None, "ifvg": ifvg.source_fvg.id,
                            **result.snapshot()})
        return result
