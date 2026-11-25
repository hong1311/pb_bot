from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Dict, List, Optional

from pb_bot.config import BotConfig
from pb_bot.core.models import Bar, CISD, Direction, FVG, FVGState
from pb_bot.core.sessions import DailySessionState, to_ny


class DailyProfile(str, Enum):
    UNKNOWN = "UNKNOWN"
    LONDON_REVERSAL_NY_CONTINUATION = "LONDON_REVERSAL_NY_CONTINUATION"
    LONDON_JUDAS_NY_REVERSAL = "LONDON_JUDAS_NY_REVERSAL"
    AMD_PO3 = "AMD_PO3"
    SEEK_AND_DESTROY = "SEEK_AND_DESTROY"


@dataclass
class BiasState:
    bias: Direction = Direction.NEUTRAL
    profile: DailyProfile = DailyProfile.UNKNOWN
    london_sponsored: bool = False
    london_sponsorship_tf: Optional[str] = None
    condition_fvg: Optional[FVG] = None
    bias_flipped_today: bool = False
    seek_and_destroy_active: bool = False
    notes: str = ""


class BiasAndProfileEngine:
    """Evaluates HTF Bias, binary IF/THEN condition flips, >=30m sponsorship, and 3 Daily Profiles."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.state = BiasState()
        self._london_cisd_15m: Optional[CISD] = None
        self._ny_cisd_15m_dirs: List[Direction] = []
        self._current_day = None

    def reset_for_new_day(self, trading_day) -> None:
        self._current_day = trading_day
        self.state = BiasState(bias=self.state.bias)
        self._london_cisd_15m = None
        self._ny_cisd_15m_dirs = []

    def on_htf_fvg_update(
        self,
        timeframe: str,
        bar: Bar,
        fvgs_by_tf: Dict[str, List[FVG]],
        newly_inverted_dirs: List[Direction],
    ) -> None:
        """Binary HTF Bias Switcher (CONDITIONS / DOL 2.0 / Charting Session)."""
        if timeframe not in ("4H", "1H", "15m"):
            return

        # 4H inversion is highest authority for macro bias
        if timeframe == "4H" and newly_inverted_dirs:
            self.state.bias = newly_inverted_dirs[-1]
            self.state.bias_flipped_today = True
            self.state.notes = f"4H_FVG_INVERTED_TO_{self.state.bias.value}"
            return

        # 1H FVG binary switcher
        if timeframe == "1H":
            if newly_inverted_dirs:
                self.state.bias = newly_inverted_dirs[-1]
                self.state.bias_flipped_today = True
                self.state.notes = f"1H_FVG_INVERTED_TO_{self.state.bias.value}"
                return
            active_1h = [
                f for f in fvgs_by_tf.get("1H", []) if f.state == FVGState.ACTIVE_CONDITION
            ]
            if active_1h:
                latest = active_1h[-1]
                self.state.condition_fvg = latest
                if self.state.bias == Direction.NEUTRAL:
                    self.state.bias = latest.direction

        # 15m FVG flip only when no deeper 15m+ FVG still holds in the current bias direction
        if timeframe == "15m" and newly_inverted_dirs:
            still_holding_bias_fvg = any(
                f.direction == self.state.bias
                and f.state in (FVGState.UNFILLED_TARGET, FVGState.ACTIVE_CONDITION)
                for tf in ("4H", "1H", "30m", "15m")
                for f in fvgs_by_tf.get(tf, [])
            )
            if not still_holding_bias_fvg:
                self.state.bias = newly_inverted_dirs[-1]
                self.state.bias_flipped_today = True
                self.state.notes = f"15M_FVG_INVERTED_TO_{self.state.bias.value}"
                return

        # Fallback bootstrap from recent un-inverted HTF order flow
        if self.state.bias == Direction.NEUTRAL:
            for tf in ("4H", "1H", "30m", "15m"):
                active = [
                    f for f in fvgs_by_tf.get(tf, [])
                    if f.state in (FVGState.UNFILLED_TARGET, FVGState.ACTIVE_CONDITION)
                ]
                if active:
                    self.state.bias = active[-1].direction
                    self.state.condition_fvg = active[-1]
                    break

    def on_15m_cisd(
        self,
        cisd: CISD,
        session: DailySessionState,
        htf_fvgs_by_tf: Dict[str, List[FVG]],
    ) -> None:
        ts_ny = to_ny(cisd.confirmed_at)
        t = ts_ny.time()

        # Overnight/London 15m CISD updates intraday delivery direction
        if time(2, 0) <= t < time(9, 0):
            self._london_cisd_15m = cisd
            has_4h_lock = any(
                f.state == FVGState.ACTIVE_CONDITION for f in htf_fvgs_by_tf.get("4H", [])
            )
            if not has_4h_lock:
                self.state.bias = cisd.direction

        if time(8, 30) <= t < time(12, 0):
            self._ny_cisd_15m_dirs.append(cisd.direction)

        # HTF Override Rule (daily profiles Example 4):
        # Dominant 1D FVG tap + 15m CISD overrides minor 1H London sponsorship
        if self.state.london_sponsored and self.state.london_sponsorship_tf == "1H":
            daily_tapped = any(
                f.state == FVGState.ACTIVE_CONDITION and f.direction == cisd.direction
                for f in htf_fvgs_by_tf.get("1D", [])
            )
            if daily_tapped and cisd.direction != self.state.bias:
                self.state.london_sponsored = False
                self.state.bias = cisd.direction
                self.state.bias_flipped_today = True
                self.state.notes = "1D_FVG_OVERRIDE_INVALIDATED_1H_LONDON"

    def evaluate_daily_profile(
        self,
        now: datetime,
        session: DailySessionState,
        htf_fvgs_by_tf: Dict[str, List[FVG]],
        current_price: Optional[float] = None,
    ) -> BiasState:
        if self._current_day != session.trading_day:
            self.reset_for_new_day(session.trading_day)

        t = to_ny(now).time()

        # Seek & Destroy Check:
        # Both sides swept WITHOUT clean directional expansion outside London range
        london_both = session.london.high_swept and session.london.low_swept
        conflicting_ny_cisds = (
            Direction.BULLISH in self._ny_cisd_15m_dirs
            and Direction.BEARISH in self._ny_cisd_15m_dirs
        )
        clean_directional_expansion = False
        if current_price is not None and session.london.is_valid and not conflicting_ny_cisds:
            if self.state.bias == Direction.BULLISH and current_price > session.london.high:
                clean_directional_expansion = True
            elif self.state.bias == Direction.BEARISH and current_price < session.london.low:
                clean_directional_expansion = True

        london_range_valid = session.london.is_valid and (session.london.high - session.london.low) >= 20.0
        london_swept_both_asia = session.london_swept_asia_high and session.london_swept_asia_low
        is_sd_now = (
            (london_both and london_range_valid and not clean_directional_expansion)
            or (london_swept_both_asia and not clean_directional_expansion)
            or (session.asia.high_swept and session.asia.low_swept and conflicting_ny_cisds)
        )
        if is_sd_now:
            self.state.profile = DailyProfile.SEEK_AND_DESTROY
            self.state.seek_and_destroy_active = True
            self.state.notes = "BOTH_SIDES_SWEPT_NO_CLEAN_EXPANSION"
            return self.state
        elif self.state.seek_and_destroy_active and clean_directional_expansion and not conflicting_ny_cisds:
            self.state.seek_and_destroy_active = False
            self.state.notes = "EXPANSION_CLEARED_SEEK_AND_DESTROY"

        if t < time(5, 0) or not session.london.is_valid:
            return self.state

        # Check >= 30m sponsorship during London (02:00-05:00)
        sponsor_tf = self._check_london_sponsorship(session, htf_fvgs_by_tf)
        self.state.london_sponsored = sponsor_tf is not None
        self.state.london_sponsorship_tf = sponsor_tf

        # Classify into the 3 Daily Profiles
        if self.state.london_sponsored and self._london_cisd_15m is not None:
            self.state.profile = DailyProfile.LONDON_REVERSAL_NY_CONTINUATION
            if self.state.bias == Direction.NEUTRAL:
                self.state.bias = self._london_cisd_15m.direction
        elif not self.state.london_sponsored and session.true_day_open is not None:
            london_range = session.london.high - session.london.low
            asia_swept = session.asia.high_swept or session.asia.low_swept
            asia_tight = (not session.asia.is_valid) or (
                (session.asia.high - session.asia.low) <= 45.0
            )
            if london_range <= 40.0 and not asia_swept and asia_tight:
                self.state.profile = DailyProfile.AMD_PO3
            else:
                self.state.profile = DailyProfile.LONDON_JUDAS_NY_REVERSAL

        # Fallback bias from True Day Open PO3 if still neutral after 09:30
        if self.state.bias == Direction.NEUTRAL and session.true_day_open is not None and t >= time(9, 30):
            if session.london.low < session.true_day_open and session.london.close_price > session.true_day_open:
                self.state.bias = Direction.BULLISH
            elif session.london.high > session.true_day_open and session.london.close_price < session.true_day_open:
                self.state.bias = Direction.BEARISH

        return self.state

    def _check_london_sponsorship(
        self,
        session: DailySessionState,
        htf_fvgs_by_tf: Dict[str, List[FVG]],
    ) -> Optional[str]:
        """Strict >= 30m sponsorship floor (15m POIs do NOT qualify)."""
        if not session.london.is_valid:
            return None

        if session.pdh is not None and session.london.high >= session.pdh:
            return "1D"
        if session.pdl is not None and session.london.low <= session.pdl:
            return "1D"

        for tf in ("1D", "4H", "1H", "30m"):
            for fvg in htf_fvgs_by_tf.get(tf, []):
                if fvg.first_tap_at is not None:
                    tap_t = to_ny(fvg.first_tap_at).time()
                    if time(2, 0) <= tap_t < time(5, 0) or tap_t >= time(20, 0):
                        return tf
        return None

    def is_overnight_exhausted(
        self, session: DailySessionState, current_price: Optional[float] = None
    ) -> bool:
        """Condition #2 (Recognizing Market Conditions): extended overnight run + HTF objective taken."""
        if not self.config.enable_overnight_exhaustion_filter or not session.overnight_objective_swept:
            return False
        ref_price = current_price or session.true_day_open or session.overnight_low or 20_000.0
        threshold = max(
            self.config.overnight_exhaustion_pts,
            ref_price * self.config.overnight_exhaustion_pct,
        )
        return session.overnight_range > threshold
