# PB Bot V2.4 - Structural premium/discount and mechanical discretion

Research preview based on the merged V2.3 tree (GitHub main commit `a4546687a6bbdea34864b7c85a5e9535a8da0f5f`, tree `189cc24a91c6cef2bdb751f6fdef939c0065a81c`). No live trading integration or claim of profitability.

## Source rules

- CONDITIONS, Premium/Discount Filter: measure the relevant impulse's swing-to-swing range; beware extended continuations without equilibrium retracement, especially with a 1H gap and LRL behind.
- PB Recognizing Market Conditions (1)_part01, Stacked FVGs: select gaps from the same displacement leg relative to its 50% equilibrium, not the full trading day.
- PB Breaker Blocks: avoid excessively premium longs/discounted shorts; size down eligible continuation trades in the unfavorable half of their range.
- PB Charting Session and building discretion: insist on convincing directional displacement; anchor stops to the manipulation extreme; the oversized-wick midpoint exception requires a genuine retest and rejection, not an arbitrary tighter stop.

## Implementation and explicit research assumptions

`pb_bot/engine/discretion.py` consumes closed 1m data and completed 5m/15m buckets. It clears state on a missing minute or CME trading-day change, rejects partial/flushed buckets, and builds a dealing range from a prior aligned impulse containing the actual three-candle formation of the selected condition FVG. Both anchors are strict three-bar pivots. The right-hand candle of the ending pivot must have closed by the sweep minute's open. The range uses the absolute wick extremes between the pivots, and is rejected if broken before or on the sweep. This is a conservative, testable definition; not an instructor-specified mathematical pivot algorithm.

In STRICT, the old day-wide premium/discount and sizing gates are replaced. MAC and PO3 retain the manipulation-wick stop and normal risk sizing unless the optional wick exception qualifies. Continuation requires a confirmed prior impulse on its selected condition's 5m or 15m timeframe. Position is normalized from 0 at impulse origin to 1 at directional extreme (mirrored for shorts). EQ = 0.5. Only active/unfilled aligned gaps with verified formation inside that impulse count as stacked siblings; reject a continuation selecting a gap entirely in the unfavorable half when there are at least two such gaps. Straddling EQ is allowed. A premium long/discounted short that never retraced to EQ is rejected if an aligned unfilled 1H gap is behind, or if its entry is at/beyond the configured 0.705 extreme threshold. The 1H guard is intentionally more conservative than the source's combined 1H+LRL example (LRL association remains pending). An otherwise eligible continuation entering in the unfavorable half uses `continuation_size_mult` (default 0.5); the existing risk floor rejects quantities below one contract. No reduced-risk sizing for MAC/PO3 merely because entry is near a daily extreme.

All models in STRICT require directionally aligned IFVG confirmation, a body close beyond its boundary, and body/range >= `min_displacement_body_fraction` (default 0.60). Existing speed, narrative, model, structure, obstruction, RR, risk and other gates remain. The 0.60 and 0.705 thresholds, 50% risk multiplier, and pivot convention are research assumptions, not numerical instructor rules. This is a quality proxy, not a comprehensive displacement model.

An optional experimental `--validated-wick-stop` is available only with STRICT discretion. Default is OFF. Only market-close entries qualify: use the exact structural 1m sweep candle; its wick must exceed 2.5 times the body (body floored at one tick), a later minute must touch the midpoint and close directionally beyond or at the wick's body-side boundary, the original extreme must remain intact, and the proposed buffered stop cannot have been breached after the qualifying retest. Otherwise retain the full extreme. The threshold is configurable; no stop is tightened merely for reducing risk. Limit orders never use this exception. Risk and RR are calculated using the actual selected anchor. OHLC cannot reconstruct tick ordering, so this exception needs finer-data validation.

Default OBSERVE records decisions for candidates reaching the discretion gate and does not change V2.3 trading behavior. The diagnostic may use legacy leg/model evidence when earlier modes are OBSERVE; it is not a full strict-mode shadow simulation. Orders carry discretion metadata. Backtests support JSONL export; paper replay/polling append `discretion_events.jsonl` when events exist.

## Run and verify

```sh
python -m unittest discover -v
python run_backtest.py --suite --discretion-mode OBSERVE --discretion-log observe_discretion.jsonl
python run_backtest.py --suite --narrative-mode STRICT --model-mode STRICT --structure-mode STRICT --discretion-mode STRICT --discretion-log strict_discretion.jsonl
```

100 tests pass (82 prior + 18 new). The original synthetic suite still yields two OBSERVE trades and $50,421.56 ending balance; all STRICT yields no trades and $50,000. The OBSERVE run has two discretion audits; the all-STRICT run has none because all 66 structural entry evaluations fail the upstream structural-leg gate. This is a validation gap, not evidence of market performance. New mirrored continuation tests stream real 1m synthetic candles through the resampler, FVG detector, swing evidence, structure, model, discretion and order path, with seeded narrative/bias; they demonstrate six-to-three contract reduction (not an actual trading result). Mirrored MAC order tests preserve the wick stop and standard sizing with seeded context.

## Remaining work

No fully unseeded positive all-STRICT market replay yet. Verify annotated examples and synchronized CME futures data, including instrument rolls, data completeness, costs and forward paper trading before considering any live deployment. The 09:30 Judas exception, SMT-equivalent source taps, CISD-only discretionary entry, nearby DOL/breakaway exceptions, LRL-confirmed rebalancing, advanced BE management, broader structural time alignment, and restart recovery remain incomplete. The current range selector requires same-timeframe, same-session, uninterrupted candles and may reject valid discretionary setups. Do not tune thresholds to force trades on the original synthetic fixture. Calendar filters still require supplied news CSV.
