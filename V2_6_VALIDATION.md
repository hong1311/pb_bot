# PB Bot V2.6 — Research validation toolkit

Baseline: merged GitHub V2.5 (`f3530a9a0c4a55381338cf3d6e72503c92606cd1`, tree `a1140fb777fae0132431950ffa235db1dddbdaee`). This is a validation-tool release, **not a validated trading edge**. It does not change the strategy gates, default `run_backtest.py` behavior, paper runner, or remote deployment. No live orders or paid historical-data requests were made during development.

## What changed

- `pb_bot/data/validation.py`: reject naive, duplicate, unordered or sub-minute timestamps; nonfinite/invalid OHLCV; off-tick prices; mixed fixed contract symbols/IDs; bad file hashes; missing/extra minutes in either feed, including minutes missing from both. Coverage uses explicit UTC regular-session windows, with New York DST alignment. A successful audit is not proof of authentic data.
- `fetch_data.py --validation-export`: optional Databento raw-symbol export. Preserves the instrument ID, raw symbol and SHA-256 per CSV, and creates a request-range manifest and **review-required** weekday schedule. Requires matching quarterly NQ and ES contracts. No continuous or back-adjusted series. Refuses duplicate/mixed rows and existing outputs. Estimate cost first; require a finite nonnegative spending ceiling.
- `pb_bot/backtest/validation.py`: simulated next-minute-open market execution, explicit commissions and adverse fixed slippage, actual-fill risk/reward checks, conservative fee/slippage-aware sizing, stop-first OHLC ambiguity policy, mandatory same-session settlement at 16:59 NY, minute-close marked drawdown, and detailed decisions. No increase over the signal's original size or nominal risk. Gaps through protective stops can still exceed planned risk. A fill at the next open is a model, not a guaranteed real fill. No limit queue model is implemented in this runner.
- `validate_replay.py`: seven fixed gate profiles, cost sensitivity, chronological split, nontrading warmup, full configuration/source fingerprint, file hashes, per-gate rejection counts, optional JSONL events, and comparison to independently labeled setup windows. A measured development period and holdout restart at the same initial account balance; these results are not compounded. No automatic optimization or selection of a winning configuration.
- `tests/test_validation.py`: 27 new tests (152 total), including mocked fixed-contract exports, DST, incomplete/duplicate/mixed input, coverage of entire missing sessions, next-open order behavior, cost and risk caps, warmup and holdout separation, and annotation matching.

## A. Verify the release (no data account needed)

From the repository root, activate the virtual environment:

```bash
source .venv/bin/activate
python -m unittest discover -v
python validate_replay.py --suite --matrix --slippage-ticks 1 2 4 \
  --out validation_results/synthetic_matrix.json \
  --details validation_results/synthetic_details
```

The `--suite` input is synthetic. Seven gate profiles run for each specified cost scenario. At one tick and the existing assumed $0.62 fee per contract per side, the results reproduced in development were:

| Profile | Signals | Closed trades | Net P&L |
|---|---:|---:|---:|
| OBSERVE | 2 | 2 | $281.04 |
| NARRATIVE | 0 | 0 | $0.00 |
| MODELS | 0 | 0 | $0.00 |
| STRUCTURE | 0 | 0 | $0.00 |
| DISCRETION | 0 | 0 | $0.00 |
| ALL_STRICT | 0 | 0 | $0.00 |
| REGIME_ONLY | 2 | 2 | $281.04 |

OBSERVE at two and four ticks produced $279.04 and $275.04 respectively. The legacy close-fill diagnostic (`python validate_replay.py --suite --legacy-close --out validation_results/legacy.json`) still reproduces two trades and $421.56. The change in synthetic P&L reflects next-open fills and smaller quantity after conservative cost-inclusive sizing, not a change in signal logic. No real market conclusions follow from these two trades.

Optional synthetic split example (one day of warmup before each fold):

```bash
python validate_replay.py --suite --holdout-start 2025-06-27 \
  --warmup-sessions 1 --out validation_results/synthetic_holdout.json
```

The development fold generated one OBSERVE trade; the synthetic holdout (Seek & Destroy day) generated none. This validates split mechanics, not out-of-sample profitability.

## B. Audit the existing Yahoo cache

The original uploaded ZIP contained `data_cache/nq_1m_yf.csv` and `data_cache/es_1m_yf.csv`, exported by the existing Yahoo NQ=F/ES=F data path. These are not contract-identified, verified exchange data. If these files are available on your Mac:

```bash
python validate_replay.py --exploratory \
  --nq-csv data_cache/nq_1m_yf.csv --es-csv data_cache/es_1m_yf.csv \
  --audit-only --out validation_results/yahoo_audit.json
```

The supplied snapshot covered 2026-11-18 through 2026-11-25 trading dates, 7,109 paired minutes. Against an **inferred** regular weekday grid it had 1,171 missing minutes per feed (1,113 from partial first/last sessions; 58 in the four interior sessions). There were no NQ-only or ES-only timestamps. Both feeds can omit the same minute, so simple pair matching is insufficient. The audit returned REJECTED (exit code 2), as intended. Some omissions can reflect legitimate no-trade minutes rather than missing records; the audit cannot distinguish the causes. Do not manufacture flat candles, discard missing/losing sessions, or regard the result as audited exchange data.

An exploratory run using the previous V2.5 legacy loader on those CSVs returned zero trades in both modes. ALL_STRICT recorded two structural IFVG successes; both failed `NARRATIVE_NOT_READY`. Regime and structural rejections also occurred. This is a diagnostic only, not an eligible replay. The old Dukascopy 12-month index proxies are unsuitable for CME contract/SMT validation.

## C. Obtain a real fixed-contract pair

Use an authorized historical feed or your broker's unadjusted export. Databento is optional. Its `GLBX.MDP3` / `ohlcv-1m` data has a bar-open `ts_event`; intervals with no trades may have no record. See:

- https://databento.com/docs/schemas/ohlcv
- https://databento.com/docs/api-historical/metadata/metadata-get-cost?lang=python
- https://www.cmegroup.com/markets/equities/sp-500-and-nasdaq-100-futures.html

Example request: matching December 2026 contracts `NQZ6` and `ESZ6`, November 2–20 trading dates. The range starts at 18:00 New York on Nov 1 (23:00 UTC), ends at 17:00 New York on Nov 20 (22:00 UTC). This is a proposed research sample, not a verified schedule or guarantee of data availability. Confirm symbols, active contract, schedule and cost with the provider first. The downloader does **not** auto-roll or select a winning contract ex post.

Install the optional SDK with `python -m pip install databento`. Set `DATABENTO_API_KEY` locally from your own account (never share the key in chat, commit it, or include it in a report). Run a dry run before authorizing any spend:

```bash
python fetch_data.py --source databento --validation-export --stype raw_symbol \
  --nq-sym NQZ6 --es-sym ESZ6 \
  --start 2026-11-01T23:00:00Z --end 2026-11-20T22:00:00Z \
  --out-dir data_cache/nqz6_esz6_nov --dry-run
```

Only if you approve the displayed estimate, repeat the command without `--dry-run` and add `--max-cost` with your chosen USD ceiling (for example `--max-cost 5` if you are willing to spend up to $5). The estimate is not a guarantee of the provider's final charge. The download can fail if your account lacks data entitlement. This API path was tested with mocked data, not a real authenticated download.

The output directory contains `manifest.json`, `nq_1m_db.csv`, `es_1m_db.csv` and `sessions_REVIEW_REQUIRED.csv`. The candidate schedule includes every weekday in the request, including possible holidays. **Review it against CME holiday and session notices** and copy to `sessions_reviewed.csv`; do not omit a trading day merely because input data is absent. Only complete regular 18:00–17:00 ET sessions are supported in this release. Early closes/halts or shortened sessions require a separate implementation before inclusion; use separately declared runs around those sessions. Keep one raw contract pair per replay; for rolls, stop before the roll, warm up the next pair independently, and report each segment separately. Do not concatenate contract prices or carry positions/state across a roll. Calendar coverage and raw-symbol provenance are operator declarations, not independently authenticated.

A broker export can be used by creating a matching manifest (source `exchange_export`, schema `ohlcv-1m`, timestamp_convention `bar_open`, adjustment `none`, stype_in `raw_symbol`; `nq` and `es` objects must each contain `contract`, `csv` relative to manifest, and `sha256`; include `instrument_id` if available). Each CSV must have `timestamp,symbol,open,high,low,close,volume` (and `instrument_id` if declared). Timestamps must be minute-aligned ISO datetimes with explicit offsets. Use a reviewed session schedule CSV with `trading_day,open,close` and exclusive close. Example schedule row for the above proposed sample: `2026-11-02,2026-11-01T23:00:00Z,2026-11-02T22:00:00Z`. This example is not a substitute for verification. Each price must be on the 0.25-point tick and volume must be an integer.

## D. Audit before replay

```bash
python validate_replay.py \
  --manifest data_cache/nqz6_esz6_nov/manifest.json \
  --sessions data_cache/nqz6_esz6_nov/sessions_reviewed.csv \
  --audit-only --out validation_results/futures_audit.json
```

A REJECTED report is a blocker. Find the reason and retrieve a corrected export or explicitly implement supported no-trade/session rules rather than forward-filling. Even one shared missing minute rejects the full declared run. For the approved exchange source, verify that the CSV and manifest are authentic and that the declared contract was appropriate throughout the period. The current simulator uses NQ signal prices with MNQ point value, not independently observed MNQ executions. An actual fill study requires synchronized MNQ quotes/trades too (or changing instrument sizing and execution assumptions appropriately).

## E. Chronological research replay

Prepare a reviewed historical news CSV in `data_cache/news_nov.csv` with columns `date,time_ny,title,impact`. Format dates `YYYY-MM-DD`, NY local times `HH:MM`, impact `RED`, `ORANGE` or `YELLOW`. Include all relevant events through the day after the last session for pre-CPI/NFP filtering. Coverage start/end arguments are operator attestations; an empty calendar is not proof there were no events. Historical event timing, release postponements, and exchange holiday status need external verification. Freeze the config and select development/holdout boundaries before looking at holdout results. This sample is a short smoke test, not enough trades to establish a reliable edge.

```bash
python validate_replay.py \
  --manifest data_cache/nqz6_esz6_nov/manifest.json \
  --sessions data_cache/nqz6_esz6_nov/sessions_reviewed.csv \
  --calendar data_cache/news_nov.csv \
  --calendar-coverage-start 2026-11-02 --calendar-coverage-end 2026-11-21 \
  --warmup-sessions 5 --holdout-start 2026-11-16 \
  --matrix --slippage-ticks 1 2 4 --commission-per-side 0.62 \
  --out validation_results/futures_matrix.json \
  --details validation_results/futures_details
```

Five warmup sessions are required; ten or more are preferable in a longer study for weekly structure context. Each measured fold has no trades during warmup and starts from the same account balance. Full-period long runs may be slow because the original engine retains history. The fee is an inherited assumption; substitute the actual all-in cost for your instrument. No execution queue, latency model, intrabar tick ordering, spread, margin/maintenance or partial fills are modeled. The next-open broker cancels rather than chase an invalid gap, clamps size to nominal risk including fixed costs, and enforces stop-before-target when both are reachable on one OHLC bar. It closes at session end; live behavior is unchanged. Reported marked drawdown uses minute closes and modeled liquidation cost, not worst intrabar drawdown.

## F. Validate PB rule fidelity, not just P&L

Annotate historical chart windows independently, without using bot outputs. Include both valid and invalid examples for MAC, Continuation, PO3, and the conditions where the bot must stand aside. Use `--annotations labels.csv` to compare signal decisions; a CSV header and **synthetic formatting example only**:

```csv
start,end,expected,direction,label
2025-06-25T09:54:00-04:00,2025-06-25T09:56:00-04:00,ENTER,BULLISH,example_MAC
2025-06-27T10:00:00-04:00,2025-06-27T10:10:00-04:00,NO_ENTRY,ANY,example_chop
```

Use half-open windows, sorted and non-overlapping. `ENTER` requires exactly one matching-direction order request and no extra signals; `NO_ENTRY` requires zero. Only fully observed measured windows are scored. These are signal decisions, not fills, and unannotated periods do not establish precision. The sample rows are not instructor-verified labels. Review JSONL narrative, model, structure, discretion, regime, order, trade and rejection logs against annotated bars. No counts of rejection events should be mistaken for counts of unique trades.

## Remaining blockers / next milestone

1. No independently verified contract-identified exchange sample or historical news calendar was available in this conversation; the actual provider call was not made. Data coverage validation rejected the existing Yahoo cache.
2. All-STRICT remains zero trades on the original unseeded fixture. Narrative-only already rejects both legacy signals, and strict structure rejects 102 candidate events on the original suite. The incomplete Yahoo diagnostic shows two successful structural inversions that fail the narrative gate. We have not resolved this with independently annotated real PB setups, and did not relax gates to force trades.
3. Validate actual unseeded positive and negative real-market examples, including first-tap multi-candle manipulations, SMT-equivalent taps, Judas exceptions, alternative bias invalidation and entry timing. Distinguish genuinely invalid examples from overly restrictive implementation. Do not tune on the holdout or force positive P&L.
4. Qualify complete fixed-contract data and real execution costs, then run chronological holdout/walk-forward tests across multiple months and regimes, keeping every failure and no-trade day visible. This toolkit does not automate selecting rolling contracts or walking across multiple contract segments, and does not independently verify calendar provenance.
5. Only after the actual-data and rule-fidelity blockers are resolved, proceed to V2.7 forward paper testing. Passing synthetic tests is not production readiness. Keep real-money trading disabled.
