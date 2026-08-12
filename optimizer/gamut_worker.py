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
    d = os.path.join(RUNS, name)
    return (os.path.exists(os.path.join(d, "best_config.json"))
            or os.path.exists(os.path.join(d, "no_survivor.json")))


def ensure_ai_space(spec):
    """Generate the AI space once per space name (thread-safe).
    NOTE: the plan stores the path from the machine that BUILT it — recompute
    locally so offload boxes resolve their own copy."""
    sp_name = spec["ai_space"][0]
    sp_path = os.path.join(HERE, "param_spaces", "variants",
                           f"{sp_name}.ai.json")
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
    ap.add_argument("--procs-cap", type=int, default=0,
                    help="rewrite each spec's --procs to at most N "
                         "(per-machine CPU limit; 0 = use the plan's value)")
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
    # startup sweep: nothing is running yet, so any 'running' entries are
    # strandings from a previous incarnation (spot interruption) — mark them
    # so the dashboard can show them honestly
    swept = 0
    for k, v in state.items():
        if v.get("status") == "running":
            v["status"] = "interrupted"
            swept += 1
    if swept:
        _atomic_dump(state, state_p)
        print(f"startup: marked {swept} stranded 'running' entries as "
              f"interrupted (will retry)", flush=True)
    n_total = len(specs)
    print(f"worker: {n_total} candidate specs, jobs={a.jobs}", flush=True)

    it = iter(specs)
    counters = dict(done=0, failed=0, skipped=0)

    def note(name, status, tries=None):
        with _lock:
            e = dict(status=status, at=time.strftime("%F %T"))
            if tries and tries > 1:
                e["try"] = tries
            state[name] = e
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
                    state.get(name, {}).get("status") in ("done", "skipped"):
                # record the skip ONCE and never overwrite a real 'done'
                # entry — restart skip-floods used to re-stamp thousands of
                # entries and poison the completion-rate/ETA math
                if name not in state:
                    note(name, "skipped")
                continue
            # cmd[0] is the python of the machine that BUILT the plan — use ours
            cmd = [sys.executable] + list(s["cmd"])[1:]
            if a.procs_cap:
                for _pi, _tok in enumerate(cmd):
                    if _tok == "--procs" and _pi + 1 < len(cmd):
                        cmd[_pi + 1] = str(min(int(cmd[_pi + 1]), a.procs_cap))
            if s.get("ai_space"):
                sp = ensure_ai_space(s)
                if sp:
                    cmd += ["--space", sp]
            prev = state.get(name, {})
            tries = (prev.get("try", 1) + 1
                     if prev.get("status") in ("interrupted", "failed") else 1)
            print(f"[{time.strftime('%H:%M:%S')}] T{tid} start {name}"
                  + (f" (retry #{tries})" if tries > 1 else ""), flush=True)
            note(name, "running", tries)
            try:
                with open(os.path.join(logs, name + ".log"), "w") as lf:
                    rc = subprocess.run(cmd, cwd=HERE, stdout=lf,
                                        stderr=subprocess.STDOUT).returncode
            except Exception as e:
                print(f"T{tid} spawn error {name}: {e}", flush=True)
                rc = 1
            if rc == 0 and not os.path.exists(
                    os.path.join(RUNS, name, "best_config.json")):
                # durable no-survivor marker: without it, this outcome's only
                # record is the state file, which AMI-fresh replacement boxes
                # clobber — forcing pointless re-runs of feasibility deserts
                try:
                    os.makedirs(os.path.join(RUNS, name), exist_ok=True)
                    json.dump(dict(at=time.strftime("%F %T"),
                                   note="search completed; no feasible config"),
                              open(os.path.join(RUNS, name,
                                                "no_survivor.json"), "w"))
                except Exception:
                    pass
            note(name, "done" if rc == 0 else "failed", tries)
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
