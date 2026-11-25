from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from pb_bot.core.models import Bar
from pb_bot.core.sessions import to_ny

SUPPORTED_TIMEFRAMES = ("1m", "2m", "3m", "4m", "5m", "15m", "30m", "1H", "4H", "1D")

_MINUTE_MAP = {
    "1m": 1,
    "2m": 2,
    "3m": 3,
    "4m": 4,
    "5m": 5,
    "15m": 15,
    "30m": 30,
}


def bucket_start(ts_ny: datetime, timeframe: str) -> datetime:
    """Compute exact NY-aligned candle open timestamp for a given timeframe.

    4H bars align strictly to 02:00, 06:00, 10:00, 14:00, 18:00, 22:00 NY
    (matching the 10:00 AM and 2:00 PM 4H candle opens taught in PB).
    1D bars align to the 18:00 NY CME Globex session open.
    """
    ts = to_ny(ts_ny).replace(second=0, microsecond=0)

    if timeframe in _MINUTE_MAP:
        step = _MINUTE_MAP[timeframe]
        return ts.replace(minute=(ts.minute // step) * step)

    if timeframe == "1H":
        return ts.replace(minute=0)

    if timeframe == "4H":
        base_hour = ts.replace(minute=0)
        offset_hours = (base_hour.hour - 2) % 4
        return base_hour - timedelta(hours=offset_hours)

    if timeframe == "1D":
        anchor = ts.replace(hour=18, minute=0)
        if ts.hour < 18:
            anchor -= timedelta(days=1)
        return anchor

    raise ValueError(f"Unsupported timeframe: {timeframe}")


class MultiTimeframeResampler:
    """Zero-lookahead streaming 1m -> multi-TF bar builder."""

    def __init__(self, symbol: str, timeframes: Sequence[str] = SUPPORTED_TIMEFRAMES) -> None:
        self.symbol = symbol
        self.timeframes = tuple(timeframes)
        self._in_progress: Dict[str, Optional[Bar]] = {tf: None for tf in self.timeframes}
        self.history: Dict[str, List[Bar]] = {tf: [] for tf in self.timeframes}

    def update(self, bar_1m: Bar) -> Dict[str, List[Bar]]:
        """Ingest one closed 1m bar; returns any bars across all TFs that closed on this tick."""
        ts = to_ny(bar_1m.timestamp)
        next_min = ts + timedelta(minutes=1)
        is_cme_close = ts.hour == 16 and ts.minute == 59
        closed_now: Dict[str, List[Bar]] = {}

        for tf in self.timeframes:
            if tf == "1m":
                normalized = Bar(
                    symbol=self.symbol,
                    timeframe="1m",
                    timestamp=ts,
                    open=bar_1m.open,
                    high=bar_1m.high,
                    low=bar_1m.low,
                    close=bar_1m.close,
                    volume=bar_1m.volume,
                )
                self.history["1m"].append(normalized)
                closed_now["1m"] = [normalized]
                continue

            b_start = bucket_start(ts, tf)
            active = self._in_progress[tf]

            # Flush stale bar if a data gap skipped its natural closing minute
            if active is not None and active.timestamp != b_start:
                self.history[tf].append(active)
                closed_now.setdefault(tf, []).append(active)
                active = None

            if active is None:
                active = Bar(
                    symbol=self.symbol,
                    timeframe=tf,
                    timestamp=b_start,
                    open=bar_1m.open,
                    high=bar_1m.high,
                    low=bar_1m.low,
                    close=bar_1m.close,
                    volume=bar_1m.volume,
                )
            else:
                active = Bar(
                    symbol=self.symbol,
                    timeframe=tf,
                    timestamp=active.timestamp,
                    open=active.open,
                    high=max(active.high, bar_1m.high),
                    low=min(active.low, bar_1m.low),
                    close=bar_1m.close,
                    volume=active.volume + bar_1m.volume,
                )

            # Close immediately if this 1m bar is the final minute of the bucket
            next_bucket = bucket_start(next_min, tf)
            closes_bucket = (next_bucket != b_start) or (is_cme_close and tf in ("4H", "1D"))

            if closes_bucket:
                self.history[tf].append(active)
                closed_now.setdefault(tf, []).append(active)
                self._in_progress[tf] = None
            else:
                self._in_progress[tf] = active

        return closed_now
