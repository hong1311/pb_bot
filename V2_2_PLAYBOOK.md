# PB Bot V2.2 - Independent model evaluators (research preview)

Baseline: GitHub `hong1311/pb_bot` main merge commit `fd0a53d72fd9bf610f13d5cbe7ec4ed4c990af26`. The source baseline was verified against the GitHub tree's blob hashes. No remote files were changed.

## Model rules and source mapping

The three evaluators in `pb_bot/engine/playbook.py` have distinct prerequisites and share the existing highest-timeframe IFVG, time-alignment, session, regime, risk and broker paths. Evidence is recorded bar by bar, not manufactured at the time of the entry. All require a non-terminal, active, aligned V2.1 narrative with a locked, untouched target ahead of the confirmation candle. STRICT model mode replaces the legacy key-level gate, not the risk or execution filters.

| Model | Source lesson | V2.2 mechanical rule |
| --- | --- | --- |
| MAC | PB Charting Session, three core playbook models, 15m+ reversal key levels [17:28-17:38, 45:10-45:25] | Fresh sweep of previously observed completed session/previous-day level, or fresh 15m+ FVG tap / verified internal sweep in another HTF key level; target ahead. The active narrative condition is not treated as a separate MAC source. |
| Continuation | PB Charting Session [06:42-07:05, 17:08-17:55] | DOL locked before the new sweep, external target, retest of the narrative's 5m/15m FVG, and confirmed 5m/15m/1H swing inside that FVG after first actual 1m touch. Pivot must be available before sweep bar. |
| PO3 | PB Charting Session [00:40, 11:42-11:50]; full applications and concepts, PDI [24:54-25:55, 29:35-29:52] | Prior contiguous consolidation range, one-sided raid on a candle opening inside the range and interacting with a 15m+ FVG, then IFVG confirmation with price across range midpoint and locked DOL beyond range. |

## Explicit research decisions

- Default OBSERVE preserves V2.1 order selection. It evaluates all three models on selected IFVG candidates, records eligibility and a shadow model but does not make a new model a requirement. The `model_mode` metadata distinguishes shadow classification from strict enforcement.
- STRICT requires `narrative_mode=STRICT`. It requires a selected enabled model. It does not force an order when a model is eligible: all shared gates and RR still apply. The model audit is recorded before the strict narrative rejection, so the existing strict replay's rejection counts differ from V2.1 (but not its trades).
- `enabled_models` defaults to all three. If more than one qualifies, deterministic precedence is PO3 > Continuation > MAC. This is a research policy to avoid duplicate orders, not a ranking taught by the instructor.
- Evidence expires after 45 minutes and must fall within the existing selected manipulation leg, with the same adverse extreme. The leg itself is still the V2.0 rolling-window heuristic. No same-minute touch and confirmation in strict model mode. Special 09:30 Judas override has no verified leg and is not enabled for strict models.
- A new gap must have been visible on a prior bar to sponsor a setup. A sweep of a known pivot needs the price to cross it, not merely touch or gap beyond it. An earlier session swept flag alone is insufficient. A swing whose aggregate bar began before the initial tap is conservatively excluded. On a data discontinuity, reset candidate evidence and the accumulation buffer. The legacy resampler itself has not been repaired here.
- PO3 uses prior 15 consecutive closed 1m bars. Range efficiency = (max high - min low) / sum of individual candle high-low ranges; maximum 0.35, at least two midpoint-side changes, no same-bar dual-sided raid, later directional close beyond the frozen range midpoint. These are configurable/inferred proxies, not numeric thresholds supplied by the mentor. No direct-breakout or unsponsored Judas exception in strict mode.
- A selected strict setup's manipulation evidence is consumed on order submission; a pending order is cancelled after the key FVG is inverted or removed from tracking. Existing narrative cancellation remains. No retroactive same-bar cancellation; OHLC execution ambiguity remains.

## Running V2.2

```bash
python -m unittest discover -v
python run_backtest.py --suite --model-mode OBSERVE --model-log model_observe.jsonl
python run_backtest.py --suite --narrative-mode STRICT --model-mode STRICT --model-log model_strict.jsonl
python run_backtest.py --suite --narrative-mode STRICT --model-mode STRICT --models MAC
```

Replace MAC with CONTINUATION or PO3 for isolated evaluation. `--models` also accepts several space-separated names. Without `--narrative-mode STRICT`, strict model mode is rejected. `--model-log` contains JSONL with model name, eligibility, reason, evidence, IFVG ID/timeframe, narrative ID and selected candidate. An eligible model is not proof of an executed order. Use a separate output path for each experiment. For paper CSV replay use `paper_runner.py --nq-csv ... --es-csv ... --log-dir ... --model-mode OBSERVE` (also supports strict mode with both flags); `model_events.jsonl` and `narrative_events.jsonl` are appended to the selected log directory.

## Validation

- 48 existing tests plus 19 new tests passed (67 total).
- Controlled detector-output fixtures exercise both directions of all three evaluators and the real broker order path. These tests inject a known narrative and IFVG rather than claiming an end-to-end realistic market setup.
- Tests cover stale sources, invalidated anchors, causal swing confirmation, model isolation, missing minutes, same-bar gap visibility, dual-sided raid, no distribution, target reach, and pending cancellation.
- Three-day synthetic replay: OBSERVE unchanged at 2 closed trades, $50,421.56 ending balance; both STRICT flags yield 0 trades, $50,000 ending balance, with 2 candidate decisions rejected at narrative gate. These are synthetic, not profitability evidence.

## Limitations and follow-up

V2.1 strict narrative confirmation remains conservative and can prevent early MAC/PO3 reversal entries. The synthetic suite does not yet contain a full raw-bar sequence that yields a strict order. Exact manipulation-leg segmentation, breakaway gaps, true structural dealing-range equilibrium, SMT-equivalent continuation/source taps, broader external liquidity coverage, unsponsored Judas exceptions, advanced daily-profile/bias transitions and market-data gap/contract-roll handling remain unfinished. Profiles are recorded, with existing Seek & Destroy blocking; they are not mechanically used as standalone entry patterns. The existing HTF alignment and order risk logic remain imperfect. No actual historical CME futures or live polling/broker execution has been validated. Use OBSERVE for research and do not connect a live-order webhook.
