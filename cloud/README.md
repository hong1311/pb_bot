# PB Trading Assistant — consolidated research system

One local service: existing PB V2.6 engine, strict signal qualification, scheduled NQ/ES monitoring, Telegram private/group delivery, personal trade journal, dashboard, reference-level tracking, startup/health/session messages, persistent duplicate protection and local Mac auto-start. No broker API or automated order execution. This is **research/paper only**; no evidence of trading profitability or approval for Alpha Futures is implied.

## Install once (existing paired Mac user)

1. Stop the previous Telegram assistant with Ctrl+C in its Terminal. If a background installation of this package is already running, use `bash stop.sh` from that package instead.
2. Extract `pb_telegram_complete.zip` to Downloads; keep the extracted directory there. Open Terminal and run:

```bash
cd "$HOME/Downloads/pb_telegram_complete" && bash install.sh
```

Requires macOS and Python 3.12+ (the package retains syntax used in the prior version), internet and permission to install free Python dependencies. The installer checks for the existing token, takes a private SQLite backup, makes a virtual environment, installs dependencies including certifi, and registers a launchd user agent. No token or account re-registration. It uses `~/.pb_telegram` for your original private pairing and journal. It preserves those records and adds tables. The old instance must not run simultaneously. The agent runs after macOS login and restarts on exit. `caffeinate -i` inhibits idle sleep, not lid-close sleep or power-off. Leave your Mac awake, logged in, connected to power and internet. The service's schedule is based on New York time, not your Mac's timezone. GitHub's missed schedule is bypassed, not repaired remotely.

3. Send `/status` in your paired private Telegram chat. If the group is already linked, it remains linked. Otherwise add your bot to a group you administer, send `/group` privately, then send the generated `/linkgroup@BotUsername CODE` in the group. Keep privacy mode enabled and no unnecessary bot administrator rights. `/groupdemo` is an explicitly labeled group test, `/demo` is a private test. `/groupoff` unlinks. Group journal and configuration commands are ignored except owner-admin linking.

## External prerequisite for qualified alerts: reviewed calendar

The market monitor runs automatically, but **qualified broadcasts are locked until current session and news coverage is reviewed**. No verified free economic calendar is configured. The package deliberately ships an empty example rather than assuming there is no news. You may supply a reviewed `~/.pb_telegram/calendar_review.json`. The service loads it automatically. Use the included `calendar_review.example.json` as its format; do not mark any date reviewed without verifying it against an authoritative CME schedule and a comprehensive economic calendar for relevant high-impact releases. Include the next calendar day's releases to implement the PB pre-CPI/NFP rule. Review normal CME equity-index session hours, special closures/early closes, all relevant RED/ORANGE news, and their New York times. A conservative XNYS calendar proxy also excludes weekends, exchange holidays and shortened cash-market days. This proxy is not a substitute for CME's actual hours. Review must be refreshed at least every seven days. If your data provider/calendar cannot support the checks, the correct result is **no broadcast**. The following example is syntax only, NOT valid reviewed market data:

```json
{
  "reviewed_at": "YYYY-MM-DDTHH:MM:SS+00:00",
  "source": "Reviewed CME session schedule and named economic calendar, with URLs",
  "regular_cme_days": ["YYYY-MM-DD"],
  "covered_news_dates": ["YYYY-MM-DD", "NEXT-DATE"],
  "events": [
    {"date": "YYYY-MM-DD", "time_ny": "HH:MM", "title": "Actual event title", "impact": "RED"}
  ]
}
```

An empty `events` array is valid only if the operator has actually confirmed no relevant events in all declared covered dates. This is an operator attestation, not independent verification by the program. Do not supply invented dates or events. The bot reports the calendar lock in `/status`; it continues analysis with the strict calendar gate closed. An expired or changed calendar is also checked again before sending. News conditions are evaluated by the existing PB engine and at qualification/delivery. No automatic unverified scraping of news.

## What runs automatically

- Local NY weekday monitor 08:30–11:35; entries 09:30–11:00 only. Outside-window data prefetch runs at most once per local date for historical warmup, never generates entries. Cash-market holidays and early closes are conservatively skipped. A user-reviewed normal CME day is required for broadcasts.
- Closed, timestamp-aligned NQ=F and ES=F Yahoo bars. Reject malformed, off-tick, duplicate, future, mismatched, stale (>90 seconds after bar close), missing-current-session-minute or revised-price observations. Three prior complete regular sessions for research warmup; save complete prior Yahoo sessions locally to reduce retention failures. Cache is never used to establish current freshness. First/recovery polls warm up only; only the latest newly received bar may originate a candidate. No catch-up entries. No claim that Yahoo provides exchange-certified real-time data or fixed-contract/roll integrity.
- All five engine gates STRICT: narrative, playbook, structure, discretion, regime. Mandatory evidence, approved calendar, >=1.4 gross RR, preliminary evidence score >=80/100, no explicitly failed component; only MAC, CONTINUATION, PO3 with recorded evidence. Unknown evidence remains unknown. Scoring is alignment, not win probability. SMT only credited if explicitly present; an eligible model need not use SMT as sponsorship. Existing model gates remain authoritative. The simulated order quantity uses the original $50k MNQ research configuration, not your Alpha evaluation's buying power or limits.
- Persist a stable signal ID and maximum two qualified alerts per NY session across restarts. Expire after 90 seconds from candle close, not log time. No historical pending alerts on upgrade. Sends group alerts automatically only when linked, plus private copy with journal buttons. Group receives no personal journal data. Delivery retries while fresh; Telegram cannot guarantee exactly-once delivery after ambiguous network failure. Dedup prevents duplicate database signal IDs.
- One startup message and session summary to private/group. Data/calendar failures privately, not group spam. Status reports monitor heartbeat, last closed paired bar, and engine rejection count including warmup. If the computer or network is offline it cannot send a failure message until recovery; there is no external uptime monitor.
- Reference-only follow-ups for subsequent NQ price stop/target touches or loss of narrative/regime support. Both touched in one OHLC minute means sequence unknown. Missing/restart gaps mean outcome unknown. Tracking ends at 11:35 NY. These are not MNQ broker fills or order-management instructions; no P&L inferred. A reference follow-up goes only to destinations that received the initial alert. Private manual records remain separate.

## Private commands

`/status` includes market monitor health and linked group. `/dashboard` shows today's signals, journal counts, net realized P&L/R, win rate and market health. `/journal`, `/trade ID`, `/note ID TEXT`, `/manual LONG|SHORT ENTRY QTY SL TP`, `/take ID ENTRY QTY SL TP`, `/close ID EXIT TOTAL_FEES`, `/skip ID`, `/help`. Tap Took Trade to get the entry confirmation command; actual entry, quantity and original stop/target must be entered manually. Record actual total USD fees when closing. Demo entries do not enter actual dashboard performance. Only full-position integer MNQ trades supported. No partial fills, scaling, actual account balance, live risk-limit synchronization or Alpha-specific rule certification. Confirm your evaluation account ownership and signal-assistance rules before relying on alerts. No trading from Telegram buttons.

## Operational commands

```bash
# Stop automatic agent (journal unchanged)
cd "$HOME/Downloads/pb_telegram_complete" && bash stop.sh
# See private runtime logs (never share token or account data)
tail -n 50 "$HOME/.pb_telegram/service.log"
tail -n 50 "$HOME/.pb_telegram/service.err.log"
```

If you prefer foreground instead of launchd or need setup for a new bot:

```bash
cd "$HOME/Downloads/pb_telegram_complete"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export SSL_CERT_FILE="$(.venv/bin/python -m certifi)"
# only if no existing token:
# .venv/bin/python pb_telegram.py --setup
caffeinate -i .venv/bin/python -u pb_telegram.py
```

Do not run foreground and background instances concurrently. `--log` is intentionally disabled; arbitrary legacy paper events cannot bypass qualification. Existing GitHub workflow and MT5 remain untouched. GitHub write access was rejected in earlier branch creation (403); no remote workflow or secrets were installed. Hosting while Mac is off requires a reliable permitted persistent host and credentials, not ephemeral public runner journal storage. Actual Telegram transport, live Yahoo API and launchd behavior have to be verified on the Mac. Offline tests cannot establish profitable live setups or uptime. No guarantee of a signal on any date; no news, missing history, stale quotes or strict rejection can all lead to zero alerts.

## Tests

Run `.venv/bin/python -m unittest -q` from this directory. The included PB V2.6 strategy/validation tests, previous Telegram tests and new consolidated qualification/service tests are offline. `test_complete.py` uses synthetic events and data; it does not represent live trading returns. `TEST_RESULTS.txt` records the test output from the build environment.
