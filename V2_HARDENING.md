# PB bot V2 hardening (research / paper only)

This is the first hardening increment, not a complete implementation of the PB mentorship or proof of a profitable strategy. Default paper and backtest configs now match (`BotConfig`: 09:30–11:00 NY, market close, 5m BE, reduced continuation risk). Paper mode is simulated and should not be connected to a live order-routing webhook. A change in the paper defaults will change alerts relative to the older runner.

## Run

```sh
python -m pip install -r requirements.txt
python -m unittest discover -v
python run_backtest.py --suite
python run_backtest.py --nq-csv NQ.csv --es-csv ES.csv --calendar news.csv --html report.html
python paper_runner.py --nq-csv NQ.csv --es-csv ES.csv --calendar news.csv --log-dir paper_logs_v2
```

Input bar timestamps are NY-local or timezone-aware, representing the minute open. Use synchronized closed bars from NQ/ES contract data, not index CFDs. The calendar CSV columns are `date,time_ny,title,impact` (NY times and RED/ORANGE/YELLOW impact). If no calendar is supplied, both CLIs warn and run without news filters; no verified calendar feed is included. Yahoo polling is only an indicative paper feed. Do not treat the 3-day synthetic suite as a performance evaluation.

## Decisions in this increment

- Conservative OHLC execution: a same-bar stop on a possible limit fill is counted; a same-bar target is not assumed, since ordering is unknown. Stops gapping through fill at the worse open, plus modeled slippage. Market-on-close entries cannot be stopped by earlier wicks. A pending limit cannot be overwritten, expires after 15 minutes by default, outside the session or when the target is reached without a fill. End-of-data closes open positions at the last synchronized close. Replay and backtest share one execution loop.
- Reduced-risk sizing can return zero; order symbol matches the configured contract spec (MNQ by default; signal data NQ). Planned risk excludes commissions, slippage and gaps, so it is not a guaranteed maximum loss.
- Old FVG taps cannot sponsor another trading day; already-swept session and prior-day/week targets are excluded. Repeated unchanged swing pairs do not refresh SMT.
- IFVG selection scans the available FVGs in the same bounded leg for both inversion candidates and blockers, requiring the outer gap on the highest eligible timeframe. A 45-minute heuristic replaces the 20-minute window; the selected extreme anchors the stop, with no automatic wick-midpoint tightening. The 09:30 exception stays disabled by default. The configured DOL/CISD alignment matrix is used for HTF entries unless the existing re-sweep proxy passes.

## Remaining validation / work before live use

- Reconcile the different alignment matrices in the lesson documents, define the exact manipulation leg with confirmed structural swings, and implement persistent per-setup state with proper invalidation, including pending condition/news/SMT invalidation. Current time and swing proxies are imperfect. Premium/discount and stacked FVG logic still use full-day ranges; do not treat these as exact lesson implementations.
- Implement a persistent HTF delivery narrative, preselected DOL and invalidation boundary, and separate MAC, Continuation and PO3 state machines. Current target selection still follows the original RR-based policy. Validate sponsorship direction, fresh session sweeps, 5m+ IML/IMH, correlated key-level pairing and DOL consumption for all pool types.
- Improve feed validation (gaps, duplicates, partial HTF buckets, futures trading calendar, DST and roll handling), live paper restart/state recovery and broker fill reconciliation. Scheduled workflow is unchanged. A live polling stop is not the same as a backtest end-of-data settlement.
- Use annotated positive and negative lesson examples, actual synchronized NQ/ES futures data, realistic commissions/slippage and out-of-sample validation. No live deployment or complete historical rerun is included.

Local verification: `python -m unittest discover` (33 tests, all passed); `python run_backtest.py --suite` (two synthetic trades, no trades on seek-and-destroy day). These are functional checks, not trading results.
