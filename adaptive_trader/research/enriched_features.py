#!/usr/bin/env python3
"""Enriched features: order book, true CVD, tick volume profile, open
interest / long-short ratios, and MEXC funding — built into per-bar columns
aligned to the MEXC candles the engines already run on.

WHY BINANCE FOR THE HISTORY
    MEXC exposes its book, trade stream and OI only live. Binance publishes
    daily bulk files for USD-M perps going back years (data.binance.vision):
      aggTrades  tick-level trades with aggressor side  -> CVD, volume profile
      bookDepth  ~30s snapshots of depth at +-1..5%     -> book imbalance
      metrics    5-min open interest + long/short ratios -> OI features
    Same assets, deeper market; aligned to MEXC candles by UTC timestamp.
    Funding is a COST and must be the venue's own: MEXC's public
    funding_rate/history is used for that.

ALIGNMENT (no look-ahead)
    A candle row with bar start t covers [t, t+tf). Feature values for that
    row are computed from Binance data inside that same window, so a feature
    is known exactly when the candle's close is known — the same moment the
    engines already make decisions on. Nothing from after t+tf is used.

LAYOUT
    research/features/raw/<COIN>/<YYYY-MM-DD>.parquet   per-minute aggregates
    research/features/raw/<COIN>/<YYYY-MM-DD>.hist.parquet  per-minute
                                                        volume-at-price (sparse)
    research/features/<coin>_<tf>min.parquet            aligned per-bar features
    research/features/manifest.json                     coverage + bin widths
    research/data/funding_<coin>.json                   [[settle_ms, rate], ...]

USAGE (run on the box that holds research/data; resumable, day-granular)
    python3 enriched_features.py build   --coin sui [--since 2024-09-13] [--tf 1 3]
    python3 enriched_features.py funding --coin sui
    python3 enriched_features.py status
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import zipfile

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FEAT = os.path.join(HERE, "features")
RAW = os.path.join(FEAT, "raw")
MANIFEST = os.path.join(FEAT, "manifest.json")

BINANCE = "https://data.binance.vision/data/futures/um/daily"
MEXC_FUNDING = "https://contract.mexc.com/api/v1/contract/funding_rate/history"

# Binance USD-M symbol per coin (all the coins we trade exist there).
SYMBOL = {c: c.upper() + "USDT" for c in
          ("btc", "eth", "sol", "doge", "xrp", "sui", "hype")}

VP_LOOKBACKS_MIN = (120, 300, 600)      # minutes; POC/value-area windows
VP_BIN_BPS = 5.0                        # price bin = 5 bp of the reference px
VA_FRACTION = 0.70                      # value area = 70% of window volume
DEPTH_LEVELS = (1, 2, 5)                # imbalance at +-1%, +-2%, +-5%
FEATURE_COLS = ["vol_buy", "vol_sell", "n_trades", "vwap",
                "bid1", "ask1", "bid2", "ask2", "bid5", "ask5",
                "oi", "oi_val", "ls_top_acct", "ls_top_pos", "ls_acct",
                "taker_ls"]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ---------------------------------------------------------------- manifest --
def _load_manifest():
    try:
        return json.load(open(MANIFEST))
    except Exception:
        return {"coins": {}}


def _save_manifest(m):
    # several coins build in parallel: per-PID tmp + a lock, or two writers
    # rename the same tmp and one of them dies with FileNotFoundError
    import fcntl
    os.makedirs(FEAT, exist_ok=True)
    with open(MANIFEST + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tmp = f"{MANIFEST}.tmp{os.getpid()}"
        json.dump(m, open(tmp, "w"), indent=1)
        os.replace(tmp, MANIFEST)


def _update_manifest(fn):
    """Read-modify-write under the lock; fn(manifest) mutates in place."""
    import fcntl
    os.makedirs(FEAT, exist_ok=True)
    with open(MANIFEST + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        m = _load_manifest()
        fn(m)
        tmp = f"{MANIFEST}.tmp{os.getpid()}"
        json.dump(m, open(tmp, "w"), indent=1)
        os.replace(tmp, MANIFEST)
        return m


# ---------------------------------------------------------------- download --
def _fetch_zip(dataset, symbol, day, retries=3):
    """Return the CSV bytes of one daily file, or None if it does not exist
    (before listing / after today). Raises on network failure after retries."""
    fn = f"{symbol}-{dataset}-{day}.zip"
    url = f"{BINANCE}/{dataset}/{symbol}/{fn}"
    for k in range(retries):
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                return z.read(name)
        except Exception as e:
            if k == retries - 1:
                raise
            time.sleep(2 * (k + 1))


def _read_csv(raw, **kw):
    """Binance files sometimes carry a header row and sometimes not."""
    head = raw[:200].decode("utf-8", "ignore")
    has_header = not head[0].isdigit()
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None, **kw)


# ------------------------------------------------------------- per-day raw --
def build_day(coin, day, bin_width, force=False):
    """Download the three Binance files for one UTC day and reduce them to
    per-minute aggregates + a sparse per-minute volume-at-price histogram.
    Returns (minutes_df, hist_df) and caches both. None if the day is not
    published (yet)."""
    sym = SYMBOL[coin]
    d = os.path.join(RAW, sym)
    os.makedirs(d, exist_ok=True)
    p_min = os.path.join(d, f"{day}.parquet")
    p_hist = os.path.join(d, f"{day}.hist.parquet")
    if not force and os.path.exists(p_min) and os.path.exists(p_hist):
        return pd.read_parquet(p_min), pd.read_parquet(p_hist)

    raw = _fetch_zip("aggTrades", sym, day)
    if raw is None:
        return None
    t = _read_csv(raw)
    if t.shape[1] < 7:
        raise RuntimeError(f"{sym} {day}: unexpected aggTrades layout")
    t.columns = ["agg_trade_id", "price", "quantity", "first_trade_id",
                 "last_trade_id", "transact_time", "is_buyer_maker"][:t.shape[1]]
    px = t["price"].astype(np.float64).values
    qty = t["quantity"].astype(np.float64).values
    ts = t["transact_time"].astype(np.int64).values
    ibm = t["is_buyer_maker"]
    if ibm.dtype != bool:
        ibm = ibm.astype(str).str.lower().isin(["true", "1"])
    sell = ibm.values.astype(bool)          # buyer is maker => aggressor SOLD
    day0 = int(pd.Timestamp(day, tz="UTC").timestamp() * 1000)
    minute = ((ts - day0) // 60000).astype(np.int64)
    ok = (minute >= 0) & (minute < 1440)
    px, qty, sell, minute = px[ok], qty[ok], sell[ok], minute[ok]

    vol_buy = np.bincount(minute, weights=np.where(sell, 0.0, qty), minlength=1440)
    vol_sell = np.bincount(minute, weights=np.where(sell, qty, 0.0), minlength=1440)
    n_tr = np.bincount(minute, minlength=1440).astype(np.float64)
    pv = np.bincount(minute, weights=px * qty, minlength=1440)
    vol = vol_buy + vol_sell
    vwap = np.where(vol > 0, pv / np.maximum(vol, 1e-12), np.nan)

    # sparse volume-at-price per minute: (minute, bin, vol)
    b = np.floor(px / bin_width).astype(np.int64)
    key = minute * (1 << 32) + (b - b.min() + 1)      # combine for grouping
    uniq, inv = np.unique(key, return_inverse=True)
    vsum = np.bincount(inv, weights=qty)
    hm = (uniq >> 32).astype(np.int32)
    hb = ((uniq & 0xFFFFFFFF) + b.min() - 1).astype(np.int64)
    hist = pd.DataFrame({"minute": hm, "bin": hb, "vol": vsum})

    mins = pd.DataFrame({
        "t": pd.to_datetime(day0 + np.arange(1440) * 60000, unit="ms", utc=True),
        "vol_buy": vol_buy, "vol_sell": vol_sell, "n_trades": n_tr, "vwap": vwap})

    # ---- book depth: last snapshot in each minute, +-1/2/5% notional -------
    raw = _fetch_zip("bookDepth", sym, day)
    for L in DEPTH_LEVELS:
        mins[f"bid{L}"] = np.nan
        mins[f"ask{L}"] = np.nan
    if raw is not None:
        bd = _read_csv(raw)
        bd.columns = ["timestamp", "percentage", "depth", "notional"][:bd.shape[1]]
        bd["ts"] = pd.to_datetime(bd["timestamp"], utc=True)
        # unit-safe: pandas' astype("int64") on datetimes is version-dependent
        bd["minute"] = ((bd["ts"] - pd.Timestamp(day, tz="UTC")).dt.total_seconds() // 60).astype(int)
        bd = bd[(bd["minute"] >= 0) & (bd["minute"] < 1440)]
        bd["pct"] = bd["percentage"].astype(int)
        last = bd.sort_values("ts").groupby(["minute", "pct"])["notional"].last().unstack("pct")
        for L in DEPTH_LEVELS:
            if -L in last.columns:
                mins.loc[last.index, f"bid{L}"] = last[-L].values
            if L in last.columns:
                mins.loc[last.index, f"ask{L}"] = last[L].values
        for L in DEPTH_LEVELS:
            mins[f"bid{L}"] = mins[f"bid{L}"].ffill()
            mins[f"ask{L}"] = mins[f"ask{L}"].ffill()

    # ---- metrics: 5-min OI + ratios, forward-filled to minutes -------------
    raw = _fetch_zip("metrics", sym, day)
    for c in ("oi", "oi_val", "ls_top_acct", "ls_top_pos", "ls_acct", "taker_ls"):
        mins[c] = np.nan
    if raw is not None:
        m = _read_csv(raw)
        m.columns = ["create_time", "symbol", "sum_open_interest",
                     "sum_open_interest_value", "count_toptrader_long_short_ratio",
                     "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                     "sum_taker_long_short_vol_ratio"][:m.shape[1]]
        m["ts"] = pd.to_datetime(m["create_time"], utc=True)
        m["minute"] = ((m["ts"] - pd.Timestamp(day, tz="UTC")).dt.total_seconds() // 60).astype(int)
        m = m[(m["minute"] >= 0) & (m["minute"] < 1440)].set_index("minute")
        col = {"oi": "sum_open_interest", "oi_val": "sum_open_interest_value",
               "ls_top_acct": "count_toptrader_long_short_ratio",
               "ls_top_pos": "sum_toptrader_long_short_ratio",
               "ls_acct": "count_long_short_ratio",
               "taker_ls": "sum_taker_long_short_vol_ratio"}
        for k, src in col.items():
            if src in m.columns:
                mins.loc[m.index, k] = pd.to_numeric(m[src], errors="coerce").values
        for k in col:
            mins[k] = mins[k].ffill()

    mins.to_parquet(p_min + ".tmp", index=False); os.replace(p_min + ".tmp", p_min)
    hist.to_parquet(p_hist + ".tmp", index=False); os.replace(p_hist + ".tmp", p_hist)
    return mins, hist


# ------------------------------------------------------------ volume profile --
def _rolling_vp(hist_days, n_minutes_total, bin_width, lookbacks, va_frac):
    """hist_days: list of per-day sparse histograms in chronological order
    (minute offsets are per day). Returns dict of arrays over ALL minutes:
    poc_<L> price, and vah/val for the longest lookback in `lookbacks`."""
    # global minute index + dense bin range
    rows = []
    off = 0
    for h in hist_days:
        if len(h):
            rows.append(np.column_stack([h["minute"].values + off, h["bin"].values, h["vol"].values]))
        off += 1440
    if not rows:
        out = {f"poc{L}": np.full(n_minutes_total, np.nan) for L in lookbacks}
        out["vah"] = np.full(n_minutes_total, np.nan); out["val"] = np.full(n_minutes_total, np.nan)
        return out
    allh = np.vstack(rows)
    mi = allh[:, 0].astype(np.int64); bi = allh[:, 1].astype(np.int64); vv = allh[:, 2]
    b0, b1 = int(bi.min()), int(bi.max())
    nb = b1 - b0 + 1
    # dense (minutes x bins) is too big for long histories (e.g. 700k x 3000);
    # instead process in chunks with a carried rolling window.
    Lmax = max(lookbacks)
    out = {f"poc{L}": np.full(n_minutes_total, np.nan) for L in lookbacks}
    out["vah"] = np.full(n_minutes_total, np.nan); out["val"] = np.full(n_minutes_total, np.nan)
    CH = 4320                                   # 3 days per chunk
    order = np.argsort(mi, kind="stable")
    mi, bi, vv = mi[order], bi[order], vv[order]
    ptr = 0
    prev_tail = None                            # dense (Lmax x nb) of the last Lmax minutes
    for start in range(0, n_minutes_total, CH):
        end = min(n_minutes_total, start + CH)
        # dense block for [start-Lmax, end)
        lo = max(0, start - Lmax)
        blk = np.zeros((end - lo, nb), dtype=np.float32)
        if prev_tail is not None and lo < start:
            blk[:start - lo] = prev_tail[-(start - lo):]
        # fill this chunk's minutes
        while ptr < len(mi) and mi[ptr] < end:
            if mi[ptr] >= start:
                blk[mi[ptr] - lo, bi[ptr] - b0] += vv[ptr]
            ptr += 1
        cs = np.cumsum(blk, axis=0)
        cs = np.vstack([np.zeros((1, nb), dtype=np.float32), cs])
        for L in lookbacks:
            # window sum for minutes m in [start, end): rows (m-lo-L+1 .. m-lo]
            idx_hi = np.arange(start - lo + 1, end - lo + 1)
            idx_lo = np.maximum(idx_hi - L, 0)
            win = cs[idx_hi] - cs[idx_lo]
            tot = win.sum(axis=1)
            pk = win.argmax(axis=1)
            poc = (pk + b0 + 0.5) * bin_width
            poc[tot <= 0] = np.nan
            out[f"poc{L}"][start:end] = poc
            if L == Lmax:
                # value area: expand from POC until va_frac of volume covered
                vah = np.full(end - start, np.nan); val = np.full(end - start, np.nan)
                for k in range(end - start):
                    if tot[k] <= 0:
                        continue
                    w = win[k]; p = pk[k]; acc = w[p]; a = p; z = p; need = va_frac * tot[k]
                    while acc < need and (a > 0 or z < nb - 1):
                        up = w[z + 1] if z < nb - 1 else -1.0
                        dn = w[a - 1] if a > 0 else -1.0
                        if up >= dn:
                            z += 1; acc += up
                        else:
                            a -= 1; acc += dn
                    vah[k] = (z + b0 + 1.0) * bin_width
                    val[k] = (a + b0) * bin_width
                out["vah"][start:end] = vah; out["val"][start:end] = val
        prev_tail = blk[-Lmax:] if blk.shape[0] >= Lmax else np.vstack(
            [np.zeros((Lmax - blk.shape[0], nb), dtype=np.float32), blk])
    return out


# ----------------------------------------------------------------- funding --
def fetch_funding(coin):
    """MEXC funding-rate history -> research/data/funding_<coin>.json as
    [[settle_ms, rate], ...] ascending (the format hype_overlay_search
    already expects). Public endpoint, direct (no proxy needed)."""
    sym = coin.upper() + "_USDT"
    rows = {}
    page = 1
    while True:
        r = requests.get(MEXC_FUNDING, params=dict(symbol=sym, page_num=page,
                                                   page_size=1000), timeout=30)
        r.raise_for_status()
        d = (r.json() or {}).get("data") or {}
        lst = d.get("resultList") or []
        for x in lst:
            try:
                rows[int(x["settleTime"])] = float(x["fundingRate"])
            except Exception:
                pass
        if not lst or page >= int(d.get("totalPage") or 1):
            break
        page += 1
    out = sorted(rows.items())
    p = os.path.join(DATA, f"funding_{coin}.json")
    json.dump(out, open(p + ".tmp", "w")); os.replace(p + ".tmp", p)
    return out


def _load_funding(coin):
    p = os.path.join(DATA, f"funding_{coin}.json")
    try:
        return json.load(open(p))
    except Exception:
        return []


# ---------------------------------------------------------------- assemble --
def assemble(coin, tf, since=None):
    """Join the per-minute raw days to the MEXC candle file for (coin, tf)
    and write research/features/<coin>_<tf>min.parquet."""
    sym = SYMBOL[coin]
    d = os.path.join(RAW, sym)
    days = sorted(f[:-8] for f in os.listdir(d) if f.endswith(".parquet") and not f.endswith(".hist.parquet")) if os.path.isdir(d) else []
    if since:
        days = [x for x in days if x >= since]
    if not days:
        log(f"{coin}: no raw days — nothing to assemble"); return None
    mins = pd.concat([pd.read_parquet(os.path.join(d, f"{x}.parquet")) for x in days], ignore_index=True)
    hists = [pd.read_parquet(os.path.join(d, f"{x}.hist.parquet")) for x in days]
    man = _load_manifest()
    bw = float(man["coins"][coin]["bin_width"])
    vp = _rolling_vp(hists, len(mins), bw, VP_LOOKBACKS_MIN, VA_FRACTION)
    for k, v in vp.items():
        mins[k] = v
    # derived per-minute
    mins["cvd_delta"] = mins["vol_buy"] - mins["vol_sell"]
    for L in DEPTH_LEVELS:
        b, a = mins[f"bid{L}"], mins[f"ask{L}"]
        mins[f"obi{L}"] = (b - a) / (b + a).replace(0, np.nan)

    # ---- resample to the candle timeframe -------------------------------
    mins = mins.set_index("t")
    rule = f"{tf}min"
    agg = {"vol_buy": "sum", "vol_sell": "sum", "n_trades": "sum", "cvd_delta": "sum",
           "vwap": "mean"}
    for c in mins.columns:
        if c not in agg:
            agg[c] = "last"
    bars = mins.resample(rule, label="left", closed="left").agg(agg)
    bars["cvd"] = bars["cvd_delta"].fillna(0).cumsum()

    # ---- funding: settle flag + rate on the bar containing the settlement --
    fund = _load_funding(coin)
    bars["fund_settle"] = 0.0
    bars["fund_rate"] = 0.0
    if fund:
        ft = pd.to_datetime([x[0] for x in fund], unit="ms", utc=True)
        fr = np.array([x[1] for x in fund], dtype=float)
        fb = ft.floor(rule)
        s = pd.Series(fr, index=fb).groupby(level=0).sum()
        hit = s.index.intersection(bars.index)
        bars.loc[hit, "fund_settle"] = 1.0
        bars.loc[hit, "fund_rate"] = s.loc[hit].values

    # ---- align to the MEXC candle file (bar start t, UTC) ------------------
    cp = os.path.join(DATA, f"{coin}_{tf}min.parquet")
    if not os.path.exists(cp):
        log(f"{coin} {tf}m: no candle file {cp}"); return None
    cand = pd.read_parquet(cp, columns=["t"])
    ct = pd.DatetimeIndex(pd.to_datetime(cand["t"], utc=True)).as_unit("ns")
    bars.index = pd.DatetimeIndex(bars.index).as_unit("ns")
    out = bars.reindex(ct)
    out.insert(0, "t", ct)
    out = out.reset_index(drop=True)
    os.makedirs(FEAT, exist_ok=True)
    p = os.path.join(FEAT, f"{coin}_{tf}min.parquet")
    out.to_parquet(p + ".tmp", index=False); os.replace(p + ".tmp", p)
    cov = out["obi1"].notna() & out["cvd_delta"].notna() & out["oi"].notna()
    first = out.loc[cov, "t"].min(); last = out.loc[cov, "t"].max()
    _update_manifest(lambda m: m["coins"].setdefault(coin, {}).__setitem__(f"tf{tf}", dict(
        rows=int(len(out)), covered=int(cov.sum()),
        first=str(first)[:16] if pd.notna(first) else None,
        last=str(last)[:16] if pd.notna(last) else None,
        built=time.strftime("%Y-%m-%d %H:%M"))))
    log(f"{coin} {tf}m: {int(cov.sum()):,} of {len(out):,} candles covered "
        f"({str(first)[:10]} → {str(last)[:10]})")
    return p


# -------------------------------------------------------------------- build --
def build(coin, since=None, until=None, tfs=(1, 3), force=False):
    sym = SYMBOL[coin]
    def _ensure_bin(m):
        mc = m["coins"].setdefault(coin, {})
        if "bin_width" not in mc:
            cp = os.path.join(DATA, f"{coin}_1min.parquet")
            ref = float(pd.read_parquet(cp, columns=["close"])["close"].median())
            mc["bin_width"] = ref * VP_BIN_BPS / 1e4      # fixed for the whole history
            mc["ref_price"] = ref
    man = _update_manifest(_ensure_bin)
    bw = float(man["coins"][coin]["bin_width"])
    if not since:
        cp = os.path.join(DATA, f"{coin}_1min.parquet")
        since = str(pd.to_datetime(pd.read_parquet(cp, columns=["t"])["t"].min()).date())
    until = until or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    day = dt.date.fromisoformat(since)
    last = dt.date.fromisoformat(until)
    n_ok = n_skip = 0
    t0 = time.time()
    while day <= last:
        ds = day.isoformat()
        try:
            r = build_day(coin, ds, bw, force=force)
        except Exception as e:
            log(f"{sym} {ds}: FAILED {str(e)[:100]} — will retry next run")
            day += dt.timedelta(days=1); continue
        if r is None:
            n_skip += 1
        else:
            n_ok += 1
            if n_ok % 25 == 0:
                log(f"{sym}: {n_ok} days built ({ds}), {n_skip} unpublished, "
                    f"{(time.time()-t0)/60:.1f} min")
        day += dt.timedelta(days=1)
    log(f"{sym}: raw done — {n_ok} days, {n_skip} not published")
    if not _load_funding(coin):
        try:
            fetch_funding(coin); log(f"{coin}: funding history fetched")
        except Exception as e:
            log(f"{coin}: funding fetch failed: {e}")
    for tf in tfs:
        assemble(coin, tf)


def status():
    man = _load_manifest()
    for coin, mc in sorted(man.get("coins", {}).items()):
        d = os.path.join(RAW, SYMBOL[coin])
        n = len([f for f in os.listdir(d) if f.endswith(".parquet") and not f.endswith(".hist.parquet")]) if os.path.isdir(d) else 0
        print(f"{coin:5s} raw days={n:4d}  bin={mc.get('bin_width', 0):.6g}  "
              + "  ".join(f"{k}: {v.get('covered', 0):,}/{v.get('rows', 0):,} "
                          f"{v.get('first', '?')[:10] if v.get('first') else '?'}→"
                          f"{v.get('last', '?')[:10] if v.get('last') else '?'}"
                          for k, v in mc.items() if k.startswith("tf")))
        fp = os.path.join(DATA, f"funding_{coin}.json")
        if os.path.exists(fp):
            f = json.load(open(fp))
            print(f"      funding: {len(f)} settlements, "
                  f"{pd.to_datetime(f[0][0], unit='ms').date()} → {pd.to_datetime(f[-1][0], unit='ms').date()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--coin", required=True, choices=sorted(SYMBOL))
    b.add_argument("--since"); b.add_argument("--until")
    b.add_argument("--tf", nargs="+", type=int, default=[1, 3]); b.add_argument("--force", action="store_true")
    f = sub.add_parser("funding"); f.add_argument("--coin", required=True, choices=sorted(SYMBOL))
    a = sub.add_parser("assemble"); a.add_argument("--coin", required=True); a.add_argument("--tf", nargs="+", type=int, default=[1, 3])
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.coin, args.since, args.until, tuple(args.tf), args.force)
    elif args.cmd == "funding":
        out = fetch_funding(args.coin); print(f"{len(out)} settlements")
    elif args.cmd == "assemble":
        for tf in args.tf:
            assemble(args.coin, tf)
    else:
        status()


if __name__ == "__main__":
    main()
