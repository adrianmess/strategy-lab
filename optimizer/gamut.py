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


def report(pdir, plan):
    lines = [f"# Gamut {plan['config']['name']} — report",
             f"updated {time.strftime('%Y-%m-%d %H:%M')}",
             "", "Ranked by honest holdout %/mo (OOS-best preferred, no liq).", ""]
    for coin in plan["config"]["pairs"]:
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
        specs = build_plan(cfg)
        json.dump(dict(config=cfg, specs=specs,
                       made=time.strftime("%Y-%m-%d %H:%M")),
                  open(plan_p, "w"), indent=1)
        print(f"plan: {len(specs)} runs across {len(cfg['pairs'])} pairs",
              flush=True)
    for f in (os.path.join(pdir, "STOP"),):
        if os.path.exists(f):
            os.remove(f)
    execute(pdir)


if __name__ == "__main__":
    main()
