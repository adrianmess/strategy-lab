#!/usr/bin/env python3
"""Local control panel for the adaptive trader + optimizer.

  pip install flask
  python3 panel/server.py            # http://127.0.0.1:8800

Lets you: watch live trader status, start/stop it (dry-run or LIVE),
launch backtests / optimizations / walk-forwards / refits with live logs,
and open the results dashboard. Everything runs as local subprocesses of
this server — closing the server stops the trader too.
"""
import json, os, signal, subprocess, sys, time, uuid
from flask import Flask, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AT = os.path.join(REPO, "adaptive_trader")
OPT = os.path.join(REPO, "optimizer")
DASH = os.path.join(REPO, "dashboard")
JOBS_DIR = os.path.join(HERE, "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

app = Flask(__name__)
trader = {"proc": None, "config": None, "live": False, "started": None}
jobs = {}  # id -> dict(proc, cmd, log, name, kind, started)


def tail(path, lines=80):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except Exception:
        return []


def spawn(kind, name, cmd, cwd):
    jid = f"{kind}_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
    log = os.path.join(JOBS_DIR, jid + ".log")
    with open(log, "w") as lf:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=lf, stderr=subprocess.STDOUT)
    jobs[jid] = dict(proc=proc, cmd=" ".join(cmd), log=log, name=name,
                     kind=kind, started=time.strftime("%H:%M:%S"))
    return jid


# ---------------- pages ----------------
@app.route("/")
def index():
    return send_from_directory(HERE, "panel.html")

@app.route("/api/doctor")
def doctor_route():
    import doctor
    return jsonify(doctor.run_all())

@app.route("/api/doctor/fix_caches", methods=["POST"])
def doctor_fix():
    import doctor
    return jsonify(removed=doctor.fix_caches())

@app.route("/dashboard/<path:p>")
def dash(p):
    return send_from_directory(DASH, p)


# ---------------- trader ----------------
@app.route("/api/status")
def status():
    p = trader["proc"]
    running = p is not None and p.poll() is None
    cfg_name = trader["config"] or "config.json"
    cfg_path = os.path.join(AT, cfg_name)
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    state_file = os.path.join(AT, cfg.get("state_file", "trader_state.json"))
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}
    return jsonify(dict(
        running=running, live=trader["live"] if running else False,
        config=cfg_name, started=trader["started"] if running else None,
        exit_code=(None if running or p is None else p.poll()),
        mode=cfg.get("mode"), method=cfg.get("method"),
        equity_usdt=cfg.get("equity_usdt"),
        candidate=cfg.get("candidate"),
        position=state.get("position"),
        log=tail(os.path.join(AT, cfg.get("log_file", "trader.log")), 60),
    ))

@app.route("/api/trader/start", methods=["POST"])
def trader_start():
    d = request.get_json(force=True)
    if trader["proc"] is not None and trader["proc"].poll() is None:
        return jsonify(error="trader already running"), 400
    cfg_name = d.get("config", "config.json")
    live = bool(d.get("live"))
    if live and d.get("confirm") != "LIVE":
        return jsonify(error="live start requires confirm='LIVE'"), 400
    cmd = [sys.executable, "trader.py"] + (["--live"] if live else [])
    # trader logs to its own file already; also capture stdout
    log = os.path.join(JOBS_DIR, "trader_stdout.log")
    with open(log, "a") as lf:
        proc = subprocess.Popen(cmd, cwd=AT, stdout=lf, stderr=subprocess.STDOUT,
                                env={**os.environ, "TRADER_CONFIG": cfg_name})
    trader.update(proc=proc, config=cfg_name, live=live,
                  started=time.strftime("%Y-%m-%d %H:%M:%S"))
    return jsonify(ok=True)

@app.route("/api/trader/stop", methods=["POST"])
def trader_stop():
    p = trader["proc"]
    if p is None or p.poll() is not None:
        return jsonify(error="not running"), 400
    p.send_signal(signal.SIGINT)
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.terminate()
    return jsonify(ok=True)


# ---------------- jobs ----------------
@app.route("/api/jobs", methods=["GET"])
def jobs_list():
    out = []
    for jid, j in sorted(jobs.items(), reverse=True):
        rc = j["proc"].poll()
        out.append(dict(id=jid, kind=j["kind"], name=j["name"], cmd=j["cmd"],
                        started=j["started"],
                        status="running" if rc is None else f"done ({rc})",
                        log=tail(j["log"], 25)))
    return jsonify(out)

@app.route("/api/jobs/backtest", methods=["POST"])
def job_backtest():
    d = request.get_json(force=True)
    cfg = d.get("config", "../adaptive_trader/research2/final_config_v6_lev_none.json")
    name = d.get("name") or f"bt_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, "backtest_cli.py", "--config", cfg, "--name", name]
    if d.get("oos_start"):
        cmd += ["--oos-start", d["oos_start"]]
    return jsonify(id=spawn("backtest", name, cmd, OPT))

@app.route("/api/jobs/optimize", methods=["POST"])
def job_optimize():
    d = request.get_json(force=True)
    name = d.get("name") or f"opt_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, "optimize_cli.py",
           "--strategy", d.get("strategy", "v6"), "--mode", d.get("mode", "lev"),
           "--method", d.get("method", "none"),
           "--procs", str(d.get("procs", 4)), "--name", name]
    if d.get("hours"): cmd += ["--hours", str(d["hours"])]
    if d.get("total"): cmd += ["--total", str(d["total"])]
    return jsonify(id=spawn("optimize", name, cmd, OPT))

@app.route("/api/jobs/walkforward", methods=["POST"])
def job_wf():
    d = request.get_json(force=True)
    name = d.get("name") or f"wf_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, "walkforward_cli.py",
           "--strategy", d.get("strategy", "v6"), "--mode", d.get("mode", "lev"),
           "--method", d.get("method", "none"), "--window", str(d.get("window", "all")),
           "--refit-days", str(d.get("refit_days", 28)),
           "--samples", str(d.get("samples", 500)),
           "--procs", str(d.get("procs", 4)), "--name", name]
    return jsonify(id=spawn("walkforward", name, cmd, OPT))

@app.route("/api/jobs/refit", methods=["POST"])
def job_refit():
    d = request.get_json(force=True)
    cmd = [sys.executable, "refit.py"]
    if d.get("procs"): cmd += ["--procs", str(d["procs"])]
    if d.get("hours"): cmd += ["--hours", str(d["hours"])]
    if d.get("dry"): cmd += ["--dry"]
    return jsonify(id=spawn("refit", "refit", cmd, AT))

@app.route("/api/jobs/update_data", methods=["POST"])
def job_data():
    cmd = [sys.executable, os.path.join(AT, "research", "update_data.py")]
    return jsonify(id=spawn("data", "update_data", cmd, AT))

@app.route("/api/jobs/<jid>/stop", methods=["POST"])
def job_stop(jid):
    j = jobs.get(jid)
    if not j:
        return jsonify(error="unknown job"), 404
    if j["proc"].poll() is None:
        j["proc"].terminate()
    return jsonify(ok=True)


# ---------------- webhook executor (Playwright) ----------------
webhook = {"proc": None, "started": None, "port": None}

def _port_free(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def _sync_webhook_url(port):
    """Keep every trader config pointing at the executor's actual port."""
    changed = []
    for f in os.listdir(AT):
        if f.startswith("config") and f.endswith(".json"):
            p = os.path.join(AT, f)
            try:
                c = json.load(open(p))
            except Exception:
                continue
            url = f"http://127.0.0.1:{port}/webhook"
            if c.get("webhook_url") != url:
                c["webhook_url"] = url
                json.dump(c, open(p, "w"), indent=1)
                changed.append(f)
    return changed

@app.route("/api/webhook/start", methods=["POST"])
def webhook_start():
    if webhook["proc"] is not None and webhook["proc"].poll() is None:
        return jsonify(error="webhook server already running"), 400
    d = request.get_json(force=True) or {}
    want = int(d.get("port", 5000))
    port = None
    for cand in [want] + [p for p in range(5001, 5011) if p != want]:
        if _port_free(cand):
            port = cand
            break
    if port is None:
        return jsonify(error="no free port found between 5000-5010"), 500
    note = ""
    if port != want:
        note = (f"Port {want} was busy (on macOS that's usually the AirPlay Receiver — "
                f"System Settings > General > AirDrop & Handoff — or an old executor still running). "
                f"Started on port {port} instead and updated the trader configs to match.")
    changed = _sync_webhook_url(port)
    log = os.path.join(JOBS_DIR, "webhook_server.log")
    with open(log, "a") as lf:
        proc = subprocess.Popen([sys.executable, "webhook_server.py",
                                 "--instance", "1", "--port", str(port)],
                                cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
    webhook.update(proc=proc, started=time.strftime("%H:%M:%S"), port=port)
    return jsonify(ok=True, port=port, note=note, configs_updated=changed)

@app.route("/api/webhook/stop", methods=["POST"])
def webhook_stop():
    p = webhook["proc"]
    if p is None or p.poll() is not None:
        return jsonify(error="not running"), 400
    p.terminate()
    return jsonify(ok=True)

@app.route("/api/webhook/status")
def webhook_status():
    p = webhook["proc"]
    running = p is not None and p.poll() is None
    return jsonify(running=running, started=webhook["started"] if running else None,
                   port=webhook.get("port"),
                   log=tail(os.path.join(JOBS_DIR, "webhook_server.log"), 20))


# ---------------- configs / runs / adoption ----------------
def _config_entries():
    out = []
    r2 = os.path.join(AT, "research2")
    for f in sorted(os.listdir(r2)):
        if f.startswith("final_config_") and f.endswith(".json"):
            out.append(dict(path=os.path.join(r2, f),
                            label=f"production: {f[13:-5]}", kind="production"))
    runs_dir = os.path.join(OPT, "runs")
    if os.path.isdir(runs_dir):
        for d in sorted(os.listdir(runs_dir)):
            p = os.path.join(runs_dir, d, "best_config.json")
            if os.path.exists(p):
                out.append(dict(path=p, label=f"optimizer run: {d}", kind="run"))
    return out

@app.route("/api/configs")
def configs():
    out = _config_entries()
    for e in out:
        try:
            j = json.load(open(e["path"]))
            m = j.get("metrics", {})
            e.update(strategy=j.get("strategy"), mode=j.get("mode"),
                     method=j.get("method"),
                     eq=m.get("eq"), maxdd=m.get("maxdd"), n=m.get("n"))
        except Exception as ex:
            e["error"] = str(ex)
    return jsonify(out)

@app.route("/api/adopt", methods=["POST"])
def adopt():
    """Splice a best_config candidate into a trader config (with backup)."""
    d = request.get_json(force=True)
    src = d["source"]
    if not os.path.isabs(src):
        src = os.path.join(OPT, src)
    target = os.path.join(AT, d.get("target", "config.json"))
    best = json.load(open(src))
    cfg = json.load(open(target))
    if best.get("mode") and cfg.get("mode") and best["mode"] != cfg["mode"]:
        if not d.get("force"):
            right = "config_spot.json" if best["mode"] == "spot" else "config.json"
            return jsonify(error=(
                f"This is a {best['mode']}-mode strategy, but "
                f"{os.path.basename(target)} is the {cfg['mode']}-mode trader config. "
                f"Choose '{right}' as the adopt target instead "
                f"(spot strategies -> config_spot.json, leveraged -> config.json).")), 400
    import shutil
    shutil.copy(target, target + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
    cfg["candidate"] = best["cand"]
    if best.get("mode"): cfg["mode"] = best["mode"]
    if best.get("method"): cfg["method"] = best["method"]
    cfg["adopted_from"] = dict(source=src, at=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(cfg, open(target, "w"), indent=1)
    return jsonify(ok=True, target=os.path.basename(target))

@app.route("/api/trader_configs")
def trader_configs():
    """Describe each trader config file: which strategy/mode it carries."""
    out = []
    for f in sorted(os.listdir(AT)):
        if not (f.startswith("config") and f.endswith(".json")):
            continue
        try:
            c = json.load(open(os.path.join(AT, f)))
        except Exception:
            continue
        cand = c.get("candidate") or {}
        strat = cand.get("strategy") or ("v7" if "regs" in cand else
                                         ("v6" if cand else "legacy"))
        out.append(dict(file=f, strategy=strat, mode=c.get("mode"),
                        method=c.get("method"), equity=c.get("equity_usdt"),
                        adopted_from=(c.get("adopted_from") or {}).get("source"),
                        adopted_at=(c.get("adopted_from") or {}).get("at")))
    return jsonify(out)

@app.route("/api/trader_config", methods=["GET", "POST"])
def trader_config():
    fname = request.args.get("file", "config.json")
    path = os.path.join(AT, os.path.basename(fname))
    if request.method == "GET":
        return jsonify(json.load(open(path)))
    d = request.get_json(force=True)
    cfg = json.load(open(path))
    allowed = {"equity_usdt", "webhook_url", "poll_seconds",
               "emergency_exit_adverse", "dry_run", "symbol"}
    changed = {k: v for k, v in d.items() if k in allowed}
    import shutil
    shutil.copy(path, path + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
    cfg.update(changed)
    json.dump(cfg, open(path, "w"), indent=1)
    return jsonify(ok=True, changed=changed)

@app.route("/api/param_space", methods=["GET", "POST"])
def param_space():
    path = os.path.join(OPT, "param_space.json")
    if request.method == "GET":
        return jsonify(json.load(open(path)))
    d = request.get_json(force=True)
    import shutil
    shutil.copy(path, path + ".bak")
    json.dump(d, open(path, "w"), indent=1)
    return jsonify(ok=True)

@app.route("/api/jobs/optimize2", methods=["POST"])
def job_optimize2():
    d = request.get_json(force=True)
    name = d.get("name") or f"opt2_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, "optimize2_cli.py",
           "--algo", d.get("algo", "genetic"),
           "--mode", d.get("mode", "lev"), "--method", d.get("method", "vol3"),
           "--procs", str(d.get("procs", 4)), "--batch", str(d.get("batch", 100)),
           "--name", name]
    if d.get("single_set"): cmd += ["--single-set"]
    if d.get("hours"): cmd += ["--hours", str(d["hours"])]
    if d.get("total"): cmd += ["--total", str(d["total"])]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    if d.get("resume_from"): cmd += ["--resume-from", d["resume_from"]]
    return jsonify(id=spawn("optimize-v2", name, cmd, OPT))

@app.route("/api/jobs/ai_suggest", methods=["POST"])
def job_ai():
    d = request.get_json(force=True)
    cmd = [sys.executable, "ai_advisor.py", "--run", d["run"],
           "--n", str(d.get("n", 12))]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    return jsonify(id=spawn("ai-advisor", d["run"], cmd, OPT))

@app.route("/api/ai_key_status")
def ai_key():
    ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    env_path = os.path.join(REPO, ".env")
    if not ok and os.path.exists(env_path):
        ok = "ANTHROPIC_API_KEY" in open(env_path).read()
    return jsonify(configured=ok)

@app.route("/api/runs2")
def runs2():
    out = []
    runs_dir = os.path.join(OPT, "runs")
    for d in sorted(os.listdir(runs_dir)):
        pool_p = os.path.join(runs_dir, d, "pool2.json")
        if not os.path.exists(pool_p):
            pool_p = os.path.join(runs_dir, d, "pool.json")   # legacy v6 runs
        best_p = os.path.join(runs_dir, d, "best_config.json")
        if not os.path.exists(pool_p):
            continue
        e = dict(name=d, run=f"runs/{d}",
                 last_run=time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(os.path.getmtime(pool_p))))
        try:
            pd_ = json.load(open(pool_p))
            e["evaluated"] = pd_.get("evaluated")
            e["feasible"] = len(pd_.get("pool", []))
            if pd_.get("pool"):
                s, c, m = pd_["pool"][0]
                e.update(best_score=s, best_eq=m.get("eq"), maxdd=m.get("maxdd"),
                         mode=c.get("mode"), regimes=len(c.get("regs", c.get("zL", []))),
                         strategy=c.get("strategy") or ("v7" if "regs" in c else "v6"))
        except Exception:
            pass
        if os.path.exists(best_p):
            try:
                bc = json.load(open(best_p))
                e["method"] = bc.get("method")
                e["holdout"] = bc.get("holdout")
                e["best_config"] = f"runs/{d}/best_config.json"
                e["strategy"] = bc.get("strategy", e.get("strategy", "v7"))
                e["finished"] = bc.get("generated")
            except Exception:
                pass
        if not e.get("strategy"):  # last-resort inference so the UI never shows '?'
            nm = d.lower()
            e["strategy"] = "scalpx" if "scalp" in nm else \
                ("v7" if pool_p.endswith("pool2.json") else "v6")
        out.append(e)
    return jsonify(out)

@app.route("/api/backtests/delete", methods=["POST"])
def backtests_delete():
    name = request.get_json(force=True).get("name")
    path = os.path.join(DASH, "backtests.js")
    txt = open(path).read()
    entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
    entries = [e for e in entries if e.get("name") != name]
    with open(path, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")
    return jsonify(ok=True, remaining=len(entries))


if __name__ == "__main__":
    print("Control panel: http://127.0.0.1:8800")
    app.run(host="127.0.0.1", port=8800, debug=False)
