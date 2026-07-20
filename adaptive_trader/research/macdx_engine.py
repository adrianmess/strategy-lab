"""
Python replication of the standalone Pine strategy
"MACD Crossover Strategy with TP, SL, Time, MACD Thresholds + Histogram Filter
 + Leverage" (MEXC SOLUSDT.P, 3-min chart).

TradingView semantics reproduced:
- signals evaluated on bar close, market orders fill at NEXT bar open
- strategy.entry REVERSES an opposite position (pyramiding=1 -> same-side
  entries are rejected, but their side effects still run: stop-loss price,
  entry-time and last-order-bar are refreshed)
- ONE shared lastOrderBarIndex gates both directions:
  canOpen = (bar_index - lastOrderBarIndex) > minTimeBetweenOrders * 20 bars
- stop loss price is computed from the SIGNAL bar close; profit targets
  compare the bar close against the actual FILL price (position_avg_price)
- profit target %: base TP until Position Duration 1, then Adjusted PT 1,
  after Position Duration 2 Adjusted PT 2 (durations in HOURS from the
  minute-precision order timestamp)
- cooldowns: long uses 1-minute close-to-close drop (request.security "1"),
  short uses the 3-minute chart's own close-to-close increase; a triggered
  cooldown blocks entries for its period (including the trigger bar)
Validated against the 2026-07-12 TradingView export.
"""
import numpy as np
import pandas as pd
from numba import njit
from engine import last_1m_metric

# P-matrix column order (per-regime rows) for the optimizer path
MACDX_PNAMES = [
    "tpL", "tpS", "slL", "slS", "minT", "mMinS", "mMaxL", "hrb", "reqH",
    "lev", "a1L", "a2L", "d1L", "d2L", "cdPL", "cdTL",
    "a1S", "a2S", "d1S", "d2S", "cdPS", "cdTS", "eL", "eS",
]
_KEY2P = {k: (dk) for k, dk in [
    ("tpL", "takeProfitLongPct"), ("tpS", "takeProfitShortPct"),
    ("slL", "stopLossLongPct"), ("slS", "stopLossShortPct"),
    ("minT", "minTimeBetweenOrders"), ("mMinS", "macdMinForShort"),
    ("mMaxL", "macdMaxForLong"), ("hrb", "histRisingBars"),
    ("reqH", "requireHistAboveZero"), ("lev", "leverage"),
    ("a1L", "apt1Long"), ("a2L", "apt2Long"), ("d1L", "dur1Long"), ("d2L", "dur2Long"),
    ("cdPL", "cdPctLong"), ("cdTL", "cdPeriodLong"),
    ("a1S", "apt1Short"), ("a2S", "apt2Short"), ("d1S", "dur1Short"), ("d2S", "dur2Short"),
    ("cdPS", "cdPctShort"), ("cdTS", "cdPeriodShort"),
    ("eL", "enableLong"), ("eS", "enableShort"),
]}


def macdx_P_from_dict(p):
    """One P row from a pine-named parameter dict."""
    return np.array([[float(p[_KEY2P[k]]) for k in MACDX_PNAMES]])


def pine_ema(src: np.ndarray, n: int) -> np.ndarray:
    """TradingView ta.ema exactly: na for the first n-1 (non-na) values, then
    seeded with the SMA of the first n, then standard recursive EMA. Matters
    when the evaluation window starts cold (short warmup); converges to the
    plain first-value-seeded EMA long before any real warmup ends."""
    alpha = 2.0 / (n + 1)
    out = np.full(len(src), np.nan)
    prev = np.nan
    count, acc = 0, 0.0
    for i in range(len(src)):
        x = src[i]
        if np.isnan(x):
            continue
        if np.isnan(prev):
            acc += x; count += 1
            if count == n:
                prev = acc / n
                out[i] = prev
        else:
            prev = alpha * x + (1 - alpha) * prev
            out[i] = prev
    return out

MACDX_DEFAULTS = dict(
    macdFastLength=12, macdSlowLength=26, macdSignalSmoothing=9,
    takeProfitLongPct=1.0 / 100, takeProfitShortPct=1.0 / 100,
    stopLossLongPct=1.0 / 100, stopLossShortPct=1.0 / 100,
    minTimeBetweenOrders=1,                    # minutes -> ×20 bars on 3m
    macdMinForShort=0.0, macdMaxForLong=0.0,   # RAW price units, like the pine
    histRisingBars=2, requireHistAboveZero=1.0,
    leverage=5.0,
    apt1Long=0.9 / 100, apt2Long=0.407 / 100,
    dur1Long=1.0, dur2Long=72.0,               # hours
    cdPctLong=0.402 / 100, cdPeriodLong=90.0,  # minutes
    apt1Short=0.9 / 100, apt2Short=0.55 / 100,
    dur1Short=24.0, dur2Short=64.0,            # hours
    cdPctShort=0.31 / 100, cdPeriodShort=70.0,
    enableLong=1.0, enableShort=1.0,
)


def precompute_macdx(df3: pd.DataFrame, df1: pd.DataFrame, p: dict) -> dict:
    c = df3["close"].to_numpy()
    macd = pine_ema(c, int(p["macdFastLength"])) - pine_ema(c, int(p["macdSlowLength"]))
    sig = pine_ema(macd, int(p["macdSignalSmoothing"]))
    hist = macd - sig
    m1 = last_1m_metric(df3, df1) if df1 is not None else None
    cprev = np.roll(c, 1); cprev[0] = np.nan
    m3 = cprev - c
    # priceDropLong  = (close[1]-close)/close[1]  on the 1m series (drop -> positive m)
    # priceIncShort  = (close-close[1])/close[1]  on the 3m series
    dropL = (m1 if m1 is not None else m3) / cprev
    incS = -m3 / cprev
    t_ms = (df3["t"].astype("int64") // 10**6).to_numpy().astype(np.float64)
    return dict(t=df3["t"].to_numpy(), t_ms=t_ms,
                o=df3["open"].to_numpy(), h=df3["high"].to_numpy(),
                l=df3["low"].to_numpy(), c=c,
                macd=macd, sig=sig, hist=hist, dropL=dropL, incS=incS)


MAX_TRADES = 60000

@njit(cache=True)
def _core_macdx(t_ms, o, h, l, c, macd, sig, hist, dropL, incS,
                regime, P, warmup, initial_capital, commission, no_entry):
    """Bar-close state machine with next-bar-open fills; per-regime params P.
    trade row: [entry_idx, exit_idx, dir, entry, exit, qty, net, mae, reason, lev]
    reason: 0=profit_target, 1=stop_loss, 2=LIQUIDATED, 3=reversal."""
    n = len(c)
    eq = initial_capital
    pos = 0
    qty = 0.0; entry_px = 0.0
    entry_i = -1
    entry_tm = 0.0
    sl_price = np.nan
    mae = 0.0
    cdStartL = -1e18; cdStartS = -1e18
    last_order_bar = -1e18
    pend_entry = 0
    pend_qty = 0.0; pend_sl = np.nan; pend_tm = 0.0
    pend_close = 0
    pend_lev = 1.0; cur_lev = 1.0
    liquidated = 0
    out = np.empty((MAX_TRADES, 10))
    nt = 0

    for i in range(n):
        # ---- fills at this bar's open (orders queued at previous close) ----
        if pend_close != 0 and pos != 0:
            px = o[i]
            net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
            eq += net
            if nt < MAX_TRADES:
                out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = pend_close - 1
                out[nt, 9] = cur_lev; nt += 1
            pos = 0; qty = 0.0; mae = 0.0
        if pend_entry != 0 and pos != 0 and pos != pend_entry:
            px = o[i]                                            # reversal
            net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
            eq += net
            if nt < MAX_TRADES:
                out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = 3.0
                out[nt, 9] = cur_lev; nt += 1
            pos = 0; qty = 0.0; mae = 0.0
        if pend_entry != 0 and pos == 0:
            pos = pend_entry; qty = pend_qty; entry_px = o[i]; entry_i = i
            sl_price = pend_sl; entry_tm = pend_tm; mae = 0.0
            cur_lev = pend_lev
        pend_entry = 0; pend_close = 0

        # ---- intrabar tracking / liquidation ----
        if pos != 0:
            if pos > 0:
                adverse = l[i] / entry_px - 1.0
            else:
                adverse = 1.0 - h[i] / entry_px
            if adverse < mae:
                mae = adverse
            if cur_lev > 1.0:
                liq_move = 1.0 / cur_lev - 0.008
                if adverse <= -liq_move:
                    px = entry_px * (1 - liq_move * pos)
                    net = qty * (px - entry_px) * pos - commission * qty * (entry_px + px)
                    eq += net
                    if nt < MAX_TRADES:
                        out[nt, 0] = entry_i; out[nt, 1] = i; out[nt, 2] = pos
                        out[nt, 3] = entry_px; out[nt, 4] = px; out[nt, 5] = qty
                        out[nt, 6] = net; out[nt, 7] = mae; out[nt, 8] = 2.0
                        out[nt, 9] = cur_lev; nt += 1
                    pos = 0; qty = 0.0; mae = 0.0
                    liquidated = 1
                    break

        # ---- bar-close evaluation ----
        r = regime[i]
        blocked = (i < warmup) or (no_entry[i] == 1)
        tm = t_ms[i]

        if np.isfinite(dropL[i]) and dropL[i] >= P[r, 14]:
            cdStartL = tm
        if np.isfinite(incS[i]) and incS[i] >= P[r, 20]:
            cdStartS = tm
        actL = (tm - cdStartL) < P[r, 15] * 60000.0
        actS = (tm - cdStartS) < P[r, 21] * 60000.0

        ok = (i >= 1 and np.isfinite(macd[i]) and np.isfinite(sig[i])
              and np.isfinite(macd[i - 1]) and np.isfinite(sig[i - 1]))
        xUp = ok and macd[i] > sig[i] and macd[i - 1] <= sig[i - 1]
        xDn = ok and macd[i] < sig[i] and macd[i - 1] >= sig[i - 1]
        nrb = int(P[r, 7])
        histRising = True
        for k in range(1, nrb + 1):
            if i - k < 0 or not (hist[i - k + 1] > hist[i - k]):
                histRising = False
                break
        longCond = (P[r, 22] > 0 and xUp and macd[i] < P[r, 6] and histRising
                    and (P[r, 8] <= 0 or hist[i] > 0) and not actL)
        shortCond = (P[r, 23] > 0 and xDn and macd[i] > P[r, 5] and not actS)
        can_open = (i - last_order_bar) > P[r, 4] * 20.0

        mark_eq = eq
        if pos != 0:
            mark_eq = eq + qty * (c[i] - entry_px) * pos

        # pine order: long entry block, long exits, short entry block, short exits
        if longCond and can_open and not blocked:
            if pos == 1:                        # rejected order, side effects only
                sl_price = c[i] * (1 - P[r, 2]); entry_tm = tm
            else:
                pend_entry = 1
                pend_lev = P[r, 9]
                pend_qty = mark_eq * pend_lev / c[i]
                pend_sl = c[i] * (1 - P[r, 2]); pend_tm = tm
            last_order_bar = i
        if pos == 1 and pend_close == 0:
            if l[i] <= sl_price:
                pend_close = 2
            else:
                tgt = P[r, 0]
                if tm >= entry_tm + P[r, 13] * 3600000.0:
                    tgt = P[r, 11]
                elif tm >= entry_tm + P[r, 12] * 3600000.0:
                    tgt = P[r, 10]
                if c[i] >= entry_px * (1 + tgt):
                    pend_close = 1
        if shortCond and can_open and not blocked:
            if pos == -1:
                sl_price = c[i] * (1 + P[r, 3]); entry_tm = tm
            else:
                pend_entry = -1
                pend_lev = P[r, 9]
                pend_qty = mark_eq * pend_lev / c[i]
                pend_sl = c[i] * (1 + P[r, 3]); pend_tm = tm
            last_order_bar = i
        if pos == -1 and pend_close == 0:
            if h[i] >= sl_price:
                pend_close = 2
            else:
                tgt = P[r, 1]
                if tm >= entry_tm + P[r, 19] * 3600000.0:
                    tgt = P[r, 17]
                elif tm >= entry_tm + P[r, 18] * 3600000.0:
                    tgt = P[r, 16]
                if c[i] <= entry_px * (1 - tgt):
                    pend_close = 1

    return out[:nt], eq, liquidated, pos, entry_px, qty, entry_i, cur_lev


def run_macdx_P(pre, P, regime=None, warmup=0, initial_capital=1000.0,
                commission=0.0, no_entry=None, return_open=False):
    """Optimizer-path runner: per-regime P matrix (columns = MACDX_PNAMES)."""
    n = len(pre["c"])
    if regime is None:
        regime = np.zeros(n, dtype=np.int32)
    ne = no_entry if no_entry is not None else np.zeros(n, dtype=np.int8)
    arr, eq, liq, pos, epx, qty, ei, clev = _core_macdx(
        pre["t_ms"], pre["o"], pre["h"], pre["l"], pre["c"],
        pre["macd"], pre["sig"], pre["hist"], pre["dropL"], pre["incS"],
        np.asarray(regime, dtype=np.int32), np.asarray(P, dtype=np.float64),
        int(warmup), float(initial_capital), float(commission),
        np.asarray(ne, dtype=np.int8))
    t = pre["t"]
    tr = pd.DataFrame(arr, columns=["entry_idx", "exit_idx", "dir", "entry",
                                    "exit", "qty", "net", "mae", "reason", "lev"])
    if len(tr):
        tr["entry_t"] = [str(t[int(k)])[:16] for k in tr["entry_idx"]]
        tr["exit_t"] = [str(t[int(k)])[:16] for k in tr["exit_idx"]]
    else:
        tr["entry_t"] = []; tr["exit_t"] = []
    open_pos = None
    if pos != 0 and not liq:
        open_pos = dict(dir=int(pos), entry=float(epx), qty=float(qty),
                        lev=float(clev), entry_t=str(t[int(ei)])[:16],
                        entry_idx=int(ei))
    if return_open:
        return tr, float(eq), bool(liq), open_pos
    return tr, float(eq), bool(liq)


def run_macdx(pre, p, warmup=0, initial_capital=1000.0, commission=0.0,
              liq_threshold=None, no_entry=None, return_open=False):
    """Original-strategy runner: pine-named single parameter set."""
    return run_macdx_P(pre, macdx_P_from_dict(p), regime=None, warmup=warmup,
                       initial_capital=initial_capital, commission=commission,
                       no_entry=no_entry, return_open=return_open)
