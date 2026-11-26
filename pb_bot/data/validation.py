"""Strict minute-bar audit for research. Never fill in missing observations.

The manifest and exchange schedule are operator-supplied declarations, not
independent proof of provenance. Only fixed, unadjusted NQ/ES contract pairs
and regular complete Globex sessions are accepted by the validation runner.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import csv
import hashlib
import json
import math
import re

from pb_bot.core.models import Bar
from pb_bot.core.sessions import NY_TZ, cme_trading_date

UTC = timezone.utc
MINUTE = timedelta(minutes=1)
CONTRACT = re.compile(r'^(NQ|ES)([HMUZ])(\d{1,4})$')


def utc_minute(value):
    ts = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError('Timestamp must include UTC offset')
    ts = ts.astimezone(UTC)
    if ts.second or ts.microsecond:
        raise ValueError('Timestamp is not aligned to a minute')
    return ts


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fixed_pair(nq, es):
    a, b = CONTRACT.fullmatch(nq), CONTRACT.fullmatch(es)
    if not a or not b or a.group(1) != 'NQ' or b.group(1) != 'ES' or a.groups()[1:] != b.groups()[1:]:
        raise ValueError('Use matching fixed NQ/ES quarterly contracts, e.g. NQZ6 and ESZ6; not continuous symbols')


@dataclass(frozen=True)
class Session:
    trading_day: date
    start: datetime
    end: datetime  # exclusive


def regular_session(day):
    if day.weekday() >= 5:
        raise ValueError(f'Not a regular weekday session: {day}')
    return Session(day, datetime.combine(day - timedelta(days=1), time(18), NY_TZ).astimezone(UTC),
                   datetime.combine(day, time(17), NY_TZ).astimezone(UTC))


def read_sessions(path):
    """Schedule must be reviewed against exchange holidays and closures.

    Early-close or split sessions are deliberately not supported by the current
    resampler, which closes daily/4H candles at 16:59 NY.
    """
    sessions = []
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            day = date.fromisoformat(row['trading_day'])
            s = Session(day, utc_minute(row['open']), utc_minute(row['close']))
            if s != regular_session(day):
                raise ValueError(f'Non-regular session {day}: segment around early closes/holidays; no replay')
            if sessions and day <= sessions[-1].trading_day:
                raise ValueError('Session dates must be strictly increasing')
            sessions.append(s)
    if not sessions:
        raise ValueError('No declared sessions')
    return sessions


def regular_weekday_schedule(d, last):
    """Candidate only; the operator must review exchange holidays."""
    sessions = []
    while d <= last:
        if d.weekday() < 5:
            sessions.append(regular_session(d))
        d += timedelta(days=1)
    return sessions


def weekday_schedule(bars):
    """Diagnostic only: regular weekdays inferred from observed endpoints."""
    if not bars:
        return []
    return regular_weekday_schedule(min(cme_trading_date(b.timestamp) for b in bars),
                                    max(cme_trading_date(b.timestamp) for b in bars))


def requested_sessions(start, end):
    """Require full regular-session endpoints for a fixed-contract request."""
    a, b = utc_minute(start), utc_minute(end)
    first, last = cme_trading_date(a), cme_trading_date(b)
    if b <= a or regular_session(first).start != a or regular_session(last).end != b:
        raise ValueError('Request must start at 18:00 NY and end at 17:00 NY on complete regular sessions, with explicit offsets')
    return regular_weekday_schedule(first, last)


def read_bars(path, root, contract=None, expected_id=None, tick_check=True):
    """Require ISO timestamps with offsets and one unique row per minute.

    Reject unsorted, malformed, mixed-contract or off-tick input; no deduplication.
    Exploratory exports may omit symbol/ID but are never authenticated futures.
    """
    bars = []
    previous = None
    ids = set()
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        required = {'timestamp', 'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f'{path}: missing required columns {sorted(required)}')
        if contract and 'symbol' not in reader.fieldnames:
            raise ValueError(f'{path}: fixed-contract export must retain symbol column')
        if expected_id is not None and 'instrument_id' not in reader.fieldnames:
            raise ValueError(f'{path}: missing instrument_id column')
        for line, row in enumerate(reader, 2):
            try:
                ts = utc_minute(row['timestamp'])
                if previous is not None and ts <= previous:
                    raise ValueError('duplicate or non-increasing UTC timestamp')
                prices = [float(row[k]) for k in ('open', 'high', 'low', 'close')]
                op, hi, lo, cl = prices
                vol = float(row['volume'])
                if not all(math.isfinite(x) for x in (*prices, vol)) or lo <= 0 or vol < 0 or not lo <= min(op, cl) <= max(op, cl) <= hi:
                    raise ValueError('invalid OHLCV geometry or non-finite value')
                if tick_check and any(abs(p * 4 - round(p * 4)) > 1e-6 for p in prices):
                    raise ValueError('price not on 0.25-point futures tick')
                if contract and (row.get('symbol') or '').strip() != contract:
                    raise ValueError('wrong or mixed contract symbol')
                if row.get('instrument_id') not in (None, ''):
                    ids.add(int(row['instrument_id']))
                elif expected_id is not None:
                    raise ValueError('missing instrument_id')
                if contract and vol != int(vol):
                    raise ValueError('non-integer futures volume')
                bars.append(Bar(root, '1m', ts.astimezone(NY_TZ), op, hi, lo, cl, vol))
                previous = ts
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise ValueError(f'{path}:{line}: {exc}') from exc
    if not bars:
        raise ValueError(f'{path}: empty feed')
    if len(ids) > 1 or (expected_id is not None and ids != {int(expected_id)}):
        raise ValueError(f'{path}: unexpected/multiple instrument IDs {sorted(ids)}')
    return bars


def load_manifest(path):
    path = Path(path)
    m = json.loads(path.read_text(encoding='utf-8'))
    if m.get('source') not in ('databento', 'exchange_export'):
        raise ValueError('Validation requires declared exchange data, not Yahoo or index proxies')
    if m.get('schema') != 'ohlcv-1m' or m.get('timestamp_convention') != 'bar_open' or m.get('adjustment') != 'none' or m.get('stype_in') != 'raw_symbol':
        raise ValueError('Require unadjusted raw-symbol, bar-open OHLCV-1m data')
    if m['source'] == 'databento':
        if m.get('dataset') != 'GLBX.MDP3':
            raise ValueError('Databento manifest must declare GLBX.MDP3')
        requested_sessions(m['requested_start_utc'], m['requested_end_utc'])
    nq, es = m['nq'], m['es']
    fixed_pair(nq['contract'], es['contract'])
    feeds = []
    for root, leg in [('NQ', nq), ('ES', es)]:
        p = path.parent / leg['csv']
        if m['source'] == 'databento' and leg.get('instrument_id') is None:
            raise ValueError('Databento instrument_id required for each leg')
        if sha256_file(p) != leg.get('sha256'):
            raise ValueError(f'{root}: CSV SHA-256 mismatch')
        feeds.append(read_bars(p, root, leg['contract'], leg.get('instrument_id')))
    if nq.get('instrument_id') is not None and nq.get('instrument_id') == es.get('instrument_id'):
        raise ValueError('NQ and ES cannot share the same instrument ID')
    return m, feeds[0], feeds[1]


def audit_pair(nq_bars, es_bars, sessions):
    """Compare each feed independently to the declared minute grid, in UTC.

    Even a shared missing minute is a failure. No-trade intervals may be
    legitimate but cannot be distinguished from missing data here.
    """
    expected = set()
    for s in sessions:
        t = s.start
        while t < s.end:
            expected.add(t)
            t += MINUTE
    def inspect(bars):
        seen = [b.timestamp.astimezone(UTC) for b in bars]
        counts = Counter(seen)
        return set(seen), sum(n - 1 for n in counts.values()), sum(a >= b for a, b in zip(seen, seen[1:]))
    n, nd, nu = inspect(nq_bars)
    e, ed, eu = inspect(es_bars)
    missing_n, missing_e = expected - n, expected - e
    extra_n, extra_e = n - expected, e - expected
    per_session = []
    for s in sessions:
        grid = {s.start + i * MINUTE for i in range((s.end - s.start) // MINUTE)}
        per_session.append({'trading_day': str(s.trading_day), 'expected': len(grid),
                            'nq_missing': len(grid - n), 'es_missing': len(grid - e),
                            'shared_missing': len(grid - (n | e))})
    return {'expected_minutes': len(expected), 'nq_rows': len(nq_bars), 'es_rows': len(es_bars),
            'paired_minutes': len(n & e), 'nq_only': len(n - e), 'es_only': len(e - n),
            'nq_missing': len(missing_n), 'es_missing': len(missing_e),
            'shared_missing': len(expected - (n | e)), 'nq_extra': len(extra_n), 'es_extra': len(extra_e),
            'duplicates': nd + ed, 'non_increasing': nu + eu,
            'missing_examples_utc': [x.isoformat() for x in sorted(missing_n | missing_e)[:12]],
            'sessions': per_session,
            'passed': bool(expected and not (missing_n or missing_e or extra_n or extra_e or nd or ed or nu or eu))}
