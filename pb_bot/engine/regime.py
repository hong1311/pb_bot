"""V2.5 causal market-regime research filters (NY time, closed minute input).

No new orders or exits are generated here. OBSERVE logs the proposed gates;
STRICT replaces legacy regime gates. The numerical proxies are documented.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from pb_bot.core.models import Direction, FVG, FVGState
from pb_bot.core.sessions import cme_trading_date, to_ny

M = timedelta(minutes=1)
FIVE = 5 * M


def utc(ts):
    return to_ny(ts).astimezone(timezone.utc)


def _beyond(bar, direction, level):
    return bar.close > level if direction == Direction.BULLISH else bar.close < level


def _strong(bar, direction, fraction):
    span = bar.high - bar.low
    body = bar.close - bar.open if direction == Direction.BULLISH else bar.open - bar.close
    return span > 0 and body > 0 and body / span >= fraction


def _session(ts):
    t = to_ny(ts).time()
    if t >= time(20):
        return 'ASIA'
    if time(2) <= t < time(5):
        return 'LONDON'
    if time(8, 30) <= t < time(12):
        return 'NY_AM'
    if time(12) <= t < time(17):
        return 'NY_PM'
    return 'OTHER'


@dataclass
class RangeLock:
    low: float
    high: float
    opened_at: datetime
    reason: str
    breakout_at: Optional[datetime] = None
    direction: Optional[Direction] = None
    recovery_gap: Optional[FVG] = None
    gap_observed_at: Optional[datetime] = None

    def reset_recovery(self):
        self.breakout_at = self.direction = self.recovery_gap = self.gap_observed_at = None

    def snapshot(self):
        return {'reason': self.reason, 'low': self.low, 'high': self.high,
                'opened_at': self.opened_at.isoformat(),
                'direction': self.direction.value if self.direction else None,
                'breakout_at': self.breakout_at.isoformat() if self.breakout_at else None,
                'recovery_gap': self.recovery_gap.id if self.recovery_gap else None}


class RegimeEngine:
    def __init__(self, config, calendar=None):
        self.config = config
        self.calendar = calendar
        self.events = []
        self._day = None
        self._last = None
        self._smt_cursor = 0
        self._reset_day()

    def _reset_day(self):
        self._run_start = None
        self._five = deque(maxlen=3)
        self._recent = deque(maxlen=180)
        self._smt = {}
        self.smt_lock = None
        self.seek_lock = None
        self._potential_seen = False
        self._confirmed_seen = False
        self._ny_high = self._ny_low = False
        self._objective = None  # (observed minute, label, price, direction)
        self._exhaustion_recovered = False
        self._recovery_narrative_id = None
        self.overnight_span = 0.0
        self._am_gap = False
        self._macro_gap = False
        self._macro_pivot = False
        self._macro_source = False
        self._macro_highs = []
        self._macro_lows = []
        self._macro_bars = deque(maxlen=3)
        self._macro_generating = False
        self.reasons = ()
        self._last_snapshot = None

    @property
    def seek_active(self):
        return self.seek_lock is not None

    @property
    def blocked(self):
        return bool(self.reasons)

    def snapshot(self):
        return {'mode': self.config.regime_mode.value, 'blocked': self.blocked,
                'reasons': list(self.reasons), 'calendar_available': self.calendar is not None,
                'seek_lock': self.seek_lock.snapshot() if self.seek_lock else None,
                'smt_lock': self.smt_lock.snapshot() if self.smt_lock else None,
                'overnight_span': self.overnight_span,
                'overnight_objective': self._objective[1] if self._objective else None,
                'overnight_recovered': self._exhaustion_recovered,
                'am_5m_fvg': self._am_gap, 'macro_5m_fvg': self._macro_gap,
                'macro_data_pivot': self._macro_pivot, 'macro_generating': self._macro_generating}

    def _emit(self, now, event, **extra):
        self.events.append({'timestamp': now.isoformat(), 'event': event,
                            **self.snapshot(), **extra})

    def calendar_reasons(self, ts):
        """Schedule-only blockers, knowable before a pending order can fill."""
        if self.calendar is None:
            return ('CALENDAR_REQUIRED',) if self.config.regime_require_calendar else ()
        ts = to_ny(ts)
        # The lesson's PM suppression lasts until the scheduled release has passed.
        pm = [e for e in self.calendar.events if e.is_red and e.event_date == ts.date()
              and e.event_time_ny >= time(12)]
        if pm and ts <= max(datetime.combine(e.event_date, e.event_time_ny, tzinfo=ts.tzinfo)
                            for e in pm) + 3 * M:
            return ('PM_RED_NEWS_SUPPRESSION',)
        if self.calendar.is_day_before_nfp_or_cpi(ts.date()) is not None:
            return ('PRE_NFP_CPI_CHOP',)
        if self.calendar.active_red_news_freeze(ts) is not None:
            return ('RED_NEWS_FREEZE',)
        return ()

    def pending_reasons(self, ts):
        """Known blockers at the bar open, using only preceding closed data."""
        now = to_ny(ts)
        t = now.time()
        reasons = list(self.calendar_reasons(now))
        if self.config.enable_10am_macro_pause and time(9, 55) <= t <= time(10, 10):
            reasons.append('MACRO_10AM_PAUSE')
        if self.config.enable_lunch_block and time(12) <= t < time(13):
            reasons.append('NY_LUNCH_ACCUMULATION')
        if time(10) <= t < time(11):
            same_day = self._day == cme_trading_date(now)
            if self.config.enable_am_5m_fvg_volatility_filter and not (same_day and self._am_gap):
                reasons.append('NO_5M_FVG_VOLATILITY')
            if self._has_10am_red(now.date()) and not (same_day and self._macro_gap and self._macro_pivot):
                reasons.append('WAIT_10AM_DATA_STRUCTURE')
        return tuple(reasons)

    def _has_10am_red(self, day):
        return self.calendar is not None and any(
            e.is_red and e.event_date == day and e.event_time_ny == time(10)
            for e in self.calendar.events)

    def _complete_five(self, now, closed, created):
        """Accept only contiguous complete buckets and their verified 3-bar gaps."""
        bars, gaps = [], []
        for b in closed.get('5m', ()):
            if utc(b.timestamp) < self._run_start or utc(b.timestamp) + FIVE != utc(now) + M:
                self._five.clear()
                continue
            if self._five and utc(b.timestamp) - utc(self._five[-1].timestamp) != FIVE:
                self._five.clear()
            self._five.append(b)
            bars.append(b)
            if len(self._five) < 3:
                continue
            a, mid, c = self._five
            for f in created:
                if f.timeframe != '5m' or f.created_at != c.timestamp or f.bottom >= f.top:
                    continue
                valid = (f.bottom == a.high and f.top == c.low if f.direction == Direction.BULLISH
                         else f.bottom == c.high and f.top == a.low)
                if valid:
                    gaps.append(f)
                    if time(9, 30) <= to_ny(a.timestamp).time() <= to_ny(c.timestamp).time() < time(11):
                        self._am_gap = True
                    if time(10) <= to_ny(a.timestamp).time() <= to_ny(c.timestamp).time() < time(11):
                        self._macro_gap = True
            if time(10) <= to_ny(a.timestamp).time() <= to_ny(c.timestamp).time() < time(11):
                if ((mid.high > a.high and mid.high > c.high) or
                    (mid.low < a.low and mid.low < c.low)):
                    self._macro_pivot = True
        return bars, gaps

    def _recover(self, lock, now, five_bars, gaps, require_retest):
        if lock is None or now <= lock.opened_at:
            return False
        for b in five_bars:
            # A bar that began before the range lock cannot prove its recovery.
            if utc(b.timestamp) <= utc(lock.opened_at):
                continue
            if lock.direction is not None:
                level = lock.high if lock.direction == Direction.BULLISH else lock.low
                if (not _beyond(b, lock.direction, level)
                    or (lock.recovery_gap and lock.recovery_gap.state == FVGState.INVERTED)):
                    lock.reset_recovery()
                elif (lock.recovery_gap is not None and now > lock.gap_observed_at
                      and utc(b.timestamp) > utc(lock.gap_observed_at)
                      and lock.recovery_gap.overlapped_by(b)):
                    far = lock.recovery_gap.bottom if lock.direction == Direction.BULLISH else lock.recovery_gap.top
                    respected = b.close >= far if lock.direction == Direction.BULLISH else b.close <= far
                    if respected:
                        return True
            if lock.direction is None:
                direction = (Direction.BULLISH if b.close > lock.high else
                             Direction.BEARISH if b.close < lock.low else None)
                if direction and _strong(b, direction, self.config.min_displacement_body_fraction):
                    if not require_retest:
                        return True
                    lock.direction, lock.breakout_at = direction, now
            if lock.direction and lock.recovery_gap is None:
                for f in gaps:
                    if f.created_at == b.timestamp and f.direction == lock.direction:
                        # Retest gap must itself be outside the swept range.
                        if (f.bottom >= lock.high if f.direction == Direction.BULLISH else f.top <= lock.low):
                            lock.recovery_gap, lock.gap_observed_at = f, now
                            break
        return False

    def _update_smt(self, now, bar, events, five_bars):
        if self._smt_cursor > len(events):
            self._smt_cursor = 0
        fresh = events[self._smt_cursor:]
        self._smt_cursor = len(events)
        for e in fresh:
            if (e.timeframe not in ('15m', '1H') or cme_trading_date(e.timestamp) != self._day
                or not 0 <= (utc(now) - utc(e.timestamp)).total_seconds() <= 3600):
                continue
            # Legacy swing SMT is a key-level proxy. When available, require both
            # underlying swing timestamps to be current-session, not stale pairs.
            if any(cme_trading_date(t) != self._day or _session(t) != _session(now)
                   for t in (getattr(e, 'nq_pivot_at', None), getattr(e, 'es_pivot_at', None)) if t is not None):
                continue
            self._smt[e.direction] = (now, e)
            bull, bear = self._smt.get(Direction.BULLISH), self._smt.get(Direction.BEARISH)
            if self.smt_lock is None and bull and bear:
                if (abs(utc(bull[0]) - utc(bear[0])) <= self.config.regime_smt_pair_minutes * M
                    and _session(bull[0]) == _session(bear[0])
                    and bull[1].nq_price < bar.close < bear[1].nq_price):
                    self.smt_lock = RangeLock(bull[1].nq_price, bear[1].nq_price, now, 'DUAL_SMT_CHOP')
        if self._recover(self.smt_lock, now, five_bars, (), False):
            self.smt_lock = None
            self._smt.clear()  # Old divergences cannot relock a cleared range.
            # REGIME_STATE below records the cleared range.

    def _update_seek(self, now, bar, session, five_bars, gaps):
        t = now.time()
        both_london = session.london_swept_asia_high and session.london_swept_asia_low
        if not session.london.is_valid or not time(5) <= t < time(18):
            return
        if both_london and not self._potential_seen:
            self._potential_seen = True
            self.seek_lock = RangeLock(session.london.low, session.london.high, now, 'POTENTIAL_SEEK_AND_DESTROY')
        if time(8, 30) <= t < time(11):
            self._ny_high |= bar.high > session.london.high
            self._ny_low |= bar.low < session.london.low
        if self._ny_high and self._ny_low and not self._confirmed_seen:
            self._confirmed_seen = True
            reason = 'SEEK_AND_DESTROY' if both_london else 'NY_DUAL_RAID'
            self.seek_lock = RangeLock(session.london.low, session.london.high, now, reason)
        if self._recover(self.seek_lock, now, five_bars, gaps, True):
            self.seek_lock = None
            self._ny_high = self._ny_low = False
            self._confirmed_seen = False
            # New raids can lock the range again; historical sweeps cannot.

    def _update_overnight(self, now, bar, s, narrative):
        t = now.time()
        # Revoke recovery if its distinct objective has already delivered or its
        # narrative is no longer active at the opening boundary.
        if self._exhaustion_recovered and (t >= time(18) or t <= time(9, 30)):
            p = getattr(narrative, 'primary_dol', None)
            intact = (p is not None and narrative.id == self._recovery_narrative_id
                      and narrative.can_enter(narrative.direction, now)
                      and (bar.high < p.price if p.direction == Direction.BULLISH else bar.low > p.price))
            if not intact:
                self._exhaustion_recovered = False
                self._recovery_narrative_id = None
        if not (t >= time(18) or t < time(9, 30)):
            return
        if s.asia.is_valid and s.london.is_valid:
            self.overnight_span = max(s.asia.high, s.london.high) - min(s.asia.low, s.london.low)
        levels = [('PDH', s.pdh, Direction.BULLISH), ('PDL', s.pdl, Direction.BEARISH),
                  ('PWH', s.pwh, Direction.BULLISH), ('PWL', s.pwl, Direction.BEARISH)]
        if narrative is not None and cme_trading_date(narrative.source.observed_at) != self._day:
            narrative = None
        p = getattr(narrative, 'primary_dol', None)
        if p is not None and not narrative.terminal and p.timeframe in ('1D', '1W', '4H', '1H'):
            levels.insert(0, (p.label, p.price, p.direction))
        for label, price, direction in levels:
            if price is None:
                continue
            touched = bar.high >= price if direction == Direction.BULLISH else bar.low <= price
            if touched and self._objective is None:
                self._objective = (now, label, price, direction)
                break
        # Recovery is deliberately conservative: prior narrative must already be
        # active, fresh and distinct, after a half-range rebalance before the open.
        if self._objective and self.overnight_span > self.config.overnight_exhaustion_pts and p is not None:
            at, label, price, direction = self._objective
            eq = (max(s.asia.high, s.london.high) + min(s.asia.low, s.london.low)) / 2
            rebalanced = bar.close <= eq if direction == Direction.BULLISH else bar.close >= eq
            if (rebalanced and narrative.can_enter(narrative.direction, now)
                and narrative.source.observed_at > at and p.label != label and p.price != price
                and (bar.high < p.price if p.direction == Direction.BULLISH else bar.low > p.price)):
                self._exhaustion_recovered = True
                self._recovery_narrative_id = narrative.id

    def _update_macro(self, now, bar, evidence):
        if not time(10) <= now.time() < time(11):
            self._macro_generating = False
            return
        self._macro_source |= any(e.kind in ('EXTERNAL_SWEEP', 'HTF_FIRST_TAP') for e in evidence)
        # Remove liquidity once swept, then confirm pivots only on the right bar.
        self._macro_highs = [p for p in self._macro_highs if bar.high <= p]
        self._macro_lows = [p for p in self._macro_lows if bar.low >= p]
        self._macro_bars.append(bar)
        if len(self._macro_bars) == 3:
            a, b, c = self._macro_bars
            if b.high > a.high and b.high > c.high:
                self._macro_highs.append(b.high)
            if b.low < a.low and b.low < c.low:
                self._macro_lows.append(b.low)
        h, l = self._macro_highs[-3:], self._macro_lows[-3:]
        lrl = (len(h) == 3 and h[0] > h[1] > h[2]) or (len(l) == 3 and l[0] < l[1] < l[2])
        self._macro_generating = lrl and not self._macro_gap and not self._macro_source

    def update(self, bar, session, closed, created, smt_events, evidence=(), narrative=None):
        now = to_ny(bar.timestamp)
        if self._day != session.trading_day:
            self._day = session.trading_day
            self._reset_day()
            self._last = None
        if self._last is None or utc(now) - utc(self._last) != M:
            self._run_start = utc(now)
            self._five.clear()
            self._recent.clear()
            self._macro_bars.clear()
            self._macro_highs.clear()
            self._macro_lows.clear()
            self._macro_generating = False
            for lock in (self.seek_lock, self.smt_lock):
                if lock:
                    lock.reset_recovery()
        self._last = now
        self._recent.append(bar)
        five_bars, gaps = self._complete_five(now, closed, created)
        self._update_smt(now, bar, smt_events, five_bars)
        self._update_seek(now, bar, session, five_bars, gaps)
        self._update_overnight(now, bar, session, narrative)
        self._update_macro(now, bar, evidence)
        t = now.time()
        reasons = list(self.calendar_reasons(now))
        if self.seek_lock:
            reasons.append(self.seek_lock.reason)
        if self.config.enable_dual_smt_chop_filter and self.smt_lock:
            reasons.append('DUAL_SMT_CHOP')
        if (self.config.enable_overnight_exhaustion_filter and self._objective
            and self.overnight_span > self.config.overnight_exhaustion_pts
            and not self._exhaustion_recovered and time(9, 30) <= t < time(11)):
            reasons.append('OVERNIGHT_EXHAUSTION_300PTS')
        if time(10) <= t < time(11):
            if self.config.enable_am_5m_fvg_volatility_filter and not self._am_gap:
                reasons.append('NO_5M_FVG_VOLATILITY')
            if self._macro_generating:
                reasons.append('LIQUIDITY_GENERATING_10AM')
        if self._has_10am_red(now.date()) and time(10) <= t < time(11) and not (self._macro_gap and self._macro_pivot):
            reasons.append('WAIT_10AM_DATA_STRUCTURE')
        self.reasons = tuple(reasons)
        # Transition log includes state changes, not one record per minute.
        snap = self.snapshot()
        # Range extrema change frequently overnight; log the frozen span at open.
        signature = {k: v for k, v in snap.items() if k != 'overnight_span'}
        if signature != self._last_snapshot or t == time(9, 30):
            self._last_snapshot = signature
            self._emit(now, 'REGIME_STATE')
        return self.reasons

    def audit_entry(self, now, ifvg, leg):
        """Conservative stop-area chop proxy: repeated overlapping, alternating
        1m closes inside the opposing FVG AFTER the leg extreme, before trigger.
        No future bars are used. This complements the legacy slow-inversion gate.
        """
        f = ifvg.source_fvg
        # The same IFVG may not be re-evaluated against later candles.
        if utc(ifvg.confirmed_at) + (int(f.timeframe[:-1]) - 1) * M != utc(now):
            return None
        start = leg.extreme_at if leg is not None else f.created_at
        window = [b for b in self._recent if utc(start) < utc(b.timestamp) < utc(now)
                  and f.overlapped_by(b)]
        # Restrict to a consecutive run, not unrelated historical revisits.
        run = []
        for b in window:
            if run and utc(b.timestamp) - utc(run[-1].timestamp) != M:
                run = []
            run.append(b)
        sides = [b.close > b.open for b in run if b.close != b.open]
        turns = sum(a != b for a, b in zip(sides, sides[1:]))
        trap = (len(run) >= self.config.regime_trap_min_bars and turns >= 2
                and utc(run[-1].timestamp) + M == utc(now))
        reason = 'LRLR_INVERSION_TRAP' if trap else None
        self._emit(now, 'ENTRY_REGIME_CHECK', ifvg_id=f.id, entry_reason=reason,
                   overlapping_minutes=len(run), direction_changes=turns)
        return reason
