# PB Bot V2.5 — Market-regime filters (research preview)

## Baseline and intent

Built on GitHub `hong1311/pb_bot` main at `baaa597a7ab7e4ba773e760dda8b09076e0b287c` (merged V2.4). The local tracked file hashes matched the GitHub tree `ff34f0937391b5cd0c48c9fde81cf83646eb56b3` before editing. No remote files or live trading settings were changed.

This release translates the market-condition rules from `PB Recognizing Market Conditions (1)_part00.md` (lines 24–145), `PB Recognizing Market Conditions (1)_part01.md` (26–60), `PB Charting Session.md` (24–34), and `daily profiles.md` (63–66). The notes are not calibrated numeric trading specifications; the assumptions below must be validated on actual market examples. The aim is to reduce unsafe setup eligibility, not to fit backtest returns.

## Behavior

`pb_bot/engine/regime.py` maintains daily state, consuming only the synchronized minute stream and complete 5m formations. `REGIME_STATE` events record changed state, including reasons, range locks, overnight objective and volatility, and a 09:30 snapshot. `ENTRY_REGIME_CHECK` records the local inversion-quality decision. The regime is updated during warmup, outside the trading window and while positions are open. Open positions still follow the existing risk and exit rules.

| Condition | V2.5 research rule |
| --- | --- |
| Conflicting SMT | Pair 15m/1H bullish and bearish swing SMT observed in the same session within 120 minutes, if price is between the levels. Where detector metadata is present, require the latest NQ/ES pivot timestamps to be in the same session and CME trading day. Pin the two range boundaries. Do not clear merely on a wick or age timeout; require a subsequent completed 5m directional body close outside the range with sufficient displacement. Clear consumed pair evidence on recovery. |
| Potential Seek & Destroy | Once London is complete, block when London swept both sides of Asia. |
| Confirmed Seek & Destroy | In addition to London's dual Asia sweep, NY AM (08:30–11:00) must trade through both London boundaries. `NY_DUAL_RAID` is a separate conservative blocker if only the NY portion occurs. Pre-NY London-range raids are not NY raids. |
| Seek & Destroy recovery | Pin the completed London range. Require a later strong completed 5m close outside it, a newly formed aligned 5m FVG fully beyond the range, and a subsequent completed 5m retest which holds the gap and closes beyond the range. A return close into the range or inversion of the gap resets the recovery sequence. Require new raids after clearance to relock. |
| Overnight exhaustion | Combined Asia (20:00–00:00) and London (02:00–05:00) high-to-low range strictly greater than `overnight_exhaustion_pts` (default 300 NQ points), plus an observed major objective touch before 09:30. Objective is a locked HTF narrative DOL if available, otherwise PDH/PDL/PWH/PWL as a conservative proxy. Block 09:30–11:00. Unlike the old gate, do not scale the 300-point threshold with index price. |
| Overnight recovery | Before the NY open, require a half-range rebalance and a different active, locked narrative DOL sourced after the observed objective touch. Revoke recovery if that narrative ceases to be active or its NQ target is reached by the opening minute. |
| No AM volatility | In the 10:00 hour require a complete 3-candle 5m FVG formed entirely since 09:30, when the existing volatility toggle is enabled. Do not count truncated candles or formations across gaps. |
| Liquidity generation | During 10:00–11:00 detect three confirmed unswept ascending 1m swing lows or descending 1m swing highs, with no newly observed external sweep/HTF first tap or complete 5m FVG formed since 10:00. This is a liquidity-generation proxy, not a prediction of 11:00 delivery. |
| LRLR inversion trap | At a selected IFVG, look for a consecutive run of at least five overlapping 1m candles after the manipulation extreme, immediately before the inversion, and at least two changes in candle direction. Existing slow-inversion filter remains in force. Do not reevaluate an old IFVG with later information. |
| News | Keep calendar-based red release freeze (2 minutes before through 3 minutes after); stand aside on calendar day before red NFP/CPI. A PM red event blocks until three minutes after the last such release. A red 10:00 event additionally requires a post-10:00 complete 5m gap and confirmed 5m pivot before entries during the 10:00 hour. Orange/yellow do not independently block. |
| Pending limits | In STRICT, cancel before a fill for calendar/time gates already knowable at the next bar open (including a known 10:00 volatility gate); cancel after a newly observed price-regime block at bar close, before the next bar can fill. Do not retrospectively cancel a fill already possible inside that bar. |

`RegimeMode.OBSERVE` is the default and preserves the previous order-selection logic. `RegimeMode.STRICT` replaces the legacy regime filters and Seek & Destroy classification; legacy session, loss/trade caps, lunch, optional macro buffer, and other setup/risk gates remain. Regime STRICT is independent of other new modes, so it can be ablated without changing the rest of the strategy. `--require-calendar` is available only with `--regime-mode STRICT` and blocks entries when no calendar is provided; an empty or stale CSV is not automatically detected.

## How to run

```bash
python -m unittest discover -v
python run_backtest.py --suite --regime-mode OBSERVE --regime-log /tmp/pb25-observe.jsonl
python run_backtest.py --suite --regime-mode STRICT --regime-log /tmp/pb25-regime.jsonl
python run_backtest.py --suite --narrative-mode STRICT --model-mode STRICT \
  --structure-mode STRICT --discretion-mode STRICT --regime-mode STRICT \
  --regime-log /tmp/pb25-all-strict.jsonl
```

For actual historical files, pass synchronized `--nq-csv` and `--es-csv` instead of `--suite`, with `--calendar PATH` for the economic news CSV (`date,time_ny,title,impact`). Include the whole replay date range and the next day for pre-event screening. Use a curated US-relevant red-news calendar and actual matching CME contracts. The paper runner accepts `--regime-mode` and `--require-calendar` too. It records `regime_events.jsonl` in its log directory and includes the regime snapshot on submitted orders. Use a fresh log directory for each replay. Do not enable real-money routing based on these tests.

## Verification

All 125 unittest tests passed (100 existing + 25 new). New tests cover bullish/bearish range release, wick-only and failed breakouts, fresh re-locks, stale/incorrect-session SMT, overnight threshold and recovery, post-release structure, incomplete buckets, LRLR, pre-fill vs. post-close pending cancellation, preserving open positions, rollover, and controlled all-STRICT MAC orders with actual lower-timeframe detectors and seeded HTF narrative/bias context.

The original 3-day synthetic suite produces:

| Configuration | Closed trades | Final balance | Regime events |
| --- | ---: | ---: | ---: |
| All OBSERVE | 2 | $50,421.56 | 17 |
| Regime STRICT only | 2 | $50,421.56 | 17 |
| All STRICT | 0 | $50,000.00 | 15 |

Paper replay produced identical regime JSONL events, trade counts and net P&L in each configuration. The third synthetic day triggered `NY_DUAL_RAID` before the open, then cleared on a 5m breakout/gap retest, and had no trades. It does not contain the full London-then-NY dual-raid sequence. The all-STRICT replay logged 102 `STRUCTURAL_LEG_REQUIRED` rejections; it is **not** a successful positive unseeded all-STRICT replay. The seeded bullish/bearish MAC tests show that the regime gate does not categorically prevent orders, not that the whole PB methodology is fully validated.

## Decisions and limitations

- 120-minute SMT pairing, 60% displacement body fraction inherited from V2.4, 5m breakout/retest selection, three strict 1m pivots, half-range recovery and the five-overlap/two-turn trap are research proxies. The notes do not specify all these exact thresholds. The 300-point rule is taken from the lesson; the separate approximate 200-point example is not automated. The fixed 300-point threshold is NQ-specific and should not be assumed appropriate for other markets.
- Swing SMT on 15m/1H is still a proxy for *key-level* SMT. Exact synchronized external key-level evidence and fully calibrated SMT-equivalent entry are still outstanding. The data model adds latest pivot timestamps, not a full matched-pair proof. The legacy SMT detector remains otherwise unchanged.
- Exhaustion's prior-session objective proxy can identify a touched PDH/PDL/PWH/PWL that is not the actual discretionary daily objective. Recovery uses the available narrative and NQ target, not a comprehensive ES/DOL resolution engine. Missing/partial overnight session history can reduce reliability; no full session coverage guarantee has been added.
- Red-news CSV presence is not evidence of complete dates, correct currency/impact classification or actual release completion. Post-10am 5m pivot/gap is a conservative proxy for settling; it does not inspect actual news values. No live calendar feed is supplied. Calendar day-before logic does not skip weekends/holidays.
- The early AM no-volatility gate is deliberately not enforced before 10:00. The 11:00/PM delivery ideas are not separate new entry strategies. Existing trading windows remain in force.
- `OBSERVE` adds diagnostics, not regime enforcement; the inherited legacy filters still operate. STRICT may reduce or change trade eligibility relative to the legacy baseline. No historical futures profitability, live polling or broker routing was validated. No automatic GitHub push or merge was attempted.
- V2.6 should build an actual CME futures data and validation harness, including complete session coverage, contract rollover and gap checks, unseeded annotated positive and negative replays, realistic execution costs and out-of-sample tests. First resolve the persistent zero-trade full-STRICT synthetic/real replay gap. Avoid adding or tuning filters merely to make a profitable sample.
