import unittest
from datetime import datetime, timedelta

from pb_bot.core.models import Bar, Direction, FVGState
from pb_bot.core.primitives import (
    BreakerBlockDetector,
    CISDDetector,
    DevilsMarkDetector,
    FVGDetector,
    equilibrium_levels,
    oversized_wick_eq,
)
from pb_bot.core.sessions import NY_TZ, SessionTracker
from pb_bot.data.feed import generate_synthetic_ny_session, sync_dual_feed
from pb_bot.data.resampler import MultiTimeframeResampler, bucket_start


class TestPhase1Core(unittest.TestCase):
    def test_4h_ny_alignment_matches_10am_and_2pm(self):
        t_0959 = datetime(2025, 6, 24, 9, 59, tzinfo=NY_TZ)
        t_1000 = datetime(2025, 6, 24, 10, 0, tzinfo=NY_TZ)
        t_1400 = datetime(2025, 6, 24, 14, 0, tzinfo=NY_TZ)
        t_0015 = datetime(2025, 6, 24, 0, 15, tzinfo=NY_TZ)

        self.assertEqual(bucket_start(t_0959, "4H").hour, 6)
        self.assertEqual(bucket_start(t_1000, "4H").hour, 10)
        self.assertEqual(bucket_start(t_1400, "4H").hour, 14)
        self.assertEqual(bucket_start(t_0015, "4H").hour, 22)

    def test_zero_latency_resampler_closes_5m_at_minute_4(self):
        resampler = MultiTimeframeResampler("NQ", ("1m", "3m", "5m"))
        base = datetime(2025, 6, 24, 9, 30, tzinfo=NY_TZ)
        closed_5m = []
        for m in range(5):
            b = Bar("NQ", "1m", base + timedelta(minutes=m), 100 + m, 102 + m, 99 + m, 101 + m, 10)
            out = resampler.update(b)
            if "5m" in out:
                closed_5m.extend(out["5m"])

        self.assertEqual(len(closed_5m), 1)
        self.assertEqual(closed_5m[0].timestamp, base)
        self.assertEqual(closed_5m[0].open, 100)
        self.assertEqual(closed_5m[0].close, 105)
        self.assertEqual(closed_5m[0].volume, 50)

    def test_fvg_target_to_condition_flip_and_strict_body_ifvg(self):
        det = FVGDetector("NQ", "3m")
        t = datetime(2025, 6, 24, 9, 30, tzinfo=NY_TZ)

        # Create Bearish FVG [102, 105]
        det.update(Bar("NQ", "3m", t, 107, 108, 105, 106))
        det.update(Bar("NQ", "3m", t + timedelta(minutes=3), 106, 106, 100, 101))
        fvg, _ = det.update(Bar("NQ", "3m", t + timedelta(minutes=6), 101, 102, 98, 99))
        self.assertIsNotNone(fvg)
        self.assertEqual(fvg.direction, Direction.BEARISH)
        self.assertEqual(fvg.state, FVGState.UNFILLED_TARGET)

        # Wick above 105 with body close INSIDE (104) -> flips to ACTIVE_CONDITION, NOT inverted
        _, ifvgs = det.update(Bar("NQ", "3m", t + timedelta(minutes=9), 99, 106.5, 99, 104))
        self.assertEqual(fvg.state, FVGState.ACTIVE_CONDITION)
        self.assertEqual(len(ifvgs), 0)

        # Wrong timeframe rejected by exact-TF invariant
        with self.assertRaises(ValueError):
            det.update(Bar("NQ", "1m", t + timedelta(minutes=10), 104, 108, 104, 107))

        # Exact 3m body close above 105 -> confirms Bullish IFVG
        _, ifvgs = det.update(Bar("NQ", "3m", t + timedelta(minutes=12), 104, 108, 103.5, 106.5))
        self.assertEqual(fvg.state, FVGState.INVERTED)
        self.assertEqual(len(ifvgs), 1)
        self.assertEqual(ifvgs[0].direction, Direction.BULLISH)
        self.assertEqual(ifvgs[0].candles_to_invert, 2)

    def test_cisd_requires_body_close_beyond_first_candle_open(self):
        det = CISDDetector("NQ", "1m")
        t = datetime(2025, 6, 24, 9, 30, tzinfo=NY_TZ)

        # Two consecutive up-close candles; first open = 100
        det.update(Bar("NQ", "1m", t, 100, 103, 99.5, 102))
        det.update(Bar("NQ", "1m", t + timedelta(minutes=1), 102, 106, 101.5, 105))

        # Wick below 100 with close at 100.5 -> NO CISD
        res = det.update(Bar("NQ", "1m", t + timedelta(minutes=2), 105, 105.5, 99.0, 100.5))
        self.assertIsNone(res)

        # Body close below 100 (98.5) -> Bearish CISD + OB open pinned to 100
        res = det.update(Bar("NQ", "1m", t + timedelta(minutes=3), 100.5, 101, 98.0, 98.5))
        self.assertIsNotNone(res)
        self.assertEqual(res.direction, Direction.BEARISH)
        self.assertEqual(res.cisd_level, 100.0)
        self.assertEqual(res.orderblock.open_price, 100.0)
        self.assertEqual(res.orderblock.sweep_extreme, 106.0)

        # Closing above OB high converts OB to Breaker
        det.update(Bar("NQ", "1m", t + timedelta(minutes=4), 98.5, 107.0, 98.5, 106.5))
        self.assertTrue(res.orderblock.invalidated)
        self.assertTrue(res.orderblock.converted_to_breaker)

    def test_breaker_block_rejects_wick_only_and_marks_full_candle(self):
        det = BreakerBlockDetector("NQ", "2m")
        t = datetime(2025, 6, 24, 9, 44, tzinfo=NY_TZ)

        # Bar 0 -> Low_1 (98) -> High_1 (106, up-close L=99 H=106) -> LowerLow (95)
        det.update(Bar("NQ", "2m", t, 102, 105, 100, 101))
        det.update(Bar("NQ", "2m", t + timedelta(minutes=2), 101, 103, 98, 99))
        det.update(Bar("NQ", "2m", t + timedelta(minutes=4), 99, 106, 99, 105))
        det.update(Bar("NQ", "2m", t + timedelta(minutes=6), 105, 105, 95, 96))

        # Wick above 106 with close <= 106 -> STRICTLY INVALID (sweep only)
        bbs = det.update(Bar("NQ", "2m", t + timedelta(minutes=8), 96, 107.5, 96, 104))
        self.assertEqual(len(bbs), 0)
        self.assertEqual(len(det.sweep_only_events), 1)

        # Body close above 106 -> Confirmed Bullish Breaker spanning full up-close candle [99, 106]
        bbs = det.update(Bar("NQ", "2m", t + timedelta(minutes=10), 104, 110, 103, 109))
        self.assertEqual(len(bbs), 1)
        self.assertEqual(bbs[0].direction, Direction.BULLISH)
        self.assertEqual(bbs[0].bottom, 99.0)
        self.assertEqual(bbs[0].top, 106.0)

    def test_devils_mark_and_equilibrium(self):
        dm_det = DevilsMarkDetector("NQ", "15m")
        ts_0200 = datetime(2025, 6, 24, 2, 0, tzinfo=NY_TZ)
        dm = dm_det.update(Bar("NQ", "15m", ts_0200, 20_000.0, 20_025.0, 20_000.0, 20_020.0))
        self.assertIsNotNone(dm)
        self.assertEqual(dm.open_price, 20_000.0)

        eq = equilibrium_levels(100.0, 200.0)
        self.assertEqual(eq["0.5"], 150.0)

        wick_bar = Bar("NQ", "1m", ts_0200, 110.0, 112.0, 100.0, 111.0)
        self.assertEqual(oversized_wick_eq(wick_bar, Direction.BULLISH), 105.0)

    def test_synthetic_dual_feed_and_session_tracker(self):
        nq_bars, es_bars = generate_synthetic_ny_session()
        tracker = SessionTracker()
        resampler_nq = MultiTimeframeResampler("NQ")
        paired_count = 0

        for nq_b, _ in sync_dual_feed(nq_bars, es_bars):
            paired_count += 1
            st = tracker.update(nq_b)
            resampler_nq.update(nq_b)

        self.assertEqual(paired_count, 18 * 60)
        self.assertIsNotNone(st.true_day_open)
        self.assertIsNotNone(st.open_930)
        self.assertIsNotNone(st.open_1000)
        self.assertTrue(st.asia.is_valid)
        self.assertTrue(st.london.is_valid)
        self.assertGreater(len(resampler_nq.history["15m"]), 50)
        self.assertGreater(len(resampler_nq.history["4H"]), 3)


if __name__ == "__main__":
    unittest.main()
