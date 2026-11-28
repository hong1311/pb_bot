import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from pb_bot.backtest.simulator import BacktestBroker, process_pair, run_backtest
from pb_bot.config import BotConfig, NarrativeMode, PlaybookMode, PlaybookModel
from pb_bot.core.liquidity import LiquidityPool, SwingTracker
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG, SwingPoint, OrderRequest
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.engine.bias_profile import BiasState, DailyProfile
from pb_bot.engine.narrative import DeliverySource, MarketNarrative, NarrativePhase
from pb_bot.engine.playbook import ManipulationLeg, PlaybookEngine, accumulation_before
from pb_bot.engine.strategy import PBStrategyEngine


class Fixture:
    def __init__(self, direction=Direction.BULLISH, model_mode=PlaybookMode.STRICT, models=tuple(PlaybookModel)):
        self.d = direction
        self.t = datetime(2025, 6, 25, 9, 30, tzinfo=NY_TZ)
        self.cfg = BotConfig(narrative_mode=NarrativeMode.STRICT, model_mode=model_mode, enabled_models=models)
        self.p = PlaybookEngine(self.cfg)
        self.session = DailySessionState(self.t.date())
        self.htf = self.fvg('HTF', '15m', 95, 99)
        self.cond = self.fvg('COND', '5m', 95, 101)
        self.fvgs = {'15m': [self.htf], '5m': [self.cond]}
        self.swings = {}
        target = LiquidityPool('PDH' if self.d == Direction.BULLISH else 'PDL', self.price(125), self.d, '1D', 5, 'target')
        self.n = MarketNarrative(1, self.d, DeliverySource('old_source', self.d, self.price(92), self.t-timedelta(hours=2)),
                                 phase=NarrativePhase.ACTIVE, condition=self.cond, opposing_inversion=True,
                                 primary_dol=target, condition_respected=True, locked_at=self.t-timedelta(minutes=1),
                                 condition_selected_at=self.t-timedelta(hours=1))

    def price(self, p):
        return p if self.d == Direction.BULLISH else 200-p

    def at(self, minute):
        return self.t+timedelta(minutes=minute)

    def fvg(self, label, tf, lo, hi):
        lo, hi = sorted((self.price(lo), self.price(hi)))
        return FVG(label, 'NQ', tf, self.d, self.t-timedelta(hours=1), lo, hi)

    def bar(self, minute, o=103, h=104, l=102, c=103):
        lo, hi = sorted((self.price(l), self.price(h)))
        return Bar('NQ', '1m', self.at(minute), self.price(o), hi, lo, self.price(c))

    def step(self, minute, o=103, h=104, l=102, c=103):
        b = self.bar(minute, o, h, l, c)
        # Mimic only the observed first-touch transition; inversion tested explicitly.
        for gaps in self.fvgs.values():
            for f in gaps:
                if f.state == FVGState.UNFILLED_TARGET and f.overlapped_by(b):
                    f.state = FVGState.ACTIVE_CONDITION
                    f.first_tap_at = b.timestamp
        self.p.update(b, self.session, self.fvgs, self.swings)
        return b

    def trigger(self, minute, extreme_minute, extreme, start=0):
        b = self.bar(minute, 102, 104, 102, 103.5)
        gap = self.fvg('opposite', '1m', 100, 102)
        gap.direction = self.d.opposite
        gap.state = FVGState.INVERTED
        iv = IFVG(gap, self.d, b.timestamp, b, 1)
        return b, ManipulationLeg(self.at(start), self.at(extreme_minute), self.price(extreme)), iv

    def mac(self):
        self.step(0)
        self.step(1, 103, 104, 97, 98)
        return self.trigger(2, 1, 97)

    def continuation(self):
        # Keep the HTF gap out of this example; only the active 5m condition matters.
        self.fvgs['15m'] = []
        self.step(0)
        self.step(1, 103, 104, 99, 100)
        self.step(2)
        pivot = SwingPoint(1, self.at(1), self.price(99), 'LOW' if self.d == Direction.BULLISH else 'HIGH', '5m')
        self.swings['5m'] = SimpleNamespace(lows=[pivot] if self.d == Direction.BULLISH else [],
                                             highs=[pivot] if self.d == Direction.BEARISH else [])
        self.step(3) # pivot observable at close of this minute, before the sweep
        self.step(4, 103, 104, 98, 100)
        return self.trigger(5, 4, 98)

    def po3(self):
        for i in range(15):
            self.step(i, 103, 105, 101.5, 104 if i % 2 else 102)
        self.step(15, 103, 104, 97, 98)
        return self.trigger(16, 15, 97, start=10)

    def select(self, b, leg, iv):
        return self.p.evaluate(self.d, b.timestamp, b, leg, self.n, iv, DailyProfile.UNKNOWN)


class TestPlaybook(unittest.TestCase):
    def test_mac_bull_and_bear(self):
        for d in (Direction.BULLISH, Direction.BEARISH):
            with self.subTest(d=d):
                f = Fixture(d)
                b, leg, iv = f.mac()
                selected = f.select(b, leg, iv)
                self.assertEqual(selected.model, PlaybookModel.MAC)
                self.assertEqual(selected.evidence.kind, 'HTF_FIRST_TAP')
                self.assertEqual(selected.evidence.extreme, f.price(97))

    def test_continuation_bull_and_bear(self):
        for d in (Direction.BULLISH, Direction.BEARISH):
            with self.subTest(d=d):
                f = Fixture(d)
                b, leg, iv = f.continuation()
                selected = f.select(b, leg, iv)
                self.assertEqual(selected.model, PlaybookModel.CONTINUATION)
                self.assertEqual(selected.evidence.pivot_price, f.price(99))
                self.assertEqual(selected.evidence.fvg.id, 'COND')

    def test_po3_bull_and_bear_prioritizes_specific_sequence(self):
        for d in (Direction.BULLISH, Direction.BEARISH):
            with self.subTest(d=d):
                f = Fixture(d)
                b, leg, iv = f.po3()
                selected = f.select(b, leg, iv)
                self.assertEqual(selected.model, PlaybookModel.PO3)
                self.assertEqual(selected.evidence.accumulation.start, f.t)
                self.assertLess(selected.evidence.accumulation.end, selected.evidence.observed_at)
                self.assertTrue(next(x for x in f.p.events[-1]['decisions'] if x['model']=='MAC')['eligible'])

    def test_models_can_be_isolated(self):
        f = Fixture(models=(PlaybookModel.MAC,))
        self.assertEqual(f.select(*f.po3()).model, PlaybookModel.MAC)
        f = Fixture(models=(PlaybookModel.CONTINUATION,))
        self.assertIsNone(f.select(*f.mac()))

    def test_mac_requires_actual_fresh_external_or_htf_source(self):
        f = Fixture(); f.fvgs['15m'] = []
        self.assertIsNone(f.select(*f.mac()))
        f = Fixture(); f.fvgs = {}; f.session.london = SessionRange(high=120, low=99)
        f.step(0); f.step(1,103,104,97,98)
        self.assertEqual(f.select(*f.trigger(2,1,97)).evidence.kind,'EXTERNAL_SWEEP')
        f = Fixture(); f.fvgs = {}; f.session.london = SessionRange(high=120, low=99, low_swept=True)
        f.step(0); f.step(1,103,104,97,98)
        self.assertIsNone(f.select(*f.trigger(2,1,97)))

    def test_no_future_pivot_or_unverified_1m_re_sweep(self):
        f = Fixture(); f.fvgs['15m'] = []
        f.step(0); f.step(1,103,104,99,100); f.step(2)
        pivot = SwingPoint(1, f.at(1),99,'LOW','5m')
        f.swings['5m'] = SimpleNamespace(lows=[pivot],highs=[])
        f.step(3,103,104,98,100) # Newly confirmed now, not yet known before this bar.
        self.assertIsNone(f.select(*f.trigger(4,3,98)))

    def test_actual_swing_tracker_pivot_confirmed_before_re_sweep(self):
        f=Fixture(models=(PlaybookModel.CONTINUATION,)); f.fvgs['15m']=[]
        tracker=SwingTracker('NQ','5m'); f.swings['5m']=tracker
        for i in range(15):
            # The three exact-TF bars are published at minutes 4, 9, 14.
            if i in (4,9,14):
                start=i-4; low=99 if i==9 else 102
                tracker.update(Bar('NQ','5m',f.at(start),103,104,low,103))
            f.step(i,103,104,99 if i==6 else 102,103)
        self.assertEqual(tracker.lows[-1].timestamp,f.at(5))
        # The pivot's bar opened before its first actual touch, so the initial
        # conservative rule rejects this ambiguous case. A later clean pivot
        # (bar opens after first touch) must be used instead.
        f.step(15,103,104,98,100)
        self.assertIsNone(f.select(*f.trigger(16,15,98)))

    def test_newly_visible_fvg_cannot_sponsor_same_bar(self):
        f=Fixture(); f.fvgs['15m']=[]; f.step(0)
        f.fvgs['15m']=[f.htf]; f.step(1,103,104,97,98)
        self.assertIsNone(f.select(*f.trigger(2,1,97)))

    def test_continuation_rejects_internal_target_and_newly_locked_narrative(self):
        f = Fixture(); b, leg, iv = f.continuation()
        original = f.n.primary_dol
        f.n.primary_dol = LiquidityPool('UNFILLED_FVG',125,f.d,'5m',7,'gap')
        self.assertIsNone(f.select(b,leg,iv))
        f.n.primary_dol = original; f.n.locked_at = f.at(4)
        self.assertIsNone(f.select(b,leg,iv))

    def test_source_invalidated_or_missing_and_consumed_setup(self):
        f = Fixture(); b,leg,iv = f.mac(); selected = f.select(b,leg,iv)
        f.p.consume(selected.evidence)
        self.assertIsNone(f.select(b,leg,iv))
        f = Fixture(); b,leg,iv = f.mac(); f.htf.state = FVGState.INVERTED
        f.p.update(b,f.session,f.fvgs,f.swings)
        self.assertIsNone(f.select(b,leg,iv))
        self.assertFalse(f.p.accepts_pending(f.htf.id))

    def test_no_unrelated_old_source_or_deeper_unsourced_extreme(self):
        f = Fixture(); b,leg,iv = f.mac()
        self.assertIsNone(f.select(b,ManipulationLeg(f.at(2),f.at(2),97),iv))
        self.assertIsNone(f.select(b,ManipulationLeg(f.t,f.at(1),96),iv))
        b = f.bar(47); f.p.update(b,f.session,f.fvgs,f.swings)
        self.assertIsNone(f.select(b,leg,iv))

    def test_po3_no_same_bar_range_or_two_sided_raid(self):
        f = Fixture(models=(PlaybookModel.PO3,))
        for i in range(15): f.step(i,103,105,101.5,104 if i%2 else 102)
        f.step(15,103,106,97,98)
        self.assertIsNone(f.select(*f.trigger(16,15,97,start=10)))
        f = Fixture(models=(PlaybookModel.PO3,))
        for i in range(14): f.step(i,103,105,101.5,104 if i%2 else 102)
        f.step(14,103,104,97,98)
        self.assertIsNone(f.select(*f.trigger(15,14,97,start=10)))

    def test_po3_rejects_direct_trending_range_and_missing_minutes(self):
        f = Fixture()
        bars = [f.bar(i,100+i,102+i,100+i,101+i) for i in range(15)]
        self.assertIsNone(accumulation_before(bars,f.at(15),15,.35))
        bars = [f.bar(i,103,105,101,104 if i%2 else 102) for i in range(16) if i!=5]
        self.assertIsNone(accumulation_before(bars,f.at(16),15,.35))

    def test_po3_requires_distribution_and_target_outside_range(self):
        f = Fixture(models=(PlaybookModel.PO3,)); b,leg,iv = f.po3()
        self.assertIsNone(f.select(f.bar(16,102,104,102,102),leg,iv))
        f.n.primary_dol = LiquidityPool('PDH',104.75,f.d,'1D',5,'inside_range')
        self.assertIsNone(f.select(b,leg,iv))

    def test_narrative_gate_and_target_reach(self):
        f=Fixture(); b,leg,iv=f.mac(); f.n.phase=NarrativePhase.WAITING_RESPECT
        self.assertIsNone(f.select(b,leg,iv))
        f.n.phase=NarrativePhase.ACTIVE
        self.assertIsNone(f.select(f.bar(2,102,125,102,103),leg,iv))
        with self.assertRaises(ValueError): BotConfig(model_mode=PlaybookMode.STRICT)

    def test_day_rollover_and_gap_clear_evidence(self):
        f=Fixture(); b,leg,iv=f.mac()
        f.session.trading_day += timedelta(days=1)
        f.p.update(b,f.session,f.fvgs,f.swings)
        self.assertIsNone(f.select(b,leg,iv))
        f=Fixture(); b,leg,iv=f.mac()
        f.p.update(f.bar(4),f.session,f.fvgs,f.swings)
        self.assertIsNone(f.select(f.bar(4),leg,iv))

    def test_strict_order_path_for_all_three_models(self):
        # Real evaluator and broker order path, controlled detector outputs (not market replay).
        for model, setup in ((PlaybookModel.MAC,'mac'),(PlaybookModel.CONTINUATION,'continuation'),(PlaybookModel.PO3,'po3')):
            for d in (Direction.BULLISH,Direction.BEARISH):
                with self.subTest(model=model,d=d):
                    f=Fixture(d,models=(model,)); b,leg,iv=getattr(f,setup)()
                    broker=BacktestBroker(f.cfg); engine=PBStrategyEngine(f.cfg,broker)
                    engine.playbook=f.p; engine.narrative.current=f.n
                    for tf, gaps in f.fvgs.items(): engine.nq_fvgs[tf].fvgs=gaps
                    es=Bar('ES','1m',b.timestamp,200,201,199,200)
                    def selected(*args):
                        engine._selected_leg=leg; engine._selected_leg_extreme=leg.extreme
                        return iv,False
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(engine.narrative,'update'))
                        stack.enter_context(patch.object(engine.bias_engine,'evaluate_daily_profile',return_value=BiasState(bias=d)))
                        stack.enter_context(patch.object(engine.nq_fvgs['1m'],'update',return_value=(None,[iv])))
                        stack.enter_context(patch.object(engine,'_select_highest_tf_ifvg',side_effect=selected))
                        stack.enter_context(patch.object(engine,'_check_key_level_gate',side_effect=AssertionError('legacy key gate called')))
                        order=engine.on_1m_pair(b,es)
                    self.assertIsNotNone(order,engine.rejections)
                    self.assertEqual(order.metadata['playbook_model'],model.value)
                    self.assertEqual(order.take_profit,f.n.primary_dol.price)
                    self.assertEqual(order.stop_loss,leg.extreme+(-.25 if d==Direction.BULLISH else .25))
                    self.assertIsNotNone(broker.position)
                    json.dumps(order.metadata)

    def test_pending_order_cancelled_if_model_anchor_invalidates(self):
        f=Fixture(); b,leg,iv=f.mac(); broker=BacktestBroker(f.cfg); engine=PBStrategyEngine(f.cfg,broker)
        engine.playbook=f.p; engine.narrative.current=f.n
        req=OrderRequest('MNQ',f.d,1,'LIMIT',99,94,125,'test',f.at(2),{'narrative_id':f.n.id,'model_key_level_id':f.htf.id})
        broker.submit_order(req)
        f.htf.state=FVGState.INVERTED
        bar=f.bar(3)
        def update(*args,**kwargs): f.p.update(bar,f.session,f.fvgs,f.swings)
        es=Bar('ES','1m',bar.timestamp,200,201,199,200)
        with patch.object(engine,'on_1m_pair',side_effect=update): process_pair(engine,broker,bar,es)
        self.assertIsNone(broker.pending_order)
        self.assertEqual(engine.rejections[-1].code,'PENDING_MODEL_INVALIDATED')

    def test_observe_and_strict_replay_and_audit(self):
        nq,es=generate_synthetic_multi_day_suite()
        observe=run_backtest(nq,es,BotConfig())
        strict=run_backtest(nq,es,BotConfig(narrative_mode=NarrativeMode.STRICT,model_mode=PlaybookMode.STRICT))
        self.assertEqual(len(observe.trades),2)
        self.assertAlmostEqual(observe.final_balance,50421.56)
        self.assertEqual(len(strict.trades),0)
        self.assertGreater(len(observe.model_events),0)
        self.assertGreater(len(strict.model_events),0)
        self.assertTrue(all(len(e['decisions'])==3 for e in strict.model_events))


if __name__=='__main__': unittest.main()
