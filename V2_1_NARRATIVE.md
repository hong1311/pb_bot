# PB Bot V2.1 — Market Narrative (research preview)

Baseline: `76cf410083be3b029603eaacd631bdd2aa0f1094` (merged V2.0). The patch is additive and preserves the existing execution strategy by default. No broker connection or real-money execution is added.

## Run

```sh
python -m unittest discover -v
python run_backtest.py --suite --narrative-mode OBSERVE --narrative-log observe.jsonl
python run_backtest.py --suite --narrative-mode STRICT --narrative-log strict.jsonl
```

For historical data use `--nq-csv PATH --es-csv PATH` and `--calendar PATH` with a valid NY-time news CSV. Real CME NQ and ES data should be used for validation; proxy data does not establish strategy fidelity. `paper_runner.py` accepts the same `--narrative-mode` option and writes transitions to `narrative_events.jsonl` in its log directory. Use separate directories for different runs, because log files are appended and narrative IDs are local to a process.

## Modes

- `OBSERVE` (default): update the narrative on each completed synchronized minute, record transitions and attach snapshots to submitted orders. The V2.0 order selection, execution and RR logic are unchanged. This is the migration and comparison baseline.
- `STRICT` (opt-in): require an active narrative matching the legacy directional bias, with a previously locked objective. Use only that objective for risk/reward evaluation. If it fails the existing RR bounds, stand aside instead of substituting a farther pool. Invalidate pending limits at the close where the narrative fails, before processing the next bar. Existing risk and session gates still apply. Open positions remain under V2.0 stop/target/SMT management; this release does not introduce a condition-failure market exit.

## State machine

1. Observe a new aligned delivery source: first observed tap of a 15m+ FVG or a fresh sweep through a completed Asia/London/data or prior-day level. Session-level touches consume the target but are not themselves sweeps.
2. After the source, require a newly created aligned 5m/15m condition and an opposing 5m/15m FVG inversion. Record both events. A newer source may replace a narrative that has not selected a condition yet.
3. Lock the nearest untouched eligible primary DOL once both are observed. Also record an informational opposing DOL if one exists. Neither selection uses entry price or stop distance. An objective can be reached even before the condition has been respected.
4. Require a subsequent completed candle on the condition's own timeframe to overlap the gap and not close through its far edge. A wick is not inversion. The condition is pinned rather than silently replaced.
5. Once active, strict mode can proceed to existing LTF entry gates; DOL must already have been locked on a prior input minute. Existing key-level/IFVG logic remains in place.
6. Terminate on bias/regime change, exact-timeframe inversion of the source or condition, loss of tracked anchors, NQ target touch, or ES touch of a paired session objective. On rollover clear the session state. A terminal thesis cannot be rearmed by the same old source; a fresh source is required. The alternative objective is a recorded candidate, not an automatic direction flip.

## Source mapping and implementation decisions

- `PB Daily Bias.md`, sections DOL identification and Conditions: delivery source, aligned respect/disrespect and conditional invalidation.
- `CONDITIONS.md`, Market Condition and Confirmation: 5m/15m FVG formed after HTF source; respect and inversion on appropriate timeframes.
- `building discretion.md`, Unfilled FVG as DOL vs Condition and SMT: stop treating a reached objective as untouched; paired ES objective can invalidate a thesis.
- `DOL AND CONDITIONS 2.0.md`, DOL selection and same-timeframe closes: directional target and condition invalidation.

Conservative choices, not claims that the original lessons specify these exact thresholds:

- The engine uses the V2.0 bias engine as its direction proposal; it does not implement a new full HTF structural bias model. It selects only fresh delivery sources aligned with the current proposal, and waits for both aligned condition formation and opposite 5m/15m inversion. This is a continuation-oriented research gate, not yet a complete reversal/MAC or Judas implementation. Simultaneous source and confirmation observations do not satisfy the ordered sequence. Sources observed while neutral are not retroactively activated. Conditions are pinned, with 15m preferred if simultaneously created; adaptive rolling condition selection needs separately annotated validation.
- The target universe is deliberately limited to untouched opposing FVGs (5m, 15m, 1H, 4H, 1D), finalized Asia/London/data ranges and PDH/PDL. Nearest eligible target, existing rank as tiebreaker, and label as deterministic final tiebreaker. Exact swing-liquidity identity, EQH/EQL, LRLR, weekly/opening gaps, ITH/ITL and structural equilibrium ranking are deferred. A nearest eligible target is a testable approximation, not a verified PB probability ranking.
- Session counterpart prices can be paired with ES. Already delivered ES counterparts are excluded; FVG-to-FVG SMT matching is not assumed. SMT-equivalent source taps and arbitrary correlated swing matching need more precise alignment tests. Prior-day and session range accuracy still depends on complete historical feed/warmup and the existing session tracker.
- `bar_timestamp` denotes the opening timestamp of a *completed* 1m input candle. No future candles are used. Current data-gap, resampler and OHLC intrabar-fill assumptions remain those of V2.0. Source-to-trigger exact manipulation-leg association and pending same-minute ordering are not solved. Narrative state persists only in process memory; process restart recovery is not implemented. The alternative is a snapshot, not a second live target.
- The strict mode is intentionally opt-in pending bar-by-bar lesson fixtures and actual futures replay. Synthetic zero trades is a negative validation outcome, not a proof of profitability or adequate strategy coverage. Do not weaken the filters just to recover synthetic trades.

## Regression verification

The existing 33 tests pass. Fifteen added tests cover both directional lifecycles, prior-bar locking, no target reselection, exact-timeframe condition invalidation, NQ/ES target delivery, consumed levels, bias/regime changes, fresh-source rearming, rollover, touch vs sweep, opposing inversion requirement, pending cancellation, strict order routing and insufficient RR, and V2.0 synthetic parity. The positive order tests use controlled detector/strategy inputs; they are not full market replays.

Synthetic three-day suite: OBSERVE = 2 closed trades, $50,421.56 final balance; STRICT = 0 closed trades, $50,000.00. Neither result is real-market performance.

Next: independent MAC/Continuation/PO3 evaluators and annotated positive/negative full-feed fixtures, then exact manipulation legs, structural premium/discount, advanced market filters, real futures backtests, and forward paper testing. Do not enable real-money execution.
