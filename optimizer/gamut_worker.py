#!/usr/bin/env python3
"""GAMUT WORKER — executes pending specs from an existing gamut plan.json,
several CONCURRENTLY (--jobs), for offload boxes (EC2 / second machine).

Differences from gamut.py execute():
- plan.json is treated as READ-ONLY (safe to run against a synced copy while
  another machine also works the plan); progress is written to
  worker_state.json next to the plan instead.
- skip-if-done is checked at pick-up time against runs/<name>/best_config.json,
  so any run completed elsewhere and synced in is never repeated.
- --jobs N runs N specs at once (each spec still uses its own --procs from the
  plan cmd; size the box at jobs*procs <= vCPUs).

Usage:
  python3 gamut_worker.py --plan campaigns/gamut_X/plan.json --jobs 13
Stop: touch <plan dir>/STOP_WORKER  (graceful; running specs finish)
"""
import _bootstrap as B
import argparse, json, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")

_lock = threading.Lock()
_space_locks = {}


def _atomic_dump(obj, path):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def done_already(name):
    return os.path.exists(os.path.join(RUNS, name, "best_config.json"))


def ensure_ai_space(spec):
    """Generate the AI space once per space name (thread-safe)."""
    sp_name, sp_path = spec["ai_space"]
    with _lock:
        lk = _space_locks.setdefault(sp_name, threading.Lock())
    with lk:
        if not os.path.exists(sp_path):
            print(f"generating AI space {sp_name}…", flush=True)
            subprocess.run([sys.executable, "gen_ai_spaces.py",
                            "--space", sp_name], cwd=HERE)
    return sp_path if os.path.exists(sp_path) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--jobs", type=int, default=13)
    ap.add_argument("--reverse", action="store_true",
                    help="work from the END of the plan (for meet-in-middle "
                         "splits with a forward-running primary)")
    a = ap.parse_args()
    plan_p = os.path.abspath(a.plan)
    pdir = os.path.dirname(plan_p)
    state_p = os.path.join(pdir, "worker_state.json")
    stop_p = os.path.join(pdir, "STOP_WORKER")
    logs = os.path.join(pdir, "logs")
    os.makedirs(logs, exist_ok=True)
    if os.path.exists(stop_p):
        os.remove(stop_p)

    plan = json.load(open(plan_p))
    specs = [s for s in plan["specs"] if s["status"] != "done"]
    if a.reverse:
        specs = specs[::-1]
    state = {}
    if os.path.exists(state_p):
        try:
            state = json.load(open(state_p))
        except Exception:
            state = {}
    n_total = len(specs)
    print(f"worker: {n_total} candidate specs, jobs={a.jobs}", flush=True)

    it = iter(specs)
    counters = dict(done=0, failed=0, skipped=0)

    def note(name, status):
        with _lock:
            state[name] = dict(status=status, at=time.strftime("%F %T"))
            if status in counters:
                counters[status] += 1
            _atomic_dump(state, state_p)

    def runner(tid):
        while True:
            if os.path.exists(stop_p):
                return
            with _lock:
                try:
                    s = next(it)
                except StopIteration:
                    return
            name = s["name"]
            if done_already(name) or \
                    state.get(name, {}).get("status") == "done":
                note(name, "skipped")
                continue
            cmd = list(s["cmd"])
            if s.get("ai_space"):
                sp = ensure_ai_space(s)
                if sp:
                    cmd += ["--space", sp]
            print(f"[{time.strftime('%H:%M:%S')}] T{tid} start {name}",
                  flush=True)
            note(name, "running")
            with open(os.path.join(logs, name + ".log"), "w") as lf:
                rc = subprocess.run(cmd, cwd=HERE, stdout=lf,
                                    stderr=subprocess.STDOUT).returncode
            note(name, "done" if rc == 0 else "failed")
            print(f"[{time.strftime('%H:%M:%S')}] T{tid} {name} -> "
                  f"{'done' if rc == 0 else 'FAILED'} "
                  f"({counters['done']}d/{counters['failed']}f)", flush=True)

    threads = [threading.Thread(target=runner, args=(k,), daemon=True)
               for k in range(a.jobs)]
    for t in threads:
        t.start()
        time.sleep(20)   # stagger: avoids 13 simultaneous cold cache builds
    for t in threads:
        t.join()
    print(f"worker finished: {counters}", flush=True)


if __name__ == "__main__":
    main()
