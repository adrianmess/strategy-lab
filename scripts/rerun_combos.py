#!/usr/bin/env python3
"""Re-cost every published FCFS combo at the current fee schedule.

Combos cannot go through the backtest shard path: they have no single
genome, and their numbers come from re-simulating the components together.
Each one therefore goes through refresh_combo.py, which re-backtests every
component (its _full and _oosbest_full entries) and then re-runs the merge
and the causal walk-forward.

Run ON THE MINI (it owns optimizer/runs and backtests.js), from the
optimizer directory or anywhere:

  ~/venv/bin/python3 scripts/rerun_combos.py --jobs 4
  ~/venv/bin/python3 scripts/rerun_combos.py --list
  ~/venv/bin/python3 scripts/rerun_combos.py --only Lev-insane,fcfs_0808

--jobs is the parallelism INSIDE one combo's component backtests; combos
themselves run one at a time so the box keeps serving the panel and the
live traders. Progress is written to <repo>/optimizer/rerun_combos.done so
an interrupted pass resumes.
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), ".."))
OPT = os.path.join(REPO, "optimizer")
RUNS = os.path.join(OPT, "runs")
DONE = os.path.join(OPT, "rerun_combos.done")


def combos():
    """(name, n_components) for every fcfsx run whose components all exist."""
    out = []
    for d in sorted(os.listdir(RUNS)):
        p = os.path.join(RUNS, d, "best_config.json")
        if not os.path.exists(p):
            continue
        try:
            b = json.load(open(p))
        except Exception:
            continue
        cand = b.get("cand") or {}
        if cand.get("strategy") != "fcfsx":
            continue
        comps = [c.get("run") for c in (cand.get("components") or [])
                 if c.get("run")]
        if len(comps) < 2:
            continue
        missing = [c for c in comps
                   if not os.path.exists(os.path.join(RUNS, c,
                                                      "best_config.json"))]
        out.append((d, len(comps), missing,
                    b.get("fee_mode"), b.get("fee_pct_per_side")))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=4,
                    help="parallel component backtests within one combo "
                         "(keep modest: the mini also serves the panel and "
                         "the live traders)")
    ap.add_argument("--only", default="",
                    help="comma-separated combo names (substring match)")
    ap.add_argument("--fee-mode", choices=["market", "limit"], default=None,
                    help="force a fee basis; default reuses whatever each "
                         "combo was built with")
    ap.add_argument("--list", action="store_true", help="show them and exit")
    ap.add_argument("--redo", action="store_true",
                    help="ignore the .done file and re-run everything")
    a = ap.parse_args()

    cs = combos()
    if a.only:
        want = [w.strip().lower() for w in a.only.split(",") if w.strip()]
        cs = [c for c in cs if any(w in c[0].lower() for w in want)]
    done = set()
    if os.path.exists(DONE) and not a.redo:
        done = set(open(DONE).read().split("\n"))

    runnable = [c for c in cs if not c[2]]
    skipped = [c for c in cs if c[2]]
    print(f"{len(cs)} fcfsx combos; {len(runnable)} runnable, "
          f"{len(skipped)} missing components, {len(done & {c[0] for c in cs})}"
          f" already done")
    for name, n, miss, fm, fp in cs:
        tag = ("MISSING " + ",".join(miss[:2]) if miss
               else ("done" if name in done else "todo"))
        print(f"  {name[:56]:56s} {n:2d} comps  fee={fm or '-'}"
              f"{'' if fp is None else f'/{fp}%'}  {tag}")
    if a.list:
        return 0

    todo = [c for c in runnable if c[0] not in done]
    if not todo:
        print("nothing to do")
        return 0
    print(f"\n=== re-costing {len(todo)} combos, --jobs {a.jobs} ===",
          flush=True)
    ok = fail = 0
    for k, (name, n, _m, fm, fp) in enumerate(todo, 1):
        cmd = [sys.executable, os.path.join(REPO, "scripts",
                                            "refresh_combo.py"), name,
               "--jobs", str(a.jobs)]
        # Reuse the combo's market/limit CHOICE but not a stored manual rate.
        # Any manual figure was picked against the old fee data — e.g. the
        # maker combos carry fee=0.0%, which was true only while fees.json
        # still reported MEXC's advertised web maker rate. --fee-mode
        # resolves the real per-coin number instead.
        if a.fee_mode:
            cmd += ["--fee-mode", a.fee_mode]
        elif fm:
            cmd += ["--fee-mode", fm]
        t = time.time()
        print(f"\n[{k}/{len(todo)}] {name} ({n} components) "
              f"{time.strftime('%H:%M:%S')}", flush=True)
        rc = subprocess.run(cmd, cwd=OPT).returncode
        dt = time.time() - t
        if rc == 0:
            ok += 1
            with open(DONE, "a") as f:
                f.write(name + "\n")
            print(f"  ok in {dt/60:.1f}m", flush=True)
        else:
            fail += 1
            print(f"  !! FAILED rc={rc} after {dt/60:.1f}m", flush=True)
    print(f"\n=== done: {ok} re-costed, {fail} failed ===")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
