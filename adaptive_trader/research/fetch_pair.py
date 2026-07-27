#!/usr/bin/env python3
"""Resumable CoinAPI kline downloader for ANY MEXC perp pair.

Creates/extends data/<coin>_3min.parquet and data/<coin>_1min.parquet with the
exact schema of the original SOL files (t tz-aware UTC, open, high, low,
close, volume, trades_count), so load_segments() and every engine work
unchanged on the new pair.

Usage:
  python3 fetch_pair.py btc                    # perp data (default)
  python3 fetch_pair.py --market spot btc eth  # SPOT candles -> <coin>_spot_*.parquet
Env: COINAPI_KEY overrides the default key.
"""
import gzip, json, os, sys, time, urllib.request
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API_KEY = os.environ.get("COINAPI_KEY", "caaddd33-6801-48b9-a48f-642ba05bffb5")
DATA_START = "2023-11-27T00:00:00"          # CoinAPI's MEXC coverage start
COLS = ["t", "open", "high", "low", "close", "volume", "trades_count"]


def _utc(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def fetch(symbol_id, period, time_start, time_end):
    rows_all, cur = [], time_start
    end_ts = _utc(time_end)
    while _utc(cur) < end_ts:
        url = (f"https://rest.coinapi.io/v1/ohlcv/{symbol_id}/history"
               f"?period_id={period}&time_start={cur}&time_end={time_end}"
               f"&limit=100000&include_empty_items=false")
        req = urllib.request.Request(url, headers={"X-CoinAPI-Key": API_KEY,
                                                   "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        rows = json.loads(raw)
        if isinstance(rows, dict):                       # error payload
            raise RuntimeError(str(rows)[:300])
        if not rows:
            # CoinAPI has HOLES in its MEXC minute data (e.g. 2024-06 ->
            # 2024-11, present in the original SOL files too, and the engines
            # tolerate them). An empty page therefore means "hole", not
            # "done" — hop forward and keep probing until the end date.
            nxt = _utc(cur) + pd.Timedelta(days=7)
            cur = nxt.strftime("%Y-%m-%dT%H:%M:%S")
            continue
        rows_all.extend(rows)
        cur = rows[-1]["time_period_end"]
        print(f"    {period}: +{len(rows)} rows (through {cur[:16]})", flush=True)
        # NOTE: do NOT break on len(rows) < limit — CoinAPI routinely returns
        # short pages mid-history; only an EMPTY page means we're done.
    return rows_all


def to_df(rows):
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["time_period_start"], utc=True)
    df = df.rename(columns={"price_open": "open", "price_high": "high",
                            "price_low": "low", "price_close": "close",
                            "volume_traded": "volume"})
    if "trades_count" not in df.columns:
        df["trades_count"] = pd.NA
    return df[COLS]


def backfill(coin, period, fname, market="perp"):
    os.makedirs(DATA, exist_ok=True)
    symbol_id = (f"MEXC_SPOT_{coin.upper()}_USDT" if market == "spot"
                 else f"MEXCFTS_PERP_{coin.upper()}_USDT")
    path = os.path.join(DATA, fname)
    if os.path.exists(path):
        df = pd.read_parquet(path)
        start = (pd.Timestamp(df["t"].max()) - pd.Timedelta(minutes=10)) \
            .strftime("%Y-%m-%dT%H:%M:%S")
        print(f"  {fname}: resuming from {start}", flush=True)
    else:
        df, start = pd.DataFrame(columns=COLS), DATA_START
        print(f"  {fname}: full backfill from {start}", flush=True)
    end = pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    rows = fetch(symbol_id, period, start, end)
    if not rows:
        print(f"  {fname}: no new data", flush=True)
        return
    new = to_df(rows)
    if len(df):
        if not str(df["t"].dtype).startswith("datetime64[ns,"):
            df["t"] = pd.to_datetime(df["t"], utc=True)
        merged = pd.concat([df, new], ignore_index=True)
    else:
        merged = new
    merged = (merged.drop_duplicates("t", keep="last")
              .sort_values("t").reset_index(drop=True))
    merged.to_parquet(path, index=False)
    print(f"  {fname}: {len(df)} -> {len(merged)} bars "
          f"(span {merged['t'].min()} -> {merged['t'].max()})", flush=True)


if __name__ == "__main__":
    argv = sys.argv[1:]
    market = "perp"
    if "--market" in argv:
        i = argv.index("--market")
        market = argv[i + 1].lower()
        argv = argv[:i] + argv[i + 2:]
    coins = [c.lower() for c in argv]
    if not coins:
        sys.exit("usage: fetch_pair.py [--market spot|perp] <coin> [...]")
    sfx = "_spot" if market == "spot" else ""
    for c in coins:
        print(f"=== {c.upper()}_USDT ({market}) ===", flush=True)
        backfill(c, "3MIN", f"{c}{sfx}_3min.parquet", market)
        backfill(c, "1MIN", f"{c}{sfx}_1min.parquet", market)
    print("all done", flush=True)
