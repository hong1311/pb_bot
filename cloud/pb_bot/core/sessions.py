from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from pb_bot.core.models import Bar

NY_TZ = ZoneInfo("America/New_York")


def to_ny(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=NY_TZ)
    return dt.astimezone(NY_TZ)


def cme_trading_date(ts_ny: datetime) -> date:
    """CME equity futures trading day rolls at 18:00 America/New_York."""
    ts_ny = to_ny(ts_ny)
    if ts_ny.hour >= 18:
        return (ts_ny + timedelta(days=1)).date()
    return ts_ny.date()


@dataclass
class SessionRange:
    high: Optional[float] = None
    low: Optional[float] = None
    open_price: Optional[float] = None
    close_price: Optional[float] = None
    high_swept: bool = False
    low_swept: bool = False

    def update(self, bar: Bar) -> None:
        if self.open_price is None:
            self.open_price = bar.open
        self.high = bar.high if self.high is None else max(self.high, bar.high)
        self.low = bar.low if self.low is None else min(self.low, bar.low)
        self.close_price = bar.close

    @property
    def is_valid(self) -> bool:
        return self.high is not None and self.low is not None


@dataclass
class DailySessionState:
    trading_day: date
    asia: SessionRange = field(default_factory=SessionRange)
    london: SessionRange = field(default_factory=SessionRange)
    ny_am: SessionRange = field(default_factory=SessionRange)
    full_day: SessionRange = field(default_factory=SessionRange)

    true_day_open: Optional[float] = None   # 00:00 NY
    data_830_open: Optional[float] = None   # 08:30 NY
    data_830_high: Optional[float] = None   # 08:30-08:45 window
    data_830_low: Optional[float] = None
    open_930: Optional[float] = None        # 09:30 NY equities open
    open_1000: Optional[float] = None       # 10:00 NY 4H open
    open_1400: Optional[float] = None       # 14:00 NY 4H open

    pdh: Optional[float] = None
    pdl: Optional[float] = None
    pwh: Optional[float] = None
    pwl: Optional[float] = None
    ndog_low: Optional[float] = None
    ndog_high: Optional[float] = None
    nwog_low: Optional[float] = None
    nwog_high: Optional[float] = None
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None
    overnight_objective_swept: bool = False
    london_swept_asia_high: bool = False
    london_swept_asia_low: bool = False

    @property
    def overnight_range(self) -> float:
        if self.overnight_high is None or self.overnight_low is None:
            return 0.0
        return self.overnight_high - self.overnight_low

    @property
    def is_seek_and_destroy_suspect(self) -> bool:
        """Both sides of Asia or London range swept without clean one-sided expansion."""
        asia_both = self.asia.high_swept and self.asia.low_swept
        london_both = self.london.high_swept and self.london.low_swept
        return asia_both or london_both


class SessionTracker:
    """Tracks NY-time sessions, key opens, PDH/PDL, PWH/PWL, NDOG/NWOG, and sweeps."""

    def __init__(self) -> None:
        self.current: Optional[DailySessionState] = None
        self._prev_day_range: Optional[SessionRange] = None
        self._current_week_iso: Optional[tuple] = None
        self._current_week_range: SessionRange = SessionRange()
        self._prev_week_range: Optional[SessionRange] = None
        self._prev_day_close: Optional[float] = None
        self._prev_week_close: Optional[float] = None

    def update(self, bar_1m: Bar) -> DailySessionState:
        ts = to_ny(bar_1m.timestamp)
        t_date = cme_trading_date(ts)
        week_iso = t_date.isocalendar()[:2]

        # Handle new trading day rollover (18:00 NY)
        if self.current is None or self.current.trading_day != t_date:
            if self.current is not None and self.current.full_day.is_valid:
                self._prev_day_range = self.current.full_day
                self._prev_day_close = self.current.full_day.close_price

            if self._current_week_iso is not None and week_iso != self._current_week_iso:
                if self._current_week_range.is_valid:
                    self._prev_week_range = self._current_week_range
                    self._prev_week_close = self._current_week_range.close_price
                self._current_week_range = SessionRange()
            self._current_week_iso = week_iso

            self.current = DailySessionState(trading_day=t_date)
            if self._prev_day_range is not None:
                self.current.pdh = self._prev_day_range.high
                self.current.pdl = self._prev_day_range.low
            if self._prev_week_range is not None:
                self.current.pwh = self._prev_week_range.high
                self.current.pwl = self._prev_week_range.low

            # NDOG: gap between prior 16:59 close and new 18:00 open
            if self._prev_day_close is not None and bar_1m.open != self._prev_day_close:
                self.current.ndog_low = min(self._prev_day_close, bar_1m.open)
                self.current.ndog_high = max(self._prev_day_close, bar_1m.open)

            # NWOG: gap between prior week close and new week open
            if (
                self._prev_week_close is not None
                and self._current_week_range.open_price is None
                and bar_1m.open != self._prev_week_close
            ):
                self.current.nwog_low = min(self._prev_week_close, bar_1m.open)
                self.current.nwog_high = max(self._prev_week_close, bar_1m.open)

        st = self.current
        st.full_day.update(bar_1m)
        self._current_week_range.update(bar_1m)
        t = ts.time()

        # Session range accumulation
        if t >= time(20, 0):
            st.asia.update(bar_1m)
        if t >= time(18, 0) or t < time(9, 30):
            st.overnight_high = bar_1m.high if st.overnight_high is None else max(st.overnight_high, bar_1m.high)
            st.overnight_low = bar_1m.low if st.overnight_low is None else min(st.overnight_low, bar_1m.low)
            if (
                (st.pdh is not None and bar_1m.high >= st.pdh)
                or (st.pdl is not None and bar_1m.low <= st.pdl)
                or (st.pwh is not None and bar_1m.high >= st.pwh)
                or (st.pwl is not None and bar_1m.low <= st.pwl)
            ):
                st.overnight_objective_swept = True
        if time(2, 0) <= t < time(5, 0):
            st.london.update(bar_1m)
            if st.asia.is_valid:
                if bar_1m.high > st.asia.high:
                    st.london_swept_asia_high = True
                if bar_1m.low < st.asia.low:
                    st.london_swept_asia_low = True
        if time(8, 30) <= t < time(11, 0):
            st.ny_am.update(bar_1m)

        # Key timestamps
        if t == time(0, 0) and st.true_day_open is None:
            st.true_day_open = bar_1m.open
        if t == time(8, 30) and st.data_830_open is None:
            st.data_830_open = bar_1m.open
        if time(8, 30) <= t < time(8, 45):
            st.data_830_high = bar_1m.high if st.data_830_high is None else max(st.data_830_high, bar_1m.high)
            st.data_830_low = bar_1m.low if st.data_830_low is None else min(st.data_830_low, bar_1m.low)
        if t == time(9, 30) and st.open_930 is None:
            st.open_930 = bar_1m.open
        if t == time(10, 0) and st.open_1000 is None:
            st.open_1000 = bar_1m.open
        if t == time(14, 0) and st.open_1400 is None:
            st.open_1400 = bar_1m.open

        # Post-session liquidity sweep tracking
        if t < time(18, 0) and st.asia.is_valid:
            if bar_1m.high > st.asia.high:
                st.asia.high_swept = True
            if bar_1m.low < st.asia.low:
                st.asia.low_swept = True

        if time(5, 0) <= t < time(18, 0) and st.london.is_valid:
            if bar_1m.high > st.london.high:
                st.london.high_swept = True
            if bar_1m.low < st.london.low:
                st.london.low_swept = True

        return st

    @staticmethod
    def in_trading_window(
        ts_ny: datetime,
        am_start: time = time(9, 30),
        am_end: time = time(11, 0),
        allow_pm: bool = False,
        pm_start: time = time(13, 30),
        pm_end: time = time(15, 30),
    ) -> bool:
        t = to_ny(ts_ny).time()
        if am_start <= t < am_end:
            return True
        if allow_pm and pm_start <= t < pm_end:
            return True
        return False
