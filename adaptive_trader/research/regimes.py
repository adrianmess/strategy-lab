"""Causal regime features: trailing-percentile vol/volume/trend buckets."""
import numpy as np
import pandas as pd

DAY = 480  # 3-min bars per day

def rolling_pct_rank(x: pd.Series, window: int) -> np.ndarray:
    """Causal percentile rank of latest value within trailing window."""
    r = x.rolling(window, min_periods=window // 4).rank(pct=True)
    return r.to_numpy()

def regime_features(pre, vol_win=30 * DAY):
    c = pre["c"]; h = pre["h"]; l = pre["l"]; v = pre["vol"]
    s = pd.Series(c)
    # realized vol: std of 3m log returns over 1 day, annualization irrelevant
    lr = np.log(s / s.shift(1))
    rv = lr.rolling(DAY).std()
    volPct = rolling_pct_rank(rv, vol_win)
    dollar = pd.Series(v * c)
    dv = dollar.rolling(DAY).sum()
    volumePct = rolling_pct_rank(dv, vol_win)
    # trend: close vs 1-day EMA, in daily-vol units
    emaD = s.ewm(span=DAY, adjust=False).mean()
    trend = ((s - emaD) / (rv * np.sqrt(DAY) * s)).to_numpy()  # z-ish score
    atrPct = (pd.Series(pre["h"] - pre["l"]).rolling(DAY).mean() / s).to_numpy()
    volPct7 = rolling_pct_rank(rv, 7 * DAY)
    return dict(volPct=volPct, volumePct=volumePct, trend=trend,
                rv=rv.to_numpy(), atrPct=atrPct, volPct7=volPct7)

# ---------------- regime method zoo ----------------

def _terciles(x, lo=0.33, hi=0.67):
    r = np.full(len(x), 1, dtype=np.int32)
    r[x <= lo] = 0
    r[x >= hi] = 2
    r[np.isnan(x)] = 1
    return r

def make_regimes(f, method):
    """Causal per-bar regime index for a feature dict from regime_features()."""
    n = len(f["trend"])
    if method == "none":
        return np.zeros(n, dtype=np.int32), 1
    if method == "vol3":
        return _terciles(f["volPct"]), 3
    if method == "vol3_7d":
        return _terciles(f["volPct7"]), 3
    if method == "volume3":
        return _terciles(f["volumePct"]), 3
    if method == "trend3":
        t = f["trend"]
        r = np.full(n, 1, dtype=np.int32)
        r[t <= -0.5] = 0
        r[t >= 0.5] = 2
        r[np.isnan(t)] = 1
        return r, 3
    if method == "volXtrend9":
        v, _ = make_regimes(f, "vol3")
        t, _ = make_regimes(f, "trend3")
        return (v * 3 + t).astype(np.int32), 9
    raise ValueError(method)

REGIME_METHODS = ["none", "vol3", "vol3_7d", "volume3", "trend3", "volXtrend9"]

def vol_terciles(volPct):
    """0=low,1=mid,2=high volatility regime, causal."""
    r = np.full(len(volPct), 1, dtype=np.int32)
    r[volPct <= 0.33] = 0
    r[volPct >= 0.67] = 2
    r[np.isnan(volPct)] = 1
    return r
