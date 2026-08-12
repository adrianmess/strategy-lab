#!/usr/bin/env python3
"""FCFS live adapter — per-(pair, timeframe) COMPONENT HOST (child process).

Reads one JSON config object on stdin:
  {"symbol": "BTC_USDT", "tf_min": 3, "mode": "lev", "poll_seconds": 3,
   "components": [{"i": 0, "strategy": "v7", "method": "vol3",
                   "cand": {...}, "run": "..."}, ...]}

Maintains its own anchored feed and, on every closed bar, runs EVERY
component's research engine over the synthetic-extended window (the exact
StrategyMetax technique — engine-exact, no re-ported logic), then emits one
JSON line per event on stdout:

  {"e":"ready","symbol":...,"bars":N}
  {"e":"bar","t":"YYYY-mm-dd HH:MM","px":..., "comps":[
      {"i":0,"opens_now":true,"dir":1,"lev":20.0,"open":"YYYY-mm-dd HH:MM"},
      {"i":1,"opens_now":false,"open":null}, ...]}
  {"e":"px","px":...}           (heartbeat every poll — protective checks)
  {"e":"log","msg":"..."}

The parent (fcfs_runner.py) owns the slot, arbitration and execution. LAB_TF
must be set in the environment BEFORE launch (one host per timeframe).
"""
import json
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

logging.basicConfig(level=logging.WARNING)   # engines log via stderr only


def emit(obj):
    sys.stdout.write(json.dumps(obj, default=float) + "\n")
    sys.stdout.flush()


def main():
    spec = json.loads(sys.stdin.readline())
    symbol = spec["symbol"]
    tf_min = int(spec["tf_min"])
    mode = spec["mode"]
    comps = spec["components"]
    # pace kline polling by timeframe: many hosts share one IP and MEXC
    # rate-limits aggressively (code 510). 1m bars need ~5s latency; slower
    # charts can poll far less often. The WebSocket tick price is unaffected.
    poll = max(float(spec.get("poll_seconds", 3)),
               5.0 if tf_min == 1 else 12.0)
    import random
    time.sleep(random.uniform(0, poll))     # desynchronize the fleet
    assert os.environ.get("LAB_TF") == str(tf_min), \
        f"LAB_TF={os.environ.get('LAB_TF')} != tf {tf_min}"

    # imports AFTER LAB_TF assertion — engines read it at import time
    from data_feed import Feed
    from strategy_metax import (_extend, _ts16, compute_features,
                                run_component_engine, WARMUP)

    feed = Feed(symbol, anchored=True, tf_min=tf_min)
    for attempt in range(12):
        try:
            feed.backfill()
            break
        except Exception as ex:   # rate limits with many hosts backfilling
            wait = min(120, 15 * (attempt + 1))
            emit(dict(e="log", msg=f"backfill failed ({ex}) — retry in {wait}s"))
            time.sleep(wait)
    else:
        emit(dict(e="log", msg="backfill failed 12x — giving up"))
        sys.exit(1)
    emit(dict(e="ready", symbol=symbol, tf=tf_min, bars=len(feed.df3)))
    last_closed = None
    flat_hint = True     # parent tells us whether it's safe to re-anchor

    import threading
    def read_stdin():    # parent -> child control lines {"flat": bool}
        nonlocal flat_hint
        for line in sys.stdin:
            try:
                flat_hint = bool(json.loads(line).get("flat", True))
            except Exception:
                pass
    threading.Thread(target=read_stdin, daemon=True).start()

    while True:
        try:
            feed.trim_ok = flat_hint
            feed.update()
            emit(dict(e="px", px=feed.last_price()))
            closed = feed.closed_bars()
            newest = closed["t"].iloc[-1] if len(closed) else None
            if newest is not None and newest != last_closed:
                last_closed = newest
                if len(closed) < WARMUP + 300:
                    emit(dict(e="log",
                              msg=f"window too short ({len(closed)} bars)"))
                else:
                    lo = closed["t"].iloc[0]
                    d1 = feed.df1[feed.df1["t"] >= lo].reset_index(drop=True)
                    x3, x1 = _extend(closed, d1)
                    feats = compute_features(x3)
                    syn_i = len(x3) - 1
                    out = []
                    for c in comps:
                        tr, op = run_component_engine(c, mode, x3, x1, feats)
                        opens_now = (op is not None
                                     and int(op.get("entry_idx", -1)) == syn_i)
                        out.append(dict(
                            i=c["i"],
                            opens_now=bool(opens_now),
                            dir=int(op.get("dir", 1)) if op is not None else 0,
                            lev=float(op.get("lev", 1.0)) if op is not None else 1.0,
                            open=_ts16(op["entry_t"]) if op is not None else None))
                    emit(dict(e="bar", t=_ts16(newest),
                              px=float(closed["close"].iloc[-1]), comps=out))
            time.sleep(poll)
        except Exception as ex:
            msg = str(ex)
            if "510" in msg or "frequent" in msg.lower():
                import random
                w = 20 + random.uniform(0, 15)
                emit(dict(e="log", msg=f"rate-limited — backing off {w:.0f}s"))
                time.sleep(w)
            else:
                emit(dict(e="log", msg=f"host error: {ex}"))
                time.sleep(10)


if __name__ == "__main__":
    main()
