import argparse
import json
from pathlib import Path

from datetime import time
from pb_bot.backtest.ablation import format_ablation_table, run_ablation_study
from pb_bot.backtest.compare import format_comparison_table, run_comparison_matrix
from pb_bot.backtest.report import format_report
from pb_bot.backtest.simulator import run_backtest
from pb_bot.backtest.visualizer import generate_html_report
from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode, NarrativeMode
from pb_bot.core.calendar import EconomicCalendar
from pb_bot.data.feed import (
    generate_synthetic_multi_day_suite,
    generate_synthetic_ny_session,
    load_csv_bars,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run PB Trading Bot Backtest")
    parser.add_argument("--nq-csv", type=Path, help="Path to 1m NQ/MNQ CSV file")
    parser.add_argument("--es-csv", type=Path, help="Path to 1m ES/MES CSV file")
    parser.add_argument("--synthetic", action="store_true", help="Run single-day synthetic NQ+ES session")
    parser.add_argument("--suite", action="store_true", help="Run 3-day synthetic stress-test suite")
    parser.add_argument("--compare", action="store_true", help="Compare all Entry x Break-Even config modes")
    parser.add_argument("--ablation", action="store_true", help="Run filter ablation study across Phase 4 gates")
    parser.add_argument("--am-end", default="11:00", help="NY AM window end HH:MM (e.g. 11:00 or 11:30)")
    parser.add_argument("--narrative-mode", choices=[m.value for m in NarrativeMode], default=NarrativeMode.OBSERVE.value)
    parser.add_argument("--narrative-log", type=Path, help="Write narrative transitions as JSONL")
    parser.add_argument("--calendar", type=Path, help="NY-time news CSV")
    parser.add_argument("--html", type=Path, help="Export visual HTML report to this path")
    parser.add_argument(
        "--entry-mode",
        choices=[m.value for m in EntryOrderMode],
        default=EntryOrderMode.MARKET_ON_CLOSE.value,
    )
    parser.add_argument(
        "--be-mode",
        choices=[m.value for m in BreakEvenMode],
        default=BreakEvenMode.SWING_5M.value,
    )
    parser.add_argument("--allow-pm", action="store_true", help="Enable PM session (13:30-15:30 NY)")
    parser.add_argument("--verbose", action="store_true", help="Print detailed rejection reasons")
    args = parser.parse_args()

    if args.nq_csv and args.es_csv:
        nq_bars = load_csv_bars(args.nq_csv, "NQ", "1m")
        es_bars = load_csv_bars(args.es_csv, "ES", "1m")
    elif args.suite:
        nq_bars, es_bars = generate_synthetic_multi_day_suite()
    else:
        nq_bars, es_bars = generate_synthetic_ny_session()

    end_h, end_m = (int(x) for x in args.am_end.split(':'))
    cfg = BotConfig(
        am_end=time(end_h, end_m),
        entry_mode=EntryOrderMode(args.entry_mode),
        be_mode=BreakEvenMode(args.be_mode),
        allow_pm_session=args.allow_pm,
        narrative_mode=NarrativeMode(args.narrative_mode),
    )

    calendar = EconomicCalendar.from_csv(args.calendar) if args.calendar else None
    if calendar is None:
        print("WARNING: no news calendar supplied; news filters are inactive.")

    if args.ablation:
        ab_rows = run_ablation_study(nq_bars, es_bars, cfg, calendar=calendar)
        print(format_ablation_table(ab_rows))
        print()

    if args.compare:
        rows = run_comparison_matrix(nq_bars, es_bars, cfg, calendar=calendar)
        print(format_comparison_table(rows))
        print()

    result = run_backtest(nq_bars, es_bars, cfg, calendar=calendar)
    print(f"Narrative mode: {cfg.narrative_mode.value} | transitions: {len(result.narrative_events)}")
    print(format_report(result, verbose=args.verbose))
    if args.narrative_log:
        args.narrative_log.parent.mkdir(parents=True, exist_ok=True)
        args.narrative_log.write_text("".join(json.dumps(e) + "\n" for e in result.narrative_events), encoding="utf-8")

    if args.html:
        out = generate_html_report(result, nq_bars, args.html)
        print(f"\nVisual HTML report saved to: {out.resolve()}")


if __name__ == "__main__":
    main()
