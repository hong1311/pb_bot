import argparse
import json
from pathlib import Path

from datetime import time
from pb_bot.backtest.ablation import format_ablation_table, run_ablation_study
from pb_bot.backtest.compare import format_comparison_table, run_comparison_matrix
from pb_bot.backtest.report import format_report
from pb_bot.backtest.simulator import run_backtest
from pb_bot.backtest.visualizer import generate_html_report
from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode, NarrativeMode, PlaybookMode, PlaybookModel, StructureMode, DiscretionMode, RegimeMode
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
    parser.add_argument("--regime-mode", choices=[m.value for m in RegimeMode], default=RegimeMode.OBSERVE.value)
    parser.add_argument("--require-calendar", action="store_true", help="Block strict-mode entries unless an economic calendar is supplied")
    parser.add_argument("--discretion-mode", choices=[m.value for m in DiscretionMode], default=DiscretionMode.OBSERVE.value)
    parser.add_argument("--validated-wick-stop", action="store_true", help="Experimental: validated midpoint stop, strict discretion market entries only")
    parser.add_argument("--structure-mode", choices=[m.value for m in StructureMode], default=StructureMode.OBSERVE.value)
    parser.add_argument("--model-mode", choices=[m.value for m in PlaybookMode], default=PlaybookMode.OBSERVE.value)
    parser.add_argument("--models", nargs="+", choices=[m.value for m in PlaybookModel], default=[m.value for m in PlaybookModel])
    parser.add_argument("--regime-log", type=Path, help="Write regime decisions as JSONL")
    parser.add_argument("--discretion-log", type=Path, help="Write discretion decisions as JSONL")
    parser.add_argument("--structure-log", type=Path, help="Write structural decisions as JSONL")
    parser.add_argument("--model-log", type=Path, help="Write model candidate decisions as JSONL")
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

    if args.require_calendar and args.regime_mode != "STRICT":
        parser.error("--require-calendar requires --regime-mode STRICT")
    if args.discretion_mode == "STRICT" and args.structure_mode != "STRICT":
        parser.error("--discretion-mode STRICT requires --structure-mode STRICT --model-mode STRICT --narrative-mode STRICT")
    if args.validated_wick_stop and args.discretion_mode != "STRICT":
        parser.error("--validated-wick-stop requires --discretion-mode STRICT")
    if args.structure_mode == "STRICT" and args.model_mode != "STRICT":
        parser.error("--structure-mode STRICT requires --model-mode STRICT --narrative-mode STRICT")
    if args.model_mode == "STRICT" and args.narrative_mode != "STRICT":
        parser.error("--model-mode STRICT requires --narrative-mode STRICT")
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
        model_mode=PlaybookMode(args.model_mode), structure_mode=StructureMode(args.structure_mode), discretion_mode=DiscretionMode(args.discretion_mode),
        regime_mode=RegimeMode(args.regime_mode), regime_require_calendar=args.require_calendar,
        enable_validated_wick_stop=args.validated_wick_stop,
        enabled_models=tuple(PlaybookModel(m) for m in args.models),
    )

    calendar = EconomicCalendar.from_csv(args.calendar) if args.calendar else None
    if calendar is None:
        print("WARNING: calendar required; STRICT regime blocks entries." if args.require_calendar else "WARNING: no news calendar supplied; news filters are inactive.")

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
    print(f"Model mode: {cfg.model_mode.value} | enabled: {','.join(m.value for m in cfg.enabled_models)} | decisions: {len(result.model_events)}")
    print(f"Structure mode: {cfg.structure_mode.value} | decisions: {len(result.structure_events)}")
    print(f"Discretion mode: {cfg.discretion_mode.value} | decisions: {len(result.discretion_events)}")
    print(f"Regime mode: {cfg.regime_mode.value} | events: {len(result.regime_events)}")
    print(format_report(result, verbose=args.verbose))
    if args.regime_log:
        args.regime_log.parent.mkdir(parents=True, exist_ok=True)
        args.regime_log.write_text("".join(json.dumps(e) + "\n" for e in result.regime_events), encoding="utf-8")
    if args.discretion_log:
        args.discretion_log.parent.mkdir(parents=True, exist_ok=True)
        args.discretion_log.write_text("".join(json.dumps(e) + "\n" for e in result.discretion_events), encoding="utf-8")
    if args.structure_log:
        args.structure_log.parent.mkdir(parents=True, exist_ok=True)
        args.structure_log.write_text("".join(json.dumps(e) + "\n" for e in result.structure_events), encoding="utf-8")
    if args.model_log:
        args.model_log.parent.mkdir(parents=True, exist_ok=True)
        args.model_log.write_text("".join(json.dumps(e) + "\n" for e in result.model_events), encoding="utf-8")
    if args.narrative_log:
        args.narrative_log.parent.mkdir(parents=True, exist_ok=True)
        args.narrative_log.write_text("".join(json.dumps(e) + "\n" for e in result.narrative_events), encoding="utf-8")

    if args.html:
        out = generate_html_report(result, nq_bars, args.html)
        print(f"\nVisual HTML report saved to: {out.resolve()}")


if __name__ == "__main__":
    main()
