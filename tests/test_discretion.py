import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from pb_bot.backtest.simulator import BacktestBroker, run_backtest
from pb_bot.config import (BotConfig, DiscretionMode, NarrativeMode, PlaybookMode,
                           PlaybookModel, StructureMode)
from pb_bot.core.liquidity import LiquidityPool
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG
from pb_bot.core.primitives import FVGDetector
from pb_bot.core.sessions import NY_TZ, cme_trading_date
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.data.resampler import MultiTimeframeResampler
from pb_bot.engine.bias_profile import BiasState
from pb_bot.engine.discretion import DiscretionEngine
from pb_bot.engine.playbook import ManipulationLeg
from pb_bot.engine.strategy import PBStrategyEngine
from tests.test_playbook import Fixture
from tests.test_structure import ONE_MINUTE


def strict(**kw):
    return BotConfig(narrative_mode=NarrativeMode.STRICT, model_mode=PlaybookMode.STRICT,
                     structure_mode=StructureMode.STRICT, discretion_mode=DiscretionMode.STRICT, **kw)


IMPULSE = [(100,105,98,102), (98,100,90,95), (96,101,95,100),
           (101,112,101,109), (111,119,108,118), (120,130,120,128),
           (124,128,121,123)]


class Stream:
    def __init__(self, direction=Direction.BULLISH, tf='5m', cfg=None):
        self.d = direction
        self.tf = tf
        self.step = int(tf[:-1])
        self.cfg = cfg or strict()
        self.engine = DiscretionEngine(self.cfg)
        self.t = datetime(2025,6,25,8,0,tzinfo=NY_TZ)
        self.resampler = MultiTimeframeResampler('NQ',('1m',tf))
        self.detector = FVGDetector('NQ',tf)

    def p(self,x):
        return x if self.d == Direction.BULLISH else 300-x

    def at(self,i):
        return self.t+timedelta(minutes=i)

    def b(self,i,o,h,l,c,tf='1m'):
        lo,hi=sorted((self.p(l),self.p(h)))
        return Bar('NQ',tf,self.at(i),self.p(o),hi,lo,self.p(c))

    def feed(self,i,o,h,l,c,day=None):
        b=self.b(i,o,h,l,c)
        closed=self.resampler.update(b)
        for candle in closed.get(self.tf,[]): self.detector.update(candle)
        self.engine.update(b,day or cme_trading_date(b.timestamp),closed)
        return b

    def impulse(self,count=7,start=0):
        for i in range(start,count*self.step):
            self.feed(i,*IMPULSE[i//self.step])
        return self

    def gap(self,i):
        return next(f for f in self.detector.fvgs if f.created_at==self.at(i*self.step) and f.direction==self.d)

    def setup(self,extreme=105,entry=115,gi=4,order_type='MARKET',behind=False):
        n=7*self.step
        self.feed(n, min(123,extreme+8), min(129,extreme+10), extreme,extreme+4)
        confirm=self.feed(n+1,entry-5,entry+1,entry-6,entry)
        lo,hi=sorted((self.p(entry-3),self.p(entry-2)))
        opposing=FVG('IFVG','NQ','1m',self.d.opposite,self.at(n-1),lo,hi,FVGState.INVERTED)
        iv=IFVG(opposing,self.d,confirm.timestamp,confirm,1)
        leg=ManipulationLeg(self.at(n-2),self.at(n),self.p(extreme))
        return self.engine.audit(self.d,PlaybookModel.CONTINUATION,self.gap(gi),leg,iv,self.p(entry),
                                 order_type,self.detector.fvgs,behind,confirm.timestamp)


class TestDiscretion(unittest.TestCase):
    def test_mirrored_5m_and_15m_impulse_not_day_or_manipulation(self):
        for d in (Direction.BULLISH,Direction.BEARISH):
            for tf in ('5m','15m'):
                with self.subTest(direction=d,timeframe=tf):
                    s=Stream(d,tf).impulse(); result=s.setup()
                    self.assertTrue(result.allowed,result.reason)
                    r=result.dealing_range
                    self.assertEqual(r.eq,s.p(110))
                    self.assertEqual(r.start,s.at(s.step))
                    self.assertEqual(r.end,s.at(5*s.step))
                    self.assertEqual(r.confirmed_at,s.at(7*s.step))
                    self.assertEqual(result.retracement_position,.375)
                    self.assertEqual(result.entry_position,.625)
                    self.assertTrue(result.reduced_risk)
                    json.dumps(s.engine.events)

    def test_same_impulse_stacked_premium_rejected(self):
        for d in (Direction.BULLISH,Direction.BEARISH):
            result=Stream(d).impulse().setup(extreme=115,entry=125,gi=5)
            self.assertEqual(result.reason,'STACKED_FVG_OUTSIDE_IMPULSE_EQ')
            self.assertFalse(result.allowed)

    def test_unrelated_gap_does_not_count_as_sibling(self):
        s=Stream().impulse(); gap=s.gap(4)
        # A same-timeframe gap with invented geometry is not an actual formation.
        fake=FVG('fake','NQ','5m',s.d,gap.created_at,112,115)
        s.detector.fvgs.append(fake)
        result=s.setup()
        self.assertNotIn(fake.id,result.dealing_range.gap_ids)

    def test_equilibrium_touch_full_risk_when_entry_in_discount(self):
        result=Stream().impulse().setup(extreme=104,entry=110)
        self.assertTrue(result.allowed)
        self.assertEqual(result.entry_position,.5)
        self.assertFalse(result.reduced_risk)

    def test_unrebalanced_1h_gap_blocks_even_moderate_premium(self):
        s=Stream(cfg=strict(enable_stacked_fvg_eq_filter=False)).impulse()
        result=s.setup(extreme=112,entry=116,gi=5,behind=True)
        self.assertEqual(result.reason,'UNREBALANCED_IMPULSE_WITH_1H_GAP')

    def test_extreme_without_equilibrium_retrace_rejected(self):
        s=Stream(cfg=strict(enable_stacked_fvg_eq_filter=False)).impulse()
        self.assertEqual(s.setup(extreme=113,entry=124,gi=5).reason,'EXTREME_UNREBALANCED_CONTINUATION')

    def test_qualified_retrace_not_rejected_just_for_1h_gap(self):
        result=Stream().impulse().setup(extreme=105,entry=118,behind=True)
        self.assertTrue(result.allowed)
        self.assertTrue(result.reduced_risk)

    def test_no_lookahead_end_pivot_must_already_be_known(self):
        s=Stream().impulse(6)
        leg=ManipulationLeg(s.at(25),s.at(30),105)
        self.assertIsNone(s.engine.dealing_range(s.gap(4),s.d,leg,s.detector.fvgs))
        s.impulse(start=30)
        # Even after later bars arrive, the earlier decision remains unconfirmed.
        self.assertIsNone(s.engine.dealing_range(s.gap(4),s.d,leg,s.detector.fvgs))

    def test_prior_range_invalidated_by_new_extreme(self):
        s=Stream().impulse(); s.feed(35,125,131,124,130)
        leg=ManipulationLeg(s.at(34),s.at(36),105)
        self.assertIsNone(s.engine.dealing_range(s.gap(4),s.d,leg,s.detector.fvgs))

    def test_missing_data_and_rollover_discard_range(self):
        for reset in ('gap','day'):
            s=Stream().impulse(); gap=s.gap(4)
            i=36 if reset=='gap' else 35
            s.feed(i,110,111,104,105,day=s.t.date()+timedelta(days=1) if reset=='day' else None)
            self.assertIsNone(s.engine.dealing_range(gap,s.d,ManipulationLeg(s.at(34),s.at(i),105),s.detector.fvgs))
            self.assertEqual(s.engine.closed['5m'],[])

    def test_partial_initial_bucket_rejected(self):
        s=Stream().impulse(start=1)
        self.assertNotEqual(s.engine.closed['5m'][0].timestamp,s.at(0))

    def test_weak_displacement_rejected_for_every_model(self):
        s=Stream(); bar=s.feed(0,108,120,100,109)
        g=FVG('g','NQ','1m',Direction.BEARISH,s.at(-1),106,108)
        iv=IFVG(g,s.d,bar.timestamp,bar,1)
        for m in PlaybookModel:
            decision=s.engine.evaluate(s.d,m,None,None,iv,109,'MARKET',[],False,bar.timestamp)
            self.assertEqual(decision.reason,'WEAK_DISPLACEMENT')

    def test_oversized_wick_requires_distinct_retest(self):
        for d in (Direction.BULLISH,Direction.BEARISH):
            s=Stream(d,cfg=strict(enable_validated_wick_stop=True)); s.feed(0,110,112,90,111)
            leg=ManipulationLeg(s.at(0),s.at(0),s.p(90))
            s.feed(1,112,116,110,115)
            self.assertIsNone(s.engine.wick_midpoint(d,leg,s.at(1)))
            s.feed(2,105,114,99,113)
            self.assertEqual(s.engine.wick_midpoint(d,leg,s.at(2)),s.p(100))
            s.feed(3,113,114,98,108)
            self.assertIsNone(s.engine.wick_midpoint(d,leg,s.at(3)))

    def test_wick_exception_market_only_and_opt_in(self):
        for enabled in (True,False):
            s=Stream(cfg=strict(enable_validated_wick_stop=enabled)); s.feed(0,110,112,90,111)
            bar=s.feed(1,100,116,99,115)
            leg=ManipulationLeg(s.at(0),s.at(0),90)
            iv=IFVG(FVG('g','NQ','1m',Direction.BEARISH,s.at(-1),108,109),s.d,bar.timestamp,bar,1)
            for typ in ('MARKET','LIMIT'):
                result=s.engine.evaluate(s.d,PlaybookModel.MAC,None,leg,iv,115,typ,[],False,bar.timestamp)
                self.assertTrue(result.allowed)
                self.assertEqual(result.wick_midpoint,100 if enabled and typ=='MARKET' else None)

    def test_strict_mode_requires_structure_and_valid_thresholds(self):
        with self.assertRaises(ValueError): BotConfig(discretion_mode=DiscretionMode.STRICT)
        with self.assertRaises(ValueError): BotConfig(min_displacement_body_fraction=1.1)
        with self.assertRaises(ValueError): BotConfig(continuation_size_mult=1.1)

    def test_strict_mac_order_in_real_ltf_stream(self):
        for d in (Direction.BULLISH,Direction.BEARISH):
            f=Fixture(d,models=(PlaybookModel.MAC,)); cfg=strict(enabled_models=(PlaybookModel.MAC,))
            f.n.primary_dol=LiquidityPool('PDH' if d==Direction.BULLISH else 'PDL',f.price(150),d,'1D',5,'target')
            broker=BacktestBroker(cfg); engine=PBStrategyEngine(cfg,broker)
            engine.narrative.current=f.n
            engine.nq_fvgs['15m'].fvgs=[f.htf]
            engine.nq_fvgs['5m'].fvgs=[f.cond]
            with patch.object(engine.narrative,'update'), patch.object(engine.bias_engine,'evaluate_daily_profile',return_value=BiasState(bias=d)):
                for i,o,h,l,c in ONE_MINUTE:
                    b=f.bar(i,o,h,l,c)
                    es=Bar('ES','1m',b.timestamp,200,201,199,200)
                    engine.on_1m_pair(b,es)
            self.assertEqual(len(engine.submitted_orders),1,engine.rejections)
            order=engine.submitted_orders[0]
            self.assertEqual(order.stop_loss,f.price(95.75))
            self.assertEqual(order.metadata['discretion']['reason'],'ELIGIBLE')
            self.assertFalse(order.metadata['discretion']['reduced_risk'])
            json.dumps(order.metadata)

    def test_strict_continuation_real_stream_reduces_contracts_from_impulse(self):
        # Seed the directional narrative only; candles, condition, internal
        # pivot, sweep, structural IFVG and order sizing are all detected live.
        for d in (Direction.BULLISH,Direction.BEARISH):
            with self.subTest(direction=d):
                f=Fixture(d,models=(PlaybookModel.CONTINUATION,))
                cfg=strict(enabled_models=(PlaybookModel.CONTINUATION,))
                broker=BacktestBroker(cfg); engine=PBStrategyEngine(cfg,broker)
                s=Stream(d); s.t=f.at(-40)
                def feed(i,row):
                    b=s.b(i,*row)
                    es=Bar('ES','1m',b.timestamp,200,201,199,200)
                    return engine.on_1m_pair(b,es)
                with patch.object(engine.narrative,'update'), patch.object(engine.bias_engine,'evaluate_daily_profile',return_value=BiasState(bias=d)):
                    for i in range(35): feed(i,IMPULSE[i//5])
                    f.n.condition=next(g for g in engine.nq_fvgs['5m'].fvgs if g.created_at==s.at(20) and g.direction==d)
                    f.n.locked_at=s.at(34)
                    f.n.primary_dol=LiquidityPool('PDH' if d==Direction.BULLISH else 'PDL',s.p(160),d,'1D',5,'target')
                    engine.narrative.current=f.n
                    for i in range(35,40): feed(i,(114,116,107,112))
                    for i in range(40,45): feed(i,(113,119,109,117))
                    for i,row in enumerate([(118,120,116,118),(116,118,114,115),(111,113,109,110),
                                            (109,110,104,107),(108,123,107,122)],45):
                        feed(i,row)
                self.assertEqual(len(engine.submitted_orders),1,engine.rejections)
                order=engine.submitted_orders[0]
                self.assertEqual(order.metadata['playbook_model'],'CONTINUATION')
                self.assertEqual(order.metadata['discretion']['range']['eq'],s.p(110))
                self.assertEqual(order.stop_loss,s.p(103.75))
                self.assertTrue(order.metadata['discretion']['reduced_risk'])
                self.assertEqual(order.quantity,3)
                self.assertEqual(engine.risk.calculate_contracts(order.entry_price,order.stop_loss),6)

    def test_observe_replay_unchanged_and_strict_safe(self):
        nq,es=generate_synthetic_multi_day_suite()
        a=run_backtest(nq,es,BotConfig()); b=run_backtest(nq,es,strict())
        self.assertEqual(len(a.trades),2)
        self.assertAlmostEqual(a.final_balance,50421.56)
        self.assertEqual(len(b.trades),0)
        self.assertTrue(a.discretion_events)
        self.assertTrue(all(e['mode']=='OBSERVE' for e in a.discretion_events))


if __name__=='__main__': unittest.main()
