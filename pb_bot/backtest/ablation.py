from dataclasses import dataclass, replace
from datetime import time
from typing import List, Optional, Tuple

from pb_bot.backtest.compare import _summarize
from pb_bot.backtest.simulator import run_backtest
from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode
from pb_bot.core.models import Bar


@dataclass(frozen=True)
class AblationRow:
    profile_name: str
    trades: int
    win_rate: float
    net_pnl: float
    avg_r: float
    profit_factor: float
    max_drawdown: float
    filtered_events: int


def run_ablation_study(
    nq_bars: List[Bar],
    es_bars: List[Bar],
    base_config: Optional[BotConfig] = None,
) -> List[AblationRow]:
    """Benchmarks Core PB vs. incremental Phase 4 regime filters & 11:30 delivery window."""
    base = base_config or BotConfig(
        entry_mode=EntryOrderMode.MARKET_ON_CLOSE,
        be_mode=BreakEvenMode.OFF,
    )

    variants: Tuple[Tuple[str, BotConfig], ...] = (
        (
            "1. Full Phase 4 (Strict 09:30-11:00)",
            replace(base),
        ),
        (
            "2. Full Phase 4 + 11:30 Delivery",
            replace(base, am_end=time(11, 30)),
        ),
        (
            "3. No Overnight Exhaustion Filter",
            replace(base, enable_overnight_exhaustion_filter=False),
        ),
        (
            "4. No 5m FVG Volatility Filter",
            replace(base, enable_am_5m_fvg_volatility_filter=False),
        ),
        (
            "5. No Time Alignment Matrix",
            replace(base, enable_time_alignment_matrix=False),
        ),
        (
            "6. Phase 3 Core (No Phase 4 Gates)",
            replace(
                base,
                enable_dual_smt_chop_filter=False,
                enable_overnight_exhaustion_filter=False,
                enable_am_5m_fvg_volatility_filter=False,
                enable_time_alignment_matrix=False,
                enable_stacked_fvg_eq_filter=False,
            ),
        ),
        (
            "7. Phase 3 Core + 11:30 Delivery",
            replace(
                base,
                am_end=time(11, 30),
                enable_dual_smt_chop_filter=False,
                enable_overnight_exhaustion_filter=False,
                enable_am_5m_fvg_volatility_filter=False,
                enable_time_alignment_matrix=False,
                enable_stacked_fvg_eq_filter=False,
            ),
        ),
    )

    rows: List[AblationRow] = []
    for name, cfg in variants:
        print(f'  Running ablation: {name}...', flush=True)
        res = run_backtest(nq_bars, es_bars, cfg)
        s = _summarize(res, cfg.entry_mode.value, cfg.be_mode.value)
        rows.append(
            AblationRow(
                profile_name=name,
                trades=s.trades,
                win_rate=s.win_rate,
                net_pnl=s.net_pnl,
                avg_r=s.avg_r,
                profit_factor=s.profit_factor,
                max_drawdown=s.max_drawdown,
                filtered_events=len(res.rejections),
            )
        )
    return rows


def format_ablation_table(rows: List[AblationRow]) -> str:
    header = (
        f"{'FILTER CONFIGURATION':<36} | {'TRADES':>6} | {'WIN%':>6} | "
        f"{'NET PNL':>10} | {'AVG R':>7} | {'PF':>6} | {'MAX DD':>9} | {'FILTERED':>8}"
    )
    sep = "-" * len(header)
    lines = ["================ PB BOT FILTER ABLATION STUDY ================", header, sep]
    for r in rows:
        pf_str = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        lines.append(
            f"{r.profile_name:<36} | {r.trades:>6} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | {r.avg_r:>+6.2f}R | {pf_str:>6} | ${r.max_drawdown:>8,.2f} | {r.filtered_events:>8}"
        )
    lines.append("=" * len(header))
    return "\n".join(lines)
