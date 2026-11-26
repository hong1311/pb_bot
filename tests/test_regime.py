"""Regression coverage for V2.5 using closed-minute inputs and real 5m detectors."""
import json
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import patch

from pb_bot.backtest.simulator import BacktestBroker, process_pair, run_backtest
from pb_bot.config import BotConfig, RegimeMode, PlaybookModel
from pb_bot.core.calendar import EconomicCalendar, NewsEvent
from pb_bot.core.liquidity import LiquidityPool, SMTEvent
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG, OrderRequest
from pb_bot.core.primitives import FVGDetector
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange, cme_trading_date
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.data.resampler import MultiTimeframeResampler
from pb_bot.engine.bias_profile import BiasState
from pb_bot.engine.narrative import DeliverySource, MarketNarrative, NarrativePhase
from pb_bot.engine.playbook import ManipulationLeg, Evidence
from pb_bot.engine.regime import RegimeEngine
from pb_bot.engine.strategy import PBStrategyEngine
from tests.test_discretion import strict
from tests.test_playbook import Fixture
from tests.test_structure import ONE_MINUTE

D = datetime(2025, 6, 25, 9, 30, tzinfo=NY_TZ)
BULL, BEAR = Direction.BULLISH, Direction.BEARISH


class Stream:
    def __init__(self, start=D, direction=BULL, calendar=None, config=None, session=None):
        self.t = start
        self.direction = direction
        self.session = session or DailySessionState(cme_trading_date(start))
        self.engine = RegimeEngine(config or BotConfig(regime_mode=RegimeMode.STRICT), calendar)
        self.resampler = MultiTimeframeResampler('NQ', ('1m', '5m'))
        self.detector = FVGDetector('NQ', '5m')
        self.smt = []
        self.last = None

    def p(self, x):
        return x if self.direction == BULL else 200-x

    def at(self, i):
        return self.t + timedelta(minutes=i)

    def step(self, i, o=100, h=105, l=95, c=100, evidence=(), narrative=None):
        lo, hi = sorted((self.p(l), self.p(h)))
        b = Bar('NQ', '1m', self.at(i), self.p(o), hi, lo, self.p(c))
        self.last = b
        closed = self.resampler.update(b)
        created = []
        for candle in closed.get('5m', []):
            gap, _ = self.detector.update(candle)
            if gap:
                created.append(gap)
        return self.engine.update(b, self.session, closed, created, self.smt, evidence, narrative)

    def five(self, i, o, h, l, c):
        for j in range(i, i+5):
            self.step(j,o,h,l,c)
        return self.engine.reasons


def seek_session(both=True):
    return DailySessionState(D.date(), asia=SessionRange(high=108, low=92),
                             london=SessionRange(high=110, low=90),
                             london_swept_asia_high=both, london_swept_asia_low=both)


class TestRegime(unittest.TestCase):
    def test_seek_and_destroy_needs_actual_breakout_and_respected_new_gap(self):
        for d in (BULL, BEAR):
            with self.subTest(direction=d):
                s=Stream(direction=d,session=seek_session())
                self.assertIn('SEEK_AND_DESTROY',s.five(0,100,112,89,100))
                # A wick beyond the range cannot release it.
                self.assertIn('SEEK_AND_DESTROY',s.five(5,100,114,99,109))
                # Strong body close is not yet enough for this regime.
                self.assertIn('SEEK_AND_DESTROY',s.five(10,109,119,109,118))
                self.assertEqual(s.engine.seek_lock.direction,d)
                self.assertIn('SEEK_AND_DESTROY',s.five(15,119,126,118,125))
                self.assertIsNotNone(s.engine.seek_lock.recovery_gap)
                # A subsequent completed 5m candle respects the gap.
                self.assertNotIn('SEEK_AND_DESTROY',s.five(20,125,126,115,121))
                self.assertFalse(s.engine.seek_active)
                self.assertFalse(s.engine.blocked)
                self.assertFalse(s.five(25,121,125,118,123))
                json.dumps(s.engine.events)

    def test_fresh_dual_raid_after_recovery_relocks_range(self):
        s=Stream(session=seek_session())
        s.five(0,100,112,89,100); s.five(5,100,114,99,109)
        s.five(10,109,119,109,118); s.five(15,119,126,118,125)
        s.five(20,125,126,115,121)
        self.assertFalse(s.engine.seek_active)
        s.step(25,121,122,88,100)  # new, not historical dual-side raid
        self.assertIn('SEEK_AND_DESTROY',s.engine.reasons)

    def test_failed_breakout_keeps_seek_lock(self):
        s=Stream(session=seek_session())
        s.five(0,100,112,89,100); s.five(5,100,114,99,109)
        s.five(10,109,119,109,118); s.five(15,119,126,118,125)
        self.assertIn('SEEK_AND_DESTROY',s.five(20,125,126,103,105))
        self.assertIsNone(s.engine.seek_lock.recovery_gap)
        self.assertIsNone(s.engine.seek_lock.direction)

    def test_london_two_sided_is_potential_not_ny_confirmed(self):
        s=Stream(session=seek_session())
        self.assertIn('POTENTIAL_SEEK_AND_DESTROY',s.step(0))
        self.assertIn('POTENTIAL_SEEK_AND_DESTROY',s.step(1,100,111,98,105))
        self.assertIn('SEEK_AND_DESTROY',s.step(2,105,106,88,100))

    def test_pre_ny_london_raid_is_not_ny_raid(self):
        s=Stream(start=D.replace(hour=7,minute=0),session=seek_session(False))
        self.assertNotIn('NY_DUAL_RAID',s.step(0,100,112,89,100))
        self.assertFalse(s.engine.seek_active)
        # A new AM two-sided raid is blocked, even without London sweeping Asia.
        s.step(150,100,112,89,100)
        self.assertIn('NY_DUAL_RAID',s.engine.reasons)

    def test_dual_smt_persists_through_wick_and_clears_on_body(self):
        s=Stream(); s.step(0)
        s.smt.append(SMTEvent(s.at(0),'15m',BULL,90,200,'SWING_SMT'))
        self.assertNotIn('DUAL_SMT_CHOP',s.step(1))
        s.smt.append(SMTEvent(s.at(1),'15m',BEAR,110,210,'SWING_SMT'))
        self.assertIn('DUAL_SMT_CHOP',s.step(2))
        s.step(3); s.step(4)
        self.assertIn('DUAL_SMT_CHOP',s.five(5,100,116,99,105))
        self.assertIn('DUAL_SMT_CHOP',s.five(10,105,120,102,111))  # weak body
        self.assertNotIn('DUAL_SMT_CHOP',s.five(15,105,121,104,120))
        self.assertIsNone(s.engine.smt_lock)
        self.assertFalse(s.five(20,100,105,95,100))  # old pair cannot relock

    def test_smt_rejects_stale_prior_session_or_internal_swings(self):
        s=Stream(); s.step(0)
        s.smt.extend([
            SMTEvent(s.at(0),'15m',BULL,90,200,'SWING_SMT',s.at(-360),s.at(-360)),
            SMTEvent(s.at(0),'15m',BEAR,110,210,'SWING_SMT'),
            SMTEvent(s.at(0),'5m',BULL,90,200,'SWING_SMT'),
        ])
        self.assertNotIn('DUAL_SMT_CHOP',s.step(1))
        s.smt.append(SMTEvent(s.at(1),'15m',BULL,90,200,'SWING_SMT',s.at(0),s.at(0)))
        self.assertIn('DUAL_SMT_CHOP',s.step(2))

    def test_smt_active_lock_does_not_expire_without_displacement(self):
        s=Stream(); s.step(0)
        s.smt.extend([SMTEvent(s.at(0),'15m',BULL,90,200,'SWING_SMT'),SMTEvent(s.at(0),'15m',BEAR,110,210,'SWING_SMT')])
        self.assertIn('DUAL_SMT_CHOP',s.step(1))
        self.assertIn('DUAL_SMT_CHOP',s.step(140))

    def test_overnight_range_and_objective_both_required(self):
        for span,hit,expected in ((301,True,True),(300,True,False),(450,False,False)):
            with self.subTest(span=span,hit=hit):
                s=Stream(start=D-timedelta(minutes=1),session=DailySessionState(D.date(),
                    asia=SessionRange(high=20000,low=19900),london=SessionRange(high=19900+span,low=19950),pdh=19950+span))
                price=s.session.pdh if hit else 20000
                s.step(0,price-1,price,price-2,price-1)
                reasons=s.step(1,price-1,price,price-2,price-1)
                self.assertEqual('OVERNIGHT_EXHAUSTION_300PTS' in reasons,expected)
                self.assertEqual(s.engine.overnight_span,span)

    def test_daytime_objective_hit_cannot_create_overnight_exhaustion(self):
        sess=DailySessionState(D.date(),asia=SessionRange(high=20100,low=19900),
                               london=SessionRange(high=20400,low=20000),pdh=20400)
        s=Stream(start=D-timedelta(minutes=1),session=sess)
        s.step(0,20380,20390,20370,20380)
        self.assertNotIn('OVERNIGHT_EXHAUSTION_300PTS',s.step(1,20390,20401,20389,20400))

    def test_overnight_recovery_requires_fresh_distinct_locked_dol_and_rebalance(self):
        sess=DailySessionState(D.date(),asia=SessionRange(high=20100,low=19900),
                               london=SessionRange(high=20350,low=20000),pdh=20350)
        s=Stream(start=D-timedelta(minutes=5),session=sess)
        s.step(0,20340,20350,20339,20349)
        n=MarketNarrative(1,BEAR,DeliverySource('fresh',BEAR,20340,s.at(1)),
                         phase=NarrativePhase.ACTIVE, primary_dol=LiquidityPool('PDL',19850,BEAR,'1D',5,'PDL'),locked_at=s.at(2))
        s.step(1,20340,20345,20338,20340)
        s.step(2,20260,20265,20255,20260,narrative=n)
        self.assertFalse(s.engine._exhaustion_recovered)
        s.step(3,20130,20140,20120,20125,narrative=n)
        self.assertTrue(s.engine._exhaustion_recovered)
        self.assertNotIn('OVERNIGHT_EXHAUSTION_300PTS',s.step(5,20125,20130,20120,20126,narrative=n))

    def test_overnight_recovery_revoked_if_fresh_objective_delivers_before_open(self):
        sess=DailySessionState(D.date(),asia=SessionRange(high=20100,low=19900),
                               london=SessionRange(high=20350,low=20000),pdh=20350)
        s=Stream(start=D-timedelta(minutes=5),session=sess)
        s.step(0,20340,20350,20339,20349)
        n=MarketNarrative(1,BEAR,DeliverySource('fresh',BEAR,20340,s.at(1)),
                         phase=NarrativePhase.ACTIVE, primary_dol=LiquidityPool('PDL',20000,BEAR,'1D',5,'PDL'),locked_at=s.at(2))
        s.step(3,20130,20140,20120,20125,narrative=n)
        self.assertTrue(s.engine._exhaustion_recovered)
        s.step(4,20005,20006,19999,20001,narrative=n)
        self.assertFalse(s.engine._exhaustion_recovered)
        self.assertIn('OVERNIGHT_EXHAUSTION_300PTS',s.step(5,20001,20003,19999,20002,narrative=n))

    def test_10am_macro_liquidity_generation_uses_confirmed_unswept_pivots(self):
        s=Stream(start=D.replace(hour=10,minute=0))
        highs=[112,115,112,114,111,113,110]
        for i,h in enumerate(highs):
            r=s.step(i,105,h,100,105)
        self.assertIn('LIQUIDITY_GENERATING_10AM',r)
        self.assertIn('NO_5M_FVG_VOLATILITY',r)
        # A key-level sweep ends the liquidity-generation proxy.
        evidence=[Evidence(BULL,s.at(7),95,'EXTERNAL_SWEEP','PDL')]
        self.assertNotIn('LIQUIDITY_GENERATING_10AM',s.step(7,105,110,95,106,evidence=evidence))

    def test_no_5m_gap_after_partial_bucket_or_data_gap(self):
        s=Stream(start=D.replace(hour=10,minute=0))
        # First 5m bucket is missing its first minute. Its gap is not eligible.
        for i in range(1,5): s.step(i,100,102,99,100)
        s.five(5,102,104,101,103)
        s.five(10,105,109,105,108)
        self.assertFalse(s.engine._macro_gap)
        self.assertIn('NO_5M_FVG_VOLATILITY',s.engine.reasons)
        s.five(15,110,113,110,112)
        self.assertTrue(s.engine._macro_gap)
        self.assertNotIn('NO_5M_FVG_VOLATILITY',s.engine.reasons)

    def test_news_10am_freeze_and_wait_for_confirmed_post_release_structure(self):
        cal=EconomicCalendar([NewsEvent(D.date(),time(10),'ISM','RED')])
        s=Stream(start=D.replace(hour=10,minute=0),calendar=cal)
        self.assertIn('WAIT_10AM_DATA_STRUCTURE',s.five(0,100,102,98,100))
        self.assertIn('WAIT_10AM_DATA_STRUCTURE',s.five(5,100,104,90,103))
        self.assertNotIn('WAIT_10AM_DATA_STRUCTURE',s.five(10,105,110,105,109))
        self.assertTrue(s.engine._macro_gap and s.engine._macro_pivot)
        self.assertFalse(s.engine.blocked)

    def test_news_impact_and_pm_freeze(self):
        cal=EconomicCalendar([NewsEvent(D.date(),time(14),'FOMC','RED'),
                              NewsEvent(D.date(),time(10),'Speech','YELLOW')])
        e=RegimeEngine(BotConfig(),cal)
        self.assertEqual(e.calendar_reasons(D),('PM_RED_NEWS_SUPPRESSION',))
        self.assertEqual(e.calendar_reasons(D.replace(hour=14,minute=3)),('PM_RED_NEWS_SUPPRESSION',))
        self.assertFalse(e.calendar_reasons(D.replace(hour=14,minute=4)))
        cal=EconomicCalendar([NewsEvent(D.date(),time(10),'Speech','YELLOW'),
                              NewsEvent(D.date(),time(10),'Release','ORANGE')])
        self.assertFalse(RegimeEngine(BotConfig(),cal).calendar_reasons(D.replace(hour=10,minute=0)))
        cal=EconomicCalendar([NewsEvent(D.date()+timedelta(days=1),time(8,30),'CPI','RED')])
        self.assertEqual(RegimeEngine(BotConfig(),cal).calendar_reasons(D),('PRE_NFP_CPI_CHOP',))

    def test_calendar_policy_explicitly_fails_closed_if_missing(self):
        cfg=BotConfig(regime_mode=RegimeMode.STRICT,regime_require_calendar=True)
        s=Stream(config=cfg)
        self.assertIn('CALENDAR_REQUIRED',s.step(0))
        self.assertNotIn('CALENDAR_REQUIRED',Stream(config=cfg,calendar=EconomicCalendar([])).step(0))

    def test_lrlr_trap_uses_consecutive_recent_candles(self):
        for d in (BULL,BEAR):
            s=Stream(direction=d)
            s.step(0,100,101,96,97)
            for i in range(1,6): s.step(i,100,103,99,102 if i%2 else 99)
            s.step(6,103,112,102,111)
            bottom,top=sorted((s.p(99),s.p(103)))
            gap=FVG('opp','NQ','1m',d.opposite,s.at(-1),bottom,top,FVGState.INVERTED)
            inv=IFVG(gap,d,s.at(6),s.last,1)
            leg=ManipulationLeg(s.at(-1),s.at(0),s.p(96))
            self.assertEqual(s.engine.audit_entry(s.at(6),inv,leg),'LRLR_INVERSION_TRAP')
            s.step(7,111,113,110,112)
            self.assertIsNone(s.engine.audit_entry(s.at(7),inv,leg))

    def test_scheduled_news_cancels_pending_before_fill(self):
        t=D.replace(hour=9,minute=58)
        cfg=BotConfig(regime_mode=RegimeMode.STRICT)
        cal=EconomicCalendar([NewsEvent(D.date(),time(10),'ISM','RED')])
        broker=BacktestBroker(cfg); e=PBStrategyEngine(cfg,broker,calendar=cal)
        broker.submit_order(OrderRequest('MNQ',BULL,1,'LIMIT',100,95,115,'test',t-timedelta(minutes=1)))
        process_pair(e,broker,Bar('NQ','1m',t,102,103,99,101),Bar('ES','1m',t,200,201,199,200))
        self.assertIsNone(broker.pending_order); self.assertIsNone(broker.position)
        self.assertIn('PENDING_REGIME_CANCELLED',[r.code for r in e.rejections])

    def test_known_10am_volatility_transition_cancels_before_fill(self):
        t=D.replace(hour=10,minute=0)
        cfg=BotConfig(regime_mode=RegimeMode.STRICT)
        broker=BacktestBroker(cfg); e=PBStrategyEngine(cfg,broker)
        broker.submit_order(OrderRequest('MNQ',BULL,1,'LIMIT',100,95,115,'test',t-timedelta(minutes=1)))
        process_pair(e,broker,Bar('NQ','1m',t,102,103,99,101),Bar('ES','1m',t,200,201,199,200))
        self.assertIsNone(broker.pending_order); self.assertIsNone(broker.position)
        self.assertIn('PENDING_REGIME_CANCELLED',[r.code for r in e.rejections])

    def test_price_regime_cancels_pending_after_observed_close(self):
        cfg=BotConfig(regime_mode=RegimeMode.STRICT)
        broker=BacktestBroker(cfg); e=PBStrategyEngine(cfg,broker)
        e.nq_sessions.current=seek_session(False)
        broker.submit_order(OrderRequest('MNQ',BULL,1,'LIMIT',80,50,150,'test',D-timedelta(minutes=1)))
        process_pair(e,broker,Bar('NQ','1m',D,100,112,89,100),Bar('ES','1m',D,200,201,199,200))
        self.assertIsNone(broker.pending_order); self.assertIsNone(broker.position)
        self.assertIn('PENDING_REGIME_CANCELLED',[r.code for r in e.rejections])

    def test_scheduled_block_does_not_flatten_open_trade(self):
        t=D.replace(hour=9,minute=58)
        cfg=BotConfig(regime_mode=RegimeMode.STRICT)
        cal=EconomicCalendar([NewsEvent(D.date(),time(10),'ISM','RED')])
        broker=BacktestBroker(cfg); e=PBStrategyEngine(cfg,broker,calendar=cal)
        broker.submit_order(OrderRequest('MNQ',BULL,1,'MARKET',100,95,115,'test',t-timedelta(minutes=1)))
        process_pair(e,broker,Bar('NQ','1m',t,102,103,101,102),Bar('ES','1m',t,200,201,199,200))
        self.assertIsNotNone(broker.position)
        self.assertFalse(broker.trades)

    def test_new_day_clears_locks_and_recovery_progress(self):
        s=Stream(session=seek_session()); s.step(0,100,112,89,100)
        self.assertTrue(s.engine.seek_active)
        s.session=DailySessionState(D.date()+timedelta(days=1)); s.step(24*60)
        self.assertFalse(s.engine.seek_active)
        self.assertFalse(s.engine.blocked)

    def test_all_strict_still_allows_streamed_mac_with_seeded_context(self):
        for d in (BULL,BEAR):
            f=Fixture(d,models=(PlaybookModel.MAC,)); cfg=strict(enabled_models=(PlaybookModel.MAC,),regime_mode=RegimeMode.STRICT)
            f.n.primary_dol=LiquidityPool('PDH' if d==BULL else 'PDL',f.price(150),d,'1D',5,'target')
            broker=BacktestBroker(cfg); e=PBStrategyEngine(cfg,broker)
            e.narrative.current=f.n
            e.nq_fvgs['15m'].fvgs=[f.htf]; e.nq_fvgs['5m'].fvgs=[f.cond]
            with patch.object(e.narrative,'update'),patch.object(e.bias_engine,'evaluate_daily_profile',return_value=BiasState(bias=d)):
                for i,o,h,l,c in ONE_MINUTE:
                    b=f.bar(i,o,h,l,c)
                    process_pair(e,broker,b,Bar('ES','1m',b.timestamp,200,201,199,200))
            self.assertEqual(len(e.submitted_orders),1,e.rejections)
            self.assertFalse(e.submitted_orders[0].metadata['regime']['blocked'])

    def test_observe_baseline_and_regime_only_strict_replay(self):
        nq,es=generate_synthetic_multi_day_suite()
        a=run_backtest(nq,es,BotConfig())
        b=run_backtest(nq,es,BotConfig(regime_mode=RegimeMode.STRICT))
        self.assertEqual(len(a.trades),2); self.assertAlmostEqual(a.final_balance,50421.56)
        self.assertTrue(a.regime_events); self.assertTrue(b.regime_events)
        self.assertTrue(all(r['mode']=='OBSERVE' for r in a.regime_events))
        self.assertTrue(all(r['mode']=='STRICT' for r in b.regime_events))
        self.assertTrue(any('NY_DUAL_RAID' in r['reasons'] for r in b.regime_events))
        self.assertFalse(any('SEEK_AND_DESTROY' in r['reasons'] for r in b.regime_events))
        self.assertEqual(len(b.trades),2)
        self.assertAlmostEqual(b.final_balance,50421.56)
        self.assertFalse(any(t.opened_at.date()==D.date()+timedelta(days=2) for t in b.trades))


if __name__=='__main__': unittest.main()
