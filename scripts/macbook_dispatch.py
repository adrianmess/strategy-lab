#!/usr/bin/env python3
"""MacBook optimize worker (2026-09-10).

Dormant poller installed via launchd (com.strategylab.macworker): every 15s
it asks the mini's panel for queued remote optimize jobs and otherwise costs
nothing. When Adrian submits a search from the dashboard with
run on = MacBook (or Both), this claims the job, syncs inputs from the mini,
runs optimize2_cli locally, rsyncs the run dir back to the mini, submits any
portable backtest entries, and reports done — the optimizer process starts
with the search and exits with it, exactly the "start from the dashboard,
stop automatically" behavior.

Config: ~/.strategy_lab_worker.json (written by scripts/macbook_worker_setup.sh)
  {hub, panel_key, repo, python, procs, ssh_key, remote}

Pull-based on purpose: the mini never reaches into the laptop, auth is the
panel key we already have, and a sleeping laptop simply doesn't claim work.
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CFG_P = os.path.expanduser("~/.strategy_lab_worker.json")
CFG = json.load(open(CFG_P))
HUB = CFG["hub"].rstrip("/")            # http://admns-Mac-mini.local:8800
REPO = os.path.expanduser(CFG.get("repo", "~/Code/strategy-lab"))
OPT = os.path.join(REPO, "optimizer")
PY = CFG.get("python",
             "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3")
PROCS = int(CFG.get("procs") or max(2, (os.cpu_count() or 8) - 2))
SSH_KEY = os.path.expanduser(CFG.get("ssh_key", "~/.ssh/lab_auto_ed25519"))
REMOTE = CFG.get("remote", "admn@admns-Mac-mini.local")
WORKER = CFG.get("worker", "macbook")
SSH = (f"ssh -i {SSH_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no "
       f"-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR")


def api(path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        HUB + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json",
                 "X-Panel-Key": CFG["panel_key"]})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def report(jid, status, note=None, progress=None):
    try:
        api("/api/remote/update",
            dict(worker=WORKER, host=socket.gethostname(), cores=PROCS,
                 id=jid, status=status, note=note, progress=progress))
    except Exception as e:
        print(f"report failed ({status}): {e}", flush=True)


def rsync(src, dst, timeout=1800):
    subprocess.run(["rsync", "-az", "--timeout=120", "-e", SSH, src, dst],
                   check=True, timeout=timeout)


def sync_inputs(job):
    """Pull whatever this search reads from the mini. Data sync is
    incremental — cheap after the first full copy."""
    r = f"{REMOTE}:strategy-lab"
    rsync(f"{r}/optimizer/param_space.json", f"{OPT}/param_space.json")
    os.makedirs(f"{OPT}/param_spaces", exist_ok=True)
    rsync(f"{r}/optimizer/param_spaces/", f"{OPT}/param_spaces/")
    data = os.path.join(REPO, "adaptive_trader", "research", "data")
    os.makedirs(data, exist_ok=True)
    rsync(f"{r}/adaptive_trader/research/data/", data + "/")
    # resume/merge sources must exist locally too
    rf = (job.get("args") and _argval(job["args"], "--resume-from")) or ""
    for src in [s.strip() for s in rf.split(",") if s.strip()]:
        name = os.path.basename(src.rstrip("/"))
        os.makedirs(f"{OPT}/runs/{name}", exist_ok=True)
        rsync(f"{r}/optimizer/runs/{name}/", f"{OPT}/runs/{name}/")


def _argval(args, flag):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _set_procs(args):
    a = list(args)
    try:
        a[a.index("--procs") + 1] = str(PROCS)
    except (ValueError, IndexError):
        a += ["--procs", str(PROCS)]
    return a


def run_job(job):
    jid, name = job["id"], job["name"]
    print(f"claimed {jid}: {name}", flush=True)
    report(jid, "running", note="syncing inputs from the mini")
    run_dir = os.path.join(OPT, "runs", name)
    os.makedirs(run_dir, exist_ok=True)
    try:
        sync_inputs(job)
    except Exception as e:
        report(jid, "failed", note=f"input sync failed: {e}")
        return
    for k, fn in (("seed_cand", "seed_cand.json"),
                  ("anchor_cand", "anchor_cand.json")):
        if job.get(k):
            json.dump(job[k], open(os.path.join(run_dir, fn), "w"))
    # per-job numba cache: the shared-cache corruption segfaults bit us on
    # both machines (2026-09) — isolate, and wipe on failure
    nbc = os.path.join(run_dir, ".nbcache")
    os.makedirs(nbc, exist_ok=True)
    env = dict(os.environ, NUMBA_CACHE_DIR=nbc,
               PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
    log_p = os.path.join(run_dir, "remote_worker.log")
    cmd = [PY] + _set_procs(job["args"])
    t0 = time.time()
    with open(log_p, "a") as log:
        log.write(f"\n=== {time.strftime('%F %T')} worker={WORKER} "
                  f"procs={PROCS}\n=== {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=OPT, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(45):
            note = ""
            try:
                with open(log_p, "rb") as f:
                    f.seek(max(0, os.path.getsize(log_p) - 4000))
                    lines = [l for l in
                             f.read().decode("utf-8", "replace").splitlines()
                             if l.strip()]
                    note = lines[-1][-160:] if lines else ""
            except Exception:
                pass
            report(jid, "running", note=note,
                   progress=f"{(time.time() - t0) / 60:.0f}m")

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    rc = proc.wait()
    stop.set()
    if rc != 0:
        shutil.rmtree(nbc, ignore_errors=True)   # poisoned JIT cache
        tail = ""
        try:
            tail = open(log_p, "rb").read()[-800:].decode("utf-8", "replace")
        except Exception:
            pass
        report(jid, "failed", note=f"exit {rc}: {tail[-300:]}")
        return
    report(jid, "running", note="search done — syncing results to the mini")
    try:
        shutil.rmtree(nbc, ignore_errors=True)   # don't ship the JIT cache
        rsync(run_dir + "/", f"{REMOTE}:strategy-lab/optimizer/runs/{name}/")
        n_bt = 0
        entries = []
        for p in glob.glob(os.path.join(run_dir, "bts", "*.json")):
            try:
                entries.append(json.load(open(p)))
            except Exception:
                pass
        if entries:
            api("/api/backtests/submit", entries, timeout=300)
            n_bt = len(entries)
        report(jid, "done",
               note=f"{(time.time() - t0) / 60:.0f}m on {WORKER}, "
                    f"results on the mini"
                    + (f", {n_bt} backtest entr{'y' if n_bt == 1 else 'ies'} "
                       f"published" if n_bt else ""))
        print(f"done {jid}: {name}", flush=True)
    except Exception as e:
        report(jid, "failed", note=f"finished but result sync failed: {e} — "
                                   f"run dir is on the MacBook at "
                                   f"optimizer/runs/{name}")


def main():
    print(f"strategy-lab worker '{WORKER}' polling {HUB} "
          f"(procs={PROCS})", flush=True)
    while True:
        try:
            r = api("/api/remote/poll",
                    dict(worker=WORKER, host=socket.gethostname(),
                         cores=PROCS))
            job = r.get("job")
            if job:
                run_job(job)
                continue          # drain the queue before sleeping
        except Exception as e:
            print(f"poll: {e}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    main()
