#!/usr/bin/env python3
"""Regime characterization + scalping-method battery for ADVERSE (red)
windows — the state Adrian trades manually: a parent long thesis underwater,
price chopping. Everything DYNAMIC: targets are moving levels (Bollinger mid,
VWAP), stops are ATR-trailing (chandelier); nothing is a fixed percent.

Methods (long-only, spot, taker fee both sides, forced flat at window end):
  bbrev   Bollinger reversion: enter close < lower band(20,2) on 5m;
          target = middle band (moves); chandelier stop 3*ATR.
  vwaprev VWAP deviation: enter < rolling 4h VWAP - 2*sigma; target = VWAP;
          chandelier 3*ATR.
  rsi2    Connors RSI(2)<10 entry; exit close > 5-bar high or RSI2>70;
          chandelier 3*ATR.
  stokel  Keltner(20, 2*ATR) lower-touch + stoch(14) K<20 entry;
          target = Keltner mid; chandelier 2.5*ATR.
  strend  Supertrend(10, 3) flip-long follower (trend control group).
  bbrev+f bbrev gated by regime filter: only when ADX(14)<20 (chop only).
Report per pair: total net %, % windows beating hold, avg trades/window,
worst window. Baseline: hold-to-end of the same windows.
"""
import sys
import numpy as np
import pandas as pd

FEE = 0.0005
H, STEP, RED = 3 * 1440, 6 * 60, 0.01
TF = 5   # work on 5m bars inside windows (scalp signal timeframe)


def load(pair):
    df = pd.read_parquet("/Users/admn/strategy-lab/adaptive_trader/research/"
                         "data/%s_spot_1min.parquet" % pair)
    tcol = [c for c in df.columns
            if c.lower() in ("t", "time", "ts", "datetime", "date")][0]
    df[tcol] = pd.to_datetime(df[tcol])
    df = df.set_index(tcol).loc["2025-11-20":]
    o = df["close"].resample("%dmin" % TF).ohlc().dropna()
    vol = None
    for vc in ("volume", "vol", "v"):
        if vc in df.columns:
            vol = df[vc].resample("%dmin" % TF).sum().reindex(o.index)
            break
    if vol is None:
        vol = pd.Series(1.0, index=o.index)
    return o, vol, df["close"]


def indicators(o, vol):
    c, h, l = o["close"], o["high"], o["low"]
    x = {}
    x["ma20"] = c.rolling(20).mean()
    sd = c.rolling(20).std()
    x["bb_lo"] = x["ma20"] - 2 * sd
    tr = pd.concat([(h - l), (h - c.shift()).abs(),
                    (l - c.shift()).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(14).mean()
    pv = (c * vol).rolling(48).sum()
    x["vwap"] = pv / vol.rolling(48).sum().replace(0, np.nan)
    x["vsd"] = (c - x["vwap"]).rolling(48).std()
    d = c.diff()
    up2 = d.clip(lower=0).ewm(alpha=1 / 2).mean()
    dn2 = (-d.clip(upper=0)).ewm(alpha=1 / 2).mean()
    x["rsi2"] = 100 - 100 / (1 + up2 / dn2.replace(0, np.nan))
    x["hi5"] = c.rolling(5).max()
    x["kel_lo"] = x["ma20"] - 2 * x["atr"]
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    x["stoK"] = 100 * (c - lo14) / (hi14 - lo14).replace(0, np.nan)
    # supertrend(10,3)
    atr10 = tr.rolling(10).mean()
    mid = (h + l) / 2
    ub, lb = mid + 3 * atr10, mid - 3 * atr10
    st = np.zeros(len(c)); dirn = np.ones(len(c))
    ubv, lbv, cv = ub.values, lb.values, c.values
    for i in range(1, len(c)):
        u = min(ubv[i], st[i - 1]) if dirn[i - 1] < 0 else ubv[i]
        b = max(lbv[i], st[i - 1]) if dirn[i - 1] > 0 else lbv[i]
        if cv[i] > (st[i - 1] if dirn[i - 1] < 0 else u):
            dirn[i] = 1; st[i] = b
        elif cv[i] < (st[i - 1] if dirn[i - 1] > 0 else b):
            dirn[i] = -1; st[i] = u
        else:
            dirn[i] = dirn[i - 1]
            st[i] = b if dirn[i] > 0 else u
    x["st_dir"] = pd.Series(dirn, index=c.index)
    # ADX(14)
    upm = h.diff(); dnm = -l.diff()
    plus = pd.Series(np.where((upm > dnm) & (upm > 0), upm, 0), index=c.index)
    minus = pd.Series(np.where((dnm > upm) & (dnm > 0), dnm, 0), index=c.index)
    trs = tr.rolling(14).sum().replace(0, np.nan)
    pdi = 100 * plus.rolling(14).sum() / trs
    mdi = 100 * minus.rolling(14).sum() / trs
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    x["adx"] = dx.rolling(14).mean()
    return {k: v.values for k, v in x.items()}


def simulate(kind, cv, X, a, b):
    """Long-only inside [a,b): entries per method, dynamic target + chandelier."""
    net = 0.0; trades = 0; inpos = False; entry = hi = stop = 0.0
    for i in range(a, b):
        p = cv[i]
        if inpos:
            hi = max(hi, p)
            atr = X["atr"][i] if X["atr"][i] == X["atr"][i] else p * 0.003
            m = 2.5 if kind == "stokel" else 3.0
            stop = max(stop, hi - m * atr)
            tgt = {"bbrev": X["ma20"][i], "vwaprev": X["vwap"][i],
                   "stokel": X["ma20"][i]}.get(kind)
            out = p <= stop
            if kind == "rsi2":
                out = out or p > X["hi5"][i - 1] or X["rsi2"][i] > 70
            elif kind == "strend":
                out = out or X["st_dir"][i] < 0
            elif tgt is not None and tgt == tgt:
                out = out or p >= tgt
            if out:
                net += (p / entry - 1) - 2 * FEE
                inpos = False
        else:
            e = False
            if kind in ("bbrev", "bbrev+f"):
                e = X["bb_lo"][i] == X["bb_lo"][i] and p < X["bb_lo"][i]
                if kind == "bbrev+f":
                    e = e and X["adx"][i] == X["adx"][i] and X["adx"][i] < 20
            elif kind == "vwaprev":
                v, s = X["vwap"][i], X["vsd"][i]
                e = v == v and s == s and p < v - 2 * s
            elif kind == "rsi2":
                e = X["rsi2"][i] == X["rsi2"][i] and X["rsi2"][i] < 10
            elif kind == "stokel":
                e = (X["kel_lo"][i] == X["kel_lo"][i] and p < X["kel_lo"][i]
                     and X["stoK"][i] == X["stoK"][i] and X["stoK"][i] < 20)
            elif kind == "strend":
                e = X["st_dir"][i] > 0 and X["st_dir"][i - 1] < 0
            if e:
                inpos = True; entry = hi = p
                atr = X["atr"][i] if X["atr"][i] == X["atr"][i] else p * 0.003
                stop = p - (2.5 if kind == "stokel" else 3.0) * atr
                trades += 1
    if inpos:
        net += (cv[b - 1] / entry - 1) - 2 * FEE
    return net, trades


def main(pair):
    o, vol, c1 = load(pair)
    X = indicators(o, vol)
    cv = o["close"].values
    idx = o.index
    # red windows on the 1m series, mapped to 5m offsets
    v1 = c1.values
    segs = []
    i = 0
    while i + H < len(v1):
        w = v1[i:i + H]; e = w[0]
        for j in range(1, H):
            if w[j] <= e * (1 - RED):
                t_red = c1.index[i + j]; t_end = c1.index[i + H - 1]
                a = idx.searchsorted(t_red); b = idx.searchsorted(t_end)
                if b - a > 20:
                    segs.append((a, b))
                break
        i += STEP
    hold = sum((cv[b - 1] / cv[a] - 1) - 2 * FEE for a, b in segs)
    print("%s: %d red windows | hold-to-end %+7.1f%%"
          % (pair.upper(), len(segs), 100 * hold))
    for kind in ("bbrev", "bbrev+f", "vwaprev", "rsi2", "stokel", "strend"):
        tot = 0.0; wins = 0; ntr = 0; worst = 9e9
        for a, b in segs:
            n, t = simulate(kind, cv, X, a, b)
            tot += n; ntr += t
            hb = (cv[b - 1] / cv[a] - 1) - 2 * FEE
            if n > hb:
                wins += 1
            worst = min(worst, n)
        print("  %-8s %+8.1f%%  beats hold %3d%%  %.1f trades/win  worst %+5.1f%%"
              % (kind, 100 * tot, round(100 * wins / len(segs)),
                 ntr / len(segs), 100 * worst))


if __name__ == "__main__":
    for p in (sys.argv[1:] or ["xrp", "hype", "sol", "doge"]):
        main(p)
