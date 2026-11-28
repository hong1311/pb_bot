import unittest
from datetime import datetime, timedelta

from pb_bot.backtest.simulator import BacktestBroker, process_pair, settle_end_of_data
from pb_bot.config import BotConfig
from pb_bot.core.liquidity import LiquidityTracker
from pb_bot.core.models import Bar, Direction, FVG, FVGState, IFVG, OrderRequest
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.engine.bias_profile import BiasAndProfileEngine
from pb_bot.engine.risk_manager import RiskManager
from pb_bot.engine.strategy import PBStrategyEngine


class TestHardening(unittest.TestCase):
    def setUp(self):
        self.cfg = BotConfig()
        self.t = datetime(2025, 6, 24, 9, 40, tzinfo=NY_TZ)

    def bar(self, minutes, o=102, h=103, l=93, c=94):
        return Bar('NQ', '1m', self.t + timedelta(minutes=minutes), o, h, l, c)

    def order(self, direction=Direction.BULLISH, order_type='LIMIT'):
        long = direction == Direction.BULLISH
        return OrderRequest('MNQ', direction, 1, order_type, 100,
                            95 if long else 105, 110 if long else 90, 'TEST', self.t)

    def test_limit_fill_stops_same_bar(self):
        broker = BacktestBroker(self.cfg, slippage_ticks=0)
        broker.submit_order(self.order())
        trade = broker.process_bar(self.bar(1))
        self.assertEqual(trade.exit_reason, 'STOP_LOSS')
        self.assertEqual(trade.exit_price, 95)
        self.assertIsNone(broker.position)

    def test_short_limit_fill_stops_same_bar(self):
        broker = BacktestBroker(self.cfg, slippage_ticks=0)
        broker.submit_order(self.order(Direction.BEARISH))
        trade = broker.process_bar(self.bar(1, 98, 107, 97, 106))
        self.assertEqual(trade.exit_reason, 'STOP_LOSS')
        self.assertEqual(trade.exit_price, 105)

    def test_limit_fill_does_not_assume_same_bar_target(self):
        broker = BacktestBroker(self.cfg)
        broker.submit_order(self.order())
        self.assertIsNone(broker.process_bar(self.bar(1, 102, 112, 99, 101)))
        self.assertIsNotNone(broker.position)

    def test_market_close_order_not_stopped_by_pre_entry_wick(self):
        broker = BacktestBroker(self.cfg)
        broker.submit_order(self.order(order_type='MARKET'))
        self.assertIsNone(broker.process_bar(self.bar(0)))
        self.assertIsNotNone(broker.position)

    def test_pending_expires_and_cannot_be_overwritten(self):
        broker = BacktestBroker(self.cfg)
        broker.submit_order(self.order())
        with self.assertRaises(RuntimeError):
            broker.submit_order(self.order())
        broker.process_bar(self.bar(self.cfg.pending_order_max_minutes))
        self.assertIsNone(broker.pending_order)
        self.assertIsNone(broker.position)

    def test_pending_expires_when_target_reached_without_fill(self):
        broker = BacktestBroker(self.cfg)
        broker.submit_order(self.order())
        broker.process_bar(self.bar(1, 102, 110, 101, 109))
        self.assertIsNone(broker.pending_order)

    def test_reduced_risk_cannot_force_one_contract(self):
        risk = RiskManager(self.cfg)
        self.assertEqual(risk.calculate_contracts(20000, 19900, True), 0)

    def test_old_london_tap_cannot_sponsor_today(self):
        session = DailySessionState(self.t.date(), london=SessionRange(high=120, low=90))
        old = self.t.replace(hour=3) - timedelta(days=4)
        fvg = FVG('old', 'NQ', '1H', Direction.BULLISH, old, 90, 95,
                  FVGState.ACTIVE_CONDITION, old)
        engine = BiasAndProfileEngine(self.cfg)
        self.assertIsNone(engine._check_london_sponsorship(session, {'1H': [fvg]}))
        fvg.first_tap_at = self.t.replace(hour=3)
        self.assertEqual(engine._check_london_sponsorship(session, {'1H': [fvg]}), '1H')

    def test_swept_session_target_is_excluded(self):
        session = DailySessionState(self.t.date(), london=SessionRange(high=120, low=90, high_swept=True),
                                    asia=SessionRange(high=130, low=85))
        pools = LiquidityTracker().collect_dol_targets(Direction.BULLISH, 100, self.t, session, None, [], [])
        self.assertNotIn('LONDON_HIGH', [p.label for p in pools])
        self.assertIn('ASIA_HIGH', [p.label for p in pools])

    def test_end_of_data_settles_open_trade(self):
        broker = BacktestBroker(self.cfg)
        engine = PBStrategyEngine(self.cfg, broker)
        broker.submit_order(self.order(order_type='MARKET'))
        settle_end_of_data(engine, broker, self.bar(0, 100, 103, 99, 102))
        self.assertEqual(broker.trades[0].exit_reason, 'END_OF_DATA')
        self.assertIsNone(engine.active_position)

    def test_alignment_requires_post_tap_confirmation(self):
        engine = PBStrategyEngine(self.cfg, BacktestBroker(self.cfg))
        fvg = FVG('key', 'NQ', '1H', Direction.BULLISH, self.t, 90, 95,
                  FVGState.ACTIVE_CONDITION, self.t)
        source = FVG('opp', 'NQ', '5m', Direction.BEARISH, self.t, 95, 100, FVGState.INVERTED)
        engine.nq_fvgs['5m'].ifvgs.append(IFVG(source, Direction.BULLISH, self.t - timedelta(minutes=5), self.bar(0), 1))
        self.assertFalse(engine._has_aligned_confirmation(fvg, Direction.BULLISH, self.t))
        engine.nq_fvgs['5m'].ifvgs.append(IFVG(source, Direction.BULLISH, self.t, self.bar(0), 1))
        self.assertTrue(engine._has_aligned_confirmation(fvg, Direction.BULLISH, self.t))

    def test_highest_tf_waits_for_outer_gap(self):
        engine = PBStrategyEngine(self.cfg, BacktestBroker(self.cfg))
        engine.nq_resampler.history['1m'] = [self.bar(0, 120, 121, 119, 120), self.bar(1, 120, 120, 90, 91), self.bar(2, 91, 112, 91, 110)]
        inner = FVG('inner', 'NQ', '5m', Direction.BEARISH, self.t, 95, 100, FVGState.INVERTED)
        outer = FVG('outer', 'NQ', '5m', Direction.BEARISH, self.t, 105, 110)
        engine.nq_fvgs['5m'].fvgs.extend([inner, outer])
        confirmed = IFVG(inner, Direction.BULLISH, self.t, self.bar(2), 1)
        self.assertEqual(engine._select_highest_tf_ifvg(Direction.BULLISH, {'5m': [confirmed]}, self.t, self.bar(2)), (None, False))
        outer.state = FVGState.INVERTED
        confirmed = IFVG(outer, Direction.BULLISH, self.t, self.bar(2), 1)
        self.assertIs(engine._select_highest_tf_ifvg(Direction.BULLISH, {'5m': [confirmed]}, self.t, self.bar(2))[0], confirmed)


if __name__ == '__main__':
    unittest.main()
