#!/usr/bin/env python3
"""Replicate the TOP SOL/3m optimizer runs across all other pairs and
timeframes (3m + 1m), with each run's EXACT original launch settings.

Selection (--plan):
  top 10 optimize runs by honest metric (OOS-best/holdout growth, no liq)
  UNION top 10 published full backtests mapped back to their source runs.
  SOL_USDT + 3m only; routers (metax) and merge/resume runs are skipped
  (annotated) — they aren't reproducible by a single launch.

Each selected run's launch.json command is replayed with ONLY these
overrides: --symbol <coin> --tf <1|3> --name rep_<run>__<coin>_<tf>m.
New runs land on the Optimize page with pair/timeframe pills like any other.

Usage:
  python3 replicate_top.py --plan          # write replicate_plan.json + show
  python3 replicate_top.py --execute       # run the queue (resumable; waits
                                           # for campaign c7 to finish first)
Stop:  touch replicate_STOP   (finishes the current run, then stops)
"""
import argparse, json, os, shlex, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
PLAN = os.path.join(HERE, "replicate_plan.json")
LOGS = os.path.join(HERE, "replicate_logs")
STOP = os.path.join(HERE, "replicate_STOP")
COINS = ["btc", "eth", "doge", "xrp", "sui"]
TFS = [3, 1]
PROCS = int(os.environ.get("REPL_PROCS", "14"))


def honest_growth(b):
    hb = ((b.get("holdout_best") or {}).get("holdout") or {})
    h = b.get("holdout") or {}
    for m in (hb, h):
        if m and not m.get("liq") and (m.get("growth") or 0) > 0:
            return m["growth"]
    return None


def load_best(d):
    p = os.path.join(RUNS, d, "best_config.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def eligible(d, b):
    if not b or not b.get("cand"):
        return False
    if b.get("strategy") == "metax" or (b.get("cand") or {}).get("strategy") == "metax":
        return False
    if (b.get("pair") or "SOL_USDT") != "SOL_USDT":
        return False
    if str(b.get("timeframe") or "3m") != "3m":
        return False
    return True


def fallback_cmd(b):
    """Reconstruct a launch from best_config metadata (pre-provenance runs)."""
    if not b.get("strategy") or not b.get("mode"):
        return None
    cmd = ["python3", "optimize2_cli.py",
           "--strategy", b["strategy"], "--mode", b["mode"],
           "--method", b.get("method", "vol3"),
           "--algo", b.get("algo", "genetic"), "--batch", "100",
           "--total", str(min(int(b.get("evaluated") or 100000), 150000)),
           "--gap-mode", b.get("gap_mode") or "skip_contaminated",
           "--scoring", b.get("scoring") or "classic"]
    if b.get("max_hold_days"):
        cmd += ["--max-hold-days", str(b["max_hold_days"])]
    if b.get("max_dd"):
        cmd += ["--max-dd", str(b["max_dd"])]
    if b.get("holdout_days"):
        cmd += ["--holdout-days", str(b["holdout_days"])]
    elif b.get("train_end"):
        cmd += ["--train-end", str(b["train_end"])]
    if b.get("cand", {}) and b["cand"].get("lev_stops"):
        cmd += ["--lev-stops"]
    return " ".join(cmd)


def launch_cmd(d, b=None):
    p = os.path.join(RUNS, d, "launch.json")
    if not os.path.exists(p):
        fc = fallback_cmd(b or {})
        if fc:
            return fc, None
        return None, "no launch.json and not reconstructable"
    try:
        j = json.load(open(p))
        e = j[-1] if isinstance(j, list) else j
    except Exception as ex:
        return None, f"launch.json unreadable: {ex}"
    if e.get("resume_from"):
        return None, "merge/resume run — not reproducible by one launch"
    return e.get("cmd"), None


def rebuild(cmd, coin, tf, name):
    toks = shlex.split(cmd)
    out, skip = [], 0
    for i, t in enumerate(toks):
        if skip:
            skip -= 1
            continue
        if t in ("--name", "--procs", "--symbol", "--tf", "--resume-from",
                 "--merge-mode"):
            skip = 1
            continue
        out.append(t)
    out += ["--procs", str(PROCS), "--symbol", coin, "--tf", str(tf),
            "--name", name]
    return out


def bt_source_run(name, run_dirs):
    for d in sorted(run_dirs, key=len, reverse=True):
        if name == d or name.startswith(d + "_"):
            return d
    return None


def make_plan():
    run_dirs = [d for d in os.listdir(RUNS)
                if os.path.isdir(os.path.join(RUNS, d))]
    bests = {d: load_best(d) for d in run_dirs}
    # top 10 optimize runs
    scored = [(honest_growth(b), d) for d, b in bests.items()
              if eligible(d, b) and honest_growth(b) is not None]
    scored.sort(reverse=True)
    top_opt = [d for _, d in scored[:10]]
    # top 10 backtest source runs
    raw = open(os.path.join(HERE, "..", "dashboard", "backtests.js")).read()
    ents = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    ents = [e for e in ents
            if not e["stats"].get("liq")
            and (e.get("pair") or "SOL_USDT") == "SOL_USDT"
            and str(e.get("timeframe") or "3m") == "3m"
            and e.get("strategy") != "metax"
            and "full" in (e.get("kind") or "")]
    ents.sort(key=lambda e: -(e["stats"].get("monthly_growth_pct") or -9e9))
    top_bt, seen = [], set()
    for e in ents:
        src = bt_source_run(e["name"], run_dirs)
        if src and src not in seen and eligible(src, bests.get(src)):
            seen.add(src)
            top_bt.append(src)
        if len(top_bt) >= 10:
            break
    selected, skipped, jobs = [], [], []
    seen_cmds = set()
    for d in dict.fromkeys(top_opt + top_bt):        # ordered union
        cmd, why = launch_cmd(d, bests.get(d))
        if cmd is None:
            skipped.append(dict(run=d, why=why))
            continue
        sig = " ".join(t for t in shlex.split(cmd)
                       if not t.startswith("--name") and t != d)
        if sig in seen_cmds:
            skipped.append(dict(run=d, why="duplicate settings of an "
                                            "already-selected run"))
            continue
        seen_cmds.add(sig)
        selected.append(d)
        for tf in TFS:
            for coin in COINS:
                name = f"rep_{d}__{coin}_{tf}m"[:80]
                jobs.append(dict(run=d, coin=coin, tf=tf, name=name,
                                 cmd=rebuild(cmd, coin, tf, name),
                                 status="pending"))
    # 3m jobs first (comparable to originals), then 1m
    jobs.sort(key=lambda j: (j["tf"] != 3,))
    plan = dict(selected=selected, skipped=skipped, jobs=jobs,
                made=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(plan, open(PLAN, "w"), indent=1)
    print(f"selected {len(selected)} runs "
          f"({len(top_opt)} top-optimize ∪ {len(top_bt)} top-backtest srcs), "
          f"skipped {len(skipped)}, jobs {len(jobs)}")
    for s in skipped:
        print(f"  SKIP {s['run']}: {s['why']}")
    for d in selected:
        b = bests[d]
        g = honest_growth(b)
        gtxt = f"{100*(2.718281828**g-1):+.1f}%/mo" if g is not None \
            else "(picked via top backtest)"
        print(f"  {d}  [{b.get('strategy')}/{b.get('mode')}/"
              f"{b.get('method')}]  honest {gtxt}")
    return plan


def execute():
    os.makedirs(LOGS, exist_ok=True)
    plan = json.load(open(PLAN))
    while subprocess.run(["pgrep", "-f", "campaign.py --name c7"],
                         capture_output=True).returncode == 0:
        print("waiting for campaign c7 to finish...", flush=True)
        time.sleep(300)
    for j in plan["jobs"]:
        if os.path.exists(STOP):
            print("STOP file — halting queue", flush=True)
            break
        if j["status"] == "done" or \
                os.path.exists(os.path.join(RUNS, j["name"], "best_config.json")) or \
                os.path.exists(os.path.join(RUNS, j["name"], "pool2.json")):
            j["status"] = "done"
            json.dump(plan, open(PLAN, "w"), indent=1)
            continue
        print(f"[{time.strftime('%H:%M:%S')}] {j['name']}", flush=True)
        j["status"] = "running"
        json.dump(plan, open(PLAN, "w"), indent=1)
        with open(os.path.join(LOGS, j["name"] + ".log"), "w") as lf:
            rc = subprocess.run(j["cmd"], cwd=HERE, stdout=lf,
                                stderr=subprocess.STDOUT).returncode
        j["status"] = "done" if rc == 0 else "failed"
        json.dump(plan, open(PLAN, "w"), indent=1)
        print(f"   -> {j['status']}", flush=True)
    print("queue finished", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    if a.plan or not os.path.exists(PLAN):
        make_plan()
    if a.execute:
        execute()
