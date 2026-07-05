"""Validate engine against TradingView CSV export."""
import pandas as pd
import numpy as np
from engine import DEFAULT_PARAMS, compute_signals, run_backtest

CSV = "/sessions/festive-happy-gauss/mnt/uploads/V5.2_Multi-Timeframe_RSI_MACD_BB%B_Strategy_-_3min_+_MACD_Crossover_w_histogram_MEXC_SOLUSDT.P_2026-07-02.csv"

def load_tv():
    tv = pd.read_csv(CSV)
    ent = tv[tv["Type"].str.startswith("Entry")].copy()
    ext = tv[tv["Type"].str.startswith("Exit")].copy()
    ent["t"] = pd.to_datetime(ent["Date and time"])
    ext["t"] = pd.to_datetime(ext["Date and time"])
    m = ent.merge(ext, on="Trade number", suffixes=("_e", "_x"))
    m = m.sort_values("t_e").reset_index(drop=True)
    return m

def load_data():
    df3 = pd.read_parquet("sol_3min.parquet")
    df1 = pd.read_parquet("sol_1min.parquet")
    df3["t"] = df3["t"].dt.tz_localize(None)
    df1["t"] = df1["t"].dt.tz_localize(None)
    return df3, df1

def segments(df3, gap_min=1000):
    d = df3["t"].diff().dt.total_seconds().div(60).fillna(3)
    seg_id = (d > gap_min).cumsum()
    return [g.reset_index(drop=True) for _, g in df3.groupby(seg_id)]

def run_all(params=None, warmup=3000):
    p = params or DEFAULT_PARAMS
    df3, df1 = load_data()
    all_trades = []
    for seg in segments(df3):
        if len(seg) < warmup + 100:
            continue
        lo, hi = seg["t"].min(), seg["t"].max()
        d1 = df1[(df1["t"] >= lo) & (df1["t"] <= hi + pd.Timedelta(minutes=3))].reset_index(drop=True)
        sig = compute_signals(seg, d1, p)
        tr, eq = run_backtest(sig, p, warmup_bars=warmup)
        all_trades.append(tr)
    return pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

if __name__ == "__main__":
    tv = load_tv()
    my = run_all()
    print(f"my trades: {len(my)}")
    if len(my):
        print(my.head(20).to_string())
        # compare within overlap
        for seg_start, seg_end in [("2023-12-10", "2024-06-02"), ("2024-11-25", "2026-07-01")]:
            tvw = tv[(tv["t_e"] >= seg_start) & (tv["t_e"] <= seg_end)]
            myw = my[(my["entry_t"] >= seg_start) & (my["entry_t"] <= seg_end)]
            tv_set = set(tvw["t_e"])
            my_set = set(myw["entry_t"])
            inter = tv_set & my_set
            print(f"\nwindow {seg_start}..{seg_end}: TV={len(tv_set)} mine={len(my_set)} matched_entry_time={len(inter)}")
