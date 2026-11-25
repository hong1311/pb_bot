from dataclasses import dataclass
from datetime import time
from typing import Dict, List, Optional, Tuple

from pb_bot.core.models import (
    Bar,
    BreakerBlock,
    CISD,
    DevilsMark,
    Direction,
    FVG,
    FVGState,
    IFVG,
    OrderBlock,
    SwingPoint,
)
from pb_bot.core.sessions import to_ny


class FVGDetector:
    """Detects 3-candle FVGs, Target->Condition flips, IML/IMH sweeps, and exact-TF IFVGs."""

    def __init__(self, symbol: str, timeframe: str, min_gap_points: float = 0.25) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.min_gap_points = min_gap_points
        self._bars: List[Bar] = []
        self.fvgs: List[FVG] = []
        self.ifvgs: List[IFVG] = []
        self._counter = 0

    def register_intraday_tap(self, bar_1m: Bar) -> None:
        """Marks real-time 1m touches on HTF FVGs without allowing LTF inversions."""
        for fvg in self.fvgs:
            if fvg.state == FVGState.INVERTED:
                continue
            if fvg.overlapped_by(bar_1m):
                if fvg.state == FVGState.UNFILLED_TARGET:
                    fvg.state = FVGState.ACTIVE_CONDITION
                    fvg.first_tap_at = bar_1m.timestamp
                    fvg.internal_extreme = (
                        bar_1m.low if fvg.direction == Direction.BULLISH else bar_1m.high
                    )
                elif fvg.has_bounced_after_tap:
                    fvg.re_tapped_after_bounce = True
                elif fvg.internal_extreme is not None:
                    if fvg.direction == Direction.BULLISH:
                        fvg.internal_extreme = min(fvg.internal_extreme, bar_1m.low)
                    else:
                        fvg.internal_extreme = max(fvg.internal_extreme, bar_1m.high)
            elif fvg.state == FVGState.ACTIVE_CONDITION:
                if fvg.direction == Direction.BULLISH and bar_1m.low > fvg.top:
                    fvg.has_bounced_after_tap = True
                elif fvg.direction == Direction.BEARISH and bar_1m.high < fvg.bottom:
                    fvg.has_bounced_after_tap = True

            if fvg.has_bounced_after_tap and fvg.internal_extreme is not None:
                if fvg.direction == Direction.BULLISH and bar_1m.low < fvg.internal_extreme:
                    fvg.internal_extreme_swept = True
                elif fvg.direction == Direction.BEARISH and bar_1m.high > fvg.internal_extreme:
                    fvg.internal_extreme_swept = True

    def update(self, bar: Bar) -> Tuple[Optional[FVG], List[IFVG]]:
        if bar.timeframe != self.timeframe:
            raise ValueError(
                f"Exact-TF rule violated: {self.timeframe} detector received {bar.timeframe} bar"
            )

        new_ifvgs: List[IFVG] = []

        # 1. Evaluate existing FVGs BEFORE adding a new FVG from this bar
        for fvg in self.fvgs:
            if fvg.state == FVGState.INVERTED:
                continue

            # Track taps & Target -> Condition state flip
            if fvg.overlapped_by(bar):
                fvg.bars_overlapping_fvg += 1
                if fvg.state == FVGState.UNFILLED_TARGET:
                    fvg.state = FVGState.ACTIVE_CONDITION
                    fvg.first_tap_at = bar.timestamp
                    fvg.bars_since_first_tap = 1
                    fvg.internal_extreme = bar.low if fvg.direction == Direction.BULLISH else bar.high
                else:
                    fvg.bars_since_first_tap += 1
                    if fvg.has_bounced_after_tap:
                        fvg.re_tapped_after_bounce = True
                    elif fvg.internal_extreme is not None:
                        if fvg.direction == Direction.BULLISH:
                            fvg.internal_extreme = min(fvg.internal_extreme, bar.low)
                        else:
                            fvg.internal_extreme = max(fvg.internal_extreme, bar.high)
            elif fvg.state == FVGState.ACTIVE_CONDITION:
                fvg.bars_since_first_tap += 1
                # Detect bounce away from FVG so internal IML/IMH is locked in
                if fvg.direction == Direction.BULLISH and bar.low > fvg.top:
                    fvg.has_bounced_after_tap = True
                elif fvg.direction == Direction.BEARISH and bar.high < fvg.bottom:
                    fvg.has_bounced_after_tap = True

            # Check if locked-in IML/IMH inside FVG has now been swept
            if fvg.has_bounced_after_tap and fvg.internal_extreme is not None:
                if fvg.direction == Direction.BULLISH and bar.low < fvg.internal_extreme:
                    fvg.internal_extreme_swept = True
                elif fvg.direction == Direction.BEARISH and bar.high > fvg.internal_extreme:
                    fvg.internal_extreme_swept = True

            # Strict exact-timeframe BODY CLOSE inversion check
            if fvg.direction == Direction.BEARISH and bar.close > fvg.top:
                fvg.state = FVGState.INVERTED
                ifvg = IFVG(
                    source_fvg=fvg,
                    direction=Direction.BULLISH,
                    confirmed_at=bar.timestamp,
                    confirm_bar=bar,
                    candles_to_invert=max(1, fvg.bars_overlapping_fvg),
                )
                self.ifvgs.append(ifvg)
                new_ifvgs.append(ifvg)
            elif fvg.direction == Direction.BULLISH and bar.close < fvg.bottom:
                fvg.state = FVGState.INVERTED
                ifvg = IFVG(
                    source_fvg=fvg,
                    direction=Direction.BEARISH,
                    confirmed_at=bar.timestamp,
                    confirm_bar=bar,
                    candles_to_invert=max(1, fvg.bars_overlapping_fvg),
                )
                self.ifvgs.append(ifvg)
                new_ifvgs.append(ifvg)

        # 2. Check 3-candle FVG creation
        self._bars.append(bar)
        created: Optional[FVG] = None
        if len(self._bars) >= 3:
            c1, _, c3 = self._bars[-3], self._bars[-2], self._bars[-1]
            if c3.low - c1.high >= self.min_gap_points:
                self._counter += 1
                created = FVG(
                    id=f"{self.symbol}_{self.timeframe}_FVG_{self._counter}",
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    direction=Direction.BULLISH,
                    created_at=c3.timestamp,
                    bottom=c1.high,
                    top=c3.low,
                )
                self.fvgs.append(created)
            elif c1.low - c3.high >= self.min_gap_points:
                self._counter += 1
                created = FVG(
                    id=f"{self.symbol}_{self.timeframe}_FVG_{self._counter}",
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    direction=Direction.BEARISH,
                    created_at=c3.timestamp,
                    bottom=c3.high,
                    top=c1.low,
                )
                self.fvgs.append(created)

        if len(self.fvgs) > 120:
            self.fvgs = [f for f in self.fvgs if f.state != FVGState.INVERTED][-60:]
        return created, new_ifvgs

    @staticmethod
    def select_outermost_opposing_fvg(
        opposing_fvgs: List[FVG], target_direction: Direction
    ) -> Optional[FVG]:
        """If consecutive opposing FVGs exist in a pullback leg, require clearing the outer one."""
        if not opposing_fvgs:
            return None
        if target_direction == Direction.BULLISH:
            return max(opposing_fvgs, key=lambda f: f.top)
        return min(opposing_fvgs, key=lambda f: f.bottom)


class CISDDetector:
    """Detects body-close CISD beyond opposing candle-run open & activates Orderblocks."""

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._up_run: List[Bar] = []
        self._down_run: List[Bar] = []
        self._up_run_triggered = False
        self._down_run_triggered = False
        self._last_dir: Optional[Direction] = None
        self.cisds: List[CISD] = []
        self.orderblocks: List[OrderBlock] = []
        self._counter = 0

    def update(self, bar: Bar) -> Optional[CISD]:
        if bar.timeframe != self.timeframe:
            raise ValueError(f"CISDDetector({self.timeframe}) received {bar.timeframe} bar")

        # 1. Check OB invalidation -> converts to Breaker Block
        for ob in self.orderblocks:
            if ob.invalidated:
                continue
            if ob.direction == Direction.BULLISH and bar.close < ob.low:
                ob.invalidated = True
                ob.converted_to_breaker = True
            elif ob.direction == Direction.BEARISH and bar.close > ob.high:
                ob.invalidated = True
                ob.converted_to_breaker = True

        triggered: Optional[CISD] = None

        # 2. Check CISD body-close triggers (strictly body close beyond first candle open)
        if self._up_run and not self._up_run_triggered and bar.close < self._up_run[0].open:
            self._counter += 1
            cisd_level = self._up_run[0].open
            sweep_high = max(c.high for c in self._up_run)
            ob = OrderBlock(
                id=f"{self.symbol}_{self.timeframe}_OB_{self._counter}",
                symbol=self.symbol,
                timeframe=self.timeframe,
                direction=Direction.BEARISH,
                confirmed_at=bar.timestamp,
                open_price=cisd_level,
                high=sweep_high,
                low=min(c.low for c in self._up_run),
                sweep_extreme=sweep_high,
            )
            triggered = CISD(
                symbol=self.symbol,
                timeframe=self.timeframe,
                direction=Direction.BEARISH,
                confirmed_at=bar.timestamp,
                cisd_level=cisd_level,
                sweep_extreme=sweep_high,
                orderblock=ob,
            )
            self.orderblocks.append(ob)
            self.cisds.append(triggered)
            self._up_run_triggered = True

        elif self._down_run and not self._down_run_triggered and bar.close > self._down_run[0].open:
            self._counter += 1
            cisd_level = self._down_run[0].open
            sweep_low = min(c.low for c in self._down_run)
            ob = OrderBlock(
                id=f"{self.symbol}_{self.timeframe}_OB_{self._counter}",
                symbol=self.symbol,
                timeframe=self.timeframe,
                direction=Direction.BULLISH,
                confirmed_at=bar.timestamp,
                open_price=cisd_level,
                high=max(c.high for c in self._down_run),
                low=sweep_low,
                sweep_extreme=sweep_low,
            )
            triggered = CISD(
                symbol=self.symbol,
                timeframe=self.timeframe,
                direction=Direction.BULLISH,
                confirmed_at=bar.timestamp,
                cisd_level=cisd_level,
                sweep_extreme=sweep_low,
                orderblock=ob,
            )
            self.orderblocks.append(ob)
            self.cisds.append(triggered)
            self._down_run_triggered = True

        # 3. Update contiguous up-close / down-close candle runs
        if bar.is_bullish:
            if self._last_dir == Direction.BULLISH:
                self._up_run.append(bar)
            else:
                self._up_run = [bar]
                self._up_run_triggered = False
            self._last_dir = Direction.BULLISH
        elif bar.is_bearish:
            if self._last_dir == Direction.BEARISH:
                self._down_run.append(bar)
            else:
                self._down_run = [bar]
                self._down_run_triggered = False
            self._last_dir = Direction.BEARISH

        if len(self.orderblocks) > 80:
            self.orderblocks = [o for o in self.orderblocks if not o.invalidated][-40:]
        return triggered


@dataclass
class _BreakerCandidate:
    direction: Direction
    first_swing: SwingPoint   # Low_1 (bullish) or High_1 (bearish)
    pivot_swing: SwingPoint   # High_1 (bullish) or Low_1 (bearish)
    extreme_idx: Optional[int] = None
    extreme_price: Optional[float] = None
    resolved: bool = False


class BreakerBlockDetector:
    """Detects 4-point Breaker Blocks (strict body close; wick-only rejected as sweep)."""

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._bars: List[Bar] = []
        self.swings: List[SwingPoint] = []
        self._candidates: List[_BreakerCandidate] = []
        self.breakers: List[BreakerBlock] = []
        self.sweep_only_events: List[Tuple[datetime, Direction, float]] = []
        self._counter = 0

    def update(self, bar: Bar) -> List[BreakerBlock]:
        if bar.timeframe != self.timeframe:
            raise ValueError(f"BreakerBlockDetector({self.timeframe}) received {bar.timeframe} bar")

        idx = len(self._bars)
        self._bars.append(bar)

        # 1. Update active Breaker Blocks (tap & invalidation)
        for bb in self.breakers:
            if not bb.active:
                continue
            if bar.low <= bb.top and bar.high >= bb.bottom:
                bb.tapped = True
            if bb.direction == Direction.BULLISH and bar.close < bb.bottom:
                bb.active = False
            elif bb.direction == Direction.BEARISH and bar.close > bb.top:
                bb.active = False

        # 2. Confirm 3-bar pivot on bars[-2]
        if len(self._bars) >= 3:
            b_prev, b_mid, b_curr = self._bars[-3], self._bars[-2], self._bars[-1]
            mid_idx = idx - 1
            if b_mid.high > b_prev.high and b_mid.high >= b_curr.high:
                sh = SwingPoint(mid_idx, b_mid.timestamp, b_mid.high, "HIGH", self.timeframe)
                self.swings.append(sh)
                prior_lows = [s for s in self.swings if s.kind == "LOW" and s.index < sh.index]
                if prior_lows:
                    self._candidates.append(
                        _BreakerCandidate(Direction.BULLISH, prior_lows[-1], sh)
                    )
            if b_mid.low < b_prev.low and b_mid.low <= b_curr.low:
                sl = SwingPoint(mid_idx, b_mid.timestamp, b_mid.low, "LOW", self.timeframe)
                self.swings.append(sl)
                prior_highs = [s for s in self.swings if s.kind == "HIGH" and s.index < sl.index]
                if prior_highs:
                    self._candidates.append(
                        _BreakerCandidate(Direction.BEARISH, prior_highs[-1], sl)
                    )

        # 3. Evaluate pending 4-point Breaker candidates
        newly_confirmed: List[BreakerBlock] = []
        for cand in self._candidates:
            if cand.resolved:
                continue

            if cand.direction == Direction.BULLISH:
                # Track Lower Low below Low_1 after High_1
                if idx > cand.pivot_swing.index:
                    if bar.low < cand.first_swing.price:
                        if cand.extreme_price is None or bar.low < cand.extreme_price:
                            cand.extreme_price = bar.low
                            cand.extreme_idx = idx

                if cand.extreme_idx is not None and idx >= cand.extreme_idx:
                    if bar.close > cand.pivot_swing.price:
                        # Confirmed Bullish BB: last up-close candle before Lower Low
                        bb_candle = self._find_last_candle(
                            cand.first_swing.index, cand.extreme_idx, bullish=True
                        )
                        if bb_candle is not None:
                            self._counter += 1
                            bb = BreakerBlock(
                                id=f"{self.symbol}_{self.timeframe}_BB_{self._counter}",
                                symbol=self.symbol,
                                timeframe=self.timeframe,
                                direction=Direction.BULLISH,
                                confirmed_at=bar.timestamp,
                                bottom=bb_candle.low,
                                top=bb_candle.high,
                                trigger_swing_price=cand.pivot_swing.price,
                            )
                            self.breakers.append(bb)
                            newly_confirmed.append(bb)
                        cand.resolved = True
                    elif bar.high > cand.pivot_swing.price and bar.close <= cand.pivot_swing.price:
                        self.sweep_only_events.append(
                            (bar.timestamp, Direction.BULLISH, cand.pivot_swing.price)
                        )

            else:  # BEARISH
                # Track Higher High above High_1 after Low_1
                if idx > cand.pivot_swing.index:
                    if bar.high > cand.first_swing.price:
                        if cand.extreme_price is None or bar.high > cand.extreme_price:
                            cand.extreme_price = bar.high
                            cand.extreme_idx = idx

                if cand.extreme_idx is not None and idx >= cand.extreme_idx:
                    if bar.close < cand.pivot_swing.price:
                        # Confirmed Bearish BB: last down-close candle before Higher High
                        bb_candle = self._find_last_candle(
                            cand.first_swing.index, cand.extreme_idx, bullish=False
                        )
                        if bb_candle is not None:
                            self._counter += 1
                            bb = BreakerBlock(
                                id=f"{self.symbol}_{self.timeframe}_BB_{self._counter}",
                                symbol=self.symbol,
                                timeframe=self.timeframe,
                                direction=Direction.BEARISH,
                                confirmed_at=bar.timestamp,
                                bottom=bb_candle.low,
                                top=bb_candle.high,
                                trigger_swing_price=cand.pivot_swing.price,
                            )
                            self.breakers.append(bb)
                            newly_confirmed.append(bb)
                        cand.resolved = True
                    elif bar.low < cand.pivot_swing.price and bar.close >= cand.pivot_swing.price:
                        self.sweep_only_events.append(
                            (bar.timestamp, Direction.BEARISH, cand.pivot_swing.price)
                        )

        # Prune resolved candidates
        self._candidates = [c for c in self._candidates if not c.resolved][-20:]
        if len(self.breakers) > 80:
            self.breakers = [b for b in self.breakers if b.active][-40:]
        return newly_confirmed

    def _find_last_candle(self, start_idx: int, end_idx: int, bullish: bool) -> Optional[Bar]:
        for i in range(end_idx - 1, start_idx - 1, -1):
            c = self._bars[i]
            if bullish and c.is_bullish:
                return c
            if not bullish and c.is_bearish:
                return c
        return None


class DevilsMarkDetector:
    """Detects 15m session-open candles with zero opposing wick (strong DOL magnet)."""

    SESSION_OPEN_TIMES = {time(0, 0), time(2, 0), time(8, 30), time(9, 30)}

    def __init__(self, symbol: str, timeframe: str = "15m", eps: float = 1e-9) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.eps = eps
        self.marks: List[DevilsMark] = []

    def update(self, bar: Bar) -> Optional[DevilsMark]:
        if bar.timeframe != self.timeframe:
            return None
        t = to_ny(bar.timestamp).time()
        if t not in self.SESSION_OPEN_TIMES:
            return None

        if bar.is_bullish and abs(bar.open - bar.low) <= self.eps:
            dm = DevilsMark(self.symbol, self.timeframe, bar.timestamp, Direction.BULLISH, bar.open)
            self.marks.append(dm)
            return dm
        if bar.is_bearish and abs(bar.open - bar.high) <= self.eps:
            dm = DevilsMark(self.symbol, self.timeframe, bar.timestamp, Direction.BEARISH, bar.open)
            self.marks.append(dm)
            return dm
        return None


def equilibrium_levels(low: float, high: float) -> Dict[str, float]:
    """Computes PB Fib/Gann range levels (0.5 EQ, 0.62, 0.705, 0.79)."""
    span = high - low
    return {
        "0.0": low,
        "0.5": low + 0.50 * span,
        "0.62": low + 0.62 * span,
        "0.705": low + 0.705 * span,
        "0.79": low + 0.79 * span,
        "1.0": high,
    }


def is_in_premium(price: float, range_low: float, range_high: float) -> bool:
    return price > (range_low + range_high) / 2.0


def oversized_wick_eq(bar: Bar, direction: Direction) -> float:
    """50% midpoint of lower wick (bullish) or upper wick (bearish) for oversized-wick SL rule."""
    if direction == Direction.BULLISH:
        return (bar.low + bar.body_low) / 2.0
    return (bar.high + bar.body_high) / 2.0
