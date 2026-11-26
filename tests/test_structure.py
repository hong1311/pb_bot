"""Causal structural selection tests with streaming resampling and detectors."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from pb_bot.backtest.simulator import BacktestBroker, run_backtest
from pb_bot.config import BotConfig, NarrativeMode, PlaybookMode, StructureMode, PlaybookModel
from pb_bot.core.liquidity import LiquidityPool
from pb_bot.core.models import Bar, Direction, FVG, IFVG
from pb_bot.core.primitives import FVGDetector
from pb_bot.core.sessions import NY_TZ, cme_trading_date
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.data.resampler import MultiTimeframeResampler
from pb_bot.engine.bias_profile import BiasState
from pb_bot.engine.playbook import Evidence
from pb_bot.engine.strategy import PBStrategyEngine
from pb_bot.engine.structure import StructureEngine
from tests.test_playbook import Fixture

# Pivot at 09:30, known after 09:31; full 2m bearish gap completed 09:35.
TWO_MINUTE = [(-1, 110,111,109,110), (0,117,120,116,119),
              (1,117,119,115,116), (2,113,114,112,113),
              (3,112,113,111,111.5), (4,109,110,108,109),
              (5,108,109,105,106), (6,107,113,106,112),
              (7,112,117,111,116)]
ONE_MINUTE = [(0,110,111,109,110), (1,110,115,110,114),
              (2,113,114,112,113), (3,111,112,109,109.5),
              (4,107,108,105,106), (5,103,104,100,101),
              (6,99,100,96,97), (7,98,105,97,104),
              (8,104,114,103,113.5)]


def config(**kw):
    return BotConfig(narrative_mode=NarrativeMode.STRICT, model_mode=PlaybookMode.STRICT,
                     structure_mode=StructureMode.STRICT, **kw)


class Stream:
    def __init__(self, direction=Direction.BULLISH, cfg=None):
        self.d = direction
        self.cfg = cfg or config()
        self.s = StructureEngine(self.cfg)
        self.t = datetime(2025,6,25,9,30,tzinfo=NY_TZ)
        self.r = MultiTimeframeResampler('NQ',self.cfg.ifvg_scan_ladder)
        self.det = {tf:FVGDetector('NQ',tf) for tf in self.cfg.ifvg_scan_ladder}
        self.bar = None
        self.new = {}

    def price(self,p):
        return p if self.d == Direction.BULLISH else 200-p

    def make(self,i,o,h,l,c):
        low,high = sorted((self.price(l),self.price(h)))
        return Bar('NQ','1m',self.t+timedelta(minutes=i),self.price(o),high,low,self.price(c))

    def step(self, row, evidence=False, day=None):
        b = self.make(*row); self.bar=b
        closed = self.r.update(b); created=[]; new={}
        for tf, bars in closed.items():
            for candle in bars:
                gap, inversions = self.det[tf].update(candle)
                if gap: created.append(gap)
                if inversions: new.setdefault(tf,[]).extend(inversions)
        self.new=new
        e = [Evidence(self.d,b.timestamp,b.low if self.d==Direction.BULLISH else b.high,
                      'EXTERNAL_SWEEP','PDL' if self.d==Direction.BULLISH else 'PDH')] if evidence else []
        self.s.update(b,day or cme_trading_date(b.timestamp),closed,created,new,e)
        return self.s.select(self.d,b.timestamp,new)

    def feed(self, rows, sweep=None):
        result=None
        for row in rows: result=self.step(row,row[0]==sweep)
        return result


class TestStructure(unittest.TestCase):
    def test_streaming_bull_bear_highest_tf_and_exact_confirmation(self):
        for d in (Direction.BULLISH,Direction.BEARISH):
            with self.subTest(direction=d):
                s=Stream(d)
                before=s.feed(TWO_MINUTE[:-2],sweep=5)
                self.assertEqual(before.reason,'WAITING_POST_SWEEP_CLOSE')
                self.assertEqual(before.leg.origin_at,s.t)
                self.assertEqual(before.leg.origin_confirmed_at,s.t+timedelta(minutes=1))
                self.assertEqual(before.leg.extreme,s.price(105))
                early=s.step(TWO_MINUTE[-2])
                self.assertEqual(early.reason,'WAITING_HIGHEST_TF')
                self.assertEqual(early.timeframe,'2m')
                self.assertTrue(s.new.get('1m'))
                confirmed=s.step(TWO_MINUTE[-1])
                self.assertEqual(confirmed.reason,'STRUCTURAL_IFVG')
                self.assertEqual(confirmed.ifvg.source_fvg.timeframe,'2m')
                # The 2m confirming candle opens a minute BEFORE publication.
                self.assertEqual(confirmed.ifvg.confirmed_at,s.t+timedelta(minutes=6))
                self.assertEqual(s.s._inversions[confirmed.ifvg.source_fvg.id],s.t+timedelta(minutes=7))
                json.dumps(confirmed.leg.snapshot())

    def test_1m_outermost_and_full_formation_boundary(self):
        s=Stream(); result=s.feed(ONE_MINUTE,sweep=6)
        self.assertEqual(result.reason,'STRUCTURAL_IFVG')
        self.assertEqual(result.ifvg.source_fvg.timeframe,'1m')
        self.assertEqual(result.ifvg.source_fvg.top,112)
        self.assertEqual(result.leg.origin_at,s.t+timedelta(minutes=1))
        self.assertTrue(all(f.start>=result.leg.origin_at for f in result.leg.formations))
        self.assertTrue(all(f.completed_at<=result.leg.extreme_at for f in result.leg.formations))
        self.assertTrue(all(f.gap.timeframe=='1m' for f in result.leg.formations))

    def test_same_bar_pivot_is_not_known_before_evidence(self):
        s=Stream()
        result=s.feed(ONE_MINUTE[:3],sweep=2)
        self.assertIsNone(result.leg)
        self.assertEqual(result.reason,'NO_EVIDENCE_ANCHORED_LEG')

    def test_no_evidence_no_leg(self):
        s=Stream(); self.assertIsNone(s.feed(TWO_MINUTE).leg)

    def test_deeper_unsourced_extreme_invalidates_old_leg(self):
        s=Stream(); s.feed(TWO_MINUTE[:-2],sweep=5)
        self.assertIsNone(s.step((6,106,108,104,106)).leg)
        self.assertIsNone(s.step((7,108,117,107,116)).ifvg)

    def test_missing_minute_resets_leg_and_rejects_stale_bucket(self):
        s=Stream(); s.feed(TWO_MINUTE[:-2],sweep=5)
        decision=s.step((7,109,117,108,116))
        self.assertIsNone(decision.leg)
        self.assertEqual(len(s.s._formations),0)
        self.assertFalse(s.s._inversions.get('NQ_2m_FVG_1'))

    def test_rollover_resets_active_leg(self):
        s=Stream(); s.feed(TWO_MINUTE[:-2],sweep=5)
        result=s.step(TWO_MINUTE[-2],day=s.t.date()+timedelta(days=1))
        self.assertIsNone(result.leg)

    def test_old_origin_survives_more_than_45_minutes(self):
        s=Stream(); s.feed([(-1,110,111,109,110),(0,119,120,118,119)])
        for i in range(1,52):
            p=117-i*.5
            result=s.step((i,p,p+.25,p-.25,p),evidence=(i==51))
        self.assertIsNotNone(result.leg)
        self.assertEqual(result.leg.origin_at,s.t)
        self.assertGreater(result.leg.extreme_at-result.leg.origin_at,timedelta(minutes=45))

    def test_initial_partial_bucket_not_used_as_formation(self):
        s=Stream(); s.feed(TWO_MINUTE[2:-2],sweep=5)
        # Started 09:31, so the initial 09:30 2m bucket is incomplete.
        self.assertFalse(any(f.gap.timeframe=='2m' for f in s.s._formations))

    def test_reclaim_retires_leg_after_confirmation_minute(self):
        s=Stream(); s.feed(TWO_MINUTE,sweep=5)
        s.step((8,116,122,115,121))
        self.assertIsNone(s.step((9,121,123,120,122)).leg)

    def test_minimum_tf_gate_no_fallthrough(self):
        s=Stream(cfg=config(min_ifvg_tf_minutes=3)); result=s.feed(TWO_MINUTE,sweep=5)
        self.assertEqual(result.reason,'BELOW_MINIMUM_TF')
        self.assertIsNone(result.ifvg)

    def test_later_signal_cannot_reuse_outer_inversion(self):
        s=Stream(); result=s.feed(TWO_MINUTE,sweep=5)
        self.assertIsNotNone(result.ifvg)
        next_result=s.step((8,116,117,115,116))
        self.assertEqual(next_result.reason,'NO_FRESH_OUTER_INVERSION')

    def test_strict_mode_requires_prior_strict_modes(self):
        with self.assertRaises(ValueError): BotConfig(structure_mode=StructureMode.STRICT)

    def test_strict_strategy_order_uses_real_ltf_detector_and_structural_anchor(self):
        # The HTF narrative and bias are seeded. Minute bars, resampling,
        # FVGs, evidence, structural selection, model gate and order are real.
        for d in (Direction.BULLISH,Direction.BEARISH):
            with self.subTest(direction=d):
                f=Fixture(d,models=(PlaybookModel.MAC,)); cfg=config(enabled_models=(PlaybookModel.MAC,))
                f.n.primary_dol=LiquidityPool('PDH' if d==Direction.BULLISH else 'PDL',f.price(150),d,'1D',5,'target')
                broker=BacktestBroker(cfg); engine=PBStrategyEngine(cfg,broker)
                engine.narrative.current=f.n
                engine.nq_fvgs['15m'].fvgs=[f.htf]
                engine.nq_fvgs['5m'].fvgs=[f.cond]
                orders=[]
                with patch.object(engine.narrative,'update'), patch.object(engine.bias_engine,'evaluate_daily_profile',return_value=BiasState(bias=d)):
                    for i,o,h,l,c in ONE_MINUTE:
                        b=f.bar(i,o,h,l,c)
                        es=Bar('ES','1m',b.timestamp,200,201,199,200)
                        order=engine.on_1m_pair(b,es)
                        if order: orders.append(order)
                self.assertEqual(len(orders),1,engine.rejections)
                order=orders[0]
                self.assertEqual(order.timestamp,f.at(8))
                self.assertEqual(order.metadata['playbook_model'],'MAC')
                self.assertEqual(order.metadata['structural_leg']['extreme'],f.price(96))
                self.assertEqual(order.stop_loss,f.price(95.75))
                self.assertEqual(order.take_profit,f.price(150))
                self.assertIsNotNone(broker.position)
                json.dumps(order.metadata)

    def test_observe_and_all_strict_replay(self):
        nq,es=generate_synthetic_multi_day_suite()
        observe=run_backtest(nq,es,BotConfig()); strict=run_backtest(nq,es,config())
        self.assertEqual(len(observe.trades),2)
        self.assertAlmostEqual(observe.final_balance,50421.56)
        self.assertEqual(len(strict.trades),0)
        self.assertTrue(observe.structure_events)
        self.assertTrue(strict.structure_events)
        self.assertTrue(all(e['mode']=='STRICT' for e in strict.structure_events))


if __name__=='__main__': unittest.main()
