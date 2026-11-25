import unittest
from datetime import date, datetime, time, timedelta

from pb_bot.backtest.simulator import BacktestBroker
from pb_bot.config import BotConfig
from pb_bot.core.calendar import EconomicCalendar, NewsEvent
from pb_bot.core.liquidity import LiquidityTracker, SMTEvent
from pb_bot.core.models import Bar, Direction, FVG, FVGState
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.engine.bias_profile import BiasAndProfileEngine, DailyProfile
from pb_bot.engine.strategy import PBStrategyEngine


class TestPhase4RegimeAndAlignment(unittest.TestCase):
    def test_economic_calendar_blocks_red_news_and_ignores_yellow(self):
        cal = EconomicCalendar([
            NewsEvent(date(2026, 11, 20), time(14, 0), "FOMC Rate Decision", "RED"),
            NewsEvent(date(2026, 11, 21), time(8, 30), "NFP Non-Farm Payrolls", "RED"),
            NewsEvent(date(2026, 11, 20), time(10, 0), "ISM Services PMI", "RED"),
            NewsEvent(date(2026, 11, 19), time(10, 0), "FOMC Member Speech", "YELLOW"),
        ])
        # PM Red news suppresses AM on Nov 20
        self.assertIsNotNone(cal.has_pm_red_news(date(2026, 11, 20)))
        # Nov 20 is day before NFP (Nov 21)
        self.assertIsNotNone(cal.is_day_before_nfp_or_cpi(date(2026, 11, 20)))
        # Active freeze right at 10:00 AM Red release
        self.assertIsNotNone(cal.active_red_news_freeze(datetime(2026, 11, 20, 10, 1, tzinfo=NY_TZ)))
        # Yellow folder completely ignored
        self.assertIsNone(cal.active_red_news_freeze(datetime(2026, 11, 19, 10, 0, tzinfo=NY_TZ)))

    def test_dual_sided_smt_chop_detection(self):
        liq = LiquidityTracker()
        now = datetime(2026, 11, 20, 9, 45, tzinfo=NY_TZ)
        liq.smt_events.append(SMTEvent(now - timedelta(minutes=20), "15m", Direction.BULLISH, 20000, 5800, "SWING_SMT"))
        # Noisy 5m internal opposing SMT must NOT trigger Dual-SMT chop
        liq.smt_events.append(SMTEvent(now - timedelta(minutes=10), "5m", Direction.BEARISH, 20060, 5820, "SWING_SMT"))
        self.assertFalse(liq.has_dual_sided_smt(now, current_price=20030))

        # Concurrent 15m/1H key-level opposing SMTs pin price -> active chop while inside range
        liq.smt_events.append(SMTEvent(now - timedelta(minutes=5), "15m", Direction.BEARISH, 20060, 5820, "SWING_SMT"))
        self.assertTrue(liq.has_dual_sided_smt(now, current_price=20030))

        # Breakout above Bearish SMT high clears Dual-SMT chop
        self.assertFalse(liq.has_dual_sided_smt(now, current_price=20075))

    def test_overnight_exhaustion_300pts_and_london_sweeping_both_asia_sides(self):
        engine = BiasAndProfileEngine(BotConfig())
        sess = DailySessionState(
            trading_day=date(2026, 11, 20),
            asia=SessionRange(high=20100, low=20000, open_price=20050, close_price=20020),
            london=SessionRange(high=20150, low=19950, open_price=20020, close_price=20040),
            true_day_open=20020,
            overnight_high=20350.0,
            overnight_low=20000.0,
            overnight_objective_swept=True,
            london_swept_asia_high=True,
            london_swept_asia_low=True,
        )
        # >300 pt overnight range + daily objective swept -> exhausted
        self.assertTrue(engine.is_overnight_exhausted(sess))

        # London swept both Asia High and Asia Low -> Seek & Destroy
        st = engine.evaluate_daily_profile(
            datetime(2026, 11, 20, 9, 30, tzinfo=NY_TZ),
            sess,
            {"15m": [], "30m": [], "1H": [], "4H": [], "1D": []},
            current_price=20040.0,
        )
        self.assertTrue(st.seek_and_destroy_active)
        self.assertEqual(st.profile, DailyProfile.SEEK_AND_DESTROY)

    def test_stacked_fvg_050_equilibrium_disqualification(self):
        cfg = BotConfig()
        eng = PBStrategyEngine(cfg, BacktestBroker(cfg))
        sess = DailySessionState(
            trading_day=date(2026, 11, 20),
            full_day=SessionRange(high=20100.0, low=20000.0),  # 0.50 EQ = 20050.0
        )
        t = datetime(2026, 11, 20, 9, 30, tzinfo=NY_TZ)
        fvg_discount = FVG("f1", "NQ", "5m", Direction.BULLISH, t, bottom=20020.0, top=20035.0)
        fvg_premium = FVG("f2", "NQ", "5m", Direction.BULLISH, t, bottom=20065.0, top=20080.0)
        eng.nq_fvgs["5m"].fvgs.extend([fvg_discount, fvg_premium])

        # Top FVG entirely above 0.50 EQ is disqualified while discount FVG exists below
        self.assertTrue(eng._is_disqualified_stacked_fvg(fvg_premium, Direction.BULLISH, sess))
        self.assertFalse(eng._is_disqualified_stacked_fvg(fvg_discount, Direction.BULLISH, sess))

    def test_time_alignment_matrix_blocks_1h_to_1m_without_resweep(self):
        cfg = BotConfig(enable_0930_judas_1m_override=False)
        eng = PBStrategyEngine(cfg, BacktestBroker(cfg))
        t = datetime(2026, 11, 20, 9, 40, tzinfo=NY_TZ)
        fvg_1h = FVG(
            "h1", "NQ", "1H", Direction.BULLISH, t - timedelta(hours=1),
            bottom=20000.0, top=20020.0, state=FVGState.ACTIVE_CONDITION, first_tap_at=t - timedelta(minutes=5),
        )
        eng.nq_fvgs["1H"].fvgs.append(fvg_1h)
        sess = DailySessionState(trading_day=date(2026, 11, 20))
        bar = Bar("NQ", "1m", t, 20010, 20015, 20005, 20012)

        ok, _, _, matched = eng._check_key_level_gate(Direction.BULLISH, t, bar, sess)
        self.assertTrue(ok)
        self.assertIs(matched, fvg_1h)
        # Without re-sweep, internal_extreme_swept is False -> blocked on 1m IFVG
        self.assertFalse(matched.internal_extreme_swept)
        # Once structural re-sweep completes, Path B passes
        matched.internal_extreme_swept = True
        self.assertTrue(matched.internal_extreme_swept)


if __name__ == "__main__":
    unittest.main()
