import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pb_bot.backtest.compare import format_comparison_table, run_comparison_matrix
from pb_bot.backtest.simulator import run_backtest
from pb_bot.backtest.visualizer import generate_html_report
from pb_bot.config import BotConfig
from pb_bot.core.models import Direction
from pb_bot.core.sessions import DailySessionState, NY_TZ, SessionRange
from pb_bot.data.feed import generate_synthetic_multi_day_suite, load_csv_bars
from pb_bot.engine.bias_profile import BiasAndProfileEngine, DailyProfile


class TestPhase3Toolkit(unittest.TestCase):
    def test_clean_london_judas_expansion_not_misclassified_as_seek_and_destroy(self):
        engine = BiasAndProfileEngine(BotConfig())
        engine.state.bias = Direction.BULLISH
        sess = DailySessionState(
            trading_day=datetime(2025, 6, 25).date(),
            london=SessionRange(
                high=20_050.0, low=19_950.0, open_price=20_000.0, close_price=20_010.0,
                high_swept=True, low_swept=True,
            ),
            true_day_open=20_000.0,
        )
        # Price expanded cleanly ABOVE London High -> NOT Seek & Destroy
        st = engine.evaluate_daily_profile(
            datetime(2025, 6, 25, 10, 5, tzinfo=NY_TZ),
            sess,
            {"15m": [], "30m": [], "1H": [], "4H": [], "1D": []},
            current_price=20_080.0,
        )
        self.assertFalse(st.seek_and_destroy_active)

        # Price whips back INSIDE London range after sweeping both sides -> Seek & Destroy
        st_chop = engine.evaluate_daily_profile(
            datetime(2025, 6, 25, 10, 10, tzinfo=NY_TZ),
            sess,
            {"15m": [], "30m": [], "1H": [], "4H": [], "1D": []},
            current_price=20_000.0,
        )
        self.assertTrue(st_chop.seek_and_destroy_active)
        self.assertEqual(st_chop.profile, DailyProfile.SEEK_AND_DESTROY)

    def test_multi_day_suite_trades_bull_and_bear_and_blocks_seek_and_destroy(self):
        nq_bars, es_bars = generate_synthetic_multi_day_suite()
        result = run_backtest(nq_bars, es_bars, BotConfig())

        dirs = {t.direction for t in result.trades}
        trade_days = {t.opened_at.date() for t in result.trades}
        self.assertIn(Direction.BULLISH, dirs)
        self.assertIn(Direction.BEARISH, dirs)
        # Day 3 (2025-06-27 NY) is Seek & Destroy -> must have zero trades
        self.assertNotIn(datetime(2025, 6, 27).date(), trade_days)

    def test_comparison_matrix_and_html_visualizer(self):
        nq_bars, es_bars = generate_synthetic_multi_day_suite()
        rows = run_comparison_matrix(nq_bars, es_bars)
        self.assertEqual(len(rows), 5)
        table = format_comparison_table(rows)
        self.assertIn("MARKET_ON_CLOSE", table)
        self.assertIn("LIMIT_CISD_OPEN", table)

        result = run_backtest(nq_bars, es_bars, BotConfig())
        with tempfile.TemporaryDirectory() as tmp:
            out = generate_html_report(result, nq_bars, Path(tmp) / "report.html")
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("PB Trading Bot", content)
            self.assertIn("drawTrade", content)

    def test_csv_loader_handles_tradingview_epoch_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tv_export.csv"
            p.write_text(
                "time,open,high,low,close,Volume\n"
                "1750771800,20000.0,20010.0,19995.0,20005.0,450\n",
                encoding="utf-8",
            )
            bars = load_csv_bars(p, "NQ", "1m")
            self.assertEqual(len(bars), 1)
            self.assertEqual(bars[0].close, 20005.0)
            self.assertEqual(bars[0].volume, 450.0)


if __name__ == "__main__":
    unittest.main()
