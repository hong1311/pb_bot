import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from pb_bot.core.models import Bar
from pb_bot.core.sessions import NY_TZ, to_ny


def _resolve_col(fieldnames: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    lower_map = {name.strip().lower(): name for name in fieldnames}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    return None


def load_csv_bars(
    csv_path: Path,
    symbol: str,
    timeframe: str = "1m",
    ts_col: Optional[str] = None,
) -> List[Bar]:
    """Auto-detecting OHLCV CSV loader (TradingView, Databento, NinjaTrader, generic ISO/epoch)."""
    bars: List[Bar] = []
    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return bars

        c_ts = ts_col or _resolve_col(
            reader.fieldnames, ("timestamp", "time", "ts_event", "datetime", "date")
        )
        c_open = _resolve_col(reader.fieldnames, ("open", "o"))
        c_high = _resolve_col(reader.fieldnames, ("high", "h"))
        c_low = _resolve_col(reader.fieldnames, ("low", "l"))
        c_close = _resolve_col(reader.fieldnames, ("close", "c"))
        c_vol = _resolve_col(reader.fieldnames, ("volume", "vol", "v"))

        if not all((c_ts, c_open, c_high, c_low, c_close)):
            raise ValueError(f"Missing required OHLC/timestamp columns in {csv_path}: {reader.fieldnames}")

        for row in reader:
            raw_ts = row[c_ts].strip()
            if raw_ts.isdigit():
                val = int(raw_ts)
                # Handle nanoseconds / milliseconds / seconds epoch timestamps
                if val > 10**15:
                    dt = datetime.fromtimestamp(val / 1e9, tz=NY_TZ)
                elif val > 10**12:
                    dt = datetime.fromtimestamp(val / 1e3, tz=NY_TZ)
                else:
                    dt = datetime.fromtimestamp(val, tz=NY_TZ)
            else:
                dt = to_ny(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))

            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=dt,
                    open=float(row[c_open]),
                    high=float(row[c_high]),
                    low=float(row[c_low]),
                    close=float(row[c_close]),
                    volume=float(row[c_vol]) if (c_vol and row.get(c_vol)) else 0.0,
                )
            )
    bars.sort(key=lambda b: b.timestamp)
    return bars


def _build_session_bars(
    start_date: datetime,
    mode: str,
    nq_start: float,
    es_start: float,
    total_minutes: int = 18 * 60,
) -> Tuple[List[Bar], List[Bar], float, float]:
    nq_bars: List[Bar] = []
    es_bars: List[Bar] = []
    nq_price = nq_start
    es_price = es_start

    for i in range(total_minutes):
        ts = start_date + timedelta(minutes=i)
        h, m = ts.hour, ts.minute
        nq_delta = 0.5 if (i % 2 == 0) else -0.5
        es_delta = 0.25 if (i % 2 == 0) else -0.25

        if mode == "BULLISH_DAY":
            if h == 20 and m < 20:
                nq_delta, es_delta = 0.50, 0.25
            elif h == 21 or (h == 22 and m < 40):
                nq_delta, es_delta = -0.75, -0.25
            elif (h == 2 and m >= 30) or (h == 3 and m < 30):
                nq_delta, es_delta = -1.00, -0.35
            elif (h == 3 and m >= 30) or h == 4:
                nq_delta, es_delta = 0.85, 0.25
            elif h == 5 and m < 10:
                nq_delta, es_delta = -0.50, -0.15
            elif h == 9 and 30 <= m <= 41:
                nq_delta = -2.75 if m < 38 else -0.75
                es_delta = -1.75
            elif (h == 9 and m >= 42) or (h == 10 and m <= 35):
                nq_delta, es_delta = 2.50, 0.55

        elif mode == "BEARISH_DAY":
            if h == 20 and m < 20:
                nq_delta, es_delta = -0.50, -0.25
            elif h == 21 or (h == 22 and m < 40):
                nq_delta, es_delta = 0.75, 0.25
            elif (h == 2 and m >= 30) or (h == 3 and m < 30):
                nq_delta, es_delta = 1.00, 0.35
            elif (h == 3 and m >= 30) or h == 4:
                nq_delta, es_delta = -0.85, -0.25
            elif h == 5 and m < 10:
                nq_delta, es_delta = 0.50, 0.15
            elif h == 9 and 30 <= m <= 41:
                nq_delta = 2.75 if m < 38 else 0.75
                es_delta = 1.75
            elif (h == 9 and m >= 42) or (h == 10 and m <= 35):
                nq_delta, es_delta = -2.50, -0.55

        elif mode == "SEEK_AND_DESTROY":
            # Tight London range, then violent pre-NY whipsaw sweeping BOTH London High & Low and returning inside
            if h == 2 and m < 30:
                nq_delta, es_delta = 0.50, 0.15
            elif h == 3 and m < 30:
                nq_delta, es_delta = -0.50, -0.15
            elif h == 8 and 30 <= m < 45:
                nq_delta, es_delta = 3.00, 0.90     # Sweeps London High
            elif h == 8 and 45 <= m < 60:
                nq_delta, es_delta = -6.00, -1.80   # Sweeps London Low
            elif h == 9 and 0 <= m < 25:
                nq_delta, es_delta = 1.80, 0.55     # Whips back inside London range
            elif h == 9 and 30 <= m <= 40:
                nq_delta, es_delta = -2.00, -0.60
            elif h == 9 and 41 <= m <= 55:
                nq_delta, es_delta = 2.25, 0.65

        nq_open = nq_price
        nq_close = round((nq_open + nq_delta) * 4) / 4
        nq_high = max(nq_open, nq_close) + 1.00
        nq_low = min(nq_open, nq_close) - 1.00
        nq_price = nq_close

        es_open = es_price
        es_close = round((es_open + es_delta) * 4) / 4
        es_high = max(es_open, es_close) + 0.50
        es_low = min(es_open, es_close) - 0.50
        es_price = es_close

        nq_bars.append(Bar("NQ", "1m", ts, nq_open, nq_high, nq_low, nq_close, 100.0))
        es_bars.append(Bar("ES", "1m", ts, es_open, es_high, es_low, es_close, 250.0))

    return nq_bars, es_bars, nq_price, es_price


def generate_synthetic_ny_session(
    start_date: datetime = datetime(2025, 6, 24, 18, 0, tzinfo=NY_TZ),
) -> Tuple[List[Bar], List[Bar]]:
    nq_bars, es_bars, _, _ = _build_session_bars(start_date, "BULLISH_DAY", 20_070.0, 5_820.0)
    return nq_bars, es_bars


def generate_synthetic_multi_day_suite(
    start_date: datetime = datetime(2025, 6, 24, 18, 0, tzinfo=NY_TZ),
) -> Tuple[List[Bar], List[Bar]]:
    """Generates a 3-day CME stress-test suite:
      - Day 1: Bullish NY expansion (should execute bullish trade)
      - Day 2: Bearish NY expansion (should execute bearish trade)
      - Day 3: Seek & Destroy two-sided whipsaw (must filter out with 0 trades)
    """
    all_nq: List[Bar] = []
    all_es: List[Bar] = []
    nq_p, es_p = 20_070.0, 5_820.0
    scenarios = ("BULLISH_DAY", "BEARISH_DAY", "SEEK_AND_DESTROY")

    for day_idx, mode in enumerate(scenarios):
        day_start = start_date + timedelta(days=day_idx)
        nq_d, es_d, nq_p, es_p = _build_session_bars(
            day_start, mode, nq_p, es_p, total_minutes=23 * 60
        )
        all_nq.extend(nq_d)
        all_es.extend(es_d)

    return all_nq, all_es


def sync_dual_feed(nq_bars: List[Bar], es_bars: List[Bar]) -> Iterator[Tuple[Bar, Bar]]:
    """Yields timestamp-aligned (NQ, ES) 1m bar pairs strictly in chronological order."""
    es_by_ts = {to_ny(b.timestamp): b for b in es_bars}
    for nq_bar in nq_bars:
        ts = to_ny(nq_bar.timestamp)
        es_bar = es_by_ts.get(ts)
        if es_bar is not None:
            yield nq_bar, es_bar
