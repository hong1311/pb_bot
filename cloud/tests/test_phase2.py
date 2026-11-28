import unittest
from datetime import datetime, timedelta

from pb_bot.backtest.report import format_report
from pb_bot.backtest.simulator import run_backtest
from pb_bot.config import BotConfig, BreakEvenMode, MNQ_SPEC
from pb_bot.core.liquidity import LiquidityPool, LiquidityTracker
from pb_bot.core.models import Bar, Direction, FVG, FVGState
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.data.feed import generate_synthetic_ny_session
from pb_bot.engine.bias_profile import BiasAndProfileEngine, DailyProfile
from pb_bot.engine.risk_manager import OpenPosition, RiskManager


class TestPhase2Engine(unittest.TestCase):
    def test_spread_apart_eqh_blocks_short_while_tight_eqh_allowed(self):
        liq = LiquidityTracker(eq_tolerance_pts=1.0, spread_apart_min_bars=3)
        t = datetime(2025, 6, 24, 9, 30, tzinfo=NY_TZ)

        # Build two swing highs at 105 separated by 4 bars (spread-apart EQH)
        prices = [100, 105, 101, 100, 101, 105, 101]
        for i, h in enumerate(prices):
            b = Bar("NQ", "5m", t + timedelta(minutes=5 * i), h - 2, h, h - 3, h - 1)
            liq.update_bars("5m", nq_bar=b)

        blocker = liq.has_spread_apart_obstruction(Direction.BEARISH, current_price=100.0)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.kind, "EQH")
        self.assertGreaterEqual(blocker.bar_span, 3)

    def test_bullish_smt_reclassifies_lrl_lows_as_smt(self):
        liq = LiquidityTracker()
        t = datetime(2025, 6, 24, 9, 0, tzinfo=NY_TZ)

        # NQ forms 3 ascending lows (100, 102, 104); ES sweeps LOWER on the final swing (50, 51, 49)
        nq_lows = [105, 100, 106, 102, 107, 104, 108]
        es_lows = [55, 50, 56, 51, 57, 49, 58]
        for i, (nl, el) in enumerate(zip(nq_lows, es_lows)):
            ts = t + timedelta(minutes=5 * i)
            liq.update_bars(
                "5m",
                nq_bar=Bar("NQ", "5m", ts, nl + 1, nl + 2, nl, nl + 1.5),
                es_bar=Bar("ES", "5m", ts, el + 1, el + 2, el, el + 1.5),
            )

        now = t + timedelta(minutes=35)
        self.assertTrue(liq.has_recent_smt(Direction.BULLISH, now))
        # Because Bullish SMT occurred, stacked NQ lows are NOT classified as downside LRL
        lrl_pools = liq.detect_lrl("5m", current_price=109.0, now=now)
        self.assertEqual(len(lrl_pools), 0)

    def test_risk_manager_05pct_sizing_rr_floor_and_target_smt_exit(self):
        cfg = BotConfig(account_balance=50_000.0, risk_per_trade_pct=0.005, instrument=MNQ_SPEC)
        rm = RiskManager(cfg)

        # 0.5% of $50k = $250 risk. 25 pts on MNQ ($2/pt) = $50/contract -> 5 contracts
        self.assertEqual(rm.calculate_contracts(20_000.0, 19_975.0), 5)
        # Continuation extreme halves size -> 2 contracts ($125 / $50 = 2.5 -> floor 2)
        self.assertEqual(rm.calculate_contracts(20_000.0, 19_975.0, is_continuation_extreme=True), 2)

        # Hard >= 1:1 RR floor extends past 0.6R pool to 1.5R pool
        pools = [
            LiquidityPool("ASIA_H", 20_015.0, Direction.BULLISH, "SESSION", 6, "ASIA_H"),
            LiquidityPool("PDH", 20_037.5, Direction.BULLISH, "1D", 5, "PDH", correlated_price=5820.0),
        ]
        chosen, rr = rm.select_target_with_rr_floor(Direction.BULLISH, 20_000.0, 19_975.0, pools)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.label, "PDH")
        self.assertAlmostEqual(rr, 1.5)

        # Target SMT Exit: ES hits 5820.0 before NQ reaches TP -> immediate market exit
        pos = OpenPosition(
            order_id="SIM_0001",
            symbol="NQ",
            direction=Direction.BULLISH,
            quantity=5,
            entry_price=20_000.0,
            initial_stop_loss=19_975.0,
            stop_loss=19_975.0,
            take_profit=20_037.5,
            opened_at=datetime(2025, 6, 24, 9, 45, tzinfo=NY_TZ),
            es_target_price=5820.0,
        )
        es_bar_hit = Bar("ES", "1m", datetime(2025, 6, 24, 10, 0, tzinfo=NY_TZ), 5818, 5820.5, 5817, 5819.5)
        self.assertIsNotNone(rm.check_target_smt_exit(pos, es_bar_hit))

    def test_daily_profile_seek_and_destroy_and_sponsorship_floor(self):
        cfg = BotConfig()
        engine = BiasAndProfileEngine(cfg)
        sess = DailySessionState(
            trading_day=datetime(2025, 6, 24).date(),
            london=SessionRange(high=20_050.0, low=19_950.0, open_price=20_000.0, close_price=20_010.0),
            true_day_open=20_000.0,
        )

        # Only a 15m FVG tapped in London -> NOT sponsored (>=30m floor)
        fvg_15m = FVG(
            id="f1",
            symbol="NQ",
            timeframe="15m",
            direction=Direction.BULLISH,
            created_at=datetime(2025, 6, 24, 1, 0, tzinfo=NY_TZ),
            bottom=19_940.0,
            top=19_960.0,
            state=FVGState.ACTIVE_CONDITION,
            first_tap_at=datetime(2025, 6, 24, 3, 0, tzinfo=NY_TZ),
        )
        st = engine.evaluate_daily_profile(
            datetime(2025, 6, 24, 9, 30, tzinfo=NY_TZ),
            sess,
            {"15m": [fvg_15m], "30m": [], "1H": [], "4H": [], "1D": []},
        )
        self.assertFalse(st.london_sponsored)
        self.assertEqual(st.profile, DailyProfile.LONDON_JUDAS_NY_REVERSAL)

        # Both London high & low swept -> Seek & Destroy kill switch
        sess.london.high_swept = True
        sess.london.low_swept = True
        st = engine.evaluate_daily_profile(
            datetime(2025, 6, 24, 9, 45, tzinfo=NY_TZ),
            sess,
            {"15m": [fvg_15m], "30m": [], "1H": [], "4H": [], "1D": []},
        )
        self.assertTrue(st.seek_and_destroy_active)
        self.assertEqual(st.profile, DailyProfile.SEEK_AND_DESTROY)

    def test_end_to_end_synthetic_backtest_runs_cleanly(self):
        nq_bars, es_bars = generate_synthetic_ny_session()
        cfg = BotConfig(be_mode=BreakEvenMode.SWING_5M)
        result = run_backtest(nq_bars, es_bars, cfg)
        report = format_report(result)
        self.assertIn("PB BOT BACKTEST REPORT", report)
        self.assertGreaterEqual(len(result.trades), 1)
        self.assertGreater(result.final_balance, result.initial_balance)


if __name__ == "__main__":
    unittest.main()
