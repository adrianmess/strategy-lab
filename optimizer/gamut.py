#!/usr/bin/env python3
"""GAMUT runner — autonomous per-pair sweep across everything you select:
strategies x algorithms x modes x charts x regimes x scoring x max-DD x
max-hold x holdout schemes, with the imported/active or AI parameter space.

Pair-major order: every combo for pair 1 finishes (optimize + automatic
best/OOS-best backtests) before pair 2 starts. Resumable; graceful stop via
a STOP file. A ranked report per pair is written at the end (and refreshed
after every spec).

Config JSON (written by the panel, or by hand):
{
 "name": "g1", "procs": 14, "total": 60000,
 "pairs": ["sui","doge"], "strategies": ["v7","macdx"],
 "algos": ["genetic"], "modes": ["spot"], "tfs": [3,1],
 "methods": ["vol3"], "scorings": ["classic"],
 "max_dds": [0.5], "max_holds": [5],
 "holdouts": [{"kind":"date","date":"2025-09-01"},
              {"kind":"alt","days":21},
              {"kind":"between","a":"2025-03-01","b":"2025-09-01"},
              {"kind":"before","date":"2025-01-01"},
              {"kind":"outside","a":"2025-03-01","b":"2025-09-01"}],
 "space_variant": "active" | "ai",
 "lev_stops": true, "cadapt": false
}

Usage:
  python3 gamut.py --config cfg.json          # build plan + run
  python3 gamut.py --resume campaigns/gamut_g1   # continue
Stop: touch campaigns/gamut_<name>/STOP
"""
import _bootstrap as B
import argparse, itertools, json, math, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
CAMPS = os.path.join(HERE, "campaigns")

HO_CODE = {"date": "hA", "before": "hB", "between": "hM", "outside": "hO",
           "alt": "hL"}


def ho_flags(h):
    k = h.get("kind")
    if k == "date":
        return ["--train-end", h["date"]], f"hA{h['date'][2:7].replace('-', '')}"
    if k == "before":
        return ["--holdout-before", h["date"]], f"hB{h['date'][2:7].replace('-', '')}"
    if k == "between":
        return (["--holdout-between", f"{h['a']}..{h['b']}"],
                f"hM{h['a'][2:7].replace('-', '')}")
    if k == "outside":
        return (["--holdout-outside", f"{h['a']}..{h['b']}"],
                f"hO{h['a'][2:7].replace('-', '')}")
    if k == "alt":
        return ["--holdout-days", str(h["days"])], f"hL{h['days']:g}"
    if k == "none":            # full-history train, no holdout (train-best only)
        return [], "hN"
    raise ValueError(f"bad holdout {h}")


def ai_space_path(coin, tf):
    name = "default" if (coin == "sol" and int(tf) == 3) else f"{coin}_{tf}m"
    return name, os.path.join(HERE, "param_spaces", "variants", f"{name}.ai.json")


def build_plan(cfg):
    specs = []
    totals = cfg.get("totals") or [cfg.get("total", 60000)]
    for coin in cfg["pairs"]:                       # PAIR-MAJOR
        for tf, strat, algo, mode, method, scoring, dd, mh, h, total in \
                itertools.product(
                cfg["tfs"], cfg["strategies"], cfg["algos"], cfg["modes"],
                cfg["methods"], cfg["scorings"], cfg["max_dds"],
                cfg["max_holds"], cfg["holdouts"], totals):
            hf, hcode = ho_flags(h)
            tcode = (f"_t{int(total/1000)}k" if len(totals) > 1 else "")
            name = (f"{cfg['name']}_{coin}{tf}m_{strat}_{algo[:3]}_{mode[0]}_"
                    f"{method}_{scoring[:2]}_d{int(dd*100)}_m{mh:g}_{hcode}"
                    f"{tcode}")[:78]
            cmd = [sys.executable, "optimize2_cli.py",
                   "--strategy", strat, "--mode", mode, "--method", method,
                   "--algo", algo, "--procs", str(cfg.get("procs", 14)),
                   "--batch", "100", "--total", str(total),
                   "--symbol", coin, "--tf", str(tf),
                   "--gap-mode", "skip_contaminated",
                   "--max-dd", str(dd), "--max-hold-days", str(mh),
                   "--scoring", scoring, "--name", name] + hf
            if cfg.get("lev_stops") and mode == "lev":
                cmd.append("--lev-stops")
            if cfg.get("cadapt"):
                cmd.append("--cadapt")
            spec = dict(name=name, coin=coin, tf=tf, cmd=cmd, status="pending")
            if cfg.get("space_variant") == "ai":
                spec["ai_space"] = list(ai_space_path(coin, tf))
            specs.append(spec)
    return specs


ENRICHED_FEATURES = ("ob", "cvd", "vp", "oi")
NEW_FAMILIES = ("flowx", "poctrend", "oisqueeze", "absorb")
_DROP_FLAGS = {"--name": 1, "--procs": 1, "--total": 1, "--resume-from": 1,
               "--merge-mode": 1, "--batch": 1, "--features": 1,
               "--base-run": 1, "--hours": 1}
_DROP_BARE = {"--funding"}


def feature_combos(feats, max_k=None, explicit=None):
    """Every non-empty subset of `feats` in canonical order (15 for four),
    or the explicit list of combos when the config names them."""
    feats = [f for f in ENRICHED_FEATURES if f in set(feats)]
    if explicit:
        out = []
        for c in explicit:
            c = [f for f in ENRICHED_FEATURES if f in set(c)]
            if c and c not in out:
                out.append(c)
        return out
    out = []
    for k in range(1, (max_k or len(feats)) + 1):
        out += [list(c) for c in itertools.combinations(feats, k)]
    return out


def combo_tag(combo):
    return "_".join(combo)


def base_run_cmd(base):
    """Reconstruct the optimizer command that produced a classic run, minus
    the flags the enriched variant overrides. Source of truth is the LAST
    launch in runs/<base>/launch.json (keeps --space/--sticky-oos/--anchor/
    --lev-stops exactly as run); best_config.json is the fallback."""
    import shlex
    rd = os.path.join(RUNS, base)
    lp = os.path.join(rd, "launch.json")
    toks = None
    if os.path.exists(lp):
        try:
            launches = json.load(open(lp))
            cmd = (launches[-1] or {}).get("cmd") or ""
            toks = shlex.split(cmd)
            if toks and toks[0].endswith("python3"):
                toks = toks[1:]
            if toks and toks[0].endswith("optimize2_cli.py"):
                toks = toks[1:]
        except Exception:
            toks = None
    if not toks:
        b = json.load(open(os.path.join(rd, "best_config.json")))
        toks = ["--strategy", b["strategy"], "--mode", b["mode"],
                "--method", b["method"], "--algo", b.get("algo") or "genetic",
                "--symbol", (b.get("pair") or "SOL_USDT").split("_")[0].lower(),
                "--tf", str(b.get("timeframe") or "3m").rstrip("m"),
                "--gap-mode", b.get("gap_mode") or "skip_contaminated",
                "--max-dd", str(b.get("max_dd") or 0.5),
                "--max-hold-days", str(b.get("max_hold_days") or 5),
                "--scoring", b.get("scoring") or "classic"]
        if b.get("train_end"):
            toks += ["--train-end", b["train_end"]]
        if b.get("holdout_days"):
            toks += ["--holdout-days", str(b["holdout_days"])]
        if b.get("holdout_before"):
            toks += ["--holdout-before", b["holdout_before"]]
        if b.get("holdout_between"):
            toks += ["--holdout-between", b["holdout_between"]]
        if b.get("holdout_outside"):
            toks += ["--holdout-outside", b["holdout_outside"]]
        if (b.get("cand") or {}).get("lev_stops"):
            toks.append("--lev-stops")
        if (b.get("cand") or {}).get("cadapt"):
            toks.append("--cadapt")
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t in _DROP_FLAGS:
            i += 1 + _DROP_FLAGS[t]
            continue
        if t in _DROP_BARE:
            i += 1
            continue
        if "=" in t and t.split("=")[0] in _DROP_FLAGS:
            i += 1
            continue
        out.append(t)
        i += 1
    coin = tf = None
    for j, t in enumerate(out):
        if t == "--symbol" and j + 1 < len(out):
            coin = out[j + 1].lower()
        if t == "--tf" and j + 1 < len(out):
            tf = int(out[j + 1])
    return out, (coin or "sol"), (tf or 3)


def build_plan_enriched(cfg):
    """ENRICHED gamut: (a) every base run x every feature combo, funding on
    (a cost, not a feature); (b) the new feature-native families across the
    configured pairs/tfs/modes/methods/scorings/holdouts, always with every
    feature they need. Same spec shape as build_plan so the workers, the
    progress page and the panel need no special casing."""
    e = cfg["enriched"]
    feats = e.get("features") or list(ENRICHED_FEATURES)
    combos = feature_combos(feats, e.get("max_combo"), e.get("combos"))
    funding = bool(e.get("funding", True))
    total = int(e.get("total") or cfg.get("total", 60000))
    procs = str(cfg.get("procs", 14))
    specs = []
    # (a) derived variants, base-major so a base's 15 variants finish together
    for base in e.get("base_runs") or []:
        try:
            toks, coin, tf = base_run_cmd(base)
        except Exception as ex:
            print(f"base run {base}: cannot reconstruct command ({ex}) — skipped",
                  flush=True)
            continue
        for combo in combos:
            name = f"enr_{base}__{combo_tag(combo)}"[:78]
            cmd = [sys.executable, "optimize2_cli.py"] + toks + [
                "--procs", procs, "--batch", "100", "--total", str(total),
                "--features", ",".join(combo), "--base-run", base,
                "--name", name]
            if funding:
                cmd.append("--funding")
            specs.append(dict(name=name, coin=coin, tf=tf, cmd=cmd,
                              status="pending",
                              enriched=dict(base_run=base, features=combo,
                                            funding=funding, kind="derived")))
    # (b) new families: a normal gamut grid with every feature attached
    fams = [f for f in (e.get("new_families") or []) if f in NEW_FAMILIES]
    if fams:
        sub = dict(cfg, strategies=fams)
        sub.setdefault("pairs", []); sub.setdefault("tfs", [3])
        sub.setdefault("algos", ["genetic"]); sub.setdefault("modes", ["lev"])
        sub.setdefault("methods", ["vol3"]); sub.setdefault("scorings", ["classic"])
        sub.setdefault("max_dds", [0.5]); sub.setdefault("max_holds", [1])
        sub.setdefault("holdouts", [{"kind": "none"}])
        sub["totals"] = [total]
        for s in build_plan(sub):
            s["name"] = f"enr_{s['name']}"[:78]
            s["cmd"][s["cmd"].index("--name") + 1] = s["name"]
            s["cmd"] += ["--features", ",".join(feats)]
            if funding:
                s["cmd"].append("--funding")
            fam = s["cmd"][s["cmd"].index("--strategy") + 1]
            s["enriched"] = dict(base_run=None, features=list(feats),
                                 funding=funding, kind="new", family=fam)
            specs.append(s)
    return specs


def report(pdir, plan):
    lines = [f"# Gamut {plan['config']['name']} — report",
             f"updated {time.strftime('%Y-%m-%d %H:%M')}",
             "", "Ranked by honest holdout %/mo (OOS-best preferred, no liq).", ""]
    coins = list(plan["config"].get("pairs") or [])
    coins += sorted({s["coin"] for s in plan["specs"]} - set(coins))
    for coin in coins:
        rows = []
        for s in plan["specs"]:
            if s["coin"] != coin or s["status"] != "done":
                continue
            b = None
            try:
                b = json.load(open(os.path.join(RUNS, s["name"],
                                                "best_config.json")))
            except Exception:
                pass
            if not b:
                rows.append((None, s["name"], "no config"))
                continue
            h = ((b.get("holdout_best") or {}).get("holdout")
                 or b.get("holdout") or {})
            if not h or h.get("liq") or h.get("growth") is None:
                no_ho = not any(x.startswith(("--holdout", "--train-end"))
                                for x in s.get("cmd", []))
                m = b.get("metrics") or (b.get("cand") or {}).get("m") or {}
                if no_ho and m.get("growth") is not None:
                    rows.append((None, s["name"],
                                 f"TRAIN-ONLY {100*(math.exp(m['growth'])-1):+.1f}%"
                                 f"/mo (no OOS evidence — not comparable)"))
                else:
                    rows.append((None, s["name"], "no survivor / liq"))
                continue
            rows.append((100 * (math.exp(h["growth"]) - 1), s["name"],
                         f"dd {100*(h.get('maxdd') or 0):.0f}%"))
        rows.sort(key=lambda r: (r[0] is None, -(r[0] or 0)))
        lines.append(f"## {coin.upper()}")
        for g, n, note in rows:
            lines.append(f"- {'%+.1f%%/mo' % g if g is not None else '  —  '} "
                         f"· {n} · {note}")
        lines.append("")
    open(os.path.join(pdir, "report.md"), "w").write("\n".join(lines))


def execute(pdir):
    plan_p = os.path.join(pdir, "plan.json")
    plan = json.load(open(plan_p))
    stop_p = os.path.join(pdir, "STOP")
    logs = os.path.join(pdir, "logs")
    os.makedirs(logs, exist_ok=True)
    n = len(plan["specs"])
    for i, s in enumerate(plan["specs"]):
        if os.path.exists(stop_p):
            print("STOP file — halting after current state is saved", flush=True)
            break
        if s["status"] == "done" or os.path.exists(
                os.path.join(RUNS, s["name"], "best_config.json")):
            s["status"] = "done"
            json.dump(plan, open(plan_p, "w"), indent=1)
            continue
        cmd = list(s["cmd"])
        if s.get("ai_space"):
            sp_name, sp_path = s["ai_space"]
            if not os.path.exists(sp_path):
                print(f"[{i+1}/{n}] generating AI space for {sp_name}…",
                      flush=True)
                subprocess.run([sys.executable, "gen_ai_spaces.py",
                                "--space", sp_name], cwd=HERE)
            if os.path.exists(sp_path):
                cmd += ["--space", sp_path]
            else:
                print("  AI space unavailable — falling back to the active "
                      "space", flush=True)
        print(f"[{time.strftime('%H:%M:%S')}] {i+1}/{n} {s['name']}",
              flush=True)
        s["status"] = "running"
        json.dump(plan, open(plan_p, "w"), indent=1)
        with open(os.path.join(logs, s["name"] + ".log"), "w") as lf:
            rc = subprocess.run(cmd, cwd=HERE, stdout=lf,
                                stderr=subprocess.STDOUT).returncode
        s["status"] = "done" if rc == 0 else "failed"
        json.dump(plan, open(plan_p, "w"), indent=1)
        print(f"   -> {s['status']}", flush=True)
        report(pdir, plan)
    report(pdir, plan)
    print("gamut finished.", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--plan-only", action="store_true",
                    help="build plan.json and exit — a remote worker box "
                         "(MacBook agent) executes the specs")
    a = ap.parse_args()
    if a.resume:
        execute(a.resume if os.path.isabs(a.resume)
                else os.path.join(HERE, a.resume))
        return
    cfg = json.load(open(a.config))
    pdir = os.path.join(CAMPS, f"gamut_{cfg['name']}")
    os.makedirs(pdir, exist_ok=True)
    plan_p = os.path.join(pdir, "plan.json")
    if os.path.exists(plan_p):
        print("existing plan found — resuming it", flush=True)
    else:
        specs = build_plan_enriched(cfg) if cfg.get("enriched") else build_plan(cfg)
        json.dump(dict(config=cfg, specs=specs,
                       made=time.strftime("%Y-%m-%d %H:%M")),
                  open(plan_p, "w"), indent=1)
        print(f"plan: {len(specs)} runs across {len(cfg['pairs'])} pairs",
              flush=True)
    for f in (os.path.join(pdir, "STOP"),):
        if os.path.exists(f):
            os.remove(f)
    if a.plan_only:
        print("plan-only: waiting for the remote worker to pick this up",
              flush=True)
        return
    execute(pdir)


if __name__ == "__main__":
    main()
