import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from contextlib import ExitStack

from pb_bot.backtest.simulator import BacktestBroker, process_pair, run_backtest
from pb_bot.config import BotConfig, NarrativeMode
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG, OrderRequest
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.data.feed import generate_synthetic_multi_day_suite
from pb_bot.engine.narrative import MarketNarrativeEngine, NarrativePhase
from pb_bot.engine.strategy import PBStrategyEngine
from pb_bot.engine.bias_profile import BiasState


class NarrativeFixture:
    """Deterministic detector-output fixture, independent of legacy swing heuristics."""
    def __init__(self, direction=Direction.BULLISH):
        self.direction = direction
        self.t = datetime(2025, 6, 25, 9, 30, tzinfo=NY_TZ)
        self.session = DailySessionState(self.t.date(), pdh=130, pdl=70,
                                        london=SessionRange(high=120, low=80))
        self.es_session = DailySessionState(self.t.date(), pdh=230, pdl=170,
                                           london=SessionRange(high=220, low=180))
        self.engine = MarketNarrativeEngine()
        self.fvgs = {}
        self.source = self.fvg("source", "1H", direction, 94, 96)
        self.cond = self.fvg("condition", "5m", direction, 99, 101, created_at=self.t+timedelta(minutes=4))
        self.target = self.fvg("target", "15m", direction.opposite, 110, 112)
        self.opposing = self.fvg("opposing", "5m", direction.opposite, 95, 98)
        self.fvgs = {"1H": [self.source], "15m": [self.target], "5m": [self.opposing]}

    def reflect(self, p):
        return p if self.direction == Direction.BULLISH else 200-p

    def fvg(self, id, tf, direction, lo, hi, created_at=None):
        a,b = sorted((self.reflect(lo),self.reflect(hi)))
        return FVG(id,"NQ",tf,direction,created_at or self.t-timedelta(hours=2),a,b)

    def bar(self, minute, o=103,h=104,l=102,c=103, tf="1m"):
        vals = sorted((self.reflect(h),self.reflect(l)))
        return Bar("NQ",tf,self.t+timedelta(minutes=minute),self.reflect(o),vals[1],vals[0],self.reflect(c))

    def step(self, minute, o=103,h=104,l=102,c=103, created=(), inverted=None, closed=None, bias=None, blocked=False,
             es_high=204, es_low=196):
        b = self.bar(minute,o,h,l,c)
        e = Bar("ES","1m",b.timestamp,200,es_high,es_low,200)
        self.engine.update(b,e,bias or self.direction,self.session,self.es_session,self.fvgs,
                           created,inverted or {},closed or {},blocked)
        return self.engine.current

    def start(self):
        self.step(0)
        self.source.state=FVGState.ACTIVE_CONDITION
        self.step(1,97,98,95,97)
        self.opposing.state=FVGState.INVERTED
        self.fvgs["5m"].append(self.cond)
        iv = IFVG(self.opposing,self.direction,self.t+timedelta(minutes=2),self.bar(2,tf="5m"),1)
        return self.step(8,created=[self.cond],inverted={"5m":[iv]})

    def active(self):
        self.start()
        self.cond.state=FVGState.ACTIVE_CONDITION
        return self.step(14,103,104,100,103,closed={"5m":[self.bar(10,103,104,100,103,"5m")]})


class TestNarrative(unittest.TestCase):
    def test_bull_and_bear_lock_then_respect(self):
        for direction in (Direction.BULLISH, Direction.BEARISH):
            with self.subTest(direction=direction):
                f=NarrativeFixture(direction)
                n=f.start()
                self.assertEqual(n.phase,NarrativePhase.WAITING_RESPECT)
                self.assertEqual(n.primary_dol.label,"target")
                self.assertIsNotNone(n.alternative_dol)
                self.assertFalse(n.can_enter(direction,f.t+timedelta(minutes=9)))
                f.cond.state=FVGState.ACTIVE_CONDITION
                f.step(10,103,104,100,103) # 1m wick cannot establish exact-TF respect
                self.assertFalse(n.condition_respected)
                f.step(14,103,104,100,103,closed={"5m":[f.bar(10,103,104,100,103,"5m")]})
                self.assertTrue(n.can_enter(direction,f.t+timedelta(minutes=14)))
                self.assertEqual(n.phase,NarrativePhase.ACTIVE)
                self.assertEqual(n.invalidation_price,f.reflect(99))
                self.assertEqual(n.primary_dol.price,f.reflect(110))

    def test_target_locked_no_reselection_when_new_closer_target_appears(self):
        f=NarrativeFixture();n=f.active();target=n.primary_dol
        f.fvgs["15m"].append(f.fvg("new_closer","15m",Direction.BEARISH,106,108))
        f.step(15)
        self.assertIs(n.primary_dol,target)
        self.assertEqual(n.primary_dol.label,"target")

    def test_wick_respects_condition_then_exact_inversion_invalidates(self):
        f=NarrativeFixture();n=f.active()
        f.step(15,103,104,97,103)
        self.assertEqual(n.phase,NarrativePhase.ACTIVE)
        f.cond.state=FVGState.INVERTED
        f.step(19,103,104,96,97)
        self.assertEqual(n.phase,NarrativePhase.INVALIDATED)
        self.assertEqual(f.engine.events[-1]["event"],"CONDITION_INVERTED")
        self.assertFalse(f.engine.accepts_pending(n.id,n.direction,f.t+timedelta(minutes=20)))

    def test_target_touch_terminates_without_rearming_old_source(self):
        f=NarrativeFixture();n=f.active()
        f.step(15,104,110,103,105)
        self.assertEqual(n.phase,NarrativePhase.TARGET_REACHED)
        f.step(16)
        self.assertIs(f.engine.current,n)
        self.assertFalse(n.can_enter(n.direction,f.t+timedelta(minutes=16)))
        # New, distinct observed HTF tap permits a fresh narrative.
        new_source=f.fvg("new_source","1H",Direction.BULLISH,98,100)
        f.fvgs["1H"].append(new_source);f.step(17)
        new_source.state=FVGState.ACTIVE_CONDITION
        f.step(18,102,103,99,102)
        self.assertNotEqual(f.engine.current.id,n.id)
        self.assertEqual(f.engine.current.phase,NarrativePhase.WAITING_CONDITION)

    def test_bias_and_regime_invalidation(self):
        for kwargs in ({"bias":Direction.BEARISH},{"blocked":True}):
            f=NarrativeFixture();n=f.active();f.step(15,**kwargs)
            self.assertEqual(n.phase,NarrativePhase.INVALIDATED)
            self.assertEqual(f.engine.events[-1]["event"],"REGIME_OR_BIAS_CHANGED")

    def test_session_rollover_clears_prior_narrative(self):
        f=NarrativeFixture();n=f.active()
        f.session.trading_day+=timedelta(days=1);f.es_session.trading_day+=timedelta(days=1)
        f.step(1440)
        self.assertTrue(n.terminal)
        self.assertIsNone(f.engine.current)
        self.assertEqual(f.engine.events[-1]["event"],"DAY_ROLLOVER")

    def test_no_same_bar_lock_and_entry(self):
        f=NarrativeFixture();n=f.active()
        n.locked_at=f.t+timedelta(minutes=14)
        self.assertFalse(n.can_enter(n.direction,n.locked_at))
        self.assertTrue(n.can_enter(n.direction,n.locked_at+timedelta(minutes=1)))

    def test_es_target_reach_cancels_thesis(self):
        f=NarrativeFixture();f.fvgs["15m"]=[]
        n=f.active()
        self.assertEqual(n.primary_dol.label,"LONDON_H")
        self.assertEqual(n.primary_dol.correlated_price,220)
        f.step(15,es_high=220)
        self.assertEqual(n.phase,NarrativePhase.TARGET_REACHED)
        self.assertEqual(f.engine.events[-1]["event"],"ES_TARGET_REACHED")

    def test_swept_es_level_is_not_reintroduced_as_unpaired_target(self):
        f=NarrativeFixture();f.fvgs["15m"]=[]
        f.es_session.london.high_swept=True
        n=f.start()
        self.assertEqual(n.primary_dol.label,"PDH")

    def test_session_touch_and_subsequent_sweep_are_distinct(self):
        f=NarrativeFixture()
        f.step(0,81,82,80,81)
        self.assertIsNone(f.engine.current)
        n=f.step(1,81,82,79,81)
        self.assertEqual(n.source.label,"LONDON_L")
        self.assertEqual(n.source.direction,Direction.BULLISH)

    def test_no_condition_without_new_aligned_gap_and_opposing_inversion(self):
        f=NarrativeFixture();f.step(0);f.source.state=FVGState.ACTIVE_CONDITION
        n=f.step(1,97,98,95,97)
        f.cond.state=FVGState.ACTIVE_CONDITION
        f.fvgs["5m"].append(f.cond)
        f.step(8,created=[f.cond])
        f.step(14,closed={"5m":[f.bar(10,103,104,100,103,"5m")]})
        self.assertTrue(n.condition_respected)
        self.assertIsNone(n.primary_dol)
        self.assertFalse(n.can_enter(n.direction,f.t+timedelta(minutes=14)))

    def test_pending_cancelled_after_invalidation_before_next_fill(self):
        cfg=BotConfig(narrative_mode=NarrativeMode.STRICT)
        broker=BacktestBroker(cfg);engine=PBStrategyEngine(cfg,broker)
        f=NarrativeFixture();n=f.active();engine.narrative=f.engine
        order=OrderRequest("MNQ",Direction.BULLISH,1,"LIMIT",99,95,110,"TEST",f.bar(14).timestamp,{"narrative_id":n.id})
        broker.submit_order(order)
        b=f.bar(15);e=Bar("ES","1m",b.timestamp,200,204,196,200)
        def invalidate(*args,**kwargs):
            n.phase=NarrativePhase.INVALIDATED
        with patch.object(engine,"on_1m_pair",side_effect=invalidate):
            process_pair(engine,broker,b,e)
        self.assertIsNone(broker.pending_order)
        self.assertIsNone(broker.position)
        self.assertEqual(engine.rejections[-1].code,"PENDING_NARRATIVE_INVALIDATED")

    def _execute_strict_fixture(self, extreme):
        """Exercise the actual order path with deterministic narrative/IFVG inputs."""
        f=NarrativeFixture();n=f.active()
        cfg=BotConfig(narrative_mode=NarrativeMode.STRICT)
        broker=BacktestBroker(cfg);engine=PBStrategyEngine(cfg,broker)
        engine.narrative=f.engine
        b=f.bar(15);e=Bar("ES","1m",b.timestamp,200,204,196,200)
        inv=IFVG(f.opposing,n.direction,b.timestamp,b,1)
        with ExitStack() as stack:
            stack.enter_context(patch.object(engine.narrative,"update"))
            stack.enter_context(patch.object(engine.bias_engine,"evaluate_daily_profile",return_value=BiasState(bias=n.direction)))
            stack.enter_context(patch.object(engine.nq_fvgs["1m"],"update",return_value=(None,[inv])))
            stack.enter_context(patch.object(engine,"_select_highest_tf_ifvg",return_value=(inv,False)))
            stack.enter_context(patch.object(engine,"_check_key_level_gate",return_value=(True,"CONTINUATION","fixture",f.cond)))
            stack.enter_context(patch.object(engine,"_get_manipulation_stop_anchor",return_value=(extreme,None)))
            # If strict mode asks the old RR-driven target search to run, fail.
            stack.enter_context(patch.object(engine.liquidity,"collect_dol_targets",side_effect=AssertionError("RR target shopping")))
            order=engine.on_1m_pair(b,e)
        return order,engine,broker,n

    def test_strict_execution_uses_locked_target(self):
        order,engine,broker,n=self._execute_strict_fixture(100.25)
        self.assertIsNotNone(order)
        self.assertEqual(order.take_profit,n.primary_dol.price)
        self.assertEqual(order.metadata["narrative_id"],n.id)
        self.assertIsNotNone(broker.position)

    def test_strict_execution_rejects_rr_instead_of_changing_target(self):
        order,engine,broker,n=self._execute_strict_fixture(90.25)
        self.assertIsNone(order)
        self.assertIsNone(broker.position)
        self.assertEqual(engine.rejections[-1].code,"RR_BELOW_1_TO_1")
        self.assertEqual(n.primary_dol.price,110)

    def test_observe_preserves_baseline_strict_blocks_unready_suite(self):
        nq,es=generate_synthetic_multi_day_suite()
        observe=run_backtest(nq,es,BotConfig(narrative_mode=NarrativeMode.OBSERVE))
        strict=run_backtest(nq,es,BotConfig(narrative_mode=NarrativeMode.STRICT))
        self.assertEqual(len(observe.trades),2)
        self.assertAlmostEqual(observe.final_balance,50421.56)
        self.assertGreater(len(observe.narrative_events),0)
        self.assertEqual(observe.narrative_events,strict.narrative_events)
        self.assertEqual(len(strict.trades),0)
        self.assertTrue(any(r.code=="NARRATIVE_NOT_READY" for r in strict.rejections))


if __name__ == "__main__":
    unittest.main()
