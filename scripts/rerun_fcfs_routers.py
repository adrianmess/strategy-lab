#!/usr/bin/env python3
"""Re-run FCFS routers against the CURRENT market data.

Each router re-collects every component (engine-exact, one backtest per
component) and republishes its <name>_fcfs_full / _fcfs_wf entries — now
including the fields that were missing before: the components' still-OPEN
virtual positions and max_hold_days.

Each fcfsx_cli run is roughly one core, so concurrency = routers in flight.

  python3 rerun_fcfs_routers.py --routers A,B,C --concurrency 3
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
OPT = os.path.join(REPO, "optimizer")


def components_of(run):
    b = json.load(open(os.path.join(OPT, "runs", run, "best_config.json")))
    comps = [c.get("run") for c in ((b.get("cand") or {}).get("components") or [])]
    return [c for c in comps if c]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--routers", required=True)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--log-dir", default="/tmp/rerun_routers")
    a = ap.parse_args()

    routers = [r.strip() for r in a.routers.split(",") if r.strip()]
    os.makedirs(a.log_dir, exist_ok=True)
    lock = threading.Lock()
    idx = [0]
    results = {}

    def work():
        while True:
            with lock:
                if idx[0] >= len(routers):
                    return
                r = routers[idx[0]]
                idx[0] += 1
            comps = components_of(r)
            if len(comps) < 2:
                with lock:
                    results[r] = "skipped (no component list)"
                continue
            log = os.path.join(a.log_dir, r + ".log")
            t0 = time.time()
            print(f"[{time.strftime('%H:%M:%S')}] start {r} "
                  f"({len(comps)} components)", flush=True)
            with open(log, "w") as lf:
                rc = subprocess.run(
                    [sys.executable, "fcfsx_cli.py", "--name", r,
                     "--runs", ",".join(comps)],
                    cwd=OPT, stdout=lf, stderr=subprocess.STDOUT).returncode
            mins = (time.time() - t0) / 60
            verdict = ""
            try:
                for ln in open(log):
                    if "VERDICT" in ln:
                        verdict = ln.strip()[:110]
            except Exception:
                pass
            with lock:
                results[r] = (f"rc={rc} in {mins:.1f}m | {verdict}"
                              if rc == 0 else f"FAILED rc={rc} (see {log})")
            print(f"[{time.strftime('%H:%M:%S')}] done  {r} "
                  f"({mins:.1f}m, rc={rc})", flush=True)

    ths = [threading.Thread(target=work, daemon=True)
           for _ in range(max(1, a.concurrency))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    print("\n=== summary ===", flush=True)
    for r in routers:
        print(f"  {r[:46]:46s} {results.get(r, 'not run')}", flush=True)


if __name__ == "__main__":
    main()
