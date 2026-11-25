from collections import Counter
from pb_bot.backtest.simulator import BacktestResult


def format_report(result: BacktestResult, verbose: bool = False) -> str:
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars < 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    net_pnl = result.final_balance - result.initial_balance
    gross_win = sum(t.pnl_dollars for t in wins)
    gross_loss = abs(sum(t.pnl_dollars for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    avg_r = (sum(t.r_multiple for t in trades) / total) if total else 0.0

    # Max drawdown
    peak = result.initial_balance
    max_dd = 0.0
    for _, eq in result.equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)

    lines = [
        "================ PB BOT BACKTEST REPORT ================",
        f"Initial Balance : ${result.initial_balance:,.2f}",
        f"Final Balance   : ${result.final_balance:,.2f}",
        f"Net PnL         : ${net_pnl:+,.2f} ({net_pnl / result.initial_balance * 100:+.2f}%)",
        f"Total Trades    : {total} (Wins: {len(wins)}, Losses: {len(losses)}, Win Rate: {win_rate:.1f}%)",
        f"Profit Factor   : {profit_factor:.2f} | Expectancy: {avg_r:+.2f}R",
        f"Max Drawdown    : ${max_dd:,.2f}",
        "--------------------------------------------------------",
    ]

    if trades:
        lines.append("Trade Log:")
        for t in trades:
            lines.append(
                f"  [{t.opened_at.strftime('%Y-%m-%d %H:%M')} -> {t.closed_at.strftime('%H:%M')}] "
                f"{t.direction.value:7s} x{t.quantity} | Entry={t.entry_price:.2f} "
                f"Exit={t.exit_price:.2f} ({t.exit_reason}) | "
                f"{t.r_multiple:+.2f}R (${t.pnl_dollars:+,.2f}) | {t.setup_reason}"
            )
        lines.append("--------------------------------------------------------")

    counts = Counter(r.code for r in result.rejections)
    lines.append(f"Rejection Audit Log ({len(result.rejections)} filtered events):")
    if counts:
        for code, cnt in counts.most_common(10):
            lines.append(f"  - {code}: {cnt}")
    else:
        lines.append("  (none)")
    if verbose and result.rejections:
        lines.append("Sample Rejection Details (last 15):")
        for r in result.rejections[-15:]:
            lines.append(f"  [{r.timestamp.strftime('%Y-%m-%d %H:%M')}] {r.code}: {r.detail}")
    lines.append("========================================================")
    return "\n".join(lines)
