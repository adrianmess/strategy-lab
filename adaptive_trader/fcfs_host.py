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
    # klines go DIRECT (public, unkeyed — Adrian's policy; proxies are only
    # for private API calls). All hosts share one IP, so pace by timeframe:
    # MEXC 510-rate-limits a single IP aggressively.
    poll = max(float(spec.get("poll_seconds", 3)),
               5.0 if tf_min == 1 else 12.0)
    import random
    time.sleep(random.uniform(0, min(poll, 4)))   # desynchronize the fleet
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

    # ---- WebSocket wake-up (latency option 2): the exchange pushes a kline
    # update the moment a NEW candle starts — which means the previous one
    # just closed and is queryable. The socket is a TRIGGER ONLY: on wake we
    # run the exact same REST update as always, so the engine's data stays
    # byte-identical to the polling path and a dead socket silently degrades
    # to bar-aligned polling. The CONTRACT stream is used for both modes
    # (spot's v3 stream is protobuf; candle boundaries are the same clock,
    # and REST remains the source of truth either way).
    ws_wake = threading.Event()

    def ws_thread():
        try:
            import websocket
        except Exception:
            emit(dict(e="log", msg="websocket-client missing — WS trigger "
                                   "off, bar-aligned polling only"))
            return
        last_start = 0
        while True:
            try:
                ws = websocket.create_connection(
                    "wss://contract.mexc.com/edge", timeout=15)
                ws.send(json.dumps(dict(method="sub.kline",
                                        param=dict(symbol=symbol,
                                                   interval=f"Min{tf_min}"))))
                ws.settimeout(15)
                while True:
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.send(json.dumps(dict(method="ping")))
                        continue
                    if not msg:
                        break
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    dd = d.get("data") if isinstance(d, dict) else None
                    t0 = int((dd or {}).get("t") or 0)   # bar START (s)
                    if t0 > last_start:
                        if last_start:       # new candle => previous closed
                            ws_wake.set()
                        last_start = t0
            except Exception:
                pass
            import random as _rnd
            time.sleep(5 + _rnd.uniform(0, 10))          # reconnect backoff
    threading.Thread(target=ws_thread, daemon=True).start()

    # ---- pacing (latency option 1): a new CLOSED bar can only appear just
    # after a boundary, so poll densely there (jittered per host so the
    # fleet doesn't burst one IP into MEXC's 510 limit) and nap mid-bar,
    # waking every `poll` seconds anyway so the px heartbeat that feeds the
    # runner's protective checks keeps its old freshness.
    import math
    import random as _rnd2
    tf_s = tf_min * 60
    jit = _rnd2.uniform(0.0, 1.2)
    LADDER = [0.6 + jit, 1.6 + jit, 3.0 + jit, 5.5 + jit, 9.0 + jit]

    def paced_wait(last_lbl):
        now = time.time()
        B = math.floor(now / tf_s) * tf_s     # most recent boundary
        have = False
        if last_lbl is not None:
            try:
                have = (last_lbl.value / 1e9) >= B - tf_s
            except Exception:
                have = False
        if have:                              # up to date: nap to boundary,
            target = min(B + tf_s + LADDER[0],   # px heartbeat mid-bar
                         now + poll)
        else:                                 # closed bar due: retry ladder
            for off in LADDER:
                if now - B < off:
                    target = B + off
                    break
            else:
                target = now + poll           # exchange late — old cadence
        w = max(0.1, target - now)
        if ws_wake.wait(timeout=w):           # WS says "bar rolled" — go now
            ws_wake.clear()

    # error-noise control: a network outage makes EVERY poll fail, which
    # used to emit one line per host per poll (~60/min for the fleet) and
    # buried everything else. Log the first failure of a kind, then only
    # milestones, then an explicit RECOVERED line with the outage duration.
    err_kind = None
    err_n = 0
    err_t0 = 0.0

    def classify(ex):
        s = str(ex).lower()
        if "nameresolution" in s or "not known" in s or "resolve" in s:
            return "DNS failure (this Mac can't resolve contract.mexc.com)"
        if "'code': 510" in str(ex) or "frequent" in s:
            return "MEXC rate limit"
        if "timed out" in s or "timeout" in s:
            return "network timeout"
        if "proxy" in s or "tunnel" in s:
            return "proxy failure"
        return "network error"

    while True:
        try:
            feed.trim_ok = flat_hint
            feed.update()
            if err_kind:
                emit(dict(e="log", msg=f"RECOVERED from {err_kind} after "
                                       f"{err_n} failed polls "
                                       f"({time.time()-err_t0:.0f}s)"))
                err_kind, err_n = None, 0
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
                        ei = int(op.get("entry_idx", -1)) if op is not None else -1
                        # WHY the most recent virtual trade ended. The parent
                        # must distinguish a real exit (target/stop) from a
                        # LIQUIDATION of the simulated account — the latter is
                        # not a market signal and must not close a real
                        # position that is nowhere near its own liquidation.
                        last_exit = last_reason = None
                        try:
                            if tr is not None and len(tr):
                                _r = tr.iloc[-1]
                                last_exit = _ts16(_r["exit_t"])
                                last_reason = {0.0: "profit_target",
                                               1.0: "stop_loss",
                                               2.0: "liquidation"}.get(
                                    float(_r["reason"]), str(_r["reason"]))
                        except Exception:
                            pass
                        out.append(dict(
                            i=c["i"],
                            opens_now=bool(opens_now),
                            dir=int(op.get("dir", 1)) if op is not None else 0,
                            lev=float(op.get("lev", 1.0)) if op is not None else 1.0,
                            open=_ts16(op["entry_t"]) if op is not None else None,
                            # virtual entry price: lets the parent late-join a
                            # position opened while it was down — red only
                            entry_px=(float(x3["close"].iloc[ei])
                                      if 0 <= ei < len(x3) else None),
                            # engine's own projected close (TP/SL levels)
                            exit_proj=(op.get("exit_proj")
                                       if op is not None else None),
                            last_exit=last_exit, last_reason=last_reason))
                    emit(dict(e="bar", t=_ts16(newest),
                              px=float(closed["close"].iloc[-1]), comps=out))
            paced_wait(last_closed)
        except Exception as ex:
            import random
            kind = classify(ex)
            if kind == err_kind:
                err_n += 1
                if err_n in (5, 20) or err_n % 60 == 0:   # milestones only
                    emit(dict(e="log",
                              msg=f"still failing: {kind} "
                                  f"({err_n} polls, "
                                  f"{time.time()-err_t0:.0f}s)"))
            else:
                err_kind, err_n, err_t0 = kind, 1, time.time()
                emit(dict(e="log", msg=f"{kind}: {str(ex)[:160]}"))
            # back off harder on things that hammering cannot fix
            if kind.startswith("DNS"):
                time.sleep(15 + random.uniform(0, 10))
            elif kind == "MEXC rate limit":
                time.sleep(20 + random.uniform(0, 15))
            else:
                time.sleep(10)


if __name__ == "__main__":
    main()
