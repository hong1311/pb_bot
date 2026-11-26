# PB Bot V2.3 - Evidence-anchored manipulation structure

Research preview based on the merged V2.2 tree (GitHub main commit `c516ba04886bfacb9a0b47c0e328eecba1de0303`). No live broker integration or claim of trading edge.

## Source rules and mechanical interpretation

- PB Charting Session, highest-timeframe inversion: isolate the swing-to-sweep leg reaching the key level; inspect 5m, 4m, 3m, 2m, 1m; require exact-timeframe body close through the outer opposing gap. Do not require a second re-sweep after a valid entry confirmation.
- TIME AND STRUCTURE, re-sweep and clean trading leg: anchor the setup to actual sweep structure, not arbitrary recent extremes.
- Full Application of Concepts, highest TF in the leg: wait for the selected gap's inversion and reject late entries after the target.

The instructor notes do not provide a complete mathematical swing definition. V2.3 uses a strict three-bar, 1m pivot (center high/low strictly exceeds both neighbors), available only after the right-hand bar closes. This is a conservative approximation, not an exact reconstruction of discretionary chart reading. A pivot confirmed on the evidence bar cannot be used on that bar.

## Implementation

`pb_bot/engine/structure.py` tracks origins, actual leg extremes, completed gap formations, observed inversion timestamps, and selection audits. It consumes fresh V2.2 evidence (external sweep, HTF tap, or confirmed internal sweep/PO3 raid). Evidence must occur at a fresh directional extreme of the active leg. The origin must have been confirmed beforehand and remain intact up to the sweep. A new, deeper unsourced extreme invalidates the leg; a close past the origin retires it after that minute. Missing minutes and trading-day changes reset structural state. The origin has no fixed 45-minute lookback, although the V2.2 model evidence freshness limit remains 45 minutes.

For each scanned timeframe, only complete, consecutive candles are accepted. A 3-candle FVG must start at or after the origin and complete by the sweep minute, with its entire price zone inside the leg. Higher-timeframe candle timestamps denote opens; the structural engine separately records the 1m publication minute. Partial/flushed buckets cannot confirm strict entries. A gap spanning a leg boundary is excluded, even if a discretionary trader might accept it. Same-minute sweep and inversion is rejected. The highest timeframe containing opposing gaps must clear before entry; the outer gap must be newly inverted on its own completed timeframe, after the sweep. The selected extreme is used for the protective stop. Existing narrative/model, RR, risk, time-alignment, and other filters still apply.

## Operation

Default `--structure-mode OBSERVE` records the new selection without replacing the V2.2 selector. The new audit fields may differ from the legacy order's actual leg. `STRICT` replaces the legacy selector, has no fallback to a rolling window or Judas override, and requires both narrative and model STRICT modes. The paper replay and polling entrypoints share the structural mode; paper logs add `structure_events.jsonl`.

```sh
python -m unittest discover -v
python run_backtest.py --suite --structure-mode OBSERVE --structure-log observe_structure.jsonl
python run_backtest.py --suite --narrative-mode STRICT --model-mode STRICT --structure-mode STRICT --structure-log strict_structure.jsonl
```

Expected legacy synthetic suite: OBSERVE two closed trades, final balance $50,421.56 and 50 structural audits; all STRICT zero closed trades, final balance $50,000 and 66 structural audits. In both runs every structural audit reports `NO_EVIDENCE_ANCHORED_LEG` for the original synthetic fixture. This is a validation gap, not evidence that the strict strategy is ready. Controlled bullish and bearish tests stream real 1m data through actual LTF resamplers/FVG detectors and the structural/model/order path with seeded HTF context. Other tests verify higher-TF blocking, timestamp availability, stale and missing bars, origin lifetime, stop anchoring and configuration. 82 tests pass (67 prior + 15 new). Backtest and paper replay produce matching balances, trade counts and structural event logs for both synthetic runs.

## Remaining limitations

- Annotated market examples are needed to validate the strict pivot width and leg-boundary convention. The synthetic suite must eventually include fully end-to-end positive examples with no seeded HTF context, and historical replay must use actual synchronized CME contracts with validated rolls and data coverage.
- Fresh evidence at the exact extreme is intentionally strict and can reject legitimate multi-candle taps. Correlated SMT-equivalent source evidence, the 09:30 exception, broader structural time-alignment reconciliation, and impulse-range premium/discount are not solved here.
- No restart persistence, new live integration, live polling validation, or profitability optimization. Existing execution assumptions and broader data-resampling limitations outside this structural selector remain. News filters require a supplied calendar.
