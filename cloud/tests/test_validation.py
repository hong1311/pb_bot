"""V2.6 validation harness regression tests; no network or brokerage calls."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import json
import unittest

from pb_bot.backtest.validation import NextOpenBroker, compare_annotations, profiles, read_annotations, run_replay
from pb_bot.config import BotConfig, NarrativeMode, RegimeMode, StructureMode
from pb_bot.core.models import Bar, Direction, OrderRequest
from pb_bot.core.sessions import NY_TZ
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.data.validation import (audit_pair, fixed_pair, load_manifest, read_bars, read_sessions,
                                    regular_session, requested_sessions, sha256_file, utc_minute, weekday_schedule)
from validate_replay import get_folds, read_calendar
from fetch_data import write_fixed_contract_export

UTC = timezone.utc
T = datetime(2025, 6, 25, 9, 40, tzinfo=NY_TZ)
M = timedelta(minutes=1)


def bar(ts=T, o=100, h=103, l=98, c=101, symbol='NQ'):
    return Bar(symbol, '1m', ts, o, h, l, c, 100)


def order(direction=Direction.BULLISH, qty=10, ts=T, **metadata):
    long = direction == Direction.BULLISH
    return OrderRequest('MNQ', direction, qty, 'MARKET', 100, 95 if long else 105,
                        115 if long else 85, 'test', ts, metadata)


def write_rows(path, rows):
    with path.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'symbol', 'instrument_id', 'open', 'high', 'low', 'close', 'volume'])
        writer.writerows(rows)


def csvrow(ts=T.isoformat(), symbol='NQM5', iid=101, op=100, hi=102, lo=99, close=101, vol=5):
    return [ts, symbol, iid, op, hi, lo, close, vol]


class DataAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fixed_matching_raw_contracts(self):
        fixed_pair('NQZ6', 'ESZ6')
        for a, b in [('NQ.v.0', 'ES.v.0'), ('NQZ6', 'ESH7'), ('MNQZ6', 'ESZ6'), ('ESZ6', 'NQZ6')]:
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                fixed_pair(a, b)

    def test_timezone_required_and_minute_aligned(self):
        self.assertEqual(utc_minute(T.isoformat()), T.astimezone(UTC))
        for ts in ['2025-06-25T09:40:00', '2025-06-25T13:40:01Z']:
            with self.assertRaises(ValueError): utc_minute(ts)

    def test_equivalent_timezone_duplicate_rejected(self):
        p = self.root/'nq.csv'
        write_rows(p, [csvrow(), csvrow(T.astimezone(UTC).isoformat())])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            read_bars(p, 'NQ', 'NQM5', 101)

    def test_out_of_order_rejected(self):
        p = self.root/'nq.csv'
        write_rows(p, [csvrow((T+M).isoformat()), csvrow()])
        with self.assertRaisesRegex(ValueError, 'non-increasing'):
            read_bars(p, 'NQ', 'NQM5', 101)

    def test_bad_geometry_prices_volume_rejected(self):
        p = self.root/'nq.csv'
        for row in [csvrow(hi=98), csvrow(op=100.1), csvrow(vol=-1), csvrow(vol=1.5), csvrow(close='NaN')]:
            with self.subTest(row=row):
                write_rows(p, [row])
                with self.assertRaises(ValueError): read_bars(p, 'NQ', 'NQM5', 101)

    def test_mixed_contract_or_instrument_rejected(self):
        p = self.root/'nq.csv'
        for row in [csvrow((T+M).isoformat(), symbol='NQU5'), csvrow((T+M).isoformat(), iid=102)]:
            with self.subTest(row=row):
                write_rows(p, [csvrow(), row])
                with self.assertRaises(ValueError): read_bars(p, 'NQ', 'NQM5', 101)

    def test_regular_session_dst_grid(self):
        spring = regular_session(date(2025,3,7)); summer = regular_session(date(2025,3,10))
        self.assertEqual(spring.start.hour,23); self.assertEqual(summer.start.hour,22)
        self.assertEqual((summer.end-summer.start)//M,1380)
        for s in (spring, summer):
            bars = [bar((s.start+i*M).astimezone(NY_TZ)) for i in range(1380)]
            self.assertTrue(audit_pair(bars,bars,[s])['passed'])

    def test_shared_missing_minute_and_partial_feed_detected(self):
        s = regular_session(T.date())
        bars = [bar((s.start+i*M).astimezone(NY_TZ)) for i in range(1380)]
        both = bars[:5]+bars[6:]
        r = audit_pair(both,both,[s]); self.assertFalse(r['passed']); self.assertEqual(r['shared_missing'],1)
        r = audit_pair(bars,both,[s]); self.assertFalse(r['passed']); self.assertEqual(r['nq_only'],1)
        r = audit_pair(bars+[bar(s.end.astimezone(NY_TZ))],bars,[s]); self.assertEqual(r['nq_extra'],1)
        self.assertFalse(r['passed'])

    def test_early_close_rejected(self):
        s = regular_session(T.date()); p = self.root/'schedule.csv'
        p.write_text(f'trading_day,open,close\n{s.trading_day},{s.start.isoformat()},{(s.end-60*M).isoformat()}\n')
        with self.assertRaisesRegex(ValueError, 'Non-regular'): read_sessions(p)

    def test_manifest_hash_and_identity(self):
        session = regular_session(T.date())
        manifest = {'source':'databento','dataset':'GLBX.MDP3','schema':'ohlcv-1m','timestamp_convention':'bar_open','adjustment':'none','stype_in':'raw_symbol',
                    'requested_start_utc':session.start.isoformat(),'requested_end_utc':session.end.isoformat()}
        for k,s,i in [('nq','NQM5',101),('es','ESM5',102)]:
            p = self.root/f'{k}.csv'; write_rows(p,[csvrow(symbol=s,iid=i)])
            manifest[k] = {'csv':p.name,'contract':s,'instrument_id':i,'sha256':sha256_file(p)}
        path=self.root/'manifest.json';path.write_text(json.dumps(manifest))
        _,n,e=load_manifest(path);self.assertEqual((n[0].symbol,e[0].symbol),('NQ','ES'))
        (self.root/'nq.csv').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'SHA-256'): load_manifest(path)

    def test_mocked_fixed_contract_export_round_trip(self):
        import pandas as pd
        df = pd.DataFrame([dict(symbol='NQM5',instrument_id=101,open=100,high=102,low=99,close=101,volume=5),
                           dict(symbol='ESM5',instrument_id=102,open=100,high=102,low=99,close=101,volume=10)],
                          index=pd.DatetimeIndex([T.astimezone(UTC)]*2))
        session=regular_session(T.date())
        write_fixed_contract_export(df,self.root,'NQM5','ESM5',session.start.isoformat(),session.end.isoformat())
        m,n,e=load_manifest(self.root/'manifest.json')
        self.assertEqual(m['nq']['instrument_id'],101); self.assertEqual(len(n),1); self.assertEqual(len(e),1)
        self.assertTrue((self.root/'sessions_REVIEW_REQUIRED.csv').exists())
        with self.assertRaisesRegex(ValueError,'overwrite'):
            write_fixed_contract_export(df,self.root,'NQM5','ESM5',session.start.isoformat(),session.end.isoformat())

    def test_requested_schedule_not_inferred_from_available_bars(self):
        s1=regular_session(date(2025,6,23));s2=regular_session(date(2025,6,25))
        requested=requested_sessions(s1.start.isoformat(),s2.end.isoformat())
        self.assertEqual(len(requested),3)
        with self.assertRaises(ValueError): requested_sessions((s1.start+M).isoformat(),s2.end.isoformat())
        data=[bar((s2.start+i*M).astimezone(NY_TZ)) for i in range(1380)]
        self.assertEqual(audit_pair(data,data,requested)['shared_missing'],2760)

    def test_calendar_coverage_claim_must_include_next_day(self):
        path=self.root/'calendar.csv'; path.write_text('date,time_ny,title,impact\n')
        with self.assertRaisesRegex(ValueError,'coverage'):
            read_calendar(path,T.date(),T.date(),T.date(),T.date())
        self.assertEqual(read_calendar(path,T.date(),T.date(),T.date(),T.date()+timedelta(days=1)).events,[])


class ExecutionTests(unittest.TestCase):
    def make(self,ticks=1,fees=.62): return NextOpenBroker(BotConfig(),ticks,fees)

    def test_market_entry_is_queued_and_uses_next_open(self):
        broker=self.make(); broker.submit_order(order())
        self.assertIsNone(broker.position)
        self.assertIsNone(broker.process_bar(bar(T+M,o=101,h=104,l=99,c=102)))
        self.assertEqual(broker.position.entry_price,101.25)
        self.assertEqual(broker.position.opened_at,T+M)
        self.assertLess(broker.position.quantity,10)
        self.assertLessEqual(broker.execution_events[-1]['planned_risk_usd'],100)

    def test_no_nonadjacent_fill(self):
        broker=self.make();broker.submit_order(order());broker.process_bar(bar(T+2*M))
        self.assertIsNone(broker.position); self.assertIsNone(broker.pending_order)
        self.assertEqual(broker.execution_events[-1]['reason'],'NOT_IMMEDIATE_NEXT_MINUTE')

    def test_no_outside_window_fill(self):
        ts=T.replace(hour=10,minute=59)
        broker=self.make(); broker.submit_order(order(ts=ts)); broker.process_bar(bar(ts+M))
        self.assertEqual(broker.execution_events[-1]['reason'],'OUTSIDE_ENTRY_WINDOW')

    def test_reject_gap_or_insufficient_rr_both_sides(self):
        for direction,op in [(Direction.BULLISH,110),(Direction.BEARISH,90),(Direction.BULLISH,94),(Direction.BEARISH,106)]:
            with self.subTest(direction=direction,op=op):
                broker=self.make();broker.submit_order(order(direction));broker.process_bar(bar(T+M,o=op,h=op+1,l=op-1,c=op))
                self.assertIsNone(broker.position);self.assertEqual(broker.execution_events[-1]['reason'],'FILL_GEOMETRY_OR_RR')

    def test_no_minimum_contract_forced_after_costs(self):
        broker=self.make();broker.submit_order(order(qty=1));broker.process_bar(bar(T+M))
        self.assertIsNone(broker.position); self.assertEqual(broker.execution_events[-1]['reason'],'FILL_RISK_EXCEEDS_BUDGET')

    def test_same_bar_stop_precedes_target_both_sides(self):
        for d in [Direction.BULLISH,Direction.BEARISH]:
            with self.subTest(direction=d):
                broker=self.make();broker.submit_order(order(d));trade=broker.process_bar(bar(T+M,o=100,h=116,l=84,c=100))
                self.assertEqual(trade.exit_reason,'STOP_LOSS');self.assertLess(trade.pnl_dollars,0)
                self.assertEqual(trade.opened_at,trade.closed_at)

    def test_es_target_at_open_cancels_pending(self):
        broker=self.make();broker.submit_order(order(es_target_price=200));broker.expire_pending(bar(T+M),bar(T+M,o=201,h=205,l=199,c=204,symbol='ES'))
        self.assertEqual(broker.execution_events[-1]['reason'],'ES_TARGET_ALREADY_REACHED')
        self.assertIsNone(broker.pending_order)

    def test_next_open_target_can_be_hit_in_same_bar(self):
        broker=self.make();broker.submit_order(order());trade=broker.process_bar(bar(T+M,o=100,h=116,l=99,c=114))
        self.assertEqual(trade.exit_reason,'TAKE_PROFIT');self.assertGreater(trade.pnl_dollars,0)


class ResearchTests(unittest.TestCase):
    def test_progressive_profiles_and_independent_regime(self):
        p=profiles(BotConfig());self.assertEqual(len(p),7)
        self.assertEqual(p['REGIME_ONLY'].narrative_mode,NarrativeMode.OBSERVE)
        self.assertEqual(p['ALL_STRICT'].structure_mode,StructureMode.STRICT)
        self.assertEqual(p['ALL_STRICT'].regime_mode,RegimeMode.STRICT)

    def test_holdout_fold_replays_warmup_without_overlap(self):
        sessions=[regular_session(date(2025,6,d)) for d in (23,24,25,26,27)]
        folds=get_folds(sessions,2,date(2025,6,26))
        self.assertEqual(folds['DEVELOPMENT'][1],{date(2025,6,25)})
        self.assertEqual(folds['CHRONOLOGICAL_HOLDOUT'][0][0],date(2025,6,24))
        self.assertFalse(folds['DEVELOPMENT'][1]&folds['CHRONOLOGICAL_HOLDOUT'][1])
        with self.assertRaises(ValueError):get_folds(sessions,2,date(2025,6,25))

    def test_annotation_scores_direction_and_no_entry(self):
        windows=[dict(start=T.astimezone(UTC),end=(T+M).astimezone(UTC),expected='ENTER',direction='BULLISH',label='x'),
                 dict(start=(T+M).astimezone(UTC),end=(T+2*M).astimezone(UTC),expected='NO_ENTRY',direction='ANY',label='y')]
        observed={T.astimezone(UTC),(T+M).astimezone(UTC)}
        results=compare_annotations(windows,[order()],[],{T.date()},observed)
        self.assertTrue(all(r['passed'] for r in results))
        results=compare_annotations(windows,[order(Direction.BEARISH),order(ts=T+M)],[],{T.date()},observed)
        self.assertFalse(any(r['passed'] for r in results))
        with self.assertRaisesRegex(ValueError,'unobserved'):
            compare_annotations(windows,[],[],{T.date()},{T.astimezone(UTC)})

    def test_suite_next_open_and_legacy_replay(self):
        nq,es=generate_synthetic_multi_day_suite();days={s.trading_day for s in weekday_schedule(nq)}
        self.assertTrue(audit_pair(nq,es,weekday_schedule(nq))['passed'])
        legacy=run_replay(nq,es,BotConfig(),evaluated_days=days,next_open=False)
        nextopen=run_replay(nq,es,BotConfig(),evaluated_days=days)
        self.assertEqual(legacy['trades'],2);self.assertAlmostEqual(legacy['net_pnl'],421.56)
        self.assertEqual(nextopen['trades'],2);self.assertAlmostEqual(nextopen['net_pnl'],281.04)
        self.assertEqual(nextopen['execution_reasons']['FILLED'],2)
        self.assertIsNotNone(nextopen['net_expectancy_r'])
        json.dumps(nextopen,allow_nan=False)

    def test_warmup_cannot_trade(self):
        nq,es=generate_synthetic_multi_day_suite();days={max(s.trading_day for s in weekday_schedule(nq))}
        r=run_replay(nq,es,BotConfig(),evaluated_days=days)
        self.assertEqual(r['evaluated_minutes'],1380);self.assertEqual(r['signals'],0);self.assertEqual(r['trades'],0)

    def test_unpaired_replay_fails(self):
        with self.assertRaisesRegex(ValueError,'unsynchronized'):
            run_replay([bar(T)],[bar(T+M,symbol='ES')],BotConfig(),evaluated_days={T.date()})

if __name__=='__main__':unittest.main()
