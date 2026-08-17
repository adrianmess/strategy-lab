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


def submit(hub, entries):
    req = urllib.request.Request(
        hub + "/api/backtests/submit",
        data=json.dumps(entries, default=float).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


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
    e = BT.run_single(cfgp)
    entry = dict(
        name=it["name"], pair=it["pair"], timeframe=it["timeframe"],
        mode=it["mode"], method=it.get("method") or e.get("method"),
        kind=it.get("kind"), opt=it.get("opt"),
        strategy=e.get("strategy"), config=e.get("config"),
        stats=e.get("stats"), monthly=e.get("monthly"),
        curve=(e.get("curve") or [])[-400:],
        trades=(e.get("trades") or [])[-400:],
        open_positions=e.get("open_positions") or [],
        gap_mode=e.get("gap_mode"),
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
    a = ap.parse_args()
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
            env = {**os.environ, "LAB_TF": tf, "LAB_COIN": coin,
                   "LAB_MARKET": ("spot" if it["mode"] == "spot" else "lev")}
            ip = os.path.join(tmpd, f"{tid}_{idx[0]}.json")
            json.dump(it, open(ip, "w"))
            r = subprocess.run([sys.executable, os.path.abspath(__file__),
                                "--one", ip, "--hub", a.hub],
                               env=env, capture_output=True, text=True,
                               timeout=1800)
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
    while any(t.is_alive() for t in threads):
        time.sleep(30)
        print(f"progress: {len(open(done_f).read().split()) if os.path.exists(done_f) else 0}"
              f"/{len(items)}", flush=True)
    print("worker finished", flush=True)


if __name__ == "__main__":
    main()
