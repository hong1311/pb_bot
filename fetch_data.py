import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def fetch_yfinance(out_dir: Path, days: int) -> None:
    try:
        import pandas as pd
        import yfinance as yf
    except ImportError:
        sys.exit("Missing package. Run: pip install yfinance pandas")

    # Yahoo keeps ~30 days of 1m data but caps each request to <= 7-8 days
    days = min(max(1, days), 29)
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc - timedelta(days=days)

    for key, ticker in (("nq", "NQ=F"), ("es", "ES=F")):
        print(f"Downloading {ticker} (~{days}d chunked, 1m) via Yahoo Finance...")
        chunks = []
        cursor = start_utc
        while cursor < now_utc:
            chunk_end = min(cursor + timedelta(days=7), now_utc)
            df = yf.Ticker(ticker).history(
                start=cursor, end=chunk_end, interval="1m", prepost=True
            )
            if not df.empty:
                chunks.append(df)
            cursor = chunk_end

        if not chunks:
            sys.exit(f"No data returned for {ticker}.")

        full = pd.concat(chunks)
        full = full[~full.index.duplicated(keep="last")].sort_index()
        full = full.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )[["open", "high", "low", "close", "volume"]].dropna()

        full.index = (
            full.index.tz_convert(NY_TZ)
            if full.index.tz is not None
            else full.index.tz_localize(NY_TZ)
        )
        full.index.name = "timestamp"

        out_path = out_dir / f"{key}_1m_yf.csv"
        full.to_csv(out_path)
        print(f"  Saved {len(full):,} bars ({full.index[0]} -> {full.index[-1]}) -> {out_path}")


def fetch_dukascopy(out_dir: Path, start: str, end: str) -> None:
    try:
        import pandas as pd
        import dukascopy_python.compat as duka
    except ImportError:
        sys.exit("Missing package. Run: pip install dukascopy-python pandas")

    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)

    # USATECH.IDX/USD = Nasdaq 100 proxy; USA500.IDX/USD = S&P 500 proxy
    mapping = {
        "nq": "USATECH.IDX/USD",
        "es": "USA500.IDX/USD",
    }

    for key, inst in mapping.items():
        print(f"Downloading {inst} ({start} -> {end}, 1m) via Dukascopy (free, no key)...")
        df = duka.fetch(
            inst,
            duka.INTERVAL_MIN_1,
            duka.OFFER_SIDE_BID,
            start_dt,
            end_dt,
        )
        if df is None or df.empty:
            sys.exit(f"No data returned from Dukascopy for {inst}.")

        # Normalize columns & convert UTC timestamps -> America/New_York
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp")
        else:
            df.index = pd.to_datetime(df.index, utc=True)

        df.index = df.index.tz_convert(NY_TZ)
        df.index.name = "timestamp"
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[cols].dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()

        out_path = out_dir / f"{key}_1m_duka.csv"
        df.to_csv(out_path)
        print(f"  Saved {len(df):,} bars ({df.index[0]} -> {df.index[-1]}) -> {out_path}")


def fetch_databento(
    out_dir: Path,
    start: str,
    end: str,
    nq_sym: str,
    es_sym: str,
    stype_in: str,
    max_cost_usd: float,
    dry_run: bool,
) -> None:
    try:
        import databento as db
    except ImportError:
        sys.exit("Missing package. Run: pip install databento")

    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        sys.exit("Set DATABENTO_API_KEY first: export DATABENTO_API_KEY='db-...'")

    client = db.Historical(api_key)
    dataset = "GLBX.MDP3"
    schema = "ohlcv-1m"
    symbols = [nq_sym, es_sym]

    est_cost = client.metadata.get_cost(
        dataset=dataset,
        symbols=symbols,
        stype_in=stype_in,
        schema=schema,
        start=start,
        end=end,
    )
    print(f"Databento cost estimate for {symbols} ({start} -> {end}, {schema}): ${est_cost:.4f} USD")

    if dry_run:
        print("Dry run enabled — no data downloaded and $0.00 charged.")
        return
    if est_cost > max_cost_usd:
        sys.exit(f"Aborted: estimated cost ${est_cost:.2f} exceeds --max-cost ${max_cost_usd:.2f}.")

    store = client.timeseries.get_range(
        dataset=dataset,
        symbols=symbols,
        stype_in=stype_in,
        schema=schema,
        start=start,
        end=end,
    )
    df = store.to_df()
    if df.empty:
        sys.exit("Databento returned 0 rows.")

    df.index = df.index.tz_convert(NY_TZ)
    df.index.name = "timestamp"

    for key, sym in (("nq", nq_sym), ("es", es_sym)):
        sub = df[df["symbol"] == sym][["open", "high", "low", "close", "volume"]].copy()
        sub = sub[~sub.index.duplicated(keep="last")].sort_index()
        out_path = out_dir / f"{key}_1m_db.csv"
        sub.to_csv(out_path)
        print(f"  Saved {len(sub):,} bars ({sub.index[0]} -> {sub.index[-1]}) -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch real historical NQ + ES 1m bars for PB Bot")
    parser.add_argument("--source", choices=("yfinance", "dukascopy", "databento"), required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("data_cache"))
    parser.add_argument("--days", type=int, default=28, help="yfinance 1m lookback days (max 29)")
    parser.add_argument("--start", default="2026-09-01", help="Start date YYYY-MM-DD (UTC)")
    parser.add_argument("--end", default="2026-11-24", help="End date YYYY-MM-DD (UTC)")
    parser.add_argument("--nq-sym", default="NQ.v.0")
    parser.add_argument("--es-sym", default="ES.v.0")
    parser.add_argument("--stype", default="continuous", choices=("continuous", "raw_symbol"))
    parser.add_argument("--max-cost", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.source == "yfinance":
        fetch_yfinance(args.out_dir, args.days)
    elif args.source == "dukascopy":
        fetch_dukascopy(args.out_dir, args.start, args.end)
    else:
        fetch_databento(
            args.out_dir,
            args.start,
            args.end,
            args.nq_sym,
            args.es_sym,
            args.stype,
            args.max_cost,
            args.dry_run,
        )


if __name__ == "__main__":
    main()
