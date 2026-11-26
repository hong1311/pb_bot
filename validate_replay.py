#!/usr/bin/env python3
"""Audit and replay complete paired futures data; never sends live orders."""
import argparse
import hashlib
import csv
from datetime import date, timedelta
import json
import math
from pathlib import Path
import subprocess
import sys

from pb_bot.backtest.validation import profiles, read_annotations, run_replay
from pb_bot.config import BotConfig
from pb_bot.core.calendar import EconomicCalendar
from pb_bot.core.sessions import cme_trading_date
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.data.validation import (audit_pair, load_manifest, read_bars,
                                    read_sessions, requested_sessions, sha256_file, weekday_schedule)


def write_json(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def git_revision():
    root = Path(__file__).resolve().parent
    paths = [root / 'validate_replay.py', *sorted((root / 'pb_bot').rglob('*.py'))]
    hashes = {str(p.relative_to(root)): sha256_file(p) for p in paths}
    result = {'source_tree_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}
    try:
        result['commit'] = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
        result['worktree_dirty'] = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True))
    except (OSError, subprocess.CalledProcessError):
        result['commit'] = None
    return result


def get_folds(sessions, warmup, holdout_start):
    days = [s.trading_day for s in sessions]
    if not 0 <= warmup < len(days):
        raise ValueError('Warmup must leave at least one evaluated session')
    if holdout_start is None:
        return {'FULL_DIAGNOSTIC': (days, set(days[warmup:]))}
    if holdout_start not in days:
        raise ValueError('Holdout start must match a declared trading day')
    cut = days.index(holdout_start)
    if cut <= warmup:
        raise ValueError('Need warmup plus at least one measured development day before holdout')
    return {'DEVELOPMENT': (days[:cut], set(days[warmup:cut])),
            'CHRONOLOGICAL_HOLDOUT': (days[cut-warmup:], set(days[cut:]))}


def read_calendar(path, start, end, coverage_start, coverage_end):
    if coverage_start is None or coverage_end is None or coverage_start > start or coverage_end < end + timedelta(days=1):
        raise ValueError('Declare reviewed calendar coverage from first session through day after final session')
    with path.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not {'date', 'time_ny', 'title', 'impact'}.issubset(reader.fieldnames or []):
            raise ValueError('Missing required news calendar columns')
        for row in reader:
            if row['impact'].strip().upper() not in ('RED', 'ORANGE', 'YELLOW') or not row['title'].strip():
                raise ValueError('Invalid news impact or empty event title')
    return EconomicCalendar.from_csv(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--suite', action='store_true', help='Synthetic diagnostics, not market evidence')
    source.add_argument('--manifest', type=Path, help='Fixed-contract provenance manifest')
    source.add_argument('--exploratory', action='store_true', help='Audit unverified CSVs, never label as qualified market validation')
    parser.add_argument('--nq-csv', type=Path)
    parser.add_argument('--es-csv', type=Path)
    parser.add_argument('--sessions', type=Path, help='Reviewed regular CME session schedule CSV')
    parser.add_argument('--calendar', type=Path)
    parser.add_argument('--calendar-coverage-start', type=date.fromisoformat)
    parser.add_argument('--calendar-coverage-end', type=date.fromisoformat)
    parser.add_argument('--holdout-start', type=date.fromisoformat)
    parser.add_argument('--warmup-sessions', type=int, default=None)
    parser.add_argument('--audit-only', action='store_true')
    parser.add_argument('--matrix', action='store_true', help='Run all seven gate profiles rather than OBSERVE and ALL_STRICT')
    parser.add_argument('--slippage-ticks', type=int, nargs='+', default=[1])
    parser.add_argument('--commission-per-side', type=float, default=0.62)
    parser.add_argument('--legacy-close', action='store_true', help='Diagnostic comparison with legacy same-close fills (never exchange-validation fill model)')
    parser.add_argument('--annotations', type=Path, help='CSV of independently labeled expected setup windows')
    parser.add_argument('--details', type=Path, help='Optional JSONL event output directory; paths must not contain secrets')
    parser.add_argument('--out', type=Path, required=True, help='JSON validation report')
    args = parser.parse_args()
    if args.manifest and (args.nq_csv or args.es_csv or args.legacy_close):
        parser.error('Manifest replay uses manifest paths and next-open execution only')
    if args.exploratory and not (args.nq_csv and args.es_csv):
        parser.error('--exploratory requires both --nq-csv and --es-csv')
    if not args.exploratory and (args.nq_csv or args.es_csv):
        parser.error('Explicit CSV paths require --exploratory')
    if any(t < 0 for t in args.slippage_ticks) or not math.isfinite(args.commission_per_side) or args.commission_per_side < 0:
        parser.error('Execution costs must be finite and nonnegative')
    if len(set(args.slippage_ticks)) != len(args.slippage_ticks):
        parser.error('Duplicate cost scenario')
    report = {'format_version': 1, 'code': git_revision(), 'status': 'NOT_RUN',
              'qualification': 'UNVERIFIED_RESEARCH_ONLY', 'warnings': [], 'runs': []}
    try:
        if args.manifest:
            if not args.sessions:
                raise ValueError('--manifest requires --sessions, reviewed against the exchange calendar')
            manifest, nq, es = load_manifest(args.manifest)
            report['data'] = {'kind': 'DECLARED_FIXED_CONTRACT_FUTURES', 'manifest': manifest,
                              'manifest_sha256': sha256_file(args.manifest)}
        elif args.suite:
            nq, es = generate_synthetic_multi_day_suite()
            report['data'] = {'kind': 'SYNTHETIC_3_DAY_SUITE'}
            report['warnings'].append('Synthetic fixture is not evidence of market profitability or strategy fidelity.')
        else:
            nq = read_bars(args.nq_csv, 'NQ', tick_check=False)
            es = read_bars(args.es_csv, 'ES', tick_check=False)
            report['data'] = {'kind': 'UNVERIFIED_CSV', 'nq_sha256': sha256_file(args.nq_csv), 'es_sha256': sha256_file(args.es_csv)}
            report['warnings'].append('CSV provenance and price basis are not authenticated.')
        sessions = read_sessions(args.sessions) if args.sessions else weekday_schedule(nq + es)
        if args.manifest and manifest['source'] == 'databento':
            requested = requested_sessions(manifest['requested_start_utc'], manifest['requested_end_utc'])
            if sessions[0] != requested[0] or sessions[-1] != requested[-1]:
                raise ValueError('Reviewed schedule endpoints do not match the fixed-contract request')
            if not set(sessions).issubset(requested):
                raise ValueError('Reviewed schedule contains a session outside the request')
            skipped = sorted(str(s.trading_day) for s in requested if s not in sessions)
            report['operator_excluded_weekdays'] = skipped
            if skipped:
                report['warnings'].append('Verify excluded dates were exchange closures, not missing/losing sessions.')
        report['schedule'] = {'kind': 'USER_REVIEWED' if args.sessions else 'INFERRED_REGULAR_WEEKDAYS_DIAGNOSTIC_ONLY',
                              'sha256': sha256_file(args.sessions) if args.sessions else None,
                              'sessions': [{'trading_day': str(s.trading_day), 'open': s.start.isoformat(), 'close': s.end.isoformat()} for s in sessions]}
        report['audit'] = audit_pair(nq, es, sessions)
        if not report['audit']['passed']:
            raise ValueError('Minute coverage failed: no replay. Do not forward-fill, silently drop timestamps, or declare missing minutes as closed hours.')
        if args.audit_only:
            report['status'] = 'AUDIT_PASSED'
            write_json(args.out, report)
            print(f'Audit passed: {report["audit"]["paired_minutes"]:,} paired minutes. Report: {args.out}')
            return 0
        if args.manifest and (args.holdout_start is None or args.calendar is None):
            raise ValueError('Exchange-data replay requires --holdout-start and a reviewed --calendar')
        warmup = args.warmup_sessions if args.warmup_sessions is not None else (5 if args.manifest else 0)
        if args.manifest and warmup < 5:
            raise ValueError('Require at least five warmup sessions for exchange-data replay; ten or more recommended')
        folds = get_folds(sessions, warmup, args.holdout_start)
        calendar = None
        if args.calendar:
            calendar = read_calendar(args.calendar, sessions[0].trading_day, sessions[-1].trading_day,
                                     args.calendar_coverage_start, args.calendar_coverage_end)
            report['news'] = {'sha256': sha256_file(args.calendar), 'coverage_start': str(args.calendar_coverage_start),
                              'coverage_end': str(args.calendar_coverage_end), 'event_count_red_or_orange': len(calendar.events),
                              'coverage_is_operator_attested': True}
        else:
            report['warnings'].append('No economic calendar: news restrictions not validated.')
        annotations = read_annotations(args.annotations) if args.annotations else []
        if args.annotations:
            report['annotations_sha256'] = sha256_file(args.annotations)
        else:
            report['warnings'].append('No independent manual PB setup labels; strategy fidelity not established.')
        report['warnings'] += ['One fixed contract pair per report; do not splice rollovers or compare adjusted data.',
            'Signal stream is NQ; default simulated execution uses MNQ point value but NQ prices (basis/queue and micro liquidity unmodeled).',
            'Next-open prices, fixed slippage and stop-first OHLC fills are assumptions, not tick-level executable quotes.',
            'Holdout is chronological, not proof of independence if parameters were chosen after seeing those dates.',
            'Regime and structure numerical thresholds remain research approximations; no live-trading approval.']
        cfg = BotConfig()
        all_profiles = profiles(cfg)
        selected = all_profiles if args.matrix else {p: all_profiles[p] for p in ('OBSERVE', 'ALL_STRICT')}
        report['warmup_sessions'] = warmup
        for fold, (available, evaluated) in folds.items():
            available = set(available)
            a = [b for b in nq if cme_trading_date(b.timestamp) in available]
            b = [bar for bar in es if cme_trading_date(bar.timestamp) in available]
            for name, config in selected.items():
                for ticks in args.slippage_ticks:
                    print(f'{fold} | {name} | {ticks} tick slippage', flush=True)
                    detail = args.details / fold / name / f'{ticks}ticks' if args.details else None
                    result = run_replay(a, b, config, calendar, evaluated_days=evaluated, slippage_ticks=ticks,
                                        commission_per_side=args.commission_per_side, next_open=not args.legacy_close,
                                        annotations=annotations, detail_path=detail)
                    report['runs'].append({'fold': fold, 'profile': name, **result})
                    print(f'  signals={result["signals"]}, closed={result["trades"]}, PnL=${result["net_pnl"]:+,.2f}', flush=True)
        report['status'] = 'REPLAY_COMPLETED'
        if annotations and not any(r['annotations'] for r in report['runs']):
            report['warnings'].append('No labels were inside the evaluated periods.')
        if args.manifest:
            report['qualification'] = 'DECLARED_EXCHANGE_DATA_PASSED_MINUTE_AUDIT_RESEARCH_ONLY'
        write_json(args.out, report)
        print(f'Report: {args.out}')
        return 0
    except (ValueError, OSError, TypeError, KeyError, OverflowError) as exc:
        report['status'] = 'REJECTED'
        report['error'] = str(exc)
        write_json(args.out, report)
        print(f'Validation rejected: {exc}\nReport: {args.out}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
