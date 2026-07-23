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
jobs = {}  # id -> dict(proc, cmd, log, name, kind, started)

# ---------------- instance registry ----------------
# An "instance" = one executor (own browser profile + port) + one trader (own
# config file, which carries its own state_file/log_file/webhook_url).
# Instance "1" is the classic setup. Metadata persists in panel/instances.json;
# processes do not survive a panel restart (same as before).
INSTANCES_FILE = os.path.join(HERE, "instances.json")

def _new_instance(i):
    return dict(
        trader=dict(proc=None, config=None, live=False, started=None),
        webhook=dict(proc=None, started=None, port=None, headless=False),
        cfg="config.json", port=5000 + int(i), headless=False,
        name=f"Instance {i}")

def _load_instances():
    out = {}
    try:
        meta = json.load(open(INSTANCES_FILE))
    except Exception:
        meta = {"1": {}}
    for i, m in sorted(meta.items(), key=lambda kv: int(kv[0])):
        d = _new_instance(i)
        d["cfg"] = m.get("cfg") or d["cfg"]
        d["port"] = m.get("port") or d["port"]
        d["headless"] = bool(m.get("headless"))
        d["name"] = m.get("name") or d["name"]
        out[str(i)] = d
    if "1" not in out:
        out["1"] = _new_instance(1)
    return out

instances = _load_instances()

def _save_instances():
    json.dump({i: dict(cfg=d.get("cfg"), port=d.get("port"),
                       headless=d.get("headless", False),
                       name=d.get("name"))
               for i, d in instances.items()},
              open(INSTANCES_FILE, "w"), indent=1)

def _inst():
    """Resolve the instance addressed by the current request (default '1')."""
    i = str(request.args.get("instance")
            or (request.get_json(silent=True) or {}).get("instance") or "1")
    if i not in instances:
        instances[i] = _new_instance(i)
        _save_instances()
    return i, instances[i]

def _webhook_log(i):
    return os.path.join(JOBS_DIR, "webhook_server.log" if i == "1"
                        else f"webhook_server_i{i}.log")

# instance-1 aliases: any legacy code path keeps working
trader = instances["1"]["trader"]
webhook = instances["1"]["webhook"]


class _PidProc:
    """Popen-compatible handle for a RE-ADOPTED orphan (a process this panel
    started before a restart). Signals by pid; every poll re-verifies the
    command line still matches, so a recycled pid can never be mistaken for
    our process."""
    def __init__(self, pid, sig):
        self.pid = int(pid)
        self._sig = sig            # substring that must appear in the cmdline
        self._rc = None

    def _alive(self):
        try:
            out = subprocess.run(["ps", "-p", str(self.pid), "-o", "command="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
            return self._sig in out
        except Exception:
            return False

    def poll(self):
        if self._rc is not None:
            return self._rc
        if self._alive():
            return None
        self._rc = 0
        return self._rc

    def send_signal(self, s):
        os.kill(self.pid, s)

    def terminate(self):
        os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        os.kill(self.pid, signal.SIGKILL)

    def wait(self, timeout=None):
        deadline = time.time() + (timeout or 3600)
        while time.time() < deadline:
            if self.poll() is not None:
                return self._rc
            time.sleep(0.2)
        raise subprocess.TimeoutExpired("readopted", timeout)


def _readopt_orphans():
    """On panel startup: re-attach traders/executors that a previous panel
    started (matched by TRADER_CONFIG env / --instance flag). Without this,
    a panel restart leaves them running but invisible to the instance cards."""
    try:
        ps = subprocess.run(["ps", "eww", "-axo", "pid=,lstart=,command="],
                            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in ps.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        started = " ".join(parts[3:6])
        cmd = parts[6]
        if "Python" not in cmd and "python" not in cmd:
            continue
        if "trader.py" in cmd and "TRADER_CONFIG=" in cmd:
            cfg = cmd.split("TRADER_CONFIG=")[1].split()[0]
            for i, I in instances.items():
                t = I["trader"]
                busy = t["proc"] is not None and t["proc"].poll() is None
                if not busy and I.get("cfg") == cfg:
                    t.update(proc=_PidProc(pid, "trader.py"), config=cfg,
                             live=("--live" in cmd), started=started)
                    print(f"re-adopted trader pid {pid} ({cfg}) -> "
                          f"instance {i}", flush=True)
                    break
        elif "webhook_server.py" in cmd and "--instance" in cmd:
            inst = cmd.split("--instance")[1].split()[0].strip()
            I = instances.get(inst)
            if I is None:
                continue
            wh = I["webhook"]
            busy = wh["proc"] is not None and wh["proc"].poll() is None
            if not busy:
                port = None
                if "--port" in cmd:
                    try:
                        port = int(cmd.split("--port")[1].split()[0])
                    except ValueError:
                        pass
                wh.update(proc=_PidProc(pid, "webhook_server.py"),
                          started=started, port=(port or I.get("port")),
                          headless=("--headless" in cmd))
                print(f"re-adopted executor pid {pid} -> instance {inst}",
                      flush=True)


_readopt_orphans()


def tail(path, lines=80):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except Exception:
        return []


_LOADED_MTIME = os.path.getmtime(os.path.abspath(__file__))

@app.route("/api/version")
def api_version():
    """Lets the pages detect a stale server process after code updates."""
    try:
        cur = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        cur = _LOADED_MTIME
    return jsonify(stale=(cur != _LOADED_MTIME))


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
    i, I = _inst()
    t = I["trader"]
    p = t["proc"]
    running = p is not None and p.poll() is None
    cfg_name = t["config"] or I["cfg"] or "config.json"
    cfg_path = os.path.join(AT, cfg_name)
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    state_file = os.path.join(AT, cfg.get("state_file", "trader_state.json"))
    state = json.load(open(state_file)) if os.path.exists(state_file) else {}
    return jsonify(dict(
        instance=i,
        running=running, live=t["live"] if running else False,
        config=cfg_name, started=t["started"] if running else None,
        exit_code=(None if running or p is None else p.poll()),
        execution=cfg.get("execution", "browser"),
        api_account=cfg.get("api_account", "mexc1"),
        mode=cfg.get("mode"), method=cfg.get("method"),
        equity_usdt=cfg.get("equity_usdt"),
        candidate=cfg.get("candidate"),
        position=state.get("position"),
        log=tail(os.path.join(AT, cfg.get("log_file", "trader.log")), 60),
    ))

def _state_file_of(cfg_name):
    try:
        c = json.load(open(os.path.join(AT, cfg_name)))
        return c.get("state_file", "trader_state.json")
    except Exception:
        return None

@app.route("/api/trader/start", methods=["POST"])
def trader_start():
    d = request.get_json(force=True)
    i, I = _inst()
    t = I["trader"]
    if t["proc"] is not None and t["proc"].poll() is None:
        return jsonify(error=f"instance {i}: trader already running"), 400
    cfg_name = d.get("config", I["cfg"] or "config.json")
    live = bool(d.get("live"))
    if live and d.get("confirm") != "LIVE":
        return jsonify(error="live start requires confirm='LIVE'"), 400
    # SAFETY: two traders sharing one state file corrupt each other. Refuse.
    mine = _state_file_of(cfg_name)
    for j, J in instances.items():
        tj = J["trader"]
        if j != i and tj["proc"] is not None and tj["proc"].poll() is None:
            theirs = _state_file_of(tj["config"] or J["cfg"] or "config.json")
            if mine and theirs and mine == theirs:
                return jsonify(error=(
                    f"instance {j}'s running trader uses the same state file "
                    f"('{mine}') as {cfg_name}. Give this instance its own config "
                    f"with distinct state_file/log_file/webhook_url.")), 400
    cmd = [sys.executable, "trader.py"] + (["--live"] if live else [])
    # trader logs to its own file already; also capture stdout
    log = os.path.join(JOBS_DIR, "trader_stdout.log" if i == "1"
                       else f"trader_stdout_i{i}.log")
    with open(log, "a") as lf:
        proc = subprocess.Popen(cmd, cwd=AT, stdout=lf, stderr=subprocess.STDOUT,
                                env={**os.environ, "TRADER_CONFIG": cfg_name})
    t.update(proc=proc, config=cfg_name, live=live,
             started=time.strftime("%Y-%m-%d %H:%M:%S"))
    I["cfg"] = cfg_name
    _save_instances()
    return jsonify(ok=True, instance=i)

@app.route("/api/trader/stop", methods=["POST"])
def trader_stop():
    i, I = _inst()
    p = I["trader"]["proc"]
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
        entry = dict(id=jid, kind=j["kind"], name=j["name"], cmd=j["cmd"],
                     started=j["started"], stopping=j.get("stopping", False),
                     status="running" if rc is None else f"done ({rc})",
                     log=tail(j["log"], 25))
        if rc is None and j["kind"].startswith("optimize"):
            pp = os.path.join(OPT, "runs", j["name"], "progress.json")
            try:
                pr = json.load(open(pp))
                if time.time() - pr.get("updated", 0) < 300:
                    entry["progress"] = pr
            except Exception:
                pass
        out.append(entry)
    return jsonify(out)

def _safe_name(n):
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", n or "")

@app.route("/api/defaults")
def api_defaults():
    """The strategy's stored live-default parameters as an editable candidate."""
    strategy = request.args.get("strategy", "v7")
    mode = request.args.get("mode", "lev")
    method = request.args.get("method", "vol3")
    R = {"none": 1, "volXtrend9": 9}.get(method, 3)
    if strategy.endswith("_original"):
        code = (
            "import _bootstrap as B, json\n"
            "from backtest_cli import original_defaults\n"
            f"print(json.dumps(original_defaults({strategy!r}, {mode!r}), default=float))"
        )
    else:
        code = (
            "import _bootstrap as B, json\n"
            "from optimize2_cli import build_anchor_defaults\n"
            "sp = json.load(open(B.OPT_DIR + '/param_space.json'))\n"
            f"space = (sp.get({strategy!r} + '@spot') if {mode!r} == 'spot' else None) "
            f"or sp.get({strategy!r}) or {{}}\n"
            f"print(json.dumps(build_anchor_defaults({strategy!r}, {mode!r}, {R}, space), default=float))"
        )
    try:
        out = subprocess.run([sys.executable, "-c", code], cwd=OPT,
                             capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return jsonify(error=out.stderr.strip().splitlines()[-1] if out.stderr else "failed"), 400
        return jsonify(cand=json.loads(out.stdout.strip().splitlines()[-1]))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route("/api/jobs/backtest", methods=["POST"])
def job_backtest():
    d = request.get_json(force=True)
    name = _safe_name(d.get("name")) or f"bt_{time.strftime('%m%d_%H%M')}"
    if d.get("cand"):
        # quick backtest: a raw candidate (defaults or user-edited), no optimizer run
        qdir = os.path.join(OPT, "runs", "_backtest_tmp")
        os.makedirs(qdir, exist_ok=True)
        cfg = os.path.join(qdir, f"quick_{name}.json")
        json.dump(dict(cand=d["cand"], strategy=d.get("strategy", "v7"),
                       mode=d.get("mode", "lev"), method=d.get("method", "vol3"),
                       kind="quick backtest (no optimizer)"),
                  open(cfg, "w"))
    else:
        cfg = d.get("config", "../adaptive_trader/research2/final_config_v6_lev_none.json")
    cmd = [sys.executable, "backtest_cli.py", "--config", cfg, "--name", name]
    if d.get("oos_start"):
        cmd += ["--oos-start", d["oos_start"]]
    if d.get("holdout_days"):
        cmd += ["--holdout-days", str(d["holdout_days"])]
    if d.get("gap_mode"):
        cmd += ["--gap-mode", d["gap_mode"]]
    return jsonify(id=spawn("backtest", name, cmd, OPT))

@app.route("/api/jobs/walkforward", methods=["POST"])
def job_wf():
    d = request.get_json(force=True)
    name = _safe_name(d.get("name")) or f"wf_{time.strftime('%m%d_%H%M')}"
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

@app.route("/api/jobs/reassign", methods=["POST"])
def job_reassign():
    """Run the router re-assignment selector now (all router configs)."""
    d = request.get_json(silent=True) or {}
    cmd = [sys.executable, "metax_reassign.py", "--all"]
    if d.get("force"):
        cmd.append("--force")
    return jsonify(id=spawn("reassign", "routers", cmd, OPT))


@app.route("/api/jobs/update_data", methods=["POST"])
def job_data():
    cmd = [sys.executable, os.path.join(AT, "research", "update_data.py")]
    return jsonify(id=spawn("data", "update_data", cmd, AT))

@app.route("/api/jobs/<jid>/stop", methods=["POST"])
def job_stop(jid):
    import signal as _sig
    j = jobs.get(jid)
    if not j:
        return jsonify(error="unknown job"), 404
    if j["proc"].poll() is not None:
        return jsonify(ok=True, note="already finished")
    if j["kind"].startswith("optimize") and not j.get("stopping"):
        j["stopping"] = True
        run_dir = os.path.join(OPT, "runs", j["name"])
        try:
            open(os.path.join(run_dir, "stop.flag"), "w").write("stop")
        except OSError:
            pass
        j["proc"].send_signal(_sig.SIGTERM)
        return jsonify(ok=True, graceful=True,
                       note="Stopping gracefully: the current generation will finish, "
                            "then holdout results are computed and saved. This can take "
                            "up to a minute. Click stop again to force-kill.")
    j["proc"].terminate()
    return jsonify(ok=True, note="force-killed")


# ---------------- webhook executor (Playwright) ----------------
# (webhook state now lives per-instance; see the instance registry at the top)

def _port_free(port):
    """The executor binds 0.0.0.0, so test exactly that, WITHOUT SO_REUSEADDR
    (reuse can mask conflicts, e.g. macOS AirPlay holding *:5000). Also try
    connecting: an active listener answers even when a bind probe is unclear."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
            c.settimeout(0.3)
            if c.connect_ex(("127.0.0.1", port)) == 0:
                return False        # something is listening
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _port_owner(port):
    """Best-effort: who is holding the port? (macOS/Linux, needs lsof)"""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
        lines = out.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            return f"{parts[0]} (pid {parts[1]})"
    except Exception:
        pass
    return None

def _sync_webhook_url(port, cfg_files=None):
    """Point trader config(s) at the executor's actual port. With multiple
    instances only THIS instance's config is rewritten (cfg_files list);
    None = legacy behavior (all config*.json) used when only instance 1 exists."""
    changed = []
    files = cfg_files if cfg_files is not None else \
        [f for f in os.listdir(AT) if f.startswith("config") and f.endswith(".json")]
    for f in files:
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
    i, I = _inst()
    wh = I["webhook"]
    if wh["proc"] is not None and wh["proc"].poll() is None:
        return jsonify(error=f"instance {i}: executor already running"), 400
    d = request.get_json(force=True) or {}
    want = int(d.get("port", I["port"] or 5001))
    if want == 5000:
        want = 5001   # port 5000 is reserved by macOS AirPlay; never use it
    # ports used by OTHER instances' running executors are off limits
    taken = {J["webhook"].get("port") for j, J in instances.items()
             if j != i and J["webhook"]["proc"] is not None
             and J["webhook"]["proc"].poll() is None}
    port = None
    for cand in [want] + [p for p in range(5001, 5012) if p != want]:
        if cand not in taken and _port_free(cand):
            port = cand
            break
    if port is None:
        return jsonify(error="no free port found between 5001-5011"), 500
    note = ""
    if port != want:
        owner = _port_owner(want)
        who = f"It's held by: {owner}. " if owner else ""
        hint = ("That's an old executor still running — stop it (or Force stop all) "
                "or let this one use the new port. "
                if owner and "ython" in owner else
                "On macOS, ControlCenter on port 5000 = the AirPlay Receiver "
                "(System Settings > General > AirDrop & Handoff). ")
        note = (f"Port {want} was busy. {who}{hint}"
                f"Started on port {port} instead and updated this instance's "
                f"trader config to match.")
    # single classic instance: legacy behavior (sync every config file);
    # multiple instances: only this instance's chosen config follows the port
    only_mine = [I["cfg"]] if len(instances) > 1 else None
    changed = _sync_webhook_url(port, only_mine)
    log = _webhook_log(i)
    cmd = [sys.executable, "webhook_server.py", "--instance", i, "--port", str(port)]
    if d.get("headless"):
        cmd.append("--headless")
    with open(log, "w") as lf:   # truncate so status reads only THIS run's log
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
    wh.update(proc=proc, started=time.strftime("%H:%M:%S"), port=port,
              headless=bool(d.get("headless")))
    I["port"] = port
    I["headless"] = bool(d.get("headless"))
    _save_instances()
    return jsonify(ok=True, instance=i, port=port, note=note,
                   configs_updated=changed, headless=bool(d.get("headless")))

@app.route("/api/webhook/stop", methods=["POST"])
def webhook_stop():
    i, I = _inst()
    p = I["webhook"]["proc"]
    if p is None or p.poll() is not None:
        return jsonify(error="not running"), 400
    p.terminate()
    return jsonify(ok=True)

@app.route("/api/webhook/force_stop", methods=["POST"])
def webhook_force_stop():
    """Kill EVERY executor: the panel's own child, terminal-started ones holding
    ports 5001-5011 (the normal Stop can't reach those), and any orphaned
    executor Chromium. Non-executor port holders are reported but left alone."""
    import signal
    killed, skipped = [], []
    for j, J in instances.items():
        p = J["webhook"]["proc"]
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
            killed.append(f"panel-started executor, instance {j} (pid {p.pid})")
    for port in range(5001, 5012):
        try:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        for pid in {int(x) for x in out.split()}:
            try:
                cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                cmd = ""
            if "webhook_server.py" not in cmd:
                skipped.append(f"port {port}: pid {pid} "
                               f"({cmd.split()[0].rsplit('/', 1)[-1] if cmd else '?'}) "
                               f"— not an executor, left alone")
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            deadline = time.time() + 3
            while time.time() < deadline:
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)   # still alive after 3s
            except ProcessLookupError:
                pass
            killed.append(f"executor pid {pid} (port {port})")
    # orphaned Playwright Chromium still holding the persistent profile
    r = subprocess.run(["pkill", "-f", "chrome_user_data/instance_"],
                       capture_output=True)
    if r.returncode == 0:
        killed.append("orphaned executor browser (Chromium)")
    for J in instances.values():
        J["webhook"].update(proc=None, started=None)
    return jsonify(ok=True, killed=killed, skipped=skipped)

def _proxy_state(logtext):
    """Parse the executor log for the browser's egress-IP self-check."""
    lines = logtext.splitlines() if isinstance(logtext, str) else list(logtext or [])
    for line in reversed(lines):
        if "PROXY LEAK" in line:
            return dict(state="leak", ip=None)
        if "PROXY OK" in line:
            import re
            m = re.search(r"egress IP:\s*([0-9a-fA-F:.]+)", line)
            return dict(state="ok", ip=(m.group(1) if m else None))
        if "running WITHOUT proxy" in line:
            return dict(state="none", ip=None)
    return dict(state="unknown", ip=None)


@app.route("/api/webhook/status")
def webhook_status():
    i, I = _inst()
    wh = I["webhook"]
    p = wh["proc"]
    running = p is not None and p.poll() is None
    logtext = tail(_webhook_log(i), 40)
    return jsonify(instance=i,
                   running=running, started=wh["started"] if running else None,
                   port=wh.get("port"),
                   headless=wh.get("headless", False),
                   proxy=_proxy_state(logtext),
                   log=logtext)


@app.route("/api/webhook/screenshot")
def webhook_screenshot():
    """Proxy a live screenshot from the executor (works in headless mode too)."""
    import urllib.request
    i, I = _inst()
    port = I["webhook"].get("port") or I["port"] or 5001
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/screenshot", timeout=15) as r:
            data = r.read()
        from flask import Response
        return Response(data, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return jsonify(error=f"could not get screenshot: {e}"), 502


# ---------------- autonomous campaign ----------------
@app.route("/api/campaign/status")
def campaign_status():
    name = request.args.get("name", "c1")
    d = os.path.join(OPT, "campaigns", name)
    plan_p = os.path.join(d, "plan.json")
    if not os.path.exists(plan_p):
        return jsonify(exists=False, name=name)
    try:
        plan = json.load(open(plan_p))
    except Exception:
        return jsonify(exists=False, name=name)
    specs = plan.get("specs", [])
    running_job = any(j["kind"] == "campaign" and j["proc"].poll() is None
                      for j in jobs.values())
    cur = next((s for s in specs if s["status"] == "running"), None)
    report_p = os.path.join(d, "report.md")
    return jsonify(exists=True, name=name, wave=plan.get("wave"),
                   total=len(specs),
                   done=sum(1 for s in specs if s["status"] == "done"),
                   failed=sum(1 for s in specs if s["status"] == "failed"),
                   pending=sum(1 for s in specs
                               if s["status"] in ("pending", "interrupted")),
                   current=(cur or {}).get("id"),
                   runner_running=running_job,
                   stop_requested=os.path.exists(os.path.join(d, "STOP")),
                   report=(open(report_p).read()
                           if os.path.exists(report_p) else None))

@app.route("/api/campaign/start", methods=["POST"])
def campaign_start():
    dd = request.get_json(force=True) or {}
    name = dd.get("name", "c1")
    if any(j["kind"] == "campaign" and j["proc"].poll() is None
           for j in jobs.values()):
        return jsonify(error="a campaign runner is already running"), 400
    stopf = os.path.join(OPT, "campaigns", name, "STOP")
    if os.path.exists(stopf):
        os.remove(stopf)
    jid = spawn("campaign", name,
                [sys.executable, "campaign.py", "--name", name,
                 "--procs", str(dd.get("procs", 14)),
                 "--matrix", dd.get("matrix", "c1")], OPT)
    return jsonify(ok=True, id=jid)

@app.route("/api/campaign/stop", methods=["POST"])
def campaign_stop():
    dd = request.get_json(force=True) or {}
    name = dd.get("name", "c1")
    d = os.path.join(OPT, "campaigns", name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "STOP"), "w").write(time.strftime("%H:%M:%S"))
    return jsonify(ok=True, note="the current experiment finalizes (holdout "
                                 "evaluation), then the campaign pauses; "
                                 "Start/Resume continues it")


# ---------------- instances ----------------
@app.route("/api/instances")
def instances_list():
    out = []
    for i, I in sorted(instances.items(), key=lambda kv: int(kv[0])):
        t, w = I["trader"], I["webhook"]
        out.append(dict(
            id=i, cfg=I["cfg"], port=I["port"], headless=I["headless"],
            name=I.get("name") or f"Instance {i}",
            trader_running=(t["proc"] is not None and t["proc"].poll() is None),
            trader_live=t["live"],
            webhook_running=(w["proc"] is not None and w["proc"].poll() is None)))
    return jsonify(out)

@app.route("/api/instances/rename", methods=["POST"])
def instances_rename():
    d = request.get_json(force=True)
    i = str(d.get("id", ""))
    name = (d.get("name") or "").strip()[:40]
    if i not in instances or not name:
        return jsonify(error="unknown instance or empty name"), 400
    instances[i]["name"] = name
    _save_instances()
    return jsonify(ok=True, id=i, name=name)

@app.route("/api/instances/add", methods=["POST"])
def instances_add():
    nxt = str(max((int(k) for k in instances), default=0) + 1)
    instances[nxt] = _new_instance(nxt)
    _save_instances()
    return jsonify(ok=True, id=nxt)

@app.route("/api/instances/remove", methods=["POST"])
def instances_remove():
    d = request.get_json(force=True)
    i = str(d.get("id", ""))
    if i == "1":
        return jsonify(error="instance 1 can't be removed"), 400
    I = instances.get(i)
    if not I:
        return jsonify(error=f"no instance {i}"), 404
    for kind in ("trader", "webhook"):
        p = I[kind]["proc"]
        if p is not None and p.poll() is None:
            return jsonify(error=f"instance {i}'s {kind} is running — stop it first"), 400
    del instances[i]
    _save_instances()
    return jsonify(ok=True)


# ---------------- process viewer ----------------
_PROC_KINDS = {"trader.py": "trader", "webhook_server.py": "executor",
               "optimize2_cli.py": "optimizer", "backtest_cli.py": "backtest",
               "walkforward_cli.py": "walk-forward", "refit.py": "refit",
               "update_data.py": "data-update", "panel/server.py": "panel"}

def _scan_processes():
    out = []
    try:
        ps = subprocess.run(["ps", "eww", "-axo", "pid=,lstart=,command="],
                            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return out
    known = set()   # pids the panel started itself
    pid2inst = {}   # pid -> instance id (from live handles)
    for i, I in instances.items():
        for kind in ("trader", "webhook"):
            p = I[kind]["proc"]
            if p is not None and p.poll() is None:
                known.add(p.pid)
                pid2inst[p.pid] = i
    for j in jobs.values():
        if j["proc"].poll() is None:
            known.add(j["proc"].pid)

    def _instance_of(pid, cmd):
        if pid in pid2inst:
            return pid2inst[pid]
        # fallbacks for processes the panel doesn't hold (hidden/orphaned)
        if "TRADER_CONFIG=" in cmd:
            cfg = cmd.split("TRADER_CONFIG=")[1].split()[0]
            for i, I in instances.items():
                if I.get("cfg") == cfg:
                    return i
        if "webhook_server.py" in cmd and "--instance" in cmd:
            i = cmd.split("--instance")[1].split()[0].strip()
            if i in instances:
                return i
        return None
    me = os.getpid()
    for line in ps.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        started = " ".join(parts[1:6])
        cmd = parts[6]
        if "python" not in cmd.lower() or "grep" in cmd:
            continue
        # skip shell wrappers (zsh/bash -c "... python3 panel/server.py ...")
        if cmd.split()[0].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "dash"):
            continue
        hit = next((k for k in _PROC_KINDS if k in cmd), None)
        if not hit:
            continue
        inst = _instance_of(pid, cmd)
        out.append(dict(pid=pid, kind=_PROC_KINDS[hit], started=started,
                        live=("--live" in cmd), me=(pid == me),
                        panel_child=(pid in known),
                        instance=inst,
                        instance_name=(instances[inst].get("name")
                                       if inst in instances else None),
                        cmd=cmd[:220]))
    return out

@app.route("/api/processes")
def processes():
    """EVERY strategy-lab process on this machine — including ones this panel
    didn't start (terminal-started, orphaned). The defense against hidden
    duplicate traders/executors."""
    out = _scan_processes()
    n_traders = sum(1 for p in out if p["kind"] == "trader")
    n_exec = sum(1 for p in out if p["kind"] == "executor")
    warns = []
    if n_traders > 1:
        warns.append(f"{n_traders} traders are running — they may share a state "
                     "file and corrupt each other. Kill the ones you don't want.")
    if n_exec > 1:
        warns.append(f"{n_exec} executors are running — make sure each belongs "
                     "to an instance (different --instance and port).")
    return jsonify(processes=out, warnings=warns)

@app.route("/api/processes/kill", methods=["POST"])
def processes_kill():
    """Kill one scanned process by pid. Only pids from the scan are allowed,
    and never this panel itself."""
    import signal as _sig
    d = request.get_json(force=True)
    pid = int(d.get("pid", 0))
    target = next((p for p in _scan_processes() if p["pid"] == pid), None)
    if target is None:
        return jsonify(error=f"pid {pid} is not a strategy-lab process (rescan?)"), 400
    if target["me"]:
        return jsonify(error="that's this panel — not killing myself"), 400
    try:
        os.kill(pid, _sig.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline:
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
        try:
            os.kill(pid, 0)
            os.kill(pid, _sig.SIGKILL)
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass
    except PermissionError as e:
        return jsonify(error=str(e)), 500
    # clear any panel bookkeeping that pointed at this pid
    for I in instances.values():
        for kind in ("trader", "webhook"):
            p = I[kind]["proc"]
            if p is not None and p.pid == pid:
                I[kind].update(proc=None, started=None)
    return jsonify(ok=True, killed=dict(pid=pid, kind=target["kind"]))


# ---------------- MEXC account info (read-only) ----------------
_MEXC_CACHE = {"t": 0.0, "data": None}

def _order_sources():
    """Timestamps of bot- and panel-placed orders (from the event logs), used
    to tag exchange trades by origin. Anything unmatched = manual (web/app)."""
    out = []
    try:
        with open(os.path.join(AT, "notifications.log")) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("event", "").startswith("position_") and e.get("live"):
                    out.append(("bot", time.mktime(
                        time.strptime(e["at"], "%Y-%m-%d %H:%M:%S"))))
    except Exception:
        pass
    try:
        with open(os.path.join(JOBS_DIR, "manual_orders.log")) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("ok"):
                    out.append(("panel", time.mktime(
                        time.strptime(e["t"], "%Y-%m-%d %H:%M:%S"))))
    except Exception:
        pass
    return out

def _trade_source(ts_s, sources, tol=120):
    for kind, t0 in sources:
        if abs(ts_s - t0) <= tol:
            return kind
    return "manual"

def _avg_cost(trades, holding):
    """Volume-weighted avg BUY price of the most recent buys covering the
    current holding (recent-first walk) — an estimate, labeled as such."""
    need = holding
    cost = qty = 0.0
    for t in sorted(trades, key=lambda x: -float(x.get("time") or 0)):
        if not t.get("isBuyer"):
            continue
        q = min(float(t.get("qty") or 0), need)
        cost += q * float(t.get("price") or 0)
        qty += q
        need -= q
        if need <= 1e-12:
            break
    return (cost / qty) if qty > 0 else None

@app.route("/api/mexc/account")
def mexc_account():
    """Balances + open positions for every configured API account.
    Read-only endpoints only; cached 10s to respect rate limits.
    ?force=1 bypasses the cache (the panel's refresh-now button)."""
    force = request.args.get("force") == "1"
    if not force and _MEXC_CACHE["data"] is not None \
            and time.time() - _MEXC_CACHE["t"] < 10:
        return jsonify(_MEXC_CACHE["data"])
    keys_p = os.path.join(AT, "mexc_api_keys.json")
    if not os.path.exists(keys_p):
        return jsonify([])
    try:
        keys = json.load(open(keys_p))
    except Exception as e:
        return jsonify([dict(account="?", configured=False, error=str(e))])
    accounts = keys.get("accounts") or {"mexc1": keys}
    if AT not in sys.path:
        sys.path.insert(0, AT)
    out = []
    for name, acct in sorted(accounts.items()):
        e = dict(account=name, email=acct.get("email"), configured=True)
        if "PASTE" in str(acct.get("access_key", "")):
            e["configured"] = False
            out.append(e)
            continue
        try:
            from mexc_api import MexcFuturesAPI, MexcSpotAPI
            fapi = MexcFuturesAPI(account=name)
            e["futures"] = [
                dict(currency=a.get("currency"), equity=a.get("equity"),
                     available=a.get("availableBalance"),
                     unrealized=a.get("unrealized"),
                     position_margin=a.get("positionMargin"),
                     frozen=a.get("frozenBalance"))
                for a in (fapi.assets() or [])
                if float(a.get("equity") or 0) > 0]
            e["positions"] = [
                dict(symbol=p.get("symbol"),
                     side=("LONG" if int(p.get("positionType") or 1) == 1
                           else "SHORT"),
                     vol=p.get("holdVol"), entry=p.get("openAvgPrice"),
                     lev=p.get("leverage"), liq=p.get("liquidatePrice"),
                     margin=(p.get("im") or p.get("oim")),
                     realised=p.get("realised"))
                for p in (fapi.open_positions() or [])]
            sapi = MexcSpotAPI(account=name)
            stables = ("USDT", "USDC", "USD", "DAI")
            spot = []
            spot_positions = []
            spot_trades = []
            sources = _order_sources()
            for b in sapi.account_info().get("balances", []):
                tot = float(b.get("free") or 0) + float(b.get("locked") or 0)
                if tot <= 0:
                    continue
                row = dict(asset=b.get("asset"), free=b.get("free"),
                           locked=b.get("locked"))
                if b.get("asset") not in stables and tot > 1e-4:
                    try:
                        sym = f"{b['asset']}_USDT"
                        px = sapi.ticker_price(sym)
                        row["price"] = px
                        row["usdt_value"] = tot * px
                        trades = sapi.my_trades(sym, 20) or []
                        avg = _avg_cost(trades, tot)
                        spot_positions.append(dict(
                            asset=b["asset"], qty=tot, price=px,
                            usdt_value=tot * px, avg_cost=avg,
                            pnl_pct=(100 * (px / avg - 1)) if avg else None))
                        for t in trades:
                            ts = float(t.get("time") or 0) / 1000.0
                            spot_trades.append(dict(
                                symbol=sym,
                                t=time.strftime("%m-%d %H:%M",
                                                time.localtime(ts)),
                                ts=ts,
                                side=("BUY" if t.get("isBuyer") else "SELL"),
                                qty=t.get("qty"), price=t.get("price"),
                                quote=t.get("quoteQty"),
                                source=_trade_source(ts, sources)))
                    except Exception:
                        pass
                spot.append(row)
            spot_trades.sort(key=lambda x: -x["ts"])
            e["spot"] = spot
            e["spot_positions"] = spot_positions
            e["spot_trades"] = spot_trades[:25]
            try:
                e["spot_orders"] = [
                    dict(symbol=o.get("symbol"), side=o.get("side"),
                         type=o.get("type"), qty=o.get("origQty"),
                         price=o.get("price"))
                    for o in (sapi.open_orders() or [])]
            except Exception:
                e["spot_orders"] = []
            # bot-tracked open spot positions: any spot config on this account
            # whose trader state holds a position (entry price, qty, when)
            bot = []
            for f in sorted(os.listdir(AT)):
                if not (f.startswith("config") and f.endswith(".json")):
                    continue
                try:
                    c = json.load(open(os.path.join(AT, f)))
                except Exception:
                    continue
                if c.get("mode") != "spot" \
                        or c.get("api_account", "mexc1") != name:
                    continue
                try:
                    st = json.load(open(os.path.join(
                        AT, c.get("state_file", "trader_state.json"))))
                except Exception:
                    continue
                pos = st.get("position")
                if pos:
                    bot.append(dict(config=f, symbol=c.get("symbol"),
                                    qty=pos.get("qty"),
                                    entry=pos.get("entry_price"),
                                    opened=pos.get("opened_at"),
                                    dry_run=bool(c.get("dry_run", True))))
            e["bot_spot_positions"] = bot
        except Exception as ex:
            e["error"] = str(ex)
        out.append(e)
    _MEXC_CACHE.update(t=time.time(), data=out)
    return jsonify(out)


# ---------------- manual test orders ----------------
@app.route("/api/manual", methods=["POST"])
def manual_order():
    """Relay a manual order to the MEXC executor webhook. Used to test the
    execution pipeline. The panel UI asks for typed confirmation first."""
    d = request.get_json(force=True)
    action = d.get("action")
    if action not in ("open_long", "open_short", "close_long", "close_short",
                      "close_position"):
        return jsonify(error=f"unknown action {action}"), 400
    i, I = _inst()
    # route by the instance's execution path (same venue as its badge)
    try:
        icfg = json.load(open(os.path.join(AT, I["cfg"])))
    except Exception:
        icfg = {}
    if icfg.get("execution") == "api":
        if AT not in sys.path:
            sys.path.insert(0, AT)
        from mexc_api import MexcFuturesAPI, MexcSpotAPI
        acct = icfg.get("api_account", "mexc1")
        symbol = d.get("symbol", "SOL_USDT")
        qty = float(d.get("quantity", 1))
        lev = max(1, int(d.get("leverage", 1)))
        log = os.path.join(JOBS_DIR, "manual_orders.log")
        try:
            if icfg.get("mode") == "spot":
                sapi = MexcSpotAPI(account=acct)
                px = sapi.ticker_price(symbol)
                base_qty = qty * float(icfg.get("contract_size", 0.1))
                if action == "open_long":
                    r = sapi.market_buy_quote(symbol, base_qty * px)
                elif action in ("close_long", "close_position"):
                    free = sapi.balance(symbol.split("_")[0])
                    sell = free if action == "close_position" \
                        else min(base_qty, free)
                    if sell <= 0:
                        raise RuntimeError("nothing to sell")
                    r = sapi.market_sell(symbol, sell)
                else:
                    raise RuntimeError("spot cannot short")
            else:
                fapi = MexcFuturesAPI(account=acct)
                px = MexcSpotAPI(account=acct).ticker_price(symbol)
                if action == "open_long":
                    r = fapi.open_long(symbol, qty, lev, px)
                elif action == "open_short":
                    r = fapi.open_short(symbol, qty, lev, px)
                elif action == "close_long":
                    r = fapi.place_market(symbol, 4, qty, price=px)
                elif action == "close_short":
                    r = fapi.place_market(symbol, 2, qty, price=px)
                else:
                    r = fapi.close_position(symbol, price=px)
            out = dict(ok=True, via=f"MEXC {icfg.get('mode','lev')} API",
                       account=acct, sent=dict(action=action, symbol=symbol,
                                               quantity=qty, leverage=lev),
                       response=r)
        except Exception as e:
            out = dict(ok=False, via="MEXC API", account=acct,
                       sent=dict(action=action, symbol=symbol, quantity=qty),
                       error=f"{type(e).__name__}: {e}")
        with open(log, "a") as lf:
            lf.write(json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"),
                                     **out), default=str) + "\n")
        return jsonify(out), (200 if out.get("ok") else 502)
    port = I["webhook"].get("port") or I["port"] or 5001
    url = d.get("url") or f"http://127.0.0.1:{port}/webhook"
    payload = dict(action=action, symbol=d.get("symbol", "SOL_USDT"))
    if action.startswith("open"):
        payload["leverage"] = int(d.get("leverage", 1))
        payload["quantity"] = int(d.get("quantity", 1))
    elif action.startswith("close") and action != "close_position":
        payload["quantity"] = int(d.get("quantity", 1))
    import requests as _rq
    log = os.path.join(JOBS_DIR, "manual_orders.log")
    try:
        r = _rq.post(url, json=payload, timeout=120)
        out = dict(ok=r.ok, status=r.status_code, sent=payload, url=url)
        try:
            out["response"] = r.json()
        except Exception:
            out["response"] = r.text[:500]
    except Exception as e:
        out = dict(ok=False, sent=payload, url=url,
                   error=f"{type(e).__name__}: {e} — is the executor running?")
    with open(log, "a") as lf:
        lf.write(json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), **out),
                            default=str) + "\n")
    return jsonify(out), (200 if out.get("ok") else 502)


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
                best = os.path.exists(os.path.join(runs_dir, d, "marked_best"))
                try:
                    rp = os.path.join(runs_dir, d, "rating")
                    rating = int(open(rp).read().strip()) if os.path.exists(rp) else 0
                except Exception:
                    rating = 0
                out.append(dict(path=p, label=f"optimizer run: {d}", kind="run",
                                run=d, best=best, rating=rating))
    # starred/rated first, like on the Optimize page
    out.sort(key=lambda e: (-(e.get("best") and 1 or 0), -(e.get("rating") or 0)))
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
    if best.get("strategy") == "metax" \
            or (best.get("cand") or {}).get("strategy") == "metax":
        # ROUTER adopt: embed the resolved component configs so the trader
        # candidate is self-contained (components live in strategy_metax)
        sys.path.insert(0, AT)
        from strategy_metax import resolve_candidate
        try:
            rc = resolve_candidate(best, os.path.join(OPT, "runs"))
        except Exception as e:
            return jsonify(error=f"router adopt failed: {e}"), 400
        bad = {rc["components"][a]["strategy"] for a in rc["assign"]
               if a is not None and a >= 0} \
            - {"macdx", "scalpx", "scalpx2", "v7", "prime7", "prime", "v6"}
        if bad:
            return jsonify(error=(
                f"router assigns component families with no live runner: "
                f"{sorted(bad)}")), 400
        tname = os.path.basename(d.get("target", "config.json"))
        import re as _re
        if not _re.fullmatch(r"config[A-Za-z0-9_.\-]*\.json", tname):
            return jsonify(error=f"bad target name '{tname}'"), 400
        target = os.path.join(AT, tname)
        created = False
        if not os.path.exists(target):
            # per-router config named after the run: skeleton from config.json
            # with its OWN state/log files, starting as a dry-run
            suffix = tname[len("config_"):-len(".json")] if \
                tname.startswith("config_") else "router"
            cfg = json.load(open(os.path.join(AT, "config.json")))
            cfg.pop("candidate", None)
            cfg.pop("adopted_from", None)
            cfg.update(dry_run=True,
                       state_file=f"trader_state_{suffix}.json",
                       log_file=f"trader_{suffix}.log")
            created = True
        else:
            cfg = json.load(open(target))
            if best.get("mode") and cfg.get("mode") \
                    and best["mode"] != cfg["mode"] and not d.get("force"):
                return jsonify(error=(
                    f"This is a {best['mode']}-mode router but {tname} is the "
                    f"{cfg['mode']}-mode config.")), 400
            import shutil
            shutil.copy(target, target + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
        cfg["candidate"] = rc
        cfg["mode"] = best["mode"]
        cfg["method"] = best.get("method", "vol3")
        cfg["adopted_from"] = dict(source=src, at=time.strftime("%Y-%m-%d %H:%M"))
        json.dump(cfg, open(target, "w"), indent=1)
        return jsonify(ok=True, target=tname, created=created,
                       note=(f"ROUTER adopted into {tname}"
                             + (" (new file — own state/log, dry-run)."
                                if created else " (backup kept).")
                             + " Run test_parity_metax.py, then a dry-run "
                               "soak before LIVE."))
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
                        execution=c.get("execution", "browser"),
                        api_account=c.get("api_account", "mexc1"),
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
               "emergency_exit_adverse", "dry_run", "symbol",
               "api_account", "execution"}
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

@app.route("/api/jobs/optimize", methods=["POST"])
@app.route("/api/jobs/optimize2", methods=["POST"])
def job_optimize2():
    """One optimizer for every strategy (v7 / prime / v6 / scalpx)."""
    d = request.get_json(force=True)
    name = _safe_name(d.get("name")) or f"opt2_{time.strftime('%m%d_%H%M')}"
    cmd = [sys.executable, "optimize2_cli.py",
           "--strategy", d.get("strategy", "v7"),
           "--algo", d.get("algo", "genetic"),
           "--mode", d.get("mode", "lev"), "--method", d.get("method", "vol3"),
           "--procs", str(d.get("procs", 4)), "--batch", str(d.get("batch", 100)),
           "--name", name]
    if d.get("single_set"): cmd += ["--single-set"]
    if d.get("hours"): cmd += ["--hours", str(d["hours"])]
    if d.get("total"): cmd += ["--total", str(d["total"])]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    if d.get("max_dd"): cmd += ["--max-dd", str(d["max_dd"])]
    if d.get("holdout_days"): cmd += ["--holdout-days", str(d["holdout_days"])]
    if d.get("max_hold_days"): cmd += ["--max-hold-days", str(d["max_hold_days"])]
    if d.get("gap_mode"): cmd += ["--gap-mode", d["gap_mode"]]
    if d.get("lockbox"): cmd += ["--lockbox", d["lockbox"]]
    if d.get("scoring"): cmd += ["--scoring", d["scoring"]]
    if d.get("stop_score") is not None and d.get("stop_score") != "":
        cmd += ["--stop-score", str(d["stop_score"])]
    if d.get("resume_from"): cmd += ["--resume-from", d["resume_from"]]
    if d.get("merge_mode"): cmd += ["--merge-mode", d["merge_mode"]]
    if d.get("seed_cand"):
        run_dir = os.path.join(OPT, "runs", name)
        os.makedirs(run_dir, exist_ok=True)
        json.dump(d["seed_cand"], open(os.path.join(run_dir, "seed_cand.json"), "w"))
    if d.get("anchor_cand"):
        run_dir = os.path.join(OPT, "runs", name)
        os.makedirs(run_dir, exist_ok=True)
        json.dump(d["anchor_cand"], open(os.path.join(run_dir, "anchor_cand.json"), "w"))
        cmd += ["--anchor", "file"]
    elif d.get("anchor") == "defaults":
        cmd += ["--anchor", "defaults"]
    if d.get("anchor_strength"):
        cmd += ["--anchor-strength", str(d["anchor_strength"])]
    return jsonify(id=spawn("optimize-v2", name, cmd, OPT))

@app.route("/api/jobs/ai_suggest", methods=["POST"])
def job_ai():
    d = request.get_json(force=True)
    cmd = [sys.executable, "ai_advisor.py", "--run", d["run"],
           "--n", str(d.get("n", 12))]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    if d.get("max_dd"): cmd += ["--max-dd", str(d["max_dd"])]
    if d.get("holdout_days"): cmd += ["--holdout-days", str(d["holdout_days"])]
    if d.get("max_hold_days"): cmd += ["--max-hold-days", str(d["max_hold_days"])]
    if d.get("gap_mode"): cmd += ["--gap-mode", d["gap_mode"]]
    return jsonify(id=spawn("ai-advisor", d["run"], cmd, OPT))

@app.route("/api/ai_key_status")
def ai_key():
    ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    env_path = os.path.join(REPO, ".env")
    if not ok and os.path.exists(env_path):
        ok = "ANTHROPIC_API_KEY" in open(env_path).read()
    return jsonify(configured=ok)

def _scrub(o):
    """NaN/inf are valid for Python's json but not for browsers — replace with null."""
    import math
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub(v) for v in o]
    return o

@app.route("/api/runs2")
def runs2():
    out = []
    runs_dir = os.path.join(OPT, "runs")
    running_names = {j.get("name") for j in jobs.values()
                     if j["proc"].poll() is None and "optimize" in j.get("kind", "")}
    # which runs are being TRADED right now: a running instance whose config
    # was adopted from that run
    import re as _re
    trading_map = {}
    for _i, _I in instances.items():
        _t = _I["trader"]
        if _t["proc"] is None or _t["proc"].poll() is not None:
            continue
        try:
            _c = json.load(open(os.path.join(AT, _t["config"] or _I["cfg"])))
        except Exception:
            continue
        _m = _re.search(r"/runs/([^/]+)/", (_c.get("adopted_from") or {})
                        .get("source", ""))
        if _m:
            trading_map[_m.group(1)] = dict(
                instance=_i, name=_I.get("name") or f"Instance {_i}",
                live=bool(_t["live"]))
    for d in sorted(os.listdir(runs_dir)):
        pool_p = os.path.join(runs_dir, d, "pool2.json")
        if not os.path.exists(pool_p):
            pool_p = os.path.join(runs_dir, d, "pool.json")   # legacy v6 runs
        best_p = os.path.join(runs_dir, d, "best_config.json")
        if not os.path.exists(pool_p):
            continue
        rating_p = os.path.join(runs_dir, d, "rating")
        try:
            rating = int(open(rating_p).read().strip()) if os.path.exists(rating_p) else 0
        except Exception:
            rating = 0
        launches = []
        launch_p = os.path.join(runs_dir, d, "launch.json")
        if os.path.exists(launch_p):
            try:
                launches = json.load(open(launch_p))
            except Exception:
                launches = []
        wf = None
        wf_p = os.path.join(runs_dir, d, "walkforward.json")
        if os.path.exists(wf_p):
            try:
                wf = json.load(open(wf_p))
            except Exception:
                wf = None
        e = dict(name=d, run=f"runs/{d}",
                 best=os.path.exists(os.path.join(runs_dir, d, "marked_best")),
                 rating=rating, launches=launches, walkforward=wf,
                 trading=trading_map.get(d),
                 running=(d in running_names),
                 last_run=time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(os.path.getmtime(pool_p))))
        if d in running_names:
            prog_p = os.path.join(runs_dir, d, "progress.json")
            if os.path.exists(prog_p):
                try:
                    pr = json.load(open(prog_p))
                    if time.time() - pr.get("updated", 0) < 900:
                        e["progress"] = dict(
                            pct=pr.get("pct"), eta_s=pr.get("eta_s"),
                            evaluated_session=pr.get("evaluated_session"),
                            budget=pr.get("budget"), budget_type=pr.get("budget_type"),
                            phase=pr.get("phase"))
                except Exception:
                    pass
        try:
            pd_ = json.load(open(pool_p))
            e["evaluated"] = pd_.get("evaluated")
            e["runtime_s"] = pd_.get("runtime_s")
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
                e["mode"] = bc.get("mode", e.get("mode"))
                e["no_survivors"] = ("cand" in bc and bc.get("cand") is None)
                e["holdout"] = bc.get("holdout")
                e["holdout_best"] = bc.get("holdout_best")
                e["holdout_top10"] = bc.get("holdout_top10")
                e["holdout_scan"] = bc.get("holdout_scan")
                e["holdout_survivors"] = bc.get("holdout_survivors")
                e["holdout_days"] = bc.get("holdout_days")
                e["train_end"] = bc.get("train_end")
                e["algo"] = bc.get("algo")
                e["per_regime"] = bc.get("per_regime")
                e["max_dd"] = bc.get("max_dd")
                e["max_hold_days"] = bc.get("max_hold_days")
                e["gap_mode"] = bc.get("gap_mode")
                e["lockbox"] = bc.get("lockbox")
                e["scoring"] = bc.get("scoring")
                e["anchor"] = bc.get("anchor")
                e["anchor_strength"] = bc.get("anchor_strength")
                e["crossfit"] = bc.get("crossfit")
                e["winner_origin"] = bc.get("winner_origin")
                fp = os.path.join(runs_dir, d, "backtest_flags.json")
                if os.path.exists(fp):
                    try:
                        e["backtest_flags"] = list(json.load(open(fp)).values())
                    except Exception:
                        pass
                e["seed_holdout"] = bc.get("seed_holdout")
                if bc.get("cand") is not None:
                    e["best_config"] = f"runs/{d}/best_config.json"
                if os.path.exists(os.path.join(runs_dir, d, "holdout_best_config.json")):
                    e["holdout_best_config"] = f"runs/{d}/holdout_best_config.json"
                e["strategy"] = bc.get("strategy", e.get("strategy", "v7"))
                e["finished"] = bc.get("generated")
            except Exception:
                pass
        if not e.get("strategy"):  # last-resort inference so the UI never shows '?'
            nm = d.lower()
            e["strategy"] = "scalpx" if "scalp" in nm else \
                ("v7" if pool_p.endswith("pool2.json") else "v6")
        out.append(e)
    return jsonify(_scrub(out))

@app.route("/api/runs2/rename", methods=["POST"])
def runs2_rename():
    """Rename a run directory; associated backtest entries follow the new name."""
    d = request.get_json(force=True)
    old = os.path.basename(d.get("old", ""))
    new = _safe_name(d.get("new", ""))
    if not old or not new:
        return jsonify(error="both old and new names are required"), 400
    if new == old:
        return jsonify(ok=True, renamed_backtests=0)
    src_dir = os.path.join(OPT, "runs", old)
    dst_dir = os.path.join(OPT, "runs", new)
    if not os.path.isdir(src_dir):
        return jsonify(error=f"run '{old}' not found"), 404
    if os.path.exists(dst_dir):
        return jsonify(error=f"a run named '{new}' already exists"), 400
    for jid, j in jobs.items():
        if j["proc"].poll() is None and j.get("name") == old:
            return jsonify(error=f"a job is still running for '{old}' — stop it first"), 400
    os.rename(src_dir, dst_dir)
    # rename associated backtest entries (published as <run>, <run>_full, <run>_HOLDOUT, ...)
    n = 0
    bt_path = os.path.join(DASH, "backtests.js")
    if os.path.exists(bt_path):
        txt = open(bt_path).read()
        entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
        for e in entries:
            nm = e.get("name", "")
            if nm == old or nm.startswith(old + "_"):
                e["name"] = new + nm[len(old):]
                n += 1
        if n:
            with open(bt_path, "w") as f:
                f.write("window.BACKTESTS = ")
                json.dump(entries, f, default=float)
                f.write(";")
    return jsonify(ok=True, name=new, renamed_backtests=n)


@app.route("/api/runs2/delete", methods=["POST"])
def runs2_delete():
    """Delete an optimizer run directory (pool, configs, caches for that run)."""
    name = os.path.basename(request.get_json(force=True).get("name", ""))
    if not name or name.startswith("."):
        return jsonify(error="invalid run name"), 400
    if name == "_backtest_tmp":
        return jsonify(error="_backtest_tmp is a shared working dir; not deletable"), 400
    run_dir = os.path.join(OPT, "runs", name)
    if not os.path.isdir(run_dir):
        return jsonify(error=f"run '{name}' not found"), 404
    # refuse if a job is still running for this run
    for jid, j in jobs.items():
        if j["proc"].poll() is None and j.get("name") == name:
            return jsonify(error=f"a job is still running for '{name}' — stop it first"), 400
    import shutil
    try:
        shutil.rmtree(run_dir)
    except Exception as e:
        return jsonify(error=f"delete failed: {e}"), 500
    return jsonify(ok=True, deleted=name)


@app.route("/api/runs2/mark", methods=["POST"])
def runs2_mark():
    """Toggle the 'best' star on an optimizer run (marker file inside the run dir,
    so it follows renames and disappears with deletes)."""
    d = request.get_json(force=True)
    name = os.path.basename(d.get("name", ""))
    best = bool(d.get("best"))
    run_dir = os.path.join(OPT, "runs", name)
    if not name or not os.path.isdir(run_dir):
        return jsonify(error=f"run '{name}' not found"), 404
    marker = os.path.join(run_dir, "marked_best")
    if best:
        open(marker, "w").write(time.strftime("%Y-%m-%d %H:%M"))
    elif os.path.exists(marker):
        os.remove(marker)
    return jsonify(ok=True, name=name, best=best)


@app.route("/api/runs2/rate", methods=["POST"])
def runs2_rate():
    """Set a 1-3 star rating on an optimizer run (0 clears). Stored as a marker
    file inside the run dir, like marked_best."""
    d = request.get_json(force=True)
    name = os.path.basename(d.get("name", ""))
    rating = max(0, min(3, int(d.get("rating", 0))))
    run_dir = os.path.join(OPT, "runs", name)
    if not name or not os.path.isdir(run_dir):
        return jsonify(error=f"run '{name}' not found"), 404
    marker = os.path.join(run_dir, "rating")
    if rating:
        open(marker, "w").write(str(rating))
    elif os.path.exists(marker):
        os.remove(marker)
    return jsonify(ok=True, name=name, rating=rating)


@app.route("/api/backtests/mark", methods=["POST"])
def backtests_mark():
    """Toggle the 'best' star on a published backtest entry."""
    d = request.get_json(force=True)
    name = d.get("name")
    best = bool(d.get("best"))
    path = os.path.join(DASH, "backtests.js")
    txt = open(path).read()
    entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
    hit = False
    for e in entries:
        if e.get("name") == name:
            if best:
                e["best"] = True
            else:
                e.pop("best", None)
            hit = True
    if not hit:
        return jsonify(error=f"backtest '{name}' not found"), 404
    with open(path, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")
    return jsonify(ok=True, name=name, best=best)


@app.route("/api/backtests/rate", methods=["POST"])
def backtests_rate():
    """Set a 1-3 star rating on a published backtest entry (0 clears)."""
    d = request.get_json(force=True)
    name = d.get("name")
    rating = max(0, min(3, int(d.get("rating", 0))))
    path = os.path.join(DASH, "backtests.js")
    txt = open(path).read()
    entries = json.loads(txt[txt.index("=") + 1:].rstrip().rstrip(";"))
    hit = False
    for e in entries:
        if e.get("name") == name:
            if rating:
                e["rating"] = rating
            else:
                e.pop("rating", None)
            hit = True
    if not hit:
        return jsonify(error=f"backtest '{name}' not found"), 404
    with open(path, "w") as f:
        f.write("window.BACKTESTS = ")
        json.dump(entries, f, default=float)
        f.write(";")
    return jsonify(ok=True, name=name, rating=rating)


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
