"""
Python replication of the V5.2 Multi-Timeframe RSI MACD BB%B Pine strategy.
TradingView semantics: signals on bar close, fills at next bar open.
"""
import numpy as np
import pandas as pd

# ---------------- TradingView-exact indicator implementations ----------------

def ema(src: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1)
    out = np.empty_like(src, dtype=float)
    out[:] = np.nan
    prev = np.nan
    for i in range(len(src)):
        x = src[i]
        if np.isnan(x):
            out[i] = prev
            continue
        prev = x if np.isnan(prev) else alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out

def rma(src: np.ndarray, n: int) -> np.ndarray:
    # Pine rma: seeds with SMA of first n values
    alpha = 1.0 / n
    out = np.full(len(src), np.nan)
    prev = np.nan
    count, acc = 0, 0.0
    for i in range(len(src)):
        x = src[i]
        if np.isnan(x):
            continue
        if np.isnan(prev):
            acc += x
            count += 1
            if count == n:
                prev = acc / n
                out[i] = prev
        else:
            prev = alpha * x + (1 - alpha) * prev
            out[i] = prev
    return out

def rsi(close: np.ndarray, n: int) -> np.ndarray:
    diff = np.diff(close, prepend=np.nan)
    up = np.where(diff > 0, diff, 0.0)
    dn = np.where(diff < 0, -diff, 0.0)
    up[np.isnan(diff)] = np.nan
    dn[np.isnan(diff)] = np.nan
    ru, rd = rma(up, n), rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(rd == 0, 100.0, np.where(ru == 0, 0.0, 100.0 - 100.0 / (1 + ru / rd)))
    out[np.isnan(ru) | np.isnan(rd)] = np.nan
    return out

def atr(high, low, close, n):
    pc = np.roll(close, 1); pc[0] = np.nan
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    tr[0] = high[0] - low[0]
    return rma(tr, n)

def sma(src, n):
    s = pd.Series(src)
    return s.rolling(n).mean().to_numpy()

def stdev_pop(src, n):
    s = pd.Series(src)
    return s.rolling(n).std(ddof=0).to_numpy()

def macd_line(close, fast, slow):
    return ema(close, fast) - ema(close, slow)

# ---------------- Default parameters (Pine V5.2 defaults) ----------------

DEFAULT_PARAMS = dict(
    leverage=8.0,
    # 3m subsystem
    atrLength_3m=3,
    emaLongLenLow=95, emaLongLenHigh=75, volThreshLong=0.95,
    emaShortLenLow=270, emaShortLenHigh=75, volThreshShort=0.55,
    rsiLenLong=3, rsiValLong=68.0,
    macdFastLong=3, macdSlowLong=7, macdSmoothLong=9, macdValPctLong=-0.235 / 100,
    bbLenLong=21, bbStdLong=2.0, bbValLong=-0.05,
    ptLong=1.20 / 100, slLong=10.0 / 100, apt1Long=0.7 / 100, apt2Long=0.407 / 100,
    dur1Long=0.6, dur2Long=14.4,           # minutes
    cdPctLong=0.402 / 100, cdPeriodLong=90, cdTfLong="1",
    rsiLenShort=3, rsiValShort=68.0,
    macdFastShort=3, macdSlowShort=7, macdSmoothShort=9, macdValPctShort=0.21 / 100,
    bbLenShort=21, bbStdShort=2.0, bbValShort=0.825,
    ptShort=1.0 / 100, slShort=10.0 / 100, apt1Short=0.9 / 100, apt2Short=0.4 / 100,
    dur1Short=0.6, dur2Short=14.4,
    cdPctShort=0.3 / 100, cdPeriodShort=70, cdTfShort="3",
    # 3m_cross subsystem
    xFast=12, xSlow=26, xSig=8,
    xTpLong=0.4 / 100, xTpShort=0.15 / 100, xSlLong=10.0 / 100, xSlShort=10.0 / 100,
    xMinBetween=13,                          # minutes -> bars gap = x*(60/3)
    xMacdMinShort=0.55, xMacdMaxLong=-99.0,
    xHistRisingBars=12, xRequireHistPos=True,
    xApt1Long=0.3 / 100, xApt2Long=0.207 / 100, xDur1Long=60.0, xDur2Long=120.0,
    xCdPctLong=99.0 / 100, xCdPeriodLong=90, xCdTfLong="1",
    xApt1Short=0.1 / 100, xApt2Short=0.0 / 100, xDur1Short=60.0, xDur2Short=120.0,
    xCdPctShort=0.03 / 100, xCdPeriodShort=7, xCdTfShort="3",
    # account
    initial_capital=1000.0, commission=0.0004,
)

# ---------------- Signal computation (vectorized) ----------------

def last_1m_metric(df3: pd.DataFrame, df1: pd.DataFrame) -> np.ndarray:
    """Replicates request.security(tid, "1", close[1]-close) sampled at 3m bar closes:
    value of (prev 1m close - last 1m close) for the last 1m bar ending <= chart bar end."""
    c1 = df1["close"].to_numpy()
    d1 = np.roll(c1, 1) - c1
    d1[0] = np.nan
    end1 = (df1["t"] + pd.Timedelta(minutes=1)).to_numpy()
    end3 = (df3["t"] + pd.Timedelta(minutes=3)).to_numpy()
    idx = np.searchsorted(end1, end3, side="right") - 1
    out = np.full(len(df3), np.nan)
    ok = idx >= 0
    out[ok] = d1[idx[ok]]
    return out

def compute_signals(df3: pd.DataFrame, df1: pd.DataFrame, p: dict) -> pd.DataFrame:
    o = df3["open"].to_numpy(); h = df3["high"].to_numpy()
    l = df3["low"].to_numpy(); c = df3["close"].to_numpy()

    a = atr(h, l, c, p["atrLength_3m"])
    emaLL, emaLH = ema(c, p["emaLongLenLow"]), ema(c, p["emaLongLenHigh"])
    emaSL, emaSH = ema(c, p["emaShortLenLow"]), ema(c, p["emaShortLenHigh"])
    emaLong = np.where(a > p["volThreshLong"], emaLH, emaLL)
    emaShort = np.where(a > p["volThreshShort"], emaSH, emaSL)
    emaLongUp = np.diff(emaLong, prepend=np.nan) > 0
    emaShortDown = np.diff(emaShort, prepend=np.nan) < 0

    rsiL = rsi(c, p["rsiLenLong"]); rsiS = rsi(c, p["rsiLenShort"])
    macdL = macd_line(c, p["macdFastLong"], p["macdSlowLong"])
    macdS = macd_line(c, p["macdFastShort"], p["macdSlowShort"])

    basisL = sma(c, p["bbLenLong"]); devL = p["bbStdLong"] * stdev_pop(c, p["bbLenLong"])
    bbPctL = (c - (basisL - devL)) / (2 * devL)
    basisS = sma(c, p["bbLenShort"]); devS = p["bbStdShort"] * stdev_pop(c, p["bbLenShort"])
    bbPctS = (c - (basisS - devS)) / (2 * devS)

    xMacd = macd_line(c, p["xFast"], p["xSlow"])
    xSignal = ema(xMacd, p["xSig"])
    xHist = xMacd - xSignal
    xUp = np.zeros(len(c), bool); xDn = np.zeros(len(c), bool)
    xUp[1:] = (xMacd[1:] > xSignal[1:]) & (xMacd[:-1] <= xSignal[:-1])
    xDn[1:] = (xMacd[1:] < xSignal[1:]) & (xMacd[:-1] >= xSignal[:-1])

    nrb = p["xHistRisingBars"]
    histRising = np.ones(len(c), bool)
    for k in range(nrb):  # hist[k] > hist[k+1] for k = 0..nrb-1
        cur = np.roll(xHist, k); cur[:k] = np.nan
        nxt = np.roll(xHist, k + 1); nxt[:k + 1] = np.nan
        histRising &= (cur > nxt)

    # cooldown metrics
    cprev = np.roll(c, 1); cprev[0] = np.nan
    m1 = last_1m_metric(df3, df1)          # (close[1]-close) on 1m series
    m3 = cprev - c                          # (close[1]-close) on 3m (chart) series
    def cd_metric(tf):
        return m1 if tf == "1" else m3
    priceDropLong = cd_metric(p["cdTfLong"]) / cprev
    priceIncShort = -cd_metric(p["cdTfShort"]) / cprev
    priceDropXL = cd_metric(p["xCdTfLong"]) / cprev
    priceIncXS = -cd_metric(p["xCdTfShort"]) / cprev

    out = pd.DataFrame({
        "t": df3["t"], "open": o, "high": h, "low": l, "close": c,
        "volume": df3["volume"].to_numpy(),
        "atr": a, "rsiL": rsiL, "rsiS": rsiS, "macdL": macdL, "macdS": macdS,
        "bbPctL": bbPctL, "bbPctS": bbPctS,
        "emaLongUp": emaLongUp, "emaShortDown": emaShortDown,
        "xMacd": xMacd, "xSignal": xSignal, "xHist": xHist,
        "xUp": xUp, "xDn": xDn, "histRising": histRising,
        "cdCondL": priceDropLong <= -p["cdPctLong"],
        "cdCondS": priceIncShort >= p["cdPctShort"],
        "cdCondXL": priceDropXL <= -p["xCdPctLong"],
        "cdCondXS": priceIncXS >= p["xCdPctShort"],
    })
    return out

# ---------------- Event loop ----------------

def run_backtest(sig: pd.DataFrame, p: dict, warmup_bars: int = 3000):
    t_ms = (sig["t"].astype("int64") // 10**6).to_numpy()  # bar OPEN time ms
    o = sig["open"].to_numpy(); h = sig["high"].to_numpy()
    l = sig["low"].to_numpy(); c = sig["close"].to_numpy()
    n = len(sig)

    rsiL = sig["rsiL"].to_numpy(); rsiS = sig["rsiS"].to_numpy()
    macdL = sig["macdL"].to_numpy(); macdS = sig["macdS"].to_numpy()
    bbPctL = sig["bbPctL"].to_numpy(); bbPctS = sig["bbPctS"].to_numpy()
    emaLongUp = sig["emaLongUp"].to_numpy(); emaShortDown = sig["emaShortDown"].to_numpy()
    xMacd = sig["xMacd"].to_numpy(); xUp = sig["xUp"].to_numpy(); xDn = sig["xDn"].to_numpy()
    xHist = sig["xHist"].to_numpy(); histRising = sig["histRising"].to_numpy()
    cdCondL = sig["cdCondL"].to_numpy(); cdCondS = sig["cdCondS"].to_numpy()
    cdCondXL = sig["cdCondXL"].to_numpy(); cdCondXS = sig["cdCondXS"].to_numpy()

    equity = p["initial_capital"]
    comm = p["commission"]; lev = p["leverage"]

    cdStartL = cdStartS = cdStartXL = cdStartXS = -1e18
    lastLongBarX = lastShortBarX = None
    gapBars = p["xMinBetween"] * (60 / 3)

    pos = 0            # +1 long, -1 short
    pend_entry = None  # (dir, system) to fill at next open
    pend_exit = None   # reason
    qty = 0.0; entry_price = np.nan; entry_time = None; slPrice = np.nan; system = None
    runMin = np.inf; runMax = -np.inf
    trades = []

    for i in range(n):
        # ---- fills at this bar's open ----
        if pend_exit is not None and pos != 0:
            px = o[i]
            gross = qty * (px - entry_price) * pos
            fee = comm * qty * (entry_price + px)
            net = gross - fee
            equity += net
            # adverse move against position, as fraction of entry (negative = against you)
            adverse = (runMin / entry_price - 1) if pos > 0 else -(runMax / entry_price - 1)
            favor = (runMax / entry_price - 1) if pos > 0 else -(runMin / entry_price - 1)
            trades.append(dict(entry_t=fill_t, exit_t=sig["t"].iloc[i],
                               dir="long" if pos > 0 else "short", system=system,
                               entry=entry_price, exit=px, qty=qty, net=net,
                               mae=adverse, mfe=favor,
                               reason=pend_exit, equity=equity))
            pos = 0; pend_exit = None; qty = 0.0
        if pend_entry is not None and pos == 0:
            d, sys_, q, slp, et = pend_entry
            pos = d; qty = q; entry_price = o[i]; slPrice = slp
            system = sys_; entry_time = et; fill_t = sig["t"].iloc[i]
            runMin = np.inf; runMax = -np.inf
            pend_entry = None
        if pos != 0:
            runMin = min(runMin, l[i]); runMax = max(runMax, h[i])

        # ---- bar close processing ----
        if i < warmup_bars:
            continue
        tm = t_ms[i]

        # cooldown state updates (script order: before entries)
        if cdCondL[i]: cdStartL = tm
        if cdCondS[i]: cdStartS = tm
        if cdCondXL[i]: cdStartXL = tm
        if cdCondXS[i]: cdStartXS = tm
        actL = (tm - cdStartL) < p["cdPeriodLong"] * 60000
        actS = (tm - cdStartS) < p["cdPeriodShort"] * 60000
        actXL = (tm - cdStartXL) < p["xCdPeriodLong"] * 60000
        actXS = (tm - cdStartXS) < p["xCdPeriodShort"] * 60000

        normMacdL = p["macdValPctLong"] * c[i]
        normMacdS = p["macdValPctShort"] * c[i]

        long3m = (rsiL[i] < p["rsiValLong"] and macdL[i] < normMacdL
                  and bbPctL[i] < p["bbValLong"] and emaLongUp[i] and not actL)
        short3m = (rsiS[i] > p["rsiValShort"] and macdS[i] > normMacdS
                   and bbPctS[i] > p["bbValShort"] and emaShortDown[i] and not actS)

        canL = lastLongBarX is None or (i - lastLongBarX) > gapBars
        canS = lastShortBarX is None or (i - lastShortBarX) > gapBars
        longX = (xUp[i] and xMacd[i] < p["xMacdMaxLong"] and histRising[i]
                 and (not p["xRequireHistPos"] or xHist[i] > 0) and not actXL and canL)
        shortX = (xDn[i] and xMacd[i] > p["xMacdMinShort"] and not actXS and canS)

        longCond = long3m or longX
        shortCond = short3m or shortX

        flat = (pos == 0 and pend_entry is None and pend_exit is None) or \
               (pos == 0 and pend_entry is None)
        # entries only when no open trade (order may already be pending -> pine would
        # also allow since position not yet open; but replicate na(entry_price) check:
        # position opens only at fill, so a pending entry means previous bar signalled;
        # pine would place another order replacing it. Keep last.)
        if pos == 0:
            if longCond:
                q = equity / c[i] * lev
                if long3m:
                    pend_entry = (1, "3m", q, c[i] * (1 - p["slLong"]), tm)
                else:
                    pend_entry = (1, "3m_cross", q, c[i] * (1 - p["xSlLong"]), tm)
                    lastLongBarX = i
            if shortCond:
                q = equity / c[i] * lev
                if short3m:
                    pend_entry = (-1, "3m", q, c[i] * (1 + p["slShort"]), tm)
                else:
                    pend_entry = (-1, "3m_cross", q, c[i] * (1 + p["xSlShort"]), tm)
                    lastShortBarX = i

        # exits
        if pos > 0:
            if l[i] <= slPrice:
                pend_exit = "stop_loss"
            else:
                if system == "3m":
                    pt, a1, a2, d1, d2 = p["ptLong"], p["apt1Long"], p["apt2Long"], p["dur1Long"], p["dur2Long"]
                else:
                    pt, a1, a2, d1, d2 = p["xTpLong"], p["xApt1Long"], p["xApt2Long"], p["xDur1Long"], p["xDur2Long"]
                if tm >= entry_time + d2 * 60000: pt = a2
                elif tm >= entry_time + d1 * 60000: pt = a1
                if c[i] >= entry_price * (1 + pt):
                    pend_exit = "profit_target"
        elif pos < 0:
            if h[i] >= slPrice:
                pend_exit = "stop_loss"
            else:
                if system == "3m":
                    pt, a1, a2, d1, d2 = p["ptShort"], p["apt1Short"], p["apt2Short"], p["dur1Short"], p["dur2Short"]
                else:
                    pt, a1, a2, d1, d2 = p["xTpShort"], p["xApt1Short"], p["xApt2Short"], p["xDur1Short"], p["xDur2Short"]
                if tm >= entry_time + d2 * 60000: pt = a2
                elif tm >= entry_time + d1 * 60000: pt = a1
                if c[i] <= entry_price * (1 - pt):
                    pend_exit = "profit_target"

    return pd.DataFrame(trades), equity
