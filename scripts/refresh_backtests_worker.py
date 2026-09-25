#!/usr/bin/env python3
"""Worker: re-run stale published backtests and submit refreshed entries to
the hub panel (which ingests them into backtests.js in batches).

  parent:  refresh_backtests_worker.py --shard shard.json --procs N \
               [--hub http://admns-Mac-mini.local:8800]
  child:   refresh_backtests_worker.py --one item.json --hub URL
           (spawned by the parent with LAB_* env set — engine variants
            freeze at import, so every sim gets its own process)
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
OPT = os.path.join(REPO, "optimizer")


def _hub_headers():
    # off-box workers (MacBook offload) must authenticate: the panel
    # requires X-Panel-Key for non-local requests
    h = {"Content-Type": "application/json"}
    k = os.environ.get("PANEL_KEY")
    if k:
        h["X-Panel-Key"] = k
    return h


def submit(hub, entries):
    req = urllib.request.Request(
        hub + "/api/backtests/submit",
        data=json.dumps(entries, default=float).encode(),
        headers=_hub_headers(), method="POST")
    # generous timeout + retries: losing a finished multi-minute sim to a
    # transient submit hiccup wastes the whole re-run (bit us 2026-08-31
    # when submits timed out behind the old synchronous fold)
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(10 * (i + 1))
    raise last


def run_one(path, hub):
    it = json.load(open(path))
    sys.path.insert(0, OPT)
    import backtest_cli as BT
    cfgp = path + ".cfg"
    # normalize genome shape: entry-embedded configs may store the candidate
    # bare (no "cand" wrapper) and often lack pair/timeframe/market_data —
    # run_single needs all of them to pin the right dataset
    g = dict(it["genome"])
    if "cand" not in g:
        g = dict(cand=(g.get("candidate") or g))
    g.setdefault("pair", it["pair"])
    g.setdefault("timeframe", it["timeframe"])
    g.setdefault("mode", it["mode"])
    g.setdefault("market_data", "spot" if it["mode"] == "spot" else "perp")
    g.setdefault("method", it.get("method") or "vol3")
    if not g.get("strategy"):
        g["strategy"] = ((g.get("cand") or {}).get("strategy")
                         or it.get("strategy"))
    json.dump(g, open(cfgp, "w"))
    # The fee the shard was built against wins over whatever fees.json this
    # machine happens to have. An off-box worker's copy drifts: the MacBook's
    # was three weeks stale and still held MEXC's advertised WEB rates, so a
    # re-cost run there would have simulated every strategy at 0.0000%/side —
    # worse than the error we were fixing (2026-09-17). An explicit --fee
    # still wins over both.
    _fee = it.get("fee_now")
    try:
        if _fee is not None and 0 <= float(_fee) < 0.01:
            os.environ.setdefault("LAB_FEE_OVERRIDE", repr(float(_fee)))
    except (TypeError, ValueError):
        pass
    # holdout-only items (panel /api/backtests/holdout_selected): simulate
    # the genome on its out-of-sample window only, either from a date or on
    # alternating day blocks — run_single stamps the OOS `kind` itself
    _oos = it.get("oos_start") or None
    _hd = it.get("holdout_days") or None
    e = BT.run_single(cfgp, _oos, holdout_days=_hd) if (_oos or _hd) \
        else BT.run_single(cfgp)

    # ---- second pass at the OTHER side of the book ----------------------
    # Every run_single_* does `from wf2 import ... FUT_COMM, SPOT_COMM`
    # INSIDE the function, so the module attribute is re-read on each call
    # and setting it here is enough — no second process, no numba recompile.
    # Taker/taker is the primary (conservative, and what a market order
    # pays); maker/maker rides along as the optimistic bracket.
    alt = None
    _alt_rate = it.get("fee_alt_rate")
    if _alt_rate is not None:
        try:
            import wf2
            _keep = (wf2.FUT_COMM, wf2.SPOT_COMM)
            if it["mode"] == "spot":
                wf2.SPOT_COMM = float(_alt_rate)
            else:
                wf2.FUT_COMM = float(_alt_rate)
            os.environ["LAB_FEE_OVERRIDE"] = repr(float(_alt_rate))
            e2 = BT.run_single(cfgp)
            s2 = e2.get("stats") or {}
            alt = dict(side=it.get("fee_alt_side") or "maker",
                       per_side=float(_alt_rate),
                       monthly_growth_pct=s2.get("monthly_growth_pct"),
                       total_mult=s2.get("total_mult"), n=s2.get("n"),
                       maxdd_mtm=s2.get("maxdd_mtm"), win=s2.get("win"),
                       liq=s2.get("liq"))
            wf2.FUT_COMM, wf2.SPOT_COMM = _keep
        except Exception as _ex:
            print(f"  alt-fee pass failed for {it['name']}: {_ex}", flush=True)
    # gap metadata: the publish path attaches this, run_single's return may
    # not — without it the dashboard's gaps column shows "unknown" even
    # though segmentation/contamination-skipping WAS active (it always is;
    # gap_info just describes the loaded dataset's segments)
    try:
        gh = e.get("gap_handling") or BT.gap_info()
    except Exception:
        gh = None
    entry = dict(
        name=it["name"], pair=it["pair"], timeframe=it["timeframe"],
        mode=it["mode"], method=it.get("method") or e.get("method"),
        kind=it.get("kind") or e.get("kind"), opt=it.get("opt"),
        source_entry=it.get("source_entry"),
        strategy=e.get("strategy"), config=e.get("config"),
        stats=e.get("stats"), monthly=e.get("monthly"),
        # carry the fee basis through, or the re-costed entry lands back on
        # the dashboard indistinguishable from the stale one it replaced
        fee_per_side=e.get("fee_per_side"), fee_side=e.get("fee_side"),
        # headline numbers only — the full curve/trades would double a
        # 165MB store for a bracket you only ever read as one figure
        fee_alt=alt,
        curve=(e.get("curve") or [])[-400:],
        trades=(e.get("trades") or [])[-400:],
        open_positions=e.get("open_positions") or [],
        gap_mode=e.get("gap_mode") or "skip_contaminated",
        gap_handling=gh,
        suppressed_bars=e.get("suppressed_bars"),
        max_hold_days=(e.get("stats") or {}).get("max_hold_days"),
        created=time.strftime("%Y-%m-%d %H:%M"))
    submit(hub, [entry])
    print(f"done {it['name']}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard")
    ap.add_argument("--one")
    ap.add_argument("--procs", type=int, default=6)
    ap.add_argument("--hub", default="http://admns-Mac-mini.local:8800")
    ap.add_argument("--fee", default=None,
                    help="override commission (fraction PER SIDE, e.g. "
                         "0.0008) — what-if re-runs; blank = live fees.json")
    a = ap.parse_args()
    if a.fee:      # children read it via fees_live.per_side
        os.environ["LAB_FEE_OVERRIDE"] = str(float(a.fee))
    if a.one:
        run_one(a.one, a.hub)
        return

    items = json.load(open(a.shard))
    done_f = a.shard + ".done"
    done = set()
    if os.path.exists(done_f):
        done = set(open(done_f).read().split())
    todo = [it for it in items if it["name"] not in done]
    print(f"{len(todo)} of {len(items)} entries to do, {a.procs} procs",
          flush=True)
    lock = threading.Lock()
    idx = [0]
    tmpd = a.shard + ".tmp"
    os.makedirs(tmpd, exist_ok=True)

    def work(tid):
        while True:
            with lock:
                if idx[0] >= len(todo):
                    return
                it = todo[idx[0]]
                idx[0] += 1
            tf = (it["timeframe"] or "3").rstrip("m") or "3"
            coin = (it["pair"] or "SOL_USDT").split("_")[0].lower()
            # PER-THREAD numba cache dir: parallel children sharing the
            # default cache corrupt it when two compile the same jitted
            # function at once — the next child to LOAD it segfaults in
            # numba's Dispatcher_call (macOS crash popup, no traceback).
            # Same disease and same fix as the fcfs hosts (2026-08-23);
            # bit the router re-run batches on 2026-08-31 at --procs 4.
            nb_cache = os.path.join(tmpd, f"nb{tid}")
            os.makedirs(nb_cache, exist_ok=True)
            env = {**os.environ, "LAB_TF": tf, "LAB_COIN": coin,
                   "LAB_MARKET": ("spot" if it["mode"] == "spot" else "lev"),
                   "NUMBA_CACHE_DIR": nb_cache}
            ip = os.path.join(tmpd, f"{tid}_{idx[0]}.json")
            json.dump(it, open(ip, "w"))
            r = None
            for attempt in (1, 2):      # one retry: JIT-cache/segfault-class
                r = subprocess.run([sys.executable, os.path.abspath(__file__),
                                    "--one", ip, "--hub", a.hub],
                                   env=env, capture_output=True, text=True,
                                   timeout=1800)
                if r.returncode == 0:
                    break
                print(f"attempt {attempt} failed for {it['name']} "
                      f"(rc {r.returncode}) — "
                      f"{'retrying' if attempt == 1 else 'giving up'}",
                      flush=True)
                # a crashed child can leave a HALF-WRITTEN jit cache that
                # segfaults every later load from this thread's dir (the
                # macOS "Python quit unexpectedly" popups cascade) — wipe
                # it so the retry AND all later items compile clean
                import shutil
                shutil.rmtree(nb_cache, ignore_errors=True)
                os.makedirs(nb_cache, exist_ok=True)
            with lock:
                if r.returncode == 0:
                    with open(done_f, "a") as f:
                        f.write(it["name"] + "\n")
                else:
                    print(f"FAIL {it['name']}: "
                          f"{(r.stderr or '')[-200:]}", flush=True)
            for p in (ip, ip + ".cfg"):
                try:
                    os.remove(p)
                except OSError:
                    pass

    threads = [threading.Thread(target=work, args=(t,), daemon=True)
               for t in range(a.procs)]
    for t in threads:
        t.start()
    import socket
    me = socket.gethostname().split(".")[0]

    def beat(status="running"):
        n = len(open(done_f).read().split()) if os.path.exists(done_f) else 0
        print(f"progress: {n}/{len(items)}", flush=True)
        try:
            req = urllib.request.Request(
                a.hub + "/api/bt_refresh/heartbeat",
                data=json.dumps(dict(worker=me, done=n, total=len(items),
                                     status=status)).encode(),
                headers=_hub_headers(), method="POST")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass

    while any(t.is_alive() for t in threads):
        time.sleep(30)
        beat()
    beat("finished")
    print("worker finished", flush=True)


if __name__ == "__main__":
    main()
