import argparse
import csv
import json
import time as pytime
from datetime import datetime, time
from pathlib import Path
from typing import Any, List, Optional

from pb_bot.backtest.simulator import BacktestBroker
from pb_bot.config import BotConfig, BreakEvenMode, EntryOrderMode
from pb_bot.core.models import Bar, Direction, OrderRequest
from pb_bot.core.sessions import NY_TZ, to_ny
from pb_bot.data.feed import load_csv_bars, sync_dual_feed
from pb_bot.engine.strategy import PBStrategyEngine


class PaperAlertBroker(BacktestBroker):
    """Paper-trading & alert broker: simulates fills/SL/TP/BE and appends events to JSONL + CSV."""

    def __init__(
        self,
        config: BotConfig,
        log_dir: Path,
        webhook_url: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.webhook_url = webhook_url
        self.events_path = self.log_dir / "paper_events.jsonl"
        self.trades_csv_path = self.log_dir / "paper_trades.csv"
        self._seen_trade_ids = set()
        self.closed_count = 0

    def _emit_event(self, event_type: str, payload: dict) -> None:
        record = {
            "logged_at_ny": datetime.now(NY_TZ).isoformat(),
            "event": event_type,
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        msg = f"[PAPER {event_type}] {payload}"
        print(msg, flush=True)

        if self.webhook_url:
            try:
                import urllib.request

                req = urllib.request.Request(
                    self.webhook_url,
                    data=json.dumps({"content": msg}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=3)
            except Exception as exc:
                print(f"  [Webhook warning] {exc}", flush=True)

    def submit_order(self, order: OrderRequest) -> str:
        oid = super().submit_order(order)
        self._emit_event(
            "ORDER_SUBMITTED",
            {
                "order_id": oid,
                "direction": order.direction.value,
                "qty": order.quantity,
                "order_type": getattr(order.order_type, "value", order.order_type),
                "price": getattr(order, 'entry_price', None),
                "sl": order.stop_loss,
                "tp": order.take_profit,
                "reason": order.reason,
            },
        )
        return oid

    def modify_stop_loss(self, order_id: str, new_stop: float, reason: str) -> None:
        prev_stop = self.position.stop_loss if self.position else None
        super().modify_stop_loss(order_id, new_stop, reason)
        if prev_stop != new_stop:
            self._emit_event(
                "STOP_MODIFIED",
                {
                    "order_id": order_id,
                    "old_sl": prev_stop,
                    "new_sl": new_stop,
                    "reason": reason,
                },
            )

    def process_bar(self, bar: Bar) -> Optional[Any]:
        closed = super().process_bar(bar)
        if closed is not None and closed.order_id not in self._seen_trade_ids:
            self._seen_trade_ids.add(closed.order_id)
            self.closed_count += 1
            self._emit_event(
                "TRADE_CLOSED",
                {
                    "order_id": closed.order_id,
                    "direction": closed.direction.value,
                    "qty": closed.quantity,
                    "entry": closed.entry_price,
                    "exit": closed.exit_price,
                    "pnl_dollars": round(closed.pnl_dollars, 2),
                    "r_multiple": round(closed.r_multiple, 2),
                    "exit_reason": closed.exit_reason,
                    "opened_at": to_ny(closed.opened_at).isoformat(),
                    "closed_at": to_ny(closed.closed_at).isoformat(),
                },
            )
            self._append_trade_csv(closed)
        return closed

    def _append_trade_csv(self, t: Any) -> None:
        write_header = not self.trades_csv_path.exists()
        with self.trades_csv_path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "order_id", "direction", "qty", "opened_at", "closed_at",
                    "entry", "exit", "sl", "tp", "pnl_dollars", "r_multiple",
                    "exit_reason", "setup_reason",
                ])
            writer.writerow([
                t.order_id, t.direction.value, t.quantity,
                to_ny(t.opened_at).isoformat(), to_ny(t.closed_at).isoformat(),
                t.entry_price, t.exit_price, t.initial_stop_loss, t.take_profit,
                round(t.pnl_dollars, 2), round(t.r_multiple, 2),
                t.exit_reason, t.setup_reason,
            ])


def run_paper_replay(
    nq_bars: List[Bar],
    es_bars: List[Bar],
    cfg: BotConfig,
    log_dir: Path,
    pace_ms: float = 0.0,
    webhook_url: Optional[str] = None,
) -> None:
    broker = PaperAlertBroker(cfg, log_dir=log_dir, webhook_url=webhook_url)
    engine = PBStrategyEngine(cfg, broker)

    for nq_bar, es_bar in sync_dual_feed(nq_bars, es_bars):
        closed = broker.process_bar(nq_bar)
        if closed is not None:
            if closed.pnl_dollars < 0:
                engine.losses_today += 1
            engine.active_position = None
            engine.risk.equity = broker.balance
        engine.on_1m_pair(nq_bar, es_bar)
        if broker.position is not None and engine.active_position is not None:
            engine.active_position.entry_price = broker.position.entry_price
        if pace_ms > 0:
            pytime.sleep(pace_ms / 1000.0)

    print(
        f"\nPaper run complete | Final Balance: ${broker.balance:,.2f} | "
        f"Closed Trades: {broker.closed_count} | Logs: {log_dir.resolve()}",
        flush=True,
    )




def _df_to_bars(df, symbol: str, timeframe: str = "1m") -> List[Bar]:
    import pandas as pd
    bars: List[Bar] = []
    if df is None or df.empty:
        return bars
    sub = df.copy()
    sub.columns = [str(c).lower() for c in sub.columns]
    if sub.index.tz is None:
        sub.index = sub.index.tz_localize(NY_TZ)
    else:
        sub.index = sub.index.tz_convert(NY_TZ)
    for ts, row in sub.iterrows():
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
            v = float(row.get("volume", 0.0) or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0 or h < l:
            continue
        dt_val = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        bars.append(Bar(timestamp=dt_val, open=o, high=h, low=l, close=c, volume=v, symbol=symbol, timeframe=timeframe))
    bars.sort(key=lambda b: b.timestamp)
    return bars


def poll_live_yfinance(
    cfg: BotConfig,
    log_dir: Path,
    poll_sec: float = 60.0,
    lookback_days: int = 2,
    once: bool = False,
    ny_hours_only: bool = False,
    webhook_url: Optional[str] = None,
) -> None:
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is required for --live mode. Install with: pip install yfinance")

    from datetime import timedelta

    broker = PaperAlertBroker(cfg, log_dir=log_dir, webhook_url=webhook_url)
    engine = PBStrategyEngine(cfg, broker)
    seen_ts = None
    started_mono = pytime.monotonic()

    print(
        f"Starting live 1m polling (NQ=F / ES=F) every {poll_sec:.0f}s | "
        f"NY window {cfg.am_start.strftime('%H:%M')}-{cfg.am_end.strftime('%H:%M')} | Logs: {log_dir.resolve()}",
        flush=True,
    )

    while True:
        now_ny = datetime.now(NY_TZ)
        if ny_hours_only and (now_ny.weekday() >= 5 or not (time(9, 25) <= now_ny.time() <= time(11, 35))):
            if once:
                print(f"[{now_ny.strftime('%Y-%m-%d %H:%M:%S %Z')}] Outside NY AM window; skipping (--ny-hours-only).", flush=True)
                break
            pytime.sleep(max(15.0, min(poll_sec, 60.0)))
            continue
        now_utc = datetime.now(NY_TZ).astimezone(ZoneInfo("UTC")) if False else None
        from datetime import timezone
        now_u = datetime.now(timezone.utc)
        start_u = now_u - timedelta(days=max(1, lookback_days))

        nq_df = yf.Ticker("NQ=F").history(start=start_u, end=now_u, interval="1m", prepost=True)
        es_df = yf.Ticker("ES=F").history(start=start_u, end=now_u, interval="1m", prepost=True)

        nq_bars = _df_to_bars(nq_df, "NQ", "1m")
        es_bars = _df_to_bars(es_df, "ES", "1m")

        if seen_ts is not None:
            nq_bars = [b for b in nq_bars if b.timestamp > seen_ts]
            es_bars = [b for b in es_bars if b.timestamp > seen_ts]

        is_warmup = seen_ts is None
        new_count = 0
        for nq_bar, es_bar in sync_dual_feed(nq_bars, es_bars):
            closed = broker.process_bar(nq_bar)
            if closed is not None:
                if closed.pnl_dollars < 0:
                    engine.losses_today += 1
                engine.active_position = None
                engine.risk.equity = broker.balance
            if is_warmup:
                saved_cap = engine.config.max_trades_per_day
                engine.config = type(engine.config)(**{**vars(engine.config), "max_trades_per_day": 0})
                engine.on_1m_pair(nq_bar, es_bar)
                engine.config = type(engine.config)(**{**vars(engine.config), "max_trades_per_day": saved_cap})
            else:
                engine.on_1m_pair(nq_bar, es_bar)
            if broker.position is not None and engine.active_position is not None:
                engine.active_position.entry_price = broker.position.entry_price
            seen_ts = nq_bar.timestamp
            new_count += 1

        print(
            f"[{datetime.now(NY_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] "
            f"Processed {new_count} new bar pair(s) | Equity: ${broker.balance:,.2f} | Closed: {broker.closed_count}",
            flush=True,
        )

        if once:
            break
        if max_minutes is not None and (pytime.monotonic() - started_mono) >= max_minutes * 60.0:
            print(f'Reached --max-minutes ({max_minutes}); exiting cleanly.', flush=True)
            break
        pytime.sleep(max(5.0, poll_sec))


def main() -> None:
    parser = argparse.ArgumentParser(description="PB Bot Paper-Trading & Alert Replay Runner")
    parser.add_argument("--nq-csv", type=Path, default=None, help="Path to 1m NQ CSV (replay mode)")
    parser.add_argument("--es-csv", type=Path, default=None, help="Path to 1m ES CSV (replay mode)")
    parser.add_argument("--live", action="store_true", help="Poll live 1m NQ=F/ES=F bars via yfinance")
    parser.add_argument("--poll-sec", type=float, default=60.0, help="Polling interval in seconds for --live")
    parser.add_argument("--lookback-days", type=int, default=2, help="Warmup lookback days for --live")
    parser.add_argument("--once", action="store_true", help="Run a single poll cycle in --live mode and exit")
    parser.add_argument("--ny-hours-only", action="store_true", help="Only poll on weekdays between 09:25 and 11:35 ET")
    parser.add_argument("--log-dir", type=Path, default=Path("paper_logs"))
    parser.add_argument("--pace-ms", type=float, default=0.0, help="Optional delay per 1m bar in ms")
    parser.add_argument("--webhook", type=str, default=None, help="Optional Discord/Slack webhook URL")
    parser.add_argument("--allow-pm", action="store_true")
    args = parser.parse_args()

    cfg = BotConfig(
        am_start=time(9, 35),
        am_end=time(11, 30),
        entry_mode=EntryOrderMode.MARKET_ON_CLOSE,
        be_mode=BreakEvenMode.OFF,
        allow_pm_session=args.allow_pm,
    )
    if args.live:
        poll_live_yfinance(
            cfg,
            log_dir=args.log_dir,
            poll_sec=args.poll_sec,
            lookback_days=args.lookback_days,
            once=args.once,
            ny_hours_only=args.ny_hours_only,
            webhook_url=args.webhook,
        )
        return
    if not args.nq_csv or not args.es_csv:
        parser.error('--nq-csv and --es-csv are required unless --live is set')
    nq_bars = load_csv_bars(args.nq_csv, "NQ", "1m")
    es_bars = load_csv_bars(args.es_csv, "ES", "1m")
    run_paper_replay(nq_bars, es_bars, cfg, args.log_dir, pace_ms=args.pace_ms, webhook_url=args.webhook)


if __name__ == "__main__":
    main()
