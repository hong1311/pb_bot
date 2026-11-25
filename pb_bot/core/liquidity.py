from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from pb_bot.core.models import Bar, DevilsMark, Direction, FVG, FVGState, SwingPoint
from pb_bot.core.sessions import DailySessionState


# DOL priority ranking (Lesson #10: higher priority = lower rank number)
DOL_PRIORITY = {
    "EQH": 1,
    "EQL": 1,
    "ITH": 2,
    "ITL": 2,
    "LRL": 3,
    "DEVILS_MARK": 3,
    "DATA_H": 4,
    "DATA_L": 4,
    "PDH": 5,
    "PDL": 5,
    "PWH": 5,
    "PWL": 5,
    "ASIA_H": 6,
    "ASIA_L": 6,
    "LONDON_H": 6,
    "LONDON_L": 6,
    "UNFILLED_FVG": 7,
    "NDOG": 7,
    "NWOG": 7,
    "EQ_05": 8,
    "BSL": 9,
    "SSL": 9,
}


@dataclass(frozen=True)
class LiquidityPool:
    kind: str
    price: float
    direction: Direction       # BULLISH = buy-side target above; BEARISH = sell-side target below
    timeframe: str
    priority: int
    label: str
    bar_span: int = 0          # For EQH/EQL: separation in bars (>2 = spread-apart)
    correlated_price: Optional[float] = None  # Equivalent ES level for Target SMT exit


@dataclass(frozen=True)
class SMTEvent:
    timestamp: datetime
    timeframe: str
    direction: Direction       # BULLISH = ES swept low while NQ held higher low; BEARISH = ES swept high
    nq_price: float
    es_price: float
    kind: str                  # "SWING_SMT" or "KEY_LEVEL_SMT"


class SwingTracker:
    """Confirmed 3-bar swing high/low tracker (zero lookahead: confirms on bar t for bar t-1)."""

    def __init__(self, symbol: str, timeframe: str, max_swings: int = 80) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_swings = max_swings
        self._bars: List[Bar] = []
        self.highs: List[SwingPoint] = []
        self.lows: List[SwingPoint] = []

    def update(self, bar: Bar) -> Tuple[Optional[SwingPoint], Optional[SwingPoint]]:
        idx = len(self._bars)
        self._bars.append(bar)
        new_high: Optional[SwingPoint] = None
        new_low: Optional[SwingPoint] = None

        if len(self._bars) >= 3:
            b_prev, b_mid, b_curr = self._bars[-3], self._bars[-2], self._bars[-1]
            mid_idx = idx - 1
            if b_mid.high > b_prev.high and b_mid.high >= b_curr.high:
                new_high = SwingPoint(mid_idx, b_mid.timestamp, b_mid.high, "HIGH", self.timeframe)
                self.highs.append(new_high)
                if len(self.highs) > self.max_swings:
                    self.highs = self.highs[-self.max_swings :]
            if b_mid.low < b_prev.low and b_mid.low <= b_curr.low:
                new_low = SwingPoint(mid_idx, b_mid.timestamp, b_mid.low, "LOW", self.timeframe)
                self.lows.append(new_low)
                if len(self.lows) > self.max_swings:
                    self.lows = self.lows[-self.max_swings :]

        return new_high, new_low


class LiquidityTracker:
    """Tracks EQH/EQL, LRL, NQ/ES SMT divergence, and ranked Draw on Liquidity pools."""

    def __init__(
        self,
        eq_tolerance_pts: float = 2.0,
        spread_apart_min_bars: int = 3,
    ) -> None:
        self.eq_tolerance_pts = eq_tolerance_pts
        self.spread_apart_min_bars = spread_apart_min_bars
        self.nq_swings: Dict[str, SwingTracker] = {}
        self.es_swings: Dict[str, SwingTracker] = {}
        self.smt_events: List[SMTEvent] = []

    def _get_tracker(self, store: Dict[str, SwingTracker], symbol: str, tf: str) -> SwingTracker:
        if tf not in store:
            store[tf] = SwingTracker(symbol, tf)
        return store[tf]

    def update_bars(
        self,
        timeframe: str,
        nq_bar: Optional[Bar] = None,
        es_bar: Optional[Bar] = None,
    ) -> Optional[SMTEvent]:
        if nq_bar is not None:
            self._get_tracker(self.nq_swings, nq_bar.symbol, timeframe).update(nq_bar)
        if es_bar is not None:
            self._get_tracker(self.es_swings, es_bar.symbol, timeframe).update(es_bar)

        if nq_bar is not None and es_bar is not None:
            return self._detect_swing_smt(timeframe, nq_bar.timestamp)
        return None

    def _detect_swing_smt(self, timeframe: str, ts: datetime) -> Optional[SMTEvent]:
        nq_t = self.nq_swings.get(timeframe)
        es_t = self.es_swings.get(timeframe)
        if not nq_t or not es_t:
            return None

        # Bullish SMT: ES makes lower low while NQ makes higher/equal low (or vice versa)
        if len(nq_t.lows) >= 2 and len(es_t.lows) >= 2:
            nq_prev, nq_curr = nq_t.lows[-2], nq_t.lows[-1]
            es_prev, es_curr = es_t.lows[-2], es_t.lows[-1]
            if abs((nq_curr.timestamp - es_curr.timestamp).total_seconds()) <= 900:
                if (es_curr.price < es_prev.price and nq_curr.price >= nq_prev.price) or (
                    nq_curr.price < nq_prev.price and es_curr.price >= es_prev.price
                ):
                    evt = SMTEvent(ts, timeframe, Direction.BULLISH, nq_curr.price, es_curr.price, "SWING_SMT")
                    if not self.smt_events or (
                        self.smt_events[-1].timestamp != ts or self.smt_events[-1].direction != Direction.BULLISH
                    ):
                        self.smt_events.append(evt)
                        return evt

        # Bearish SMT: ES sweeps higher high while NQ makes lower/equal high (or vice versa)
        if len(nq_t.highs) >= 2 and len(es_t.highs) >= 2:
            nq_prev, nq_curr = nq_t.highs[-2], nq_t.highs[-1]
            es_prev, es_curr = es_t.highs[-2], es_t.highs[-1]
            if abs((nq_curr.timestamp - es_curr.timestamp).total_seconds()) <= 900:
                if (es_curr.price > es_prev.price and nq_curr.price <= nq_prev.price) or (
                    nq_curr.price > nq_prev.price and es_curr.price <= es_prev.price
                ):
                    evt = SMTEvent(ts, timeframe, Direction.BEARISH, nq_curr.price, es_curr.price, "SWING_SMT")
                    if not self.smt_events or (
                        self.smt_events[-1].timestamp != ts or self.smt_events[-1].direction != Direction.BEARISH
                    ):
                        self.smt_events.append(evt)
                        return evt

        return None

    def has_recent_smt(
        self, direction: Direction, now: datetime, max_age_minutes: int = 90
    ) -> bool:
        for evt in reversed(self.smt_events):
            age_min = (now - evt.timestamp).total_seconds() / 60.0
            if age_min > max_age_minutes:
                break
            if evt.direction == direction:
                return True
        return False

    def has_dual_sided_smt(
        self,
        now: datetime,
        current_price: Optional[float] = None,
        max_age_minutes: int = 120,
        timeframes: Tuple[str, ...] = ("15m", "1H"),
    ) -> bool:
        """Condition #1 (Recognizing Market Conditions): Concurrent HTF Bullish & Bearish SMT.

        Cleared automatically once price breaks out beyond either SMT range boundary.
        """
        latest_bull: Optional[SMTEvent] = None
        latest_bear: Optional[SMTEvent] = None
        for evt in reversed(self.smt_events):
            age_min = (now - evt.timestamp).total_seconds() / 60.0
            if age_min > max_age_minutes:
                break
            if evt.timeframe not in timeframes:
                continue
            if evt.direction == Direction.BULLISH and latest_bull is None:
                latest_bull = evt
            elif evt.direction == Direction.BEARISH and latest_bear is None:
                latest_bear = evt
            if latest_bull is not None and latest_bear is not None:
                break

        if latest_bull is None or latest_bear is None:
            return False

        # Must form a valid pinned range (Bearish SMT high above Bullish SMT low)
        if latest_bear.nq_price <= latest_bull.nq_price:
            return False

        # Invalidation: clear Dual-SMT chop once price breaks outside either boundary
        if current_price is not None:
            if current_price >= latest_bear.nq_price or current_price <= latest_bull.nq_price:
                return False

        return True

    def detect_equal_levels(
        self, timeframe: str, current_price: float
    ) -> Tuple[List[LiquidityPool], List[LiquidityPool]]:
        """Returns (spread_apart_eq_pools, tight_eq_pools)."""
        tracker = self.nq_swings.get(timeframe)
        if not tracker:
            return [], []

        spread_apart: List[LiquidityPool] = []
        tight: List[LiquidityPool] = []

        # Equal Highs (above price)
        recent_highs = tracker.highs[-10:]
        for i in range(len(recent_highs)):
            for j in range(i + 1, len(recent_highs)):
                s1, s2 = recent_highs[i], recent_highs[j]
                if abs(s1.price - s2.price) <= self.eq_tolerance_pts:
                    level = max(s1.price, s2.price)
                    if level > current_price:
                        span = abs(s2.index - s1.index)
                        pool = LiquidityPool(
                            kind="EQH",
                            price=level,
                            direction=Direction.BULLISH,
                            timeframe=timeframe,
                            priority=DOL_PRIORITY["EQH"],
                            label=f"{timeframe}_EQH",
                            bar_span=span,
                        )
                        if span >= self.spread_apart_min_bars:
                            spread_apart.append(pool)
                        else:
                            tight.append(pool)

        # Equal Lows (below price)
        recent_lows = tracker.lows[-10:]
        for i in range(len(recent_lows)):
            for j in range(i + 1, len(recent_lows)):
                s1, s2 = recent_lows[i], recent_lows[j]
                if abs(s1.price - s2.price) <= self.eq_tolerance_pts:
                    level = min(s1.price, s2.price)
                    if level < current_price:
                        span = abs(s2.index - s1.index)
                        pool = LiquidityPool(
                            kind="EQL",
                            price=level,
                            direction=Direction.BEARISH,
                            timeframe=timeframe,
                            priority=DOL_PRIORITY["EQL"],
                            label=f"{timeframe}_EQL",
                            bar_span=span,
                        )
                        if span >= self.spread_apart_min_bars:
                            spread_apart.append(pool)
                        else:
                            tight.append(pool)

        return spread_apart, tight

    def detect_lrl(
        self, timeframe: str, current_price: float, now: datetime
    ) -> List[LiquidityPool]:
        """Detects 3+ clean ascending lows (short LRL) or descending highs (long LRL).

        Per CONDITIONS [33:44]: if ES swept below its corresponding low (Bullish SMT),
        stacked NQ lows are reclassified as SMT strength rather than downside LRL.
        """
        tracker = self.nq_swings.get(timeframe)
        if not tracker:
            return []

        pools: List[LiquidityPool] = []
        if len(tracker.lows) >= 3:
            l1, l2, l3 = tracker.lows[-3], tracker.lows[-2], tracker.lows[-1]
            if l1.price < l2.price < l3.price < current_price:
                if not self.has_recent_smt(Direction.BULLISH, now, max_age_minutes=120):
                    pools.append(
                        LiquidityPool(
                            kind="LRL",
                            price=l1.price,
                            direction=Direction.BEARISH,
                            timeframe=timeframe,
                            priority=DOL_PRIORITY["LRL"],
                            label=f"{timeframe}_LRL_LOWS",
                        )
                    )

        if len(tracker.highs) >= 3:
            h1, h2, h3 = tracker.highs[-3], tracker.highs[-2], tracker.highs[-1]
            if h1.price > h2.price > h3.price > current_price:
                if not self.has_recent_smt(Direction.BEARISH, now, max_age_minutes=120):
                    pools.append(
                        LiquidityPool(
                            kind="LRL",
                            price=h1.price,
                            direction=Direction.BULLISH,
                            timeframe=timeframe,
                            priority=DOL_PRIORITY["LRL"],
                            label=f"{timeframe}_LRL_HIGHS",
                        )
                    )
        return pools

    def collect_dol_targets(
        self,
        direction: Direction,
        current_price: float,
        now: datetime,
        nq_session: DailySessionState,
        es_session: Optional[DailySessionState],
        unfilled_fvgs: List[FVG],
        devils_marks: List[DevilsMark],
    ) -> List[LiquidityPool]:
        """Collects all valid DOL targets ahead of price (strictly excludes Orderblocks)."""
        candidates: List[LiquidityPool] = []

        def add_if_ahead(
            kind: str,
            price: Optional[float],
            tf: str,
            label: str,
            es_price: Optional[float] = None,
        ) -> None:
            if price is None:
                return
            if direction == Direction.BULLISH and price > current_price:
                candidates.append(
                    LiquidityPool(kind, price, direction, tf, DOL_PRIORITY[kind], label, 0, es_price)
                )
            elif direction == Direction.BEARISH and price < current_price:
                candidates.append(
                    LiquidityPool(kind, price, direction, tf, DOL_PRIORITY[kind], label, 0, es_price)
                )

        # 1. Session & HTF external liquidity (paired with equivalent ES target when available)
        if direction == Direction.BULLISH:
            add_if_ahead("PDH", nq_session.pdh, "1D", "PDH", es_session.pdh if es_session else None)
            add_if_ahead("PWH", nq_session.pwh, "1W", "PWH", es_session.pwh if es_session else None)
            add_if_ahead(
                "LONDON_H",
                nq_session.london.high,
                "SESSION",
                "LONDON_HIGH",
                es_session.london.high if es_session else None,
            )
            add_if_ahead(
                "ASIA_H",
                nq_session.asia.high,
                "SESSION",
                "ASIA_HIGH",
                es_session.asia.high if es_session else None,
            )
            add_if_ahead(
                "DATA_H",
                nq_session.data_830_high,
                "SESSION",
                "DATA_HIGH",
                es_session.data_830_high if es_session else None,
            )
        else:
            add_if_ahead("PDL", nq_session.pdl, "1D", "PDL", es_session.pdl if es_session else None)
            add_if_ahead("PWL", nq_session.pwl, "1W", "PWL", es_session.pwl if es_session else None)
            add_if_ahead(
                "LONDON_L",
                nq_session.london.low,
                "SESSION",
                "LONDON_LOW",
                es_session.london.low if es_session else None,
            )
            add_if_ahead(
                "ASIA_L",
                nq_session.asia.low,
                "SESSION",
                "ASIA_LOW",
                es_session.asia.low if es_session else None,
            )
            add_if_ahead(
                "DATA_L",
                nq_session.data_830_low,
                "SESSION",
                "DATA_LOW",
                es_session.data_830_low if es_session else None,
            )

        # 2. Unfilled FVGs (5m/15m/1H/4H/1D) and ITH/ITL inside FVGs
        for fvg in unfilled_fvgs:
            if fvg.state == FVGState.UNFILLED_TARGET:
                edge = fvg.bottom if direction == Direction.BULLISH else fvg.top
                add_if_ahead("UNFILLED_FVG", edge, fvg.timeframe, f"{fvg.timeframe}_UNFILLED_FVG")
            if fvg.internal_extreme is not None and not fvg.internal_extreme_swept:
                kind = "ITH" if direction == Direction.BULLISH else "ITL"
                add_if_ahead(kind, fvg.internal_extreme, fvg.timeframe, f"{fvg.timeframe}_{kind}")

        # 3. Devil's Marks
        for dm in devils_marks[-5:]:
            add_if_ahead("DEVILS_MARK", dm.open_price, dm.timeframe, "DEVILS_MARK")

        # 4. EQH/EQL, LRL, and raw BSL/SSL swings on 5m & 15m
        for tf in ("15m", "5m"):
            spread_eq, _ = self.detect_equal_levels(tf, current_price)
            for p in spread_eq:
                if p.direction == direction:
                    candidates.append(p)
            for p in self.detect_lrl(tf, current_price, now):
                if p.direction == direction:
                    candidates.append(p)
            tracker = self.nq_swings.get(tf)
            if tracker:
                if direction == Direction.BULLISH:
                    for sh in tracker.highs[-6:]:
                        add_if_ahead("BSL", sh.price, tf, f"{tf}_BSL")
                else:
                    for sl in tracker.lows[-6:]:
                        add_if_ahead("SSL", sl.price, tf, f"{tf}_SSL")

        # Sort closest-first among valid targets, breaking ties by DOL priority
        candidates.sort(key=lambda p: (abs(p.price - current_price), p.priority))
        return candidates

    def has_spread_apart_obstruction(
        self, direction: Direction, current_price: float, max_distance_pts: float = 25.0
    ) -> Optional[LiquidityPool]:
        """Hard No-Trade guard: never short directly under spread-apart EQH or long above spread-apart EQL."""
        opposing_dir = direction.opposite
        for tf in ("15m", "5m", "1m"):
            spread_eq, _ = self.detect_equal_levels(tf, current_price)
            for pool in spread_eq:
                if pool.direction == opposing_dir and abs(pool.price - current_price) <= max_distance_pts:
                    return pool
        return None
