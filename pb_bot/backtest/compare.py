from dataclasses import dataclass, replace
from typing import List, Optional, Tuple

from pb_bot.backtest.simulator import BacktestResult, run_backtest
from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode
from pb_bot.core.models import Bar


@dataclass(frozen=True)
class ComparisonRow:
    entry_mode: str
    be_mode: str
    trades: int
    win_rate: float
    net_pnl: float
    avg_r: float
    profit_factor: float
    max_drawdown: float


DEFAULT_MATRIX: Tuple[Tuple[EntryOrderMode, BreakEvenMode], ...] = (
    (EntryOrderMode.MARKET_ON_CLOSE, BreakEvenMode.SWING_5M),
    (EntryOrderMode.MARKET_ON_CLOSE, BreakEvenMode.STRICT_INTERNAL),
    (EntryOrderMode.MARKET_ON_CLOSE, BreakEvenMode.OFF),
    (EntryOrderMode.LIMIT_IFVG_EDGE, BreakEvenMode.SWING_5M),
    (EntryOrderMode.LIMIT_CISD_OPEN, BreakEvenMode.SWING_5M),
)


def _summarize(result: BacktestResult, entry_mode: str, be_mode: str) -> ComparisonRow:
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars < 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    net_pnl = result.final_balance - result.initial_balance
    avg_r = (sum(t.r_multiple for t in trades) / total) if total else 0.0
    gross_win = sum(t.pnl_dollars for t in wins)
    gross_loss = abs(sum(t.pnl_dollars for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    peak = result.initial_balance
    max_dd = 0.0
    for _, eq in result.equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    return ComparisonRow(entry_mode, be_mode, total, win_rate, net_pnl, avg_r, pf, max_dd)


def run_comparison_matrix(
    nq_bars: List[Bar],
    es_bars: List[Bar],
    base_config: Optional[BotConfig] = None,
) -> List[ComparisonRow]:
    base = base_config or BotConfig()
    rows: List[ComparisonRow] = []
    for entry_mode, be_mode in DEFAULT_MATRIX:
        print(f'  Running compare: {entry_mode.value} | {be_mode.value}...', flush=True)
        cfg = replace(base, entry_mode=entry_mode, be_mode=be_mode)
        res = run_backtest(nq_bars, es_bars, cfg)
        rows.append(_summarize(res, entry_mode.value, be_mode.value))
    return rows


def format_comparison_table(rows: List[ComparisonRow]) -> str:
    header = (
        f"{'ENTRY MODE':<17} | {'BE MODE':<15} | {'TRADES':>6} | {'WIN%':>6} | "
        f"{'NET PNL':>10} | {'AVG R':>7} | {'PF':>6} | {'MAX DD':>9}"
    )
    sep = "-" * len(header)
    lines = ["================ PB BOT CONFIG COMPARISON ================", header, sep]
    for r in rows:
        pf_str = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        lines.append(
            f"{r.entry_mode:<17} | {r.be_mode:<15} | {r.trades:>6} | {r.win_rate:>5.1f}% | "
            f"${r.net_pnl:>+9,.2f} | {r.avg_r:>+6.2f}R | {pf_str:>6} | ${r.max_drawdown:>8,.2f}"
        )
    lines.append("=" * len(header))
    return "\n".join(lines)
