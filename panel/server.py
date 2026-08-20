#!/usr/bin/env python3
"""Local control panel for the adaptive trader + optimizer.

  pip install flask
  python3 panel/server.py            # http://127.0.0.1:8800

Lets you: watch live trader status, start/stop it (dry-run or LIVE),
launch backtests / optimizations / walk-forwards / refits with live logs,
and open the results dashboard. Everything runs as local subprocesses of
this server — closing the server stops the trader too.
"""
import json, os, re, signal, subprocess, sys, threading, time, uuid
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
        d["_resume"] = m.get("trader") or {}   # saved run/live intent (reboot)
        d["err_cleared"] = m.get("err_cleared") or ""
        out[str(i)] = d
    if not out:                       # empty file only — never re-seed a
        out["1"] = _new_instance(1)   # DELETED instance 1 on restart
    return out

instances = _load_instances()

def _save_instances():
    # trader run/live intent is persisted so a REBOOTED machine can resume
    # each trader in the state it was in (dry stays dry, LIVE comes back LIVE)
    json.dump({i: dict(cfg=d.get("cfg"), port=d.get("port"),
                       headless=d.get("headless", False),
                       name=d.get("name"),
                       err_cleared=d.get("err_cleared") or "",
                       trader=dict(
                           should_run=(not d.get("stopped_by_user")
                                       and d["trader"]["proc"] is not None
                                       and d["trader"]["proc"].poll() is None),
                           live=bool(d["trader"].get("live")),
                           config=d["trader"].get("config") or d.get("cfg")))
               for i, d in instances.items()},
              open(INSTANCES_FILE, "w"), indent=1)

def _inst():
    """Resolve the instance addressed by the current request (default '1').
    UNKNOWN ids are a 404 — they used to be auto-created, which meant any
    stale browser tab polling a DELETED instance silently resurrected it."""
    i = str(request.args.get("instance")
            or (request.get_json(silent=True) or {}).get("instance") or "1")
    if i not in instances:
        from flask import abort
        abort(404, description=f"no instance {i}")
    return i, instances[i]

def _webhook_log(i):
    return os.path.join(JOBS_DIR, "webhook_server.log" if i == "1"
                        else f"webhook_server_i{i}.log")

# instance-1 aliases: any legacy code path keeps working (instance 1 may be
# DELETED now — fall back to the lowest existing instance)
_alias = instances.get("1") or instances[min(instances, key=int)]
trader = _alias["trader"]
webhook = _alias["webhook"]


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


def _resume_traders():
    """After a machine REBOOT the watchdog brings the panel back, but the
    traders it used to run are gone. Restart every trader whose persisted
    state says it should be running — in the SAME live/dry state it was in.
    After a mere panel restart the orphan re-adopt above finds the processes
    still alive and this is a no-op."""
    for i, I in sorted(instances.items(), key=lambda kv: int(kv[0])):
        want = I.pop("_resume", None) or {}
        t = I["trader"]
        if not want.get("should_run"):
            continue
        if t["proc"] is not None and t["proc"].poll() is None:
            continue                      # re-adopted alive — nothing to do
        cfg_name = want.get("config") or I.get("cfg") or "config.json"
        live = bool(want.get("live"))
        cmd = [sys.executable, "trader.py"] + (["--live"] if live else [])
        log = os.path.join(JOBS_DIR, "trader_stdout.log" if i == "1"
                           else f"trader_stdout_i{i}.log")
        try:
            with open(log, "a") as lf:
                proc = subprocess.Popen(
                    cmd, cwd=AT, stdout=lf, stderr=subprocess.STDOUT,
                    env={**os.environ, "TRADER_CONFIG": cfg_name})
            t.update(proc=proc, config=cfg_name, live=live,
                     started=time.strftime("%Y-%m-%d %H:%M:%S"))
            print(f"RESUMED trader for instance {i} ({cfg_name}, "
                  f"{'LIVE' if live else 'dry'})", flush=True)
        except Exception as e:
            print(f"trader resume FAILED for instance {i}: {e}", flush=True)


_resume_traders()


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


def spawn(kind, name, cmd, cwd, jid=None):
    jid = jid or f"{kind}_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
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
    resp = send_from_directory(DASH, p)
    # pages and their inline JS change often — force revalidation so stale
    # cached pages can't hide new features ("I don't see the badges")
    if p.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------- trader ----------------
_PX_CACHE = {}

def _pub_price(symbol, mode):
    """Public last price (10s cache) for the position hero banner."""
    if not symbol:
        return None
    import requests as _rq
    k = (symbol, mode)
    v = _PX_CACHE.get(k)
    if v and time.time() - v[1] < 10:
        return v[0]
    px = None
    try:
        if mode == "spot":
            r = _rq.get("https://api.mexc.com/api/v3/ticker/price",
                        params={"symbol": symbol.replace("_", "")}, timeout=5)
            px = float(r.json()["price"])
        else:
            r = _rq.get("https://contract.mexc.com/api/v1/contract/ticker",
                        params={"symbol": symbol}, timeout=5)
            px = float(r.json()["data"]["lastPrice"])
    except Exception:
        pass
    _PX_CACHE[k] = (px, time.time())
    return px

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
    _pos = state.get("position") or {}
    _sym = _pos.get("symbol") or cfg.get("symbol")
    _ovp = os.path.join(AT, ".override_" + os.path.basename(
        cfg.get("state_file", "trader_state.json")))
    _ov = None
    if _pos and os.path.exists(_ovp):
        try:
            _o = json.load(open(_ovp))
            if _o.get("pos_key") == str(_pos.get("opened_at")):
                _ov = _o
        except Exception:
            pass
    return jsonify(dict(
        symbol=_sym,
        price=(_pub_price(_sym, cfg.get("mode")) if _pos else None),
        override=_ov,
        # SPOT quantities are already BASE UNITS (8548 DOGE), so the size
        # multiplier is 1. contract_sizes holds FUTURES sizes (DOGE=100) and
        # applying it to a spot position inflated P&L 100x.
        contract_size=(1.0 if cfg.get("mode") == "spot" else
                       ((cfg.get("contract_sizes") or {}).get(_sym)
                        or cfg.get("contract_size"))),
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
    I.pop("stopped_by_user", None)
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
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    # a DELIBERATE stop: clear the live flag and record the intent explicitly.
    # should_run used to be inferred from proc.poll(), which still read
    # "running" right after terminate() — so a stopped LIVE trader came back
    # LIVE on the next reboot, with no confirm.
    I["trader"].update(live=False)
    I["stopped_by_user"] = True
    _save_instances()
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
    with _OPTQ_LOCK:
        for i, (jid, n, _) in enumerate(OPTQ["items"]):
            out.append(dict(id=jid, kind="optimize-v2", name=n,
                            cmd="(queued)", started="—", stopping=False,
                            status=f"queued #{i+1} — starts when the running "
                                   f"search finishes", log=[]))
    return jsonify(out)

def _safe_name(n):
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", n or "")

@app.route("/api/oos_map")
def oos_map():
    """Every run's out-of-sample verdicts, from the runs2 cache:
    {run: {tb: verdict, ob: verdict}} — tb = train-best config on its
    holdout, ob = OOS-best config on its holdout. Verdicts: 'P' positive &
    non-liq, 'N' non-liq but <=0, 'L' liquidated, null = never OOS-tested."""
    _r2c_load()

    def _v(h):
        if not h:
            return None
        if h.get("liq"):
            return "L"
        return "P" if (h.get("growth") or 0) > 0 else "N"

    out = {}
    for d_, (key_, st) in _R2C["d"].items():
        tb = _v(st.get("holdout"))
        ob = _v(((st.get("holdout_best") or {}).get("holdout")))
        if tb or ob:
            out[d_] = dict(tb=tb, ob=ob)
    return jsonify(out)


# config.json / config_spot.json are the TEMPLATES every adopt clones from.
# They are not instance configs, and deleting them (the delete button happily
# matched them) made every adopt 500 with FileNotFoundError. Never open them
# bare — go through this, which falls back to any other config of the right
# mode and finally to a built-in minimum.
_BASE_FALLBACK = dict(mode="lev", execution="api", api_account="mexc1",
                      symbol="SOL_USDT", equity_usdt=100.0, poll_seconds=3,
                      dry_run=True, contract_size=0.1,
                      state_file="trader_state.json", log_file="trader.log")


def _template_cfg(mode="lev"):
    names = (["config_spot.json", "config.json"] if mode == "spot"
             else ["config.json", "config_spot.json"])
    for n in names:
        p = os.path.join(AT, n)
        if os.path.isfile(p):
            try:
                return json.load(open(p))
            except Exception:
                pass
    for n in sorted(os.listdir(AT)):          # any surviving trader config
        if re.fullmatch(r"config[A-Za-z0-9_.\-]*\.json", n):
            try:
                c = json.load(open(os.path.join(AT, n)))
                if c.get("mode") == mode:
                    c.pop("candidate", None)
                    return c
            except Exception:
                pass
    return dict(_BASE_FALLBACK, mode=mode,
                **({"contract_size": 1.0} if mode == "spot" else {}))


@app.route("/api/router_components")
def router_components():
    """The component RUNS behind one router/combo run, read from its
    optimizer/runs/<run>/best_config.json. Lets the backtests page filter the
    list down to the backtests a chosen router was actually built from. One
    file read per call — never scan the whole runs tree here, that is what
    pinned a core the last time."""
    run = os.path.basename(request.args.get("run") or "")
    if not run or not re.fullmatch(r"[A-Za-z0-9_.\-]+", run):
        return jsonify(error="bad run name"), 400
    path = os.path.join(OPT, "runs", run, "best_config.json")
    if not os.path.isfile(path):
        return jsonify(error=f"no best_config.json for run '{run}'"), 404
    try:
        b = json.load(open(path))
    except Exception as e:
        return jsonify(error=f"unreadable: {e}"), 500
    comps = ((b.get("cand") or {}).get("components") or [])
    out = [dict(run=c.get("run"), strategy=c.get("strategy"),
                pair=c.get("pair"), timeframe=c.get("timeframe"),
                file=c.get("file"))
           for c in comps if c.get("run")]
    return jsonify(run=run, n=len(out), components=out)


@app.route("/api/gauntlet")
def gauntlet_api():
    """Holdout-gauntlet verdicts for sweep families: run name ->
    {key, have, passed, full}. A leg passes when its winner (train-best or
    OOS-best) survived its own out-of-sample holdout without liquidating."""
    pat = re.compile(r"^(.+)_(hA|hB|hBtw|hOut|hAlt\d+)((?:_(?:cls|wor|und))?)$")
    runs_dir = os.path.join(OPT, "runs")
    running_names = {j.get("name") for j in jobs.values()
                     if j["proc"].poll() is None and "optimize" in j.get("kind", "")}
    groups, names = {}, {}
    for d in os.listdir(runs_dir):
        m = pat.match(d)
        if not m:
            continue
        key = m.group(1) + (m.group(3) or "")
        typ = "hAlt" if m.group(2).startswith("hAlt") else m.group(2)
        if d in running_names:
            st = "pending"
        else:
            st = "fail"
            bp = os.path.join(runs_dir, d, "best_config.json")
            if os.path.exists(bp):
                try:
                    b = json.load(open(bp))
                    h = b.get("holdout") or {}
                    hb = ((b.get("holdout_best") or {}).get("holdout") or {})
                    if b.get("cand") is not None and                             ((h and not h.get("liq")) or (hb and not hb.get("liq"))):
                        st = "pass"
                except Exception:
                    pass
        groups.setdefault(key, {})[typ] = st
        names[d] = key
    out, types = {}, ["hA", "hB", "hBtw", "hOut", "hAlt"]
    for d, key in names.items():
        g = groups[key]
        have = [t for t in types if t in g]
        passed = [t for t in types if g.get(t) == "pass"]
        out[d] = dict(key=key, have=len(have), passed=len(passed),
                      full=(len(have) == 5 and "pending" not in g.values()))
    return jsonify(out)


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

@app.route("/api/jobs/<jid>/dismiss", methods=["POST"])
def job_dismiss(jid):
    """Remove a FINISHED job from the list (its log file goes too)."""
    j = jobs.get(jid)
    if not j:
        return jsonify(ok=True, note="already gone")
    if j["proc"].poll() is None:
        return jsonify(error="job is still running — stop it first"), 400
    jobs.pop(jid, None)
    try:
        os.remove(j["log"])
    except OSError:
        pass
    return jsonify(ok=True)


@app.route("/api/jobs/clear_done", methods=["POST"])
def jobs_clear_done():
    """Sweep every finished job out of the list."""
    n = 0
    for jid in [k for k, j in jobs.items() if j["proc"].poll() is not None]:
        j = jobs.pop(jid)
        try:
            os.remove(j["log"])
        except OSError:
            pass
        n += 1
    return jsonify(ok=True, cleared=n)


@app.route("/api/jobs/<jid>/stop", methods=["POST"])
def job_stop(jid):
    import signal as _sig
    with _OPTQ_LOCK:
        for it in list(OPTQ["items"]):
            if it[0] == jid:
                OPTQ["items"].remove(it)
                _optq_save()
                return jsonify(ok=True, note="removed from the queue")
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

_BT_REFRESH = {}

@app.route("/api/bt_refresh/heartbeat", methods=["POST"])
def bt_refresh_hb():
    d = request.get_json(force=True)
    _BT_REFRESH[d.get("worker") or "?"] = dict(
        done=int(d.get("done") or 0), total=int(d.get("total") or 0),
        status=d.get("status") or "running",
        at=time.strftime("%H:%M:%S"))
    return jsonify(ok=True)

@app.route("/api/bt_refresh/progress")
def bt_refresh_progress():
    return jsonify(_BT_REFRESH)

@app.route("/api/backtests/submit", methods=["POST"])
def bt_submit():
    """Fast drop-off for refreshed backtest entries (list of entry dicts):
    saved to an incoming dir; the background ingester folds them into
    backtests.js in one parse per minute (the file is ~300MB — per-entry
    rewrites would thrash)."""
    ents = request.get_json(force=True)
    if isinstance(ents, dict):
        ents = [ents]
    if not all(isinstance(e, dict) and e.get("name") for e in ents):
        return jsonify(error="entries need names"), 400
    if len(ents) <= 3:
        # small submissions (single re-runs) fold synchronously so the page
        # sees the refreshed entry as soon as the job finishes
        _bt_fold({e["name"]: e for e in ents})
        return jsonify(ok=True, folded=len(ents))
    d = os.path.join(REPO, "dashboard", "bt_incoming")
    os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, f"{int(time.time()*1000)}_{os.getpid()}.json")
    json.dump(ents, open(fn, "w"), default=float)
    return jsonify(ok=True, queued=len(ents))


def _bt_fold(pend):
    """Replace-by-name a batch of entries in backtests.js (shared lock)."""
    import fcntl
    p = os.path.join(REPO, "dashboard", "backtests.js")
    with open(p + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(p).read()
        entries = json.JSONDecoder().raw_decode(
            txt[txt.index("=") + 1:].lstrip())[0]
        entries = [x for x in entries
                   if x.get("name") not in pend] + list(pend.values())
        tmp = p + f".tmp{os.getpid()}"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS = ")
            json.dump(entries, f, default=float)
            f.write(";")
        os.replace(tmp, p)


@app.route("/api/backtests/rerun", methods=["POST"])
def bt_rerun():
    """Re-run ONE published entry on the most recently downloaded market
    data and replace it in the store (same machinery as the bulk refresh).
    Runs as a panel job so it shows under Jobs and the page auto-reloads."""
    name = (request.get_json(force=True) or {}).get("name") or ""
    p = os.path.join(REPO, "dashboard", "backtests.js")
    txt = open(p).read()
    entries = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
    e = next((x for x in entries if x.get("name") == name), None)
    if not e:
        return jsonify(error=f"no entry '{name}'"), 404
    _SUF = ["_oosbest_full", "_best_full", "_full"]
    _GEN = {"_oosbest_full": "holdout_best_config.json",
            "_best_full": "best_config.json", "_full": "best_config.json"}
    genome = e.get("config")
    if not genome:
        for s in _SUF:
            if name.endswith(s):
                r = name[:-len(s)]
                gp = os.path.join(OPT, "runs", r, _GEN[s])
                if not os.path.exists(gp):
                    gp = os.path.join(OPT, "runs", r, "best_config.json")
                if os.path.exists(gp):
                    genome = json.load(open(gp))
                break
    if not genome:
        return jsonify(error="no genome for this entry — router combos and "
                             "quick backtests must be re-run from their own "
                             "flows (fcfsx: Re-run on the Optimize page)"), 400
    item = dict(name=name, pair=e.get("pair"),
                timeframe=str(e.get("timeframe") or ""), mode=e.get("mode"),
                method=e.get("method"), strategy=e.get("strategy"),
                kind=e.get("kind"), opt=e.get("opt"), genome=genome)
    sd = os.path.join(REPO, "dashboard", "bt_refresh")
    os.makedirs(sd, exist_ok=True)
    shard = os.path.join(sd, f"one_{int(time.time())}_{uuid.uuid4().hex[:4]}.json")
    json.dump([item], open(shard, "w"))
    jid = spawn("backtest", f"re-run {name} on current data",
                [sys.executable,
                 os.path.join(REPO, "scripts", "refresh_backtests_worker.py"),
                 "--shard", shard, "--procs", "1",
                 "--hub", "http://localhost:8800"], REPO)
    return jsonify(ok=True, id=jid)


def _bt_ingester():
    """Every 60s: fold all pending bt_incoming files into backtests.js
    (replace-by-name) under the shared lock, in a single parse+write."""
    import fcntl
    d = os.path.join(REPO, "dashboard", "bt_incoming")
    p = os.path.join(REPO, "dashboard", "backtests.js")
    while True:
        time.sleep(60)
        try:
            files = sorted(os.listdir(d)) if os.path.isdir(d) else []
            if not files:
                continue
            pend = {}
            for fn in files:
                try:
                    for e in json.load(open(os.path.join(d, fn))):
                        pend[e["name"]] = e
                except Exception:
                    pass
            if not pend:
                continue
            with open(p + ".lock", "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                txt = open(p).read()
                entries = json.JSONDecoder().raw_decode(
                    txt[txt.index("=") + 1:].lstrip())[0]
                entries = [x for x in entries
                           if x.get("name") not in pend] + list(pend.values())
                tmp = p + f".tmp{os.getpid()}"
                with open(tmp, "w") as f:
                    f.write("window.BACKTESTS = ")
                    json.dump(entries, f, default=float)
                    f.write(";")
                os.replace(tmp, p)
            for fn in files:
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass
            print(f"bt_ingester: folded {len(pend)} refreshed entries",
                  flush=True)
        except Exception as e:
            print(f"bt_ingester error: {e}", flush=True)


threading.Thread(target=_bt_ingester, daemon=True).start()


@app.route("/api/override", methods=["POST", "DELETE"])
def override():
    """Manual close-override for the CURRENT open position: a trigger price
    (given directly or as a signed % from entry, + = profit direction). The
    trader polls the sidecar and force-closes when price crosses it."""
    i, I = _inst()
    cfg_name = I["trader"].get("config") or I.get("cfg") or "config.json"
    sf = _state_file_of(cfg_name) or "trader_state.json"
    path = os.path.join(AT, ".override_" + os.path.basename(sf))
    if request.method == "DELETE":
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify(ok=True, cleared=True)
    d = request.get_json(force=True)
    sp = os.path.join(AT, sf)
    st = json.load(open(sp)) if os.path.exists(sp) else {}
    pos = st.get("position")
    if not pos:
        return jsonify(error="no open position on this instance"), 400
    cfgj = json.load(open(os.path.join(AT, cfg_name)))
    sym = pos.get("symbol") or cfgj.get("symbol")
    cur = _pub_price(sym, cfgj.get("mode"))
    if cur is None:
        return jsonify(error="no current price available"), 400
    if d.get("now"):
        json.dump(dict(now=True, pos_key=str(pos.get("opened_at")),
                       set_at=time.strftime("%Y-%m-%d %H:%M:%S")),
                  open(path, "w"))
        return jsonify(ok=True, now=True)
    if d.get("price") not in (None, ""):
        trig = float(d["price"])
    elif d.get("pct") not in (None, ""):
        p = float(d["pct"]) / 100.0
        dirn = 1 if int(pos.get("dir") or 1) > 0 else -1
        trig = float(pos["entry_price"]) * (1 + p * dirn)
    else:
        return jsonify(error="give a price or a pct"), 400
    above = trig > cur
    json.dump(dict(price=trig, above=above,
                   pos_key=str(pos.get("opened_at")),
                   set_at=time.strftime("%Y-%m-%d %H:%M:%S")),
              open(path, "w"))
    return jsonify(ok=True, trigger=round(trig, 8), above=above, current=cur)

@app.route("/api/errors")
def api_errors():
    """Per-instance API/order errors (from notifications.log) since each
    instance's last 'clear'. Feeds the panel's error banner + tab icon."""
    nl = os.path.join(AT, "notifications.log")
    events = []
    try:
        with open(nl) as f:
            for ln in f.readlines()[-3000:]:
                try:
                    e = json.loads(ln)
                except Exception:
                    continue
                ev = e.get("event") or ""
                if "fail" in ev or "error" in ev:
                    events.append(e)
    except OSError:
        pass
    out = {}
    for i, I in instances.items():
        cfgn = I["trader"].get("config") or I.get("cfg") or ""
        cleared = I.get("err_cleared") or ""
        mine = [e for e in events
                if e.get("config") == cfgn and (e.get("at") or "") > cleared]
        if mine:
            out[i] = dict(count=len(mine), last=mine[-1])
    return jsonify(out)

@app.route("/api/errors/clear", methods=["POST"])
def api_errors_clear():
    i, I = _inst()
    I["err_cleared"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_instances()
    return jsonify(ok=True, instance=i)

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
    if len(instances) <= 1:
        return jsonify(error="can't remove the last instance"), 400
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
                if kind == "trader":
                    # a killed trader is a deliberate stop — persist that, or
                    # instances.json keeps saying "should_run, live" and the
                    # next reboot resurrects it LIVE without confirmation
                    I[kind]["live"] = False
                    I["stopped_by_user"] = True
    _save_instances()
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
    ?force=1 bypasses the cache (the panel's refresh-now button).
    ?only=mexc2[,mexc1] fetches ONLY those accounts and returns the rest as
    name-only stubs — the card shows one instance's account, so there is no
    reason to hit the other account's API (or to flash its balances while the
    page decides what to hide)."""
    force = request.args.get("force") == "1"
    only = {s for s in (request.args.get("only") or "").split(",") if s}
    if only and not force and _MEXC_CACHE["data"] is not None \
            and time.time() - _MEXC_CACHE["t"] < 300:
        return jsonify([a if a.get("account") in only
                        else dict(account=a.get("account"),
                                  email=a.get("email"),
                                  configured=a.get("configured", True),
                                  skipped=True)
                        for a in _MEXC_CACHE["data"]])
    # the background refresher keeps this warm, so a page load never waits on
    # MEXC round-trips; serve anything up to 5 min old (it is re-fetched
    # every 60s anyway) and only block when explicitly forced
    if not force and _MEXC_CACHE["data"] is not None \
            and time.time() - _MEXC_CACHE["t"] < 300:
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
        if only and name not in only:
            e["skipped"] = True        # name only: never touch its API
            out.append(e)
            continue
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
            _SIDE = {1: "OPEN LONG", 2: "CLOSE SHORT",
                     3: "OPEN SHORT", 4: "CLOSE LONG"}
            try:
                fsources = _order_sources()
                e["futures_trades"] = [
                    dict(symbol=t.get("symbol"),
                         t=time.strftime("%m-%d %H:%M", time.localtime(
                             float(t.get("timestamp") or 0) / 1000)),
                         side=_SIDE.get(int(t.get("side") or 0), "?"),
                         vol=t.get("vol"), price=t.get("price"),
                         fee=t.get("fee"), profit=t.get("profit"),
                         source=_trade_source(
                             float(t.get("timestamp") or 0) / 1000, fsources))
                    for t in (fapi.order_deals("SOL_USDT", 20) or [])]
            except Exception:
                e["futures_trades"] = []
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
                    # live-ness comes from HOW THE TRADER WAS STARTED (--live),
                    # not the config's dry_run field — reading the file made
                    # every live position display as "dry-run". And a config
                    # with no running trader is a STALE record: the state file
                    # outlives the process that wrote it.
                    _run, _live = False, False
                    for _i, _I in instances.items():
                        _t = _I["trader"]
                        if (_t["proc"] is not None and _t["proc"].poll() is None
                                and (_t.get("config") or _I.get("cfg")) == f):
                            _run, _live = True, bool(_t.get("live"))
                            break
                    bot.append(dict(config=f, symbol=c.get("symbol"),
                                    qty=pos.get("qty"),
                                    entry=pos.get("entry_price"),
                                    opened=pos.get("opened_at"),
                                    running=_run, live=_live,
                                    dry_run=(not _live)))
            e["bot_spot_positions"] = bot
        except Exception as ex:
            e["error"] = str(ex)
        out.append(e)
    if not only:            # only a FULL sweep may replace the shared cache
        _MEXC_CACHE.update(t=time.time(), data=out)
    return jsonify(out)


_DEALS = {"t": 0.0, "fut": {}, "spot": {}}


def _refresh_deals():
    """Cache recent EXECUTED fills per account so the trades table can report
    what MEXC reports: actual fill prices and fees, not our signal prices.
    (Signal-vs-fill slippage plus round-trip fees made a +6.27% trade read
    +5.47% on the exchange.)"""
    if AT not in sys.path:
        sys.path.insert(0, AT)
    syms = {}
    for f in os.listdir(AT):
        if not (f.startswith("config") and f.endswith(".json")):
            continue
        try:
            c = json.load(open(os.path.join(AT, f)))
        except Exception:
            continue
        acct = c.get("api_account", "mexc1")
        s = syms.setdefault((acct, c.get("mode")), set())
        s.update((c.get("contract_sizes") or {}).keys())
        if c.get("symbol"):
            s.add(c["symbol"])
    fut, spot = {}, {}
    for (acct, mode), symbols in syms.items():
        for sym in symbols:
            try:
                if mode == "spot":
                    from mexc_api import MexcSpotAPI
                    r = MexcSpotAPI(account=acct).my_trades(sym, limit=50)
                    spot[(acct, sym)] = r if isinstance(r, list) else []
                else:
                    from mexc_api import MexcFuturesAPI
                    r = MexcFuturesAPI(account=acct).order_deals(sym,
                                                                 page_size=50)
                    fut[(acct, sym)] = r if isinstance(r, list) else (
                        r.get("data") or [])
            except Exception:
                pass
    _DEALS.update(t=time.time(), fut=fut, spot=spot)


def _mexc_warmer():
    """Refresh the account snapshot every 60s in the background so the panel
    always renders instantly from cache — no waiting on MEXC when the page
    opens, and the numbers stay current while nobody is looking."""
    time.sleep(10)                       # let the panel finish booting
    while True:
        try:
            with app.test_request_context("/api/mexc/account?force=1"):
                mexc_account()
        except Exception as e:
            print(f"mexc warmer: {e}", flush=True)
        try:
            _refresh_deals()
        except Exception as e:
            print(f"deals warmer: {e}", flush=True)
        time.sleep(60)


threading.Thread(target=_mexc_warmer, daemon=True).start()


# ---------------- manual test orders ----------------
def _trader_running(I):
    p = (I.get("trader") or {}).get("proc")
    return bool(p is not None and p.poll() is None)


def _exchange_flat(icfg, acct, symbol):
    """True when MEXC shows nothing left to close for this symbol."""
    if AT not in sys.path:
        sys.path.insert(0, AT)
    from mexc_api import MexcFuturesAPI, MexcSpotAPI
    if icfg.get("mode") == "spot":
        sapi = MexcSpotAPI(account=acct)
        free = float(sapi.balance(symbol.split("_")[0]) or 0)
        try:
            mq = float(sapi.min_qty(symbol) or 0)
        except Exception:
            mq = 0.0
        return free <= mq
    fapi = MexcFuturesAPI(account=acct)
    for p in (fapi.open_positions() or []):
        if p.get("symbol") == symbol and float(p.get("holdVol") or 0) > 0:
            return False
    return True


def _reconcile_after_manual_close(I, icfg, acct, symbol):
    """A manual close goes straight to MEXC, so the trader's state file still
    lists a position it no longer holds — that stale entry is what the panel
    keeps rendering after a 'Close ALL'. Drop it (only once the exchange
    confirms flat) and record a late-join skip, exactly as the trader's own
    manual-close path does, so a restart cannot re-enter the same virtual
    trade. Bookkeeping only — places no orders."""
    import shutil
    sf = _state_file_of((I.get("trader") or {}).get("config") or I.get("cfg")
                        or "")
    sp = os.path.join(AT, sf) if sf else ""
    if not sp or not os.path.exists(sp):
        return None
    st = json.load(open(sp))
    pos = st.get("position")
    if not pos:
        return None
    if pos.get("symbol") and symbol and pos["symbol"] != symbol:
        return None
    if not _exchange_flat(icfg, acct, symbol):
        return "MEXC still shows a position — state file left untouched"
    shutil.copy(sp, sp + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
    lbl = pos.get("mirror_entry_t")
    if lbl and pos.get("comp") is not None:
        st.setdefault("late_skips", {})[str(pos["comp"])] = lbl
    st["position"] = None
    json.dump(st, open(sp, "w"), indent=1)
    return (f"cleared the tracked {pos.get('symbol')} position from {sf}"
            + (f" and marked comp #{pos['comp']}'s virtual trade {lbl} "
               f"as skipped" if lbl and pos.get("comp") is not None else ""))


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
    # A RUNNING trader owns its state file. Closing behind its back leaves it
    # managing a position that no longer exists (and it may later try to close
    # it again); its own "Close position now" does the close AND the
    # bookkeeping, so send the user there instead.
    if action.startswith("close") and _trader_running(I):
        _sf = _state_file_of((I.get("trader") or {}).get("config")
                             or I.get("cfg") or "")
        _sp = os.path.join(AT, _sf) if _sf else ""
        try:
            _pos = (json.load(open(_sp)) or {}).get("position") \
                if _sp and os.path.exists(_sp) else None
        except Exception:
            _pos = None
        if _pos and _pos.get("symbol") == d.get("symbol"):
            return jsonify(error=(
                f"this instance's trader is RUNNING and is tracking "
                f"{_pos.get('symbol')} — use 'Close position now' on its card "
                f"so it closes AND stops tracking. Closing from here would "
                f"leave it managing a position that no longer exists.")), 400
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
                # SPOT: the quantity field is BASE UNITS (8548 DOGE, 0.5
                # HYPE). It used to be multiplied by contract_size — a
                # SOL-era "1 contract = 0.1 SOL" assumption that silently
                # mis-sized every other pair.
                base_qty = qty
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
        if out.get("ok") and action.startswith("close"):
            try:
                note = _reconcile_after_manual_close(I, icfg, acct, symbol)
                if note:
                    out["reconciled"] = note
            except Exception as e:
                out["reconcile_error"] = f"{type(e).__name__}: {e}"
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
    # adopt straight from a BACKTEST ENTRY (Backtests page): resolve its
    # genome the same way the re-run button does — the run directory when it
    # still exists, otherwise the config embedded in the entry itself
    if d.get("entry"):
        p = os.path.join(REPO, "dashboard", "backtests.js")
        txt = open(p).read()
        _es = json.JSONDecoder().raw_decode(txt[txt.index("=") + 1:].lstrip())[0]
        _e = next((x for x in _es if x.get("name") == d["entry"]), None)
        if not _e:
            return jsonify(error=f"no backtest entry '{d['entry']}'"), 404
        _SUF = ["_oosbest_full", "_best_full", "_full"]
        _GEN = {"_oosbest_full": "holdout_best_config.json",
                "_best_full": "best_config.json",
                "_full": "best_config.json"}
        src = None
        for s in _SUF:
            if d["entry"].endswith(s):
                r = d["entry"][:-len(s)]
                for fn in (_GEN[s], "best_config.json"):
                    cand_p = os.path.join(OPT, "runs", r, fn)
                    if os.path.exists(cand_p):
                        src = cand_p
                        break
                break
        if src is None:                     # run dir gone: use the embedded
            g = _e.get("config")            # genome, normalized to run shape
            if not g:
                return jsonify(error=(
                    "this entry has no genome to adopt (router combos and "
                    "quick backtests must be adopted from the Optimize "
                    "page)")), 400
            g = dict(g)
            if "cand" not in g:
                g = dict(cand=(g.get("candidate") or g))
            for k, v in (("pair", _e.get("pair")),
                         ("timeframe", str(_e.get("timeframe") or "")),
                         ("mode", _e.get("mode")),
                         ("method", _e.get("method")),
                         ("strategy", _e.get("strategy")),
                         ("market_data", _e.get("market_data"))):
                if v and not g.get(k):
                    g[k] = v
            gdir = os.path.join(AT, ".adopted_genomes")
            os.makedirs(gdir, exist_ok=True)
            src = os.path.join(gdir, _safe_name(d["entry"]) + ".json")
            json.dump(g, open(src, "w"), indent=1)
        d = dict(d, source=src, run_name=d.get("run_name") or d["entry"])
    # a missing/!found source used to surface as an opaque 500 in the browser
    src = d.get("source")
    if not src:
        return jsonify(error="adopt needs a 'source' (a run's best_config.json) "
                             "or an 'entry' name"), 400
    if not os.path.isabs(src):
        src = os.path.join(OPT, src)
    if not os.path.isfile(src):
        return jsonify(error=f"source genome not found: {src}"), 404
    target = os.path.join(AT, d.get("target", "config.json"))
    best = json.load(open(src))
    _strat = best.get("strategy") or (best.get("cand") or {}).get("strategy")
    if _strat != "fcfsx" \
            and str(best.get("timeframe") or "3m") not in ("1m", "3m", "5m"):
        return jsonify(error=f"unsupported timeframe "
                             f"{best.get('timeframe')!r} — the live trader "
                             f"supports 1m, 3m and 5m."), 400
    if _strat in ("metax2", "pairx"):
        return jsonify(error=f"'{_strat}' runs have no live adapter yet — "
                             f"research artifacts."), 400
    if _strat == "fcfsx":
        # FCFS combo adopt: embed every component's cand + pair/timeframe and
        # the per-pair contract sizes, into its own config file (dry-run).
        comps_in = (best.get("cand") or {}).get("components") or []
        if len(comps_in) < 2:
            return jsonify(error="fcfsx run has fewer than 2 components"), 400
        comps, pairs = [], set()
        for c in comps_in:
            try:
                cc = json.load(open(os.path.join(OPT, "runs", c["run"],
                                                 c["file"])))
            except Exception as e:
                return jsonify(error=f"component '{c.get('run')}' can't be "
                                     f"loaded: {e}"), 400
            fam = c.get("strategy") or cc.get("strategy")
            if fam not in ("macdx", "scalpx", "scalpx2", "v7", "prime7",
                           "prime", "v6"):
                return jsonify(error=f"component family '{fam}' has no live "
                                     f"runner"), 400
            comps.append(dict(strategy=fam,
                              method=cc.get("method", "vol3"),
                              run=c["run"], file=c["file"],
                              pair=c.get("pair") or cc.get("pair"),
                              timeframe=str(c.get("timeframe")
                                            or cc.get("timeframe") or "3m"),
                              cand=cc["cand"]))
            pairs.add(comps[-1]["pair"])
        csizes = {}
        import requests
        # futures contract sizes are meaningless for SPOT (quantities there
        # are base units); embedding them made every consumer that multiplies
        # by contract size report 100x P&L on DOGE etc.
        for p in (sorted(pairs) if best.get("mode") != "spot" else []):
            try:
                r = requests.get("https://contract.mexc.com/api/v1/contract/"
                                 f"detail?symbol={p}", timeout=10).json()
                csizes[p] = float(r["data"]["contractSize"])
            except Exception:
                pass
        missing_cs = sorted(pairs - set(csizes))
        if best.get("mode") == "lev" and missing_cs:
            return jsonify(error=f"couldn't fetch contract sizes for "
                                 f"{missing_cs} from MEXC — retry"), 400
        rname = d.get("run_name") or os.path.basename(os.path.dirname(src))
        suffix = "fcfs_" + re.sub(r"[^A-Za-z0-9_]+", "", rname)[:40]
        tname = f"config_{suffix}.json"
        target = os.path.join(AT, tname)
        created = not os.path.exists(target)
        if created:
            cfg = _template_cfg(best.get("mode", "lev"))
            cfg.pop("candidate", None)
            cfg.pop("adopted_from", None)
        else:
            cfg = json.load(open(target))
            import shutil
            shutil.copy(target, target + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
        cfg.update(dry_run=True,
                   state_file=f"trader_state_{suffix}.json",
                   log_file=f"trader_{suffix}.log",
                   mode=best.get("mode", "lev"),
                   timeframe="3m",   # cosmetic; components carry their own
                   # spot sizes in BASE UNITS (csizes is empty for spot)
                   contract_size=(1.0 if best.get("mode") == "spot"
                                  else cfg.get("contract_size", 0.1)),
                   contract_sizes=csizes,
                   candidate=dict(strategy="fcfsx",
                                  mode=best.get("mode", "lev"),
                                  components=comps,
                                  source_run=rname))
        cfg["adopted_from"] = dict(source=src, at=time.strftime("%Y-%m-%d %H:%M"))
        json.dump(cfg, open(target, "w"), indent=1)
        return jsonify(ok=True, target=tname, created=created,
                       note=(f"FCFS combo adopted into {tname} "
                             f"({len(comps)} components across "
                             f"{len(pairs)} pairs; starts as DRY-RUN). "
                             f"Add it as an instance, start the trader, soak "
                             f"dry, then flip live with confirm."))
    if (best.get("cand") or {}).get("stack"):
        return jsonify(error="this is a method-STACKED router (research "
                             "artifact) — no live adapter yet. Adopt the "
                             "single-method router it currently picks "
                             "instead (see cand.current in its config)."), 400
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
            cfg = _template_cfg(best.get("mode", "lev"))
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
        cfg["timeframe"] = best.get("timeframe", "3m")
        if best.get("pair"): cfg["symbol"] = best["pair"]   # same pair-carry
        #   as plain adopt: a non-SOL router over the SOL template must not
        #   inherit SOL_USDT
        cfg["adopted_from"] = dict(source=src, at=time.strftime("%Y-%m-%d %H:%M"))
        json.dump(cfg, open(target, "w"), indent=1)
        return jsonify(ok=True, target=tname, created=created,
                       note=(f"ROUTER adopted into {tname}"
                             + (" (new file — own state/log, dry-run)."
                                if created else " (backup kept).")
                             + " Run test_parity_metax.py, then a dry-run "
                               "soak before LIVE."))
    created = not os.path.exists(target)
    if created:
        # NEW config file: clone the mode's classic config as a template but
        # give it its OWN state/log files (so it can run as its own instance)
        # and start it DRY — same policy as router/fcfs adoption.
        cfg = _template_cfg(best.get("mode", "lev"))
        cfg.pop("candidate", None)
        cfg.pop("adopted_from", None)
        nm = re.sub(r"^config_?", "", os.path.basename(target)[:-len(".json")])
        suffix = re.sub(r"[^A-Za-z0-9_]+", "", nm)[:60] or "adopted"
        cfg.update(dry_run=True,
                   state_file=f"trader_state_{suffix}.json",
                   log_file=f"trader_{suffix}.log")
        if best.get("mode") == "spot":     # base units, not futures contracts
            cfg["contract_size"] = 1.0
            cfg.pop("contract_sizes", None)
    else:
        cfg = json.load(open(target))
        if best.get("mode") and cfg.get("mode") and best["mode"] != cfg["mode"]:
            if not d.get("force"):
                right = ("config_spot.json" if best["mode"] == "spot"
                         else "config.json")
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
    cfg["timeframe"] = best.get("timeframe", "3m")
    # carry the run's PAIR — without this, a HYPE run adopted over the classic
    # (SOL) template silently trades the HYPE genome on SOL candles
    if best.get("pair"): cfg["symbol"] = best["pair"]
    cfg["adopted_from"] = dict(source=src, at=time.strftime("%Y-%m-%d %H:%M"))
    json.dump(cfg, open(target, "w"), indent=1)
    return jsonify(ok=True, target=os.path.basename(target), created=created,
                   note=(f"adopted into NEW config "
                         f"{os.path.basename(target)} (own state/log, starts "
                         f"as DRY-RUN)" if created else
                         f"adopted into {os.path.basename(target)} (backup "
                         f"kept)"))

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
        comps = None
        if strat == "fcfsx" and cand.get("components"):
            # FCFS combos: every component is a run in its own right, so the
            # Backtests page can filter to "what this instance is trading"
            comps = [dict(run=x.get("run"), strategy=x.get("strategy"),
                          pair=x.get("pair"), timeframe=x.get("timeframe"),
                          assigned=True) for x in cand["components"]]
        elif strat in ("metax", "metax2") and cand.get("components"):
            assigned = {a for a in (cand.get("assign") or [])
                        if a is not None and a >= 0}
            comps = [dict(run=x.get("run"), strategy=x.get("strategy"),
                          pair=x.get("pair"), timeframe=x.get("timeframe"),
                          assigned=(k in assigned))
                     for k, x in enumerate(cand["components"])]
        out.append(dict(file=f, strategy=strat, mode=c.get("mode"),
                        method=c.get("method"), equity=c.get("equity_usdt"),
                        execution=c.get("execution", "browser"),
                        api_account=c.get("api_account", "mexc1"),
                        components=comps, source_run=cand.get("source_run"),
                        adopted_from=(c.get("adopted_from") or {}).get("source"),
                        adopted_at=(c.get("adopted_from") or {}).get("at")))
    return jsonify(out)

@app.route("/api/trader_configs/clear_position", methods=["POST"])
def clear_bot_position():
    """Drop a STALE tracked position from a stopped config's state file
    (bookkeeping only — places no orders). Refused while its trader runs:
    a live trader owns its state, and clearing under it would orphan real
    coins."""
    name = os.path.basename((request.get_json(force=True) or {}).get("config")
                            or "")
    path = os.path.join(AT, name)
    if not re.fullmatch(r"config[A-Za-z0-9_.\-]*\.json", name) \
            or not os.path.isfile(path):
        return jsonify(error=f"no such config '{name}'"), 404
    _mine = _state_file_of(name)
    for i, I in instances.items():
        t = I["trader"]
        if t["proc"] is None or t["proc"].poll() is not None:
            continue
        _theirs = _state_file_of(t.get("config") or I.get("cfg") or "")
        # compare STATE FILES, not config names: config.json and
        # config_legacy_stopON.json share trader_state.json, so a name-only
        # check would let this wipe a running live trader's position
        if (t.get("config") or I.get("cfg")) == name or (
                _mine and _theirs and _mine == _theirs):
            return jsonify(error=(f"instance {I.get('name') or i} is RUNNING "
                                  f"a config with the same state file — stop "
                                  f"it first, or close from its card")), 400
    c = json.load(open(path))
    sp = os.path.join(AT, c.get("state_file", "trader_state.json"))
    if not os.path.exists(sp):
        return jsonify(error="no state file"), 404
    st = json.load(open(sp))
    old = st.get("position")
    if not old:
        return jsonify(ok=True, note="already flat")
    import shutil
    shutil.copy(sp, sp + ".bak." + time.strftime("%Y%m%d_%H%M%S"))
    # mark the component's virtual trade as skipped, or the late-join will
    # re-enter it on the next start — the position was closed on purpose
    lbl = old.get("mirror_entry_t")
    if lbl and old.get("comp") is not None:
        st.setdefault("late_skips", {})[str(old["comp"])] = lbl
    st["position"] = None
    json.dump(st, open(sp, "w"), indent=1)
    return jsonify(ok=True, cleared=old,
                   late_skips=st.get("late_skips") or {})

@app.route("/api/trader_configs/delete", methods=["POST"])
def trader_config_delete():
    """Delete a trader config file. Refuses if a RUNNING trader uses it or
    an instance is pointed at it; the file is moved to a .deleted_<ts> copy
    (recoverable) and its state/log files are left alone."""
    name = os.path.basename((request.get_json(force=True) or {}).get("file")
                            or "")
    path = os.path.join(AT, name)
    if not re.fullmatch(r"config[A-Za-z0-9_.\-]*\.json", name) \
            or not os.path.isfile(path):
        return jsonify(error=f"no such config '{name}'"), 404
    # these two are the TEMPLATES every adopt clones from, not instance
    # configs. Deleting config.json is what made adopt 500 on 2026-08-19.
    if name in ("config.json", "config_spot.json"):
        return jsonify(error=(f"{name} is the {'spot' if 'spot' in name else 'leverage'} "
                              f"TEMPLATE that 'Adopt' clones every new config "
                              f"from — deleting it breaks adoption. It is not "
                              f"an instance config; leave it in place.")), 400
    for i, I in instances.items():
        t = I["trader"]
        running = t["proc"] is not None and t["proc"].poll() is None
        if running and (t.get("config") or I.get("cfg")) == name:
            return jsonify(error=(f"instance {I.get('name') or i} is RUNNING "
                                  f"this config — stop it first")), 400
    used_by = [I.get("name") or i for i, I in instances.items()
               if (I.get("cfg") == name
                   or (I["trader"].get("config") == name))]
    if used_by and not (request.get_json(force=True) or {}).get("force"):
        return jsonify(error=(f"selected on instance(s) {', '.join(map(str, used_by))} "
                              f"— point them elsewhere first, or resend with "
                              f"force"), used_by=used_by), 409
    bak = path + ".deleted_" + time.strftime("%Y%m%d_%H%M%S")
    os.replace(path, bak)
    return jsonify(ok=True, file=name, backup=os.path.basename(bak))

@app.route("/api/trader_config", methods=["GET", "POST"])
def trader_config():
    fname = os.path.basename(request.args.get("file") or "config.json")
    path = os.path.join(AT, fname)
    # an empty/odd ?file= used to resolve to the adaptive_trader DIRECTORY and
    # blow up with IsADirectoryError -> HTTP 500
    if not fname.endswith(".json") or not os.path.isfile(path):
        return jsonify(error=f"no such trader config: '{fname}'"), 404
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

def _space_paths(sel):
    """(active_path, name) for '' (canonical) or '<coin>_<tf>m'."""
    if sel:
        if not re.fullmatch(r"[a-z]{2,6}_[135]m", sel):
            return None, None
        return os.path.join(OPT, "param_spaces", f"{sel}.json"), sel
    return os.path.join(OPT, "param_space.json"), "default"


def _variant_path(name, variant, defaults=False):
    d = os.path.join(OPT, "param_spaces", "variants")
    os.makedirs(d, exist_ok=True)
    sfx = ".defaults" if defaults else ""
    return os.path.join(d, f"{name}.{variant}{sfx}.json")


@app.route("/api/gamut/start", methods=["POST"])
def gamut_start():
    cfg = request.get_json(force=True) or {}
    name = _safe_name(cfg.get("name") or "")
    if not name:
        # AI-generated name when left auto (Anthropic key via ai_advisor)
        try:
            sys.path.insert(0, OPT)
            from ai_advisor import get_key, call_claude
            key = get_key()
            if key:
                brief = {k: cfg.get(k) for k in
                         ("pairs", "strategies", "modes", "tfs", "methods",
                          "holdouts", "totals")}
                txt = call_claude(key,
                    "Suggest ONE short lowercase run-name slug (letters/"
                    "digits/underscores only, 6-16 chars, descriptive of the "
                    "sweep) for this trading optimization sweep. Reply with "
                    "ONLY the slug.\n" + json.dumps(brief))
                m = re.search(r"[a-z][a-z0-9_]{4,20}", txt.strip().lower())
                if m:
                    name = _safe_name(m.group(0)[:16])
        except Exception:
            pass
    name = name or f"g{time.strftime('%m%d_%H%M')}"
    for k in ("pairs", "strategies", "algos", "modes", "tfs", "methods",
              "scorings", "max_dds", "max_holds", "holdouts", "totals"):
        if not cfg.get(k):
            return jsonify(error=f"select at least one option for '{k}'"), 400
    # never silently resume a DIFFERENT config under the same name
    base_name, n2 = name, 2
    while os.path.exists(os.path.join(OPT, "campaigns", f"gamut_{name}",
                                      "plan.json")):
        name = f"{base_name}_{n2}"
        n2 += 1
    cfg["name"] = name
    pdir = os.path.join(OPT, "campaigns", f"gamut_{name}")
    os.makedirs(pdir, exist_ok=True)
    cfg_p = os.path.join(pdir, "config.json")
    json.dump(cfg, open(cfg_p, "w"), indent=1)
    return jsonify(id=spawn("gamut", name,
                            [sys.executable, "gamut.py", "--config", cfg_p],
                            OPT), name=name)


@app.route("/api/gamut/stop", methods=["POST"])
def gamut_stop():
    name = _safe_name((request.get_json(force=True) or {}).get("name") or "")
    pdir = os.path.join(OPT, "campaigns", f"gamut_{name}")
    if not os.path.isdir(pdir):
        return jsonify(error=f"no gamut '{name}'"), 404
    open(os.path.join(pdir, "STOP"), "w").write("")
    return jsonify(ok=True, note="stops gracefully after the current run")


@app.route("/api/gamut/status")
def gamut_status():
    name = _safe_name(request.args.get("name") or "")
    p = os.path.join(OPT, "campaigns", f"gamut_{name}", "plan.json")
    if not os.path.exists(p):
        return jsonify(error="no plan"), 404
    plan = json.load(open(p))
    from collections import Counter
    c = Counter(s["status"] for s in plan["specs"])
    cur = next((s["name"] for s in plan["specs"] if s["status"] == "running"), None)
    return jsonify(total=len(plan["specs"]), counts=dict(c), running=cur)


_PLAN_KIND = {}      # campaign -> (plan.json mtime, is a gamut plan)

@app.route("/api/gamut/progress")
def gamut_progress():
    """Rich progress for the Progress page: merges plan.json with the
    (possibly EC2-synced) worker_state.json and runs/ skip-markers."""
    import math as _m
    import datetime as _dt
    from collections import Counter, defaultdict
    name = _safe_name(request.args.get("name") or "")
    # ANY campaign directory holding a plan.json qualifies — the old filter
    # required a "gamut_" prefix, so campaigns not following that naming
    # (or lacking the customary symlink) were invisible in the dropdown
    def _is_gamut_plan(d):
        # cached by plan.json mtime: parsing every campaign's plan on every
        # poll pinned a CPU (the biggest plan holds 12,960 specs and the
        # Progress page refreshes every 20s)
        pth = os.path.join(OPT, "campaigns", d, "plan.json")
        try:
            mt = os.path.getmtime(pth)
        except OSError:
            return False
        hit = _PLAN_KIND.get(d)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            pl = json.load(open(pth))
            sp = pl.get("specs") or []
            ok = bool(sp) and isinstance(sp[0], dict) and "name" in sp[0]
        except Exception:
            ok = False
        _PLAN_KIND[d] = (mt, ok)
        return ok

    cs = [d for d in sorted(os.listdir(os.path.join(OPT, "campaigns")))
          if os.path.exists(os.path.join(OPT, "campaigns", d, "plan.json"))
          and _is_gamut_plan(d)]
    if not cs:
        return jsonify(error="no gamut campaigns"), 404

    def _disp(d):
        return d[len("gamut_"):] if d.startswith("gamut_") else d

    if not name:   # default: the campaign with the freshest WORKER ACTIVITY
        def _act(d):
            ws = os.path.join(OPT, "campaigns", d, "worker_state.json")
            return (os.path.getmtime(ws) if os.path.exists(ws) else
                    os.path.getmtime(os.path.join(OPT, "campaigns", d,
                                                  "plan.json")) - 1e9)
        name = _disp(sorted(cs, key=_act)[-1])
    # de-dup: a gamut_X symlink beside a real X directory is the same campaign
    campaign_names = sorted({_disp(d) for d in cs})
    pdir = os.path.join(OPT, "campaigns", f"gamut_{name}")
    if not os.path.exists(os.path.join(pdir, "plan.json")):
        pdir = os.path.join(OPT, "campaigns", name)
    plan_p = os.path.join(pdir, "plan.json")
    if not os.path.exists(plan_p):
        return jsonify(error="no plan"), 404
    plan = json.load(open(plan_p))
    # merge state from every box (worker_state.json, worker_state_b.json, …)
    import glob as _glob
    state = {}
    st_p = None
    for sp in sorted(_glob.glob(os.path.join(pdir, "worker_state*.json"))):
        st_p = st_p or sp
        try:
            for k, v in json.load(open(sp)).items():
                if k not in state or (v.get("at") or "") > (state[k].get("at") or ""):
                    state[k] = v
        except Exception:
            pass
    st_p = max(_glob.glob(os.path.join(pdir, "worker_state*.json")),
               key=os.path.getmtime, default=None)
    now = time.time()
    counts = Counter()
    pairs = defaultdict(lambda: [0, 0])
    running, failed, done_times = [], [], []
    for s in plan["specs"]:
        # legacy campaign plans (c1..c7) predate the name/status spec shape —
        # skip those entries instead of 500-ing the whole Progress page
        n = s.get("name")
        if not n:
            continue
        st = state.get(n, {})
        sst = st.get("status")
        rd = os.path.join(OPT, "runs", n)
        if (s.get("status") == "done" or sst in ("done", "skipped")
                or os.path.exists(os.path.join(rd, "best_config.json"))
                or os.path.exists(os.path.join(rd, "no_survivor.json"))):
            eff = "done"
        else:
            eff = sst or "pending"
        if eff == "running":
            # ghost-buster fallback (workers now mark strandings themselves
            # at startup): stale 'running' with no result = interrupted
            try:
                t = _dt.datetime.strptime(st.get("at", ""),
                                          "%Y-%m-%d %H:%M:%S").timestamp()
                if t > now + 600:
                    t -= 7 * 3600          # pre-TZ-fix UTC stamps
                if now - t > 45 * 60:
                    eff = "interrupted"
            except Exception:
                eff = "interrupted"
        counts[eff] += 1
        pairs[s["coin"]][1] += 1
        if eff == "done":
            pairs[s["coin"]][0] += 1
            # rate/recent lists use REAL completions only — 'skipped'
            # re-stamps from worker restarts inflated the rate/ETA
            if st.get("at") and sst == "done":
                done_times.append((st["at"], n))
        elif eff == "running":
            running.append(dict(name=n, since=st.get("at"),
                                **({"try": st["try"]} if st.get("try") else {})))
        elif eff == "failed":
            failed.append(dict(name=n, at=st.get("at")))
    done_times.sort()

    def _ts(a):
        try:
            t = _dt.datetime.strptime(a, "%Y-%m-%d %H:%M:%S").timestamp()
            # heal the 2026-08-03 incident: the EC2 box stamped UTC (=+7h)
            # before its timezone was fixed — timestamps can't be in the future
            if t > now + 600:
                t -= 7 * 3600
            return t
        except Exception:
            return None
    tss = [t for t in (_ts(a) for a, _ in done_times) if t]
    rate_hr = eta_h = None
    if len(tss) >= 3:
        window = [t for t in tss if t > now - 3 * 3600] or tss[-60:]
        span = max(window) - min(window)
        if span > 120 and len(window) >= 3:
            rate_hr = round((len(window) - 1) / (span / 3600.0), 1)
            remaining = (counts.get("pending", 0) + counts.get("running", 0)
                         + counts.get("interrupted", 0))
            eta_h = round(remaining / rate_hr, 1)
    recent = []
    for a, n in done_times[-15:][::-1]:
        try:
            b = json.load(open(os.path.join(OPT, "runs", n,
                                            "best_config.json")))
        except Exception:
            b = {}
        h = ((b.get("holdout_best") or {}).get("holdout")
             or b.get("holdout") or {})
        g = h.get("growth")
        recent.append(dict(
            name=n, at=a,
            survivor=bool(b),           # False = search found NO feasible config
            holdout_pct_mo=(round(100 * (_m.exp(g) - 1), 1)
                            if g is not None else None),
            dd_pct=(round(100 * (h.get("maxdd") or 0)) if h else None),
            liq=bool(h.get("liq")) if h else None))
    return jsonify(
        name=name, campaigns=campaign_names,
        total=len(plan["specs"]), counts=dict(counts),
        pairs={k: dict(done=v[0], total=v[1]) for k, v in sorted(pairs.items())},
        running=sorted(running, key=lambda r: r.get("since") or ""),
        failed=failed[-20:], recent=recent,
        rate_per_hour=rate_hr, eta_hours=eta_h,
        first_done=(done_times[0][0] if done_times else None),
        last_done=(done_times[-1][0] if done_times else None),
        worker_state_age_sec=(int(now - os.path.getmtime(st_p))
                              if st_p and os.path.exists(st_p) else None))


@app.route("/api/story", methods=["GET", "POST"])
def run_story():
    """Provenance story of a run. GET returns the cached story.md;
    POST (re)generates it via gen_story.py (LLM when a key is configured)."""
    run = _safe_name((request.args.get("run") if request.method == "GET"
                      else (request.get_json(force=True) or {}).get("run")) or "")
    if not run:
        return jsonify(error="run required"), 400
    sp = os.path.join(OPT, "runs", run, "story.md")
    if request.method == "GET":
        if os.path.exists(sp):
            return jsonify(story=open(sp).read(),
                           at=time.strftime("%Y-%m-%d %H:%M",
                                            time.localtime(os.path.getmtime(sp))))
        return jsonify(error="no story yet — generate it"), 404
    import subprocess
    r = subprocess.run([sys.executable, "gen_story.py", "--run", run],
                       cwd=OPT, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(sp):
        return jsonify(error=(r.stderr or r.stdout)[-400:]), 500
    return jsonify(story=open(sp).read())


@app.route("/api/jobs/router", methods=["POST"])
def job_router():
    """Launch router/combo builders as panel jobs: metax (single-dataset
    router), stack (router-of-routers), metax2 (cross-timeframe/pair),
    pairx (FCFS basket), plus per-run walkforward/refine."""
    d = request.get_json(force=True)
    kind = d.get("kind")
    name = _safe_name(d.get("name") or "") or f"router_{time.strftime('%m%d_%H%M')}"
    if kind == "metax":
        cmd = [sys.executable, "metax_cli.py", "--mode", d.get("mode", "spot"),
               "--buckets", d.get("buckets", "vol3"),
               "--symbol", d.get("symbol", "sol"), "--name", name,
               "--total", str(int(d.get("total") or 30000))]
    elif kind == "metax_extend":
        base = _safe_name(d.get("router") or "")
        adds = [r for r in (d.get("add") or []) if r]
        for r in adds:
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", r):
                return jsonify(error=f"bad run name '{r}'"), 400
        if not base or not adds:
            return jsonify(error="router and at least one run to add "
                                 "are required"), 400
        name = _safe_name(d.get("name") or "") or f"{base}_ext"
        cmd = [sys.executable, "metax_cli.py", "--extend", base,
               "--add", ",".join(adds), "--name", name]
    elif kind == "metax2_extend":
        base = _safe_name(d.get("router") or "")
        adds = [r for r in (d.get("add") or []) if r]
        for r in adds:
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", r):
                return jsonify(error=f"bad run name '{r}'"), 400
        if not base or not adds:
            return jsonify(error="router and at least one run to add "
                                 "are required"), 400
        name = _safe_name(d.get("name") or "") or f"{base}_ext"
        cmd = [sys.executable, "metax2_cli.py", "--extend", base,
               "--add", ",".join(adds), "--name", name]
    elif kind == "metax_wf":
        name = _safe_name(d.get("run") or "")
        cmd = [sys.executable, "metax_cli.py", "--walkforward", f"runs/{name}"]
    elif kind == "metax_refine":
        name = _safe_name(d.get("run") or "")
        cmd = [sys.executable, "metax_cli.py", "--refine", f"runs/{name}",
               "--iters", str(int(d.get("iters") or 400))]
    elif kind == "stack":
        cmd = [sys.executable, "metax_cli.py", "--stack-auto",
               d.get("mode", "spot"), "--name", name]
    elif kind == "metax2":
        cmd = [sys.executable, "metax2_cli.py", "--name", name]
        runs_sel = [r for r in (d.get("runs") or []) if r]
        if runs_sel:
            for r in runs_sel:
                if not re.fullmatch(r"[A-Za-z0-9_.\-]+", r):
                    return jsonify(error=f"bad run name '{r}'"), 400
            cmd += ["--runs", ",".join(runs_sel)]
        else:
            cmd += ["--pairs", d.get("pairs", "sol")]
    elif kind == "pairx":
        cmd = [sys.executable, "pairx_cli.py", "--name", name]
    elif kind == "fcfsx_rerun":
        # re-simulate an existing FCFS combo (same components, same name —
        # overwrites its _fcfs_full/_fcfs_wf backtests and verdict) with
        # whatever candle data is on disk NOW
        rn = d.get("run") or ""
        for j in jobs.values():   # one rerun per combo at a time
            if j.get("kind") == "router" and j.get("name") == rn \
                    and j["proc"].poll() is None:
                return jsonify(error=f"a re-run of '{rn}' is already running "
                                     f"— watch it in the Jobs section (cache "
                                     f"rebuilds make the first one slow)"), 400
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", rn):
            return jsonify(error=f"bad run name '{rn}'"), 400
        try:
            b = json.load(open(os.path.join(OPT, "runs", rn,
                                            "best_config.json")))
        except Exception as e:
            return jsonify(error=f"can't load '{rn}': {e}"), 400
        comps = [c.get("run") for c in
                 ((b.get("cand") or {}).get("components") or [])]
        comps = [c for c in comps if c]
        if len(comps) < 2:
            return jsonify(error=f"'{rn}' has no stored component list"), 400
        missing = [c for c in comps
                   if not os.path.exists(os.path.join(OPT, "runs", c,
                                                      "best_config.json"))]
        if missing:
            return jsonify(error=f"component run(s) no longer exist: "
                                 f"{missing[:3]}"), 400
        name = rn
        # full evidence-chain refresh: every component's backtests first
        # (their _full/_oosbest_full entries go stale when data refreshes),
        # THEN the combo's own replay + verdict
        cmd = [sys.executable,
               os.path.join(os.path.dirname(AT.rstrip("/")), "scripts",
                            "refresh_combo.py"), rn]
        try:
            _j = int(d.get("jobs") or 1)
        except (TypeError, ValueError):
            _j = 1
        cmd += ["--jobs", str(max(1, min(10, _j)))]
        try:
            _md = float(d.get("maxdd") or 0)
        except (TypeError, ValueError):
            _md = 0
        if 0 < _md <= 1:
            cmd += ["--max-dd", str(_md)]
    elif kind == "fcfsx":
        runs_sel = [r for r in (d.get("runs") or []) if r]
        if len(runs_sel) < 2:
            return jsonify(error="FCFS combo needs at least 2 component "
                                 "runs (fill the Specific components row)"), 400
        for r in runs_sel:
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", r):
                return jsonify(error=f"bad run name '{r}'"), 400
        cmd = [sys.executable, "fcfsx_cli.py", "--name", name,
               "--runs", ",".join(runs_sel)]
        try:
            _md = float(d.get("maxdd") or 0)
        except (TypeError, ValueError):
            _md = 0
        if 0 < _md <= 1:
            cmd += ["--max-dd", str(_md)]
    else:
        return jsonify(error=f"unknown router kind '{kind}'"), 400
    return jsonify(id=spawn("router", name, cmd, OPT))


# ---------------- gamut worker fleet (status + per-system pause/resume) ----
_GCTL = os.path.join(os.path.dirname(AT.rstrip("/")), "scripts", "gamut_ctl.sh")
_GW_CACHE = {"t": 0, "data": None}

def _gamut_systems():
    """This machine + every remote that an offload_sync loop is talking to.
    Discovered from the running sync processes, so EC2 IP changes after spot
    bounces are picked up automatically."""
    import platform
    _host = platform.node().replace(".local", "") or "this machine"
    if "mini" in _host.lower():
        _host = "Mac mini"
    elif "macbook" in _host.lower():
        _host = "MacBook"
    systems = [dict(id="local", name=f"{_host} (this computer)", local=True)]
    try:
        ps = subprocess.run(["ps", "-Ao", "command"], capture_output=True,
                            text=True, timeout=5).stdout
    except Exception:
        ps = ""
    seen = set()
    for ln in ps.splitlines():
        if "offload_sync.sh" not in ln or "grep" in ln:
            continue
        parts = ln.split("offload_sync.sh", 1)[1].split()
        if len(parts) < 3:
            continue
        key, host = parts[0], parts[2]
        # a remote may carry its repo path ('user@host:Code/strategy-lab');
        # the ssh TARGET is only the part before the colon
        rpath = host.split(":", 1)[1] if ":" in host else "strategy-lab"
        host = host.split(":", 1)[0]
        rh = host if "@" in host else f"ubuntu@{host}"
        if rh in seen:
            continue
        seen.add(rh)
        if "macbook" in rh.lower():
            name = "MacBook"
        elif "mini" in rh.lower():
            name = "Mac mini"
        elif rh.startswith("ubuntu@"):
            name = f"AWS EC2 ({rh.split('@')[1]})"
        else:
            name = rh
        # personal keys may be passphrase-protected (agent-only, unusable
        # from this daemon) — the dedicated automation key wins when present
        auto = os.path.expanduser("~/.ssh/lab_auto_ed25519")
        if not rh.startswith("ubuntu@") and os.path.exists(auto):
            key = auto
        systems.append(dict(id=rh, name=name, ssh=rh, key=key, rpath=rpath))
    return systems

def _gctl(sys_d, action, arg=None):
    """Run gamut_ctl.sh locally or piped over ssh. Returns raw output."""
    extra = [str(arg)] if arg is not None else []
    try:
        if sys_d.get("local"):
            r = subprocess.run(["bash", _GCTL, action] + extra,
                               capture_output=True, text=True, timeout=15)
            return r.stdout
        cmd = ["ssh", "-i", os.path.expanduser(sys_d["key"]),
               "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-o", "ConnectTimeout=8", sys_d["ssh"],
               "bash -s", action] + extra
        r = subprocess.run(cmd, stdin=open(_GCTL), capture_output=True,
                           text=True, timeout=20)
        return r.stdout
    except Exception as e:
        return f"ERROR {e}"

def _parse_gctl(txt):
    st, plans, npids = "unreachable", [], 0
    cores = nproc = None
    for ln in (txt or "").splitlines():
        if ln.startswith("STATE "):
            st = ln[6:].strip()
        elif ln.startswith("PLAN "):
            plans.append(ln[5:].strip())
        elif ln.startswith("PIDS "):
            try:
                npids = int(ln[5:])
            except ValueError:
                pass
        elif ln.startswith("CORES "):
            try:
                cores = int(ln[6:].strip())
            except ValueError:
                cores = None
        elif ln.startswith("NPROC "):
            try:
                nproc = int(ln[6:].strip())
            except ValueError:
                nproc = None
    return dict(state=st, plans=plans, pids=npids, cores=cores, nproc=nproc)

@app.route("/api/gamut/workers")
def gamut_workers():
    if time.time() - _GW_CACHE["t"] < 10 and _GW_CACHE["data"] \
            and not request.args.get("fresh"):
        return jsonify(_GW_CACHE["data"])
    systems = _gamut_systems()
    out = []
    def probe(s):
        d = dict(s); d.pop("key", None)
        d.update(_parse_gctl(_gctl(s, "status")))
        out.append(d)
    ts = [threading.Thread(target=probe, args=(s,)) for s in systems]
    [t.start() for t in ts]; [t.join(timeout=25) for t in ts]
    data = dict(systems=sorted(out, key=lambda x: x["name"]))
    _GW_CACHE.update(t=time.time(), data=data)
    return jsonify(data)

@app.route("/api/gamut/workers/ctl", methods=["POST"])
def gamut_workers_ctl():
    d = request.get_json(force=True)
    action = d.get("action")
    if action not in ("pause", "resume", "cores"):
        return jsonify(error="action must be pause|resume|cores"), 400
    arg = None
    if action == "cores":
        try:
            arg = int(d.get("cores"))
        except (TypeError, ValueError):
            return jsonify(error="cores must be a number"), 400
        if not 1 <= arg <= 512:
            return jsonify(error="cores must be between 1 and 512"), 400
    target = next((s for s in _gamut_systems() if s["id"] == d.get("id")), None)
    if not target:
        return jsonify(error=f"unknown system '{d.get('id')}' (it may have "
                             f"bounced to a new IP — refresh)"), 404
    txt = _gctl(target, action, arg)
    _GW_CACHE["t"] = 0            # invalidate cache
    return jsonify(ok=("ERROR" not in txt), output=txt.strip())


@app.route("/api/trades")
def trades_history():
    """Position events from adaptive_trader/notifications.log (every trader
    writes there — dry AND live, marked). Newest first.
    Params: config=<file.json> to filter, limit=N (default 50)."""
    limit = min(int(request.args.get("limit", 50)), 500)
    want_cfg = request.args.get("config")
    p = os.path.join(AT, "notifications.log")
    out = []
    try:
        with open(p, "rb") as f:            # tail-read: file grows forever
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2_000_000))
            lines = f.read().decode(errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    for ln in reversed(lines):
        if len(out) >= limit:
            break
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("event") not in ("position_opened", "position_closed"):
            continue
        if want_cfg and e.get("config") != want_cfg:
            continue
        pos = e.get("position") or {}
        out.append(dict(
            at=e.get("at"), event=("OPEN" if e["event"] == "position_opened"
                                   else "CLOSE"),
            config=e.get("config"), account=e.get("account"),
            symbol=e.get("symbol"),
            side=e.get("side") or ({1: "LONG", -1: "SHORT"}
                                   .get(pos.get("dir")) if pos else None),
            qty=e.get("qty") or pos.get("qty"),
            lev=e.get("lev") or pos.get("lev"),
            price=e.get("price"),
            entry_price=pos.get("entry_price"),
            reason=e.get("reason"), comp=e.get("comp"),
            live=bool(e.get("live")), result=e.get("result")))

    # ---- P&L on every CLOSE: % move (leverage-applied) and USDT ----
    # entry price comes from the event when the trader recorded it; older
    # events (and some paths) omit it, so fall back to pairing each CLOSE
    # with the most recent OPEN of the same config+symbol.
    _cs = {}
    for f in os.listdir(AT):
        if f.startswith("config") and f.endswith(".json"):
            try:
                c = json.load(open(os.path.join(AT, f)))
            except Exception:
                continue
            _cs[f] = (c.get("contract_sizes") or {}, c.get("mode"),
                      c.get("contract_size"))
    last_open = {}
    for r in reversed(out):                       # oldest -> newest
        key = (r.get("config"), r.get("symbol"))
        if r["event"] == "OPEN":
            last_open[key] = r
            continue
        ep = r.get("entry_price") or (last_open.get(key) or {}).get("price")
        qty = r.get("qty") or (last_open.get(key) or {}).get("qty")
        lev = r.get("lev") or (last_open.get(key) or {}).get("lev") or 1
        side = r.get("side") or (last_open.get(key) or {}).get("side")
        px = r.get("price")
        if not (ep and px):
            continue
        d = -1 if (side or "LONG").upper() == "SHORT" else 1
        r["entry_price"] = ep
        r["pct"] = 100 * (px / ep - 1.0) * d * float(lev or 1)
        sizes, cmode, csingle = _cs.get(r.get("config"), ({}, None, None))
        cs = (1.0 if cmode == "spot"          # base units, never scaled
              else (sizes.get(r.get("symbol")) or csingle))
        if qty and cs:
            r["pnl"] = float(qty) * float(cs) * (px - ep) * d
        # --- upgrade to EXCHANGE TRUTH when the fills are known: actual fill
        # prices and fees, i.e. the number MEXC itself displays ---
        if r.get("live") and cmode != "spot":
            acct = (r.get("account") or "").split("/")[0] or "mexc1"
            if acct == "fcfs":
                acct = _cs.get(r.get("config"), ({}, None, None)) and None
                try:
                    acct = json.load(open(os.path.join(
                        AT, r["config"]))).get("api_account", "mexc1")
                except Exception:
                    acct = "mexc1"
            deals = _DEALS["fut"].get((acct, r.get("symbol"))) or []

            def _near(ts_str, closing):
                try:
                    tgt = time.mktime(time.strptime(ts_str,
                                                    "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    return None
                best, bd = None, 1e9
                for x in deals:
                    sd = int(x.get("side") or 0)
                    is_close = sd in (2, 4)
                    if is_close != closing:
                        continue
                    dt = abs(int(x.get("timestamp", 0)) / 1000 - tgt)
                    if dt < bd and dt < 180:
                        best, bd = x, dt
                return best
            dc = _near(r.get("at") or "", True)
            op_row = last_open.get(key) or {}
            do = _near(op_row.get("at") or "", False)
            if dc and do:
                fee = float(dc.get("fee") or 0) + float(do.get("fee") or 0)
                gross = float(dc.get("profit") or 0)
                epx, xpx = float(do["price"]), float(dc["price"])
                margin = (float(dc.get("vol") or qty or 0) * float(cs or 0)
                          * epx / float(lev or 1))
                if margin > 0:
                    r["pnl"] = gross - fee
                    r["pct"] = 100 * (gross - fee) / margin
                    r["entry_price"] = epx
                    r["price"] = xpx
                    r["exact"] = True       # fills + fees, as MEXC reports
    return jsonify(trades=out)


@app.route("/api/settings/proxies")
def settings_proxies():
    """Proxy-pool summary for the Settings page — never returns credentials."""
    out = dict(pool=None, accounts=[], legacy=None)
    try:
        pc = json.load(open(os.path.join(AT, "proxy_pool.json")))
        out["pool"] = dict(host=pc.get("host"), ports=pc.get("ports") or [],
                           n=len(pc.get("ports") or []),
                           username=(pc.get("username") or "")[:4] + "…")
    except Exception:
        pass
    try:
        k = json.load(open(os.path.join(AT, "mexc_api_keys.json")))
        accts = sorted((k.get("accounts") or {"mexc1": k}).keys())
        ports = (out["pool"] or {}).get("ports") or []
        for i, a in enumerate(accts):
            m = re.search(r"(\d+)$", a)
            idx = (int(m.group(1)) - 1) % len(ports) if (m and ports) else None
            out["accounts"].append(dict(
                name=a, proxy_port=(ports[idx] if idx is not None else None)))
    except Exception:
        pass
    try:
        lp = json.load(open(os.path.join(os.path.dirname(AT.rstrip("/")),
                                         "proxy_config.json")))
        out["legacy"] = (lp.get("server") or "").replace("http://", "")
    except Exception:
        pass
    return jsonify(out)


@app.route("/api/settings/proxy_test", methods=["POST"])
def settings_proxy_test():
    """Run the read-only proxy verification (scripts/test_proxies.py) as a
    job; poll /api/settings/proxy_test/<id> for output. Places no orders."""
    lab = os.path.dirname(AT.rstrip("/"))
    script = os.path.join(lab, "scripts", "test_proxies.py")
    if not os.path.exists(script):
        return jsonify(error="scripts/test_proxies.py not found"), 400
    jid = spawn("proxytest", "proxy pool verification",
                [sys.executable, script], lab)
    return jsonify(id=jid)


@app.route("/api/settings/proxy_test/<jid>")
def settings_proxy_test_out(jid):
    j = jobs.get(jid)
    if not j or j.get("kind") != "proxytest":
        return jsonify(error="unknown test id"), 404
    try:
        txt = open(j["log"]).read()[-20000:]
    except Exception:
        txt = ""
    running = j["proc"].poll() is None
    return jsonify(running=running, output=txt,
                   rc=(None if running else j["proc"].returncode))


@app.route("/api/param_space", methods=["GET", "POST"])
def param_space():
    """?space=<coin>_<tf>m (default: canonical param_space.json)
    &variant=imported|ai (default: imported).
    GET returns the variant's content (imported auto-seeds from the active
    file, and its defaults snapshot is taken on first sight).
    POST saves the variant AND makes it the ACTIVE space (what searches use).
    """
    sel = request.args.get("space", "")
    variant = request.args.get("variant", "imported")
    if variant not in ("imported", "ai"):
        return jsonify(error="variant must be imported|ai"), 400
    active, name = _space_paths(sel)
    if active is None:
        return jsonify(error=f"bad space name '{sel}'"), 400
    if not os.path.exists(active):
        return jsonify(error=f"no space file for {name} — run "
                             f"gen_pair_spaces.py"), 404
    vpath = _variant_path(name, variant)
    if variant == "imported" and not os.path.exists(vpath):
        # first sight: seed the imported variant + its defaults snapshot
        # from the currently-active file
        import shutil
        shutil.copy(active, vpath)
        dpath = _variant_path(name, "imported", defaults=True)
        if not os.path.exists(dpath):
            shutil.copy(active, dpath)
    if request.method == "GET":
        if not os.path.exists(vpath):
            return jsonify(error=f"no {variant} variant yet"
                           + (" — generate it (AI generate button or "
                              "gen_ai_spaces.py)" if variant == "ai" else "")), 404
        j = json.load(open(vpath))
        j.setdefault("_meta", {})["active_variant"] = _active_variant(name)
        return jsonify(j)
    d = request.get_json(force=True)
    d.setdefault("_meta", {})["variant"] = variant
    d["_meta"].pop("active_variant", None)
    json.dump(d, open(vpath, "w"), indent=1)
    import shutil
    shutil.copy(active, active + ".bak")
    json.dump(d, open(active, "w"), indent=1)   # saving ACTIVATES this variant
    json.dump(dict(active=variant),
              open(_variant_path(name, "active_marker"), "w"))
    return jsonify(ok=True, activated=variant)


def _active_variant(name):
    p = _variant_path(name, "active_marker")
    try:
        return json.load(open(p))["active"]
    except Exception:
        return "imported"


@app.route("/api/param_space/restore_defaults", methods=["POST"])
def param_space_restore():
    """Restore the IMPORTED variant to its defaults snapshot (the AI variant
    is never touched). Does not activate — hit Save to activate."""
    sel = (request.get_json(force=True) or {}).get("space", "")
    active, name = _space_paths(sel)
    if active is None:
        return jsonify(error="bad space name"), 400
    dpath = _variant_path(name, "imported", defaults=True)
    if not os.path.exists(dpath):
        return jsonify(error="no defaults snapshot yet"), 404
    import shutil
    shutil.copy(dpath, _variant_path(name, "imported"))
    return jsonify(ok=True)


@app.route("/api/param_space/set_defaults", methods=["POST"])
def param_space_set_defaults():
    """Snapshot the CURRENT imported variant as the new defaults."""
    sel = (request.get_json(force=True) or {}).get("space", "")
    active, name = _space_paths(sel)
    if active is None:
        return jsonify(error="bad space name"), 400
    vpath = _variant_path(name, "imported")
    if not os.path.exists(vpath):
        return jsonify(error="no imported variant yet"), 404
    import shutil
    shutil.copy(vpath, _variant_path(name, "imported", defaults=True))
    return jsonify(ok=True)


@app.route("/api/param_space/ai_generate", methods=["POST"])
def param_space_ai_generate():
    """Build/refresh the AI variant: mined min/max of actually-used indicator
    values (+optional LLM sanity pass). Runs synchronously (seconds unless
    the LLM is slow)."""
    d = request.get_json(force=True) or {}
    sel = d.get("space", "")
    active, name = _space_paths(sel)
    if active is None:
        return jsonify(error="bad space name"), 400
    import subprocess
    cmd = [sys.executable, "gen_ai_spaces.py", "--space", name]
    if d.get("no_llm"):
        cmd.append("--no-llm")
    r = subprocess.run(cmd, cwd=OPT, capture_output=True, text=True,
                       timeout=600)
    if r.returncode != 0:
        return jsonify(error=(r.stderr or r.stdout)[-400:]), 500
    return jsonify(ok=True, log=r.stdout[-600:])


@app.route("/api/param_spaces")
def param_spaces_list():
    out = ["default (SOL 3m)"]
    d = os.path.join(OPT, "param_spaces")
    if os.path.isdir(d):
        out += sorted(f[:-5] for f in os.listdir(d)
                      if re.fullmatch(r"[a-z]{2,6}_[135]m\.json", f))
    return jsonify(out)

def _opt2_cmd(d, name):
    """Payload -> optimize2_cli command (shared by single launch and sweep)."""
    cmd = [sys.executable, "optimize2_cli.py",
           "--strategy", d.get("strategy", "v7"),
           "--algo", d.get("algo", "genetic"),
           "--mode", d.get("mode", "lev"), "--method", d.get("method", "vol3"),
           "--procs", str(d.get("procs", 4)), "--batch", str(d.get("batch", 100)),
           "--name", name]
    if d.get("single_set"): cmd += ["--single-set"]
    if d.get("cadapt"): cmd += ["--cadapt"]
    if d.get("symbol") and d["symbol"] != "sol":
        cmd += ["--symbol", d["symbol"]]
    if d.get("tf") and str(d["tf"]) != "3":
        cmd += ["--tf", str(d["tf"])]
    if d.get("hours"): cmd += ["--hours", str(d["hours"])]
    if d.get("total"): cmd += ["--total", str(d["total"])]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    if d.get("max_dd"): cmd += ["--max-dd", str(d["max_dd"])]
    if d.get("holdout_days"): cmd += ["--holdout-days", str(d["holdout_days"])]
    if d.get("holdout_before"): cmd += ["--holdout-before", d["holdout_before"]]
    if d.get("holdout_between"): cmd += ["--holdout-between", d["holdout_between"]]
    if d.get("holdout_outside"): cmd += ["--holdout-outside", d["holdout_outside"]]
    if d.get("max_hold_days"): cmd += ["--max-hold-days", str(d["max_hold_days"])]
    if d.get("gap_mode"): cmd += ["--gap-mode", d["gap_mode"]]
    if d.get("lockbox"): cmd += ["--lockbox", d["lockbox"]]
    if d.get("scoring"): cmd += ["--scoring", d["scoring"]]
    if d.get("stop_score") is not None and d.get("stop_score") != "":
        cmd += ["--stop-score", str(d["stop_score"])]
    if d.get("sticky_oos"):
        cmd += ["--sticky-oos"]
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
    return cmd


OPTQ = dict(items=[], running=None)   # items: [jid, name, payload]
_OPTQ_LOCK = threading.Lock()
_OPTQ_WATCH = {"t": None}
_OPTQ_STORE = os.path.join(JOBS_DIR, "optq_pending.json")


def _optq_save():
    """Queued searches survive panel restarts (they used to evaporate)."""
    try:
        tmp = _OPTQ_STORE + ".tmp"
        json.dump(OPTQ["items"], open(tmp, "w"))
        os.replace(tmp, _OPTQ_STORE)
    except Exception:
        pass


def _optq_watch():
    """Consumer: starts the next queued search when the current one ends."""
    while True:
        time.sleep(4)
        with _OPTQ_LOCK:
            r = OPTQ["running"]
            busy = r is not None and r in jobs and jobs[r]["proc"].poll() is None
            if busy:
                continue
            if not OPTQ["items"]:
                OPTQ["running"] = None
                _OPTQ_WATCH["t"] = None
                return
            jid, name, d = OPTQ["items"].pop(0)
            OPTQ["running"] = jid
            _optq_save()
        spawn("optimize-v2", name, _opt2_cmd(d, name), OPT, jid=jid)


def _optq_launch(name, d):
    """Run now if no search is active, else queue (searches use all procs —
    parallel searches would thrash). Returns (jid, queued, position)."""
    with _OPTQ_LOCK:
        r = OPTQ["running"]
        busy = (r is not None and r in jobs and jobs[r]["proc"].poll() is None)             or bool(OPTQ["items"])
        jid = f"optimize-v2_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
        if busy:
            OPTQ["items"].append([jid, name, d])
            _optq_save()
            pos = len(OPTQ["items"])
            if _OPTQ_WATCH["t"] is None or not _OPTQ_WATCH["t"].is_alive():
                _OPTQ_WATCH["t"] = threading.Thread(target=_optq_watch, daemon=True)
                _OPTQ_WATCH["t"].start()
            return jid, True, pos
        OPTQ["running"] = jid
    spawn("optimize-v2", name, _opt2_cmd(d, name), OPT, jid=jid)
    with _OPTQ_LOCK:
        if _OPTQ_WATCH["t"] is None or not _OPTQ_WATCH["t"].is_alive():
            _OPTQ_WATCH["t"] = threading.Thread(target=_optq_watch, daemon=True)
            _OPTQ_WATCH["t"].start()
    return jid, False, 0


# restore queued searches dropped by a previous panel exit
try:
    _pending = json.load(open(_OPTQ_STORE))
except Exception:
    _pending = []
if _pending:
    OPTQ["items"] = [list(x) for x in _pending]
    print(f"launch queue: restored {len(_pending)} pending search(es) from disk")
    _OPTQ_WATCH["t"] = threading.Thread(target=_optq_watch, daemon=True)
    _OPTQ_WATCH["t"].start()


def _merge_name_guard(name, d):
    """A merge (resume_from with 2+ sources) must land in a FRESH run dir —
    the browser's dedupe only sees its loaded slice of runs; disk is truth."""
    rf = d.get("resume_from") or ""
    if "," not in rf:
        return name
    if f"runs/{name}" in rf.split(","):
        return name          # deliberate self-resume, leave it alone
    base, i = name, 2
    while os.path.isdir(os.path.join(OPT, "runs", name)):
        name = f"{base}_{i}"
        i += 1
    return name


@app.route("/api/jobs/optimize", methods=["POST"])
@app.route("/api/jobs/optimize2", methods=["POST"])
def job_optimize2():
    """One optimizer for every strategy (v7 / prime / v6 / scalpx).
    Sequential by design: a second launch queues behind the running one."""
    d = request.get_json(force=True)
    name = _safe_name(d.get("name")) or f"opt2_{time.strftime('%m%d_%H%M')}"
    name = _merge_name_guard(name, d)
    jid, queued, pos = _optq_launch(name, d)
    return jsonify(id=jid, queued=queued, position=pos, name=name)


_SC_SUF = {"classic": "cls", "worst_window": "wor", "underwater": "und"}


@app.route("/api/jobs/optimize2_sweep", methods=["POST"])
def job_optimize2_sweep():
    """Cross-product sweep: one search per (holdout x scoring) variation,
    each saved under a suffixed name. All legs go through the same global
    sequential queue as plain launches."""
    d = request.get_json(force=True)
    sweep = d.pop("sweep", None) or {}
    holds = sweep.get("holdouts") or [None]
    scores = sweep.get("scorings") or [None]
    base = _safe_name(d.get("name")) or f"opt2_{time.strftime('%m%d_%H%M')}"
    variants = []
    for h in holds:
        for sc in scores:
            v = dict(d)
            for k in ("train_end", "holdout_days", "holdout_before",
                      "holdout_between", "holdout_outside", "scoring"):
                v.pop(k, None)
            suf = ""
            if h:
                for k in ("train_end", "holdout_days", "holdout_before",
                          "holdout_between", "holdout_outside"):
                    if h.get(k):
                        v[k] = h[k]
                suf += "_" + _safe_name(h.get("suffix") or "h")
            if sc:
                if sc != "classic":
                    v["scoring"] = sc
                suf += "_" + _SC_SUF.get(sc, _safe_name(sc))
            variants.append((base + suf, v))
    if len(variants) < 2:
        return jsonify(error="sweep needs at least 2 variations — tick more "
                             "holdout/scoring boxes (or use plain Start search)"), 400
    first = None
    for vname, v in variants:
        jid, _, _ = _optq_launch(vname, v)
        first = first or jid
    return jsonify(id=first, count=len(variants),
                   names=[n for n, _ in variants])

@app.route("/api/jobs/ai_suggest", methods=["POST"])
def job_ai():
    d = request.get_json(force=True)
    cmd = [sys.executable, "ai_advisor.py", "--run", d["run"],
           "--n", str(d.get("n", 12))]
    if d.get("train_end"): cmd += ["--train-end", d["train_end"]]
    if d.get("max_dd"): cmd += ["--max-dd", str(d["max_dd"])]
    if d.get("holdout_days"): cmd += ["--holdout-days", str(d["holdout_days"])]
    if d.get("holdout_before"): cmd += ["--holdout-before", d["holdout_before"]]
    if d.get("holdout_between"): cmd += ["--holdout-between", d["holdout_between"]]
    if d.get("holdout_outside"): cmd += ["--holdout-outside", d["holdout_outside"]]
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

_R2C = {"loaded": False, "d": {}}          # dir -> [key, static_entry]
_R2C_PATH = os.path.join(OPT, "runs2_cache.json")


def _r2c_load():
    if not _R2C["loaded"]:
        try:
            _R2C["d"] = json.load(open(_R2C_PATH))
        except Exception:
            _R2C["d"] = {}
        _R2C["loaded"] = True


_RUNS2_TTL = {}      # lim -> (built_at, payload)

@app.route("/api/runs2")
def runs2():
    # ~27k run directories x 5 stat() each is ~135k syscalls per call; the
    # Optimize page polls this while jobs run, so concurrent rescans were
    # stacking up and saturating a core. A 20s snapshot is plenty fresh for
    # a list of finished runs.
    _lim_key = request.args.get("lim", "")
    _hit = _RUNS2_TTL.get(_lim_key)
    if _hit and time.time() - _hit[0] < 20 and not request.args.get("fresh"):
        return jsonify(_hit[1])
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
        if not os.path.exists(pool_p) and os.path.exists(best_p):
            # a run with a best_config is real even if its pool file went
            # missing (2026-08-09 merge-run incident) — use it for mtime
            pool_p = best_p
        if not os.path.exists(pool_p):
            continue
        # ---- mtime-keyed cache: pool2.json is ~MBs and there are ~9k runs;
        # parsing everything per request froze the page (combine bug) ----
        _r2c_load()
        _key = []
        for _f in (pool_p, best_p, os.path.join(runs_dir, d, "walkforward.json"),
                   os.path.join(runs_dir, d, "launch.json"),
                   os.path.join(runs_dir, d, "backtest_flags.json")):
            try:
                _key.append(os.path.getmtime(_f))
            except OSError:
                _key.append(0)
        _hit = _R2C["d"].get(d)
        if _hit and _hit[0] == _key:
            e = dict(_hit[1])
            e.update(best=os.path.exists(os.path.join(runs_dir, d, "marked_best")),
                     trading=trading_map.get(d), running=(d in running_names))
            try:
                e["rating"] = int(open(os.path.join(runs_dir, d, "rating")).read().strip())
            except Exception:
                e["rating"] = e.get("rating", 0)
            if d in running_names:
                prog_p = os.path.join(runs_dir, d, "progress.json")
                if os.path.exists(prog_p):
                    try:
                        pr = json.load(open(prog_p))
                        if time.time() - pr.get("updated", 0) < 900:
                            e["progress"] = dict(pct=pr.get("pct"), eta_s=pr.get("eta_s"),
                                evaluated_session=pr.get("evaluated_session"),
                                budget=pr.get("budget"), budget_type=pr.get("budget_type"),
                                phase=pr.get("phase"))
                    except Exception:
                        pass
            out.append(e)
            continue
        _R2C.setdefault("dirty", True)
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
                e["pair"] = bc.get("pair", "SOL_USDT")
                e["market_data"] = bc.get("market_data", "perp")
                e["timeframe"] = bc.get("timeframe", "3m")
                e["mode"] = bc.get("mode", e.get("mode"))
                e["no_survivors"] = ("cand" in bc and bc.get("cand") is None)
                e["holdout"] = bc.get("holdout")
                e["holdout_best"] = bc.get("holdout_best")
                e["holdout_top10"] = bc.get("holdout_top10")
                e["holdout_scan"] = bc.get("holdout_scan")
                e["holdout_survivors"] = bc.get("holdout_survivors")
                e["holdout_days"] = bc.get("holdout_days")
                e["holdout_before"] = bc.get("holdout_before")
                e["holdout_between"] = bc.get("holdout_between")
                e["holdout_outside"] = bc.get("holdout_outside")
                if e.get("strategy") in ("metax", "metax2", "fcfsx") or \
                        (bc.get("cand") or {}).get("strategy") in ("metax", "metax2", "fcfsx"):
                    _cc = (bc.get("cand") or {}).get("components") or []
                    _asn = {a for a in ((bc.get("cand") or {}).get("assign") or [])
                            if a is not None and a >= 0}
                    e["components"] = [dict(run=x.get("run"),
                                            strategy=x.get("strategy"),
                                            pair=x.get("pair"),
                                            timeframe=x.get("timeframe"),
                                            assigned=(k in _asn))
                                       for k, x in enumerate(_cc)]
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
        # store the static portion in the cache (dynamic bits recomputed per hit)
        _static = {k: v for k, v in e.items()
                   if k not in ("best", "trading", "running", "progress", "rating")}
        _static["rating"] = e.get("rating", 0)
        _R2C["d"][d] = [_key, _static]
        _R2C["dirty"] = True
        out.append(e)
    # LIMIT: with ~9k runs the full list is heavy; default to the newest
    # 1500 by activity plus everything running/trading/marked/rated.
    try:
        lim = int(request.args.get("lim", 1500))
    except Exception:
        lim = 1500
    if lim and len(out) > lim:
        keep = [e for e in out if e.get("running") or e.get("trading")
                or e.get("best") or (e.get("rating") or 0) > 0]
        _kid = set(map(id, keep))
        rest = [e for e in out if id(e) not in _kid]
        rest.sort(key=lambda e: e.get("last_run") or "", reverse=True)
        out = keep + rest[:max(0, lim - len(keep))]
    if _R2C.pop("dirty", False):
        try:
            _tmp = _R2C_PATH + ".tmp"
            json.dump(_R2C["d"], open(_tmp, "w"))
            os.replace(_tmp, _R2C_PATH)
        except Exception:
            pass
    _payload = _scrub(out)
    _RUNS2_TTL[_lim_key] = (time.time(), _payload)
    return jsonify(_payload)

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


BT_META = os.path.join(DASH, "bt_meta.json")


def _bt_meta_set(name, key, val):
    """Ratings/best marks live in a tiny sidecar (bt_meta.json) — updating
    them used to rewrite the entire multi-hundred-MB backtests.js (~5s) and
    could be clobbered by the offload merge loop."""
    import fcntl
    with open(BT_META + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            m = json.load(open(BT_META))
        except Exception:
            m = {}
        ent = m.setdefault(name, {})
        if val:
            ent[key] = val
        else:
            ent.pop(key, None)
        if not ent:
            m.pop(name, None)
        tmp = BT_META + ".tmp"
        json.dump(m, open(tmp, "w"))
        os.replace(tmp, BT_META)


@app.route("/api/bt_anchors")
def bt_anchors():
    """Anchor candidates for the Optimize page: STARRED/RATED backtests only
    (the old path parsed the entire backtests.js — 177MB — for a dropdown).
    Configs come from the per-entry detail files."""
    try:
        meta = json.load(open(BT_META))
    except Exception:
        meta = {}
    out = []
    det = os.path.join(DASH, "bt_detail")
    for nm, m in meta.items():
        if not (m.get("best") or (m.get("rating") or 0) > 0):
            continue
        try:
            cfg = json.load(open(os.path.join(det, nm + ".json"))).get("config")
        except Exception:
            cfg = None
        if cfg and cfg.get("strategy"):
            out.append(dict(name=nm, config=cfg))
    return jsonify(out)


@app.route("/api/backtests/mark", methods=["POST"])
def backtests_mark():
    """Toggle the 'best' star on a published backtest entry."""
    d = request.get_json(force=True)
    name = d.get("name")
    best = bool(d.get("best"))
    _bt_meta_set(name, "best", True if best else None)
    return jsonify(ok=True, name=name, best=best)


@app.route("/api/backtests/rate", methods=["POST"])
def backtests_rate():
    """Set a 1-3 star rating on a published backtest entry (0 clears)."""
    d = request.get_json(force=True)
    name = d.get("name")
    rating = max(0, min(3, int(d.get("rating", 0))))
    _bt_meta_set(name, "rating", rating or None)
    return jsonify(ok=True, name=name, rating=rating)


@app.route("/api/backtests/delete", methods=["POST"])
def backtests_delete():
    """Delete one entry ({name}) or many ({names:[...]}). Tombstones every
    deleted name in dashboard/bt_deleted.json so the 5-minute merge loop
    can't resurrect it from per-run payload copies or box mirrors."""
    d = request.get_json(force=True)
    names = set(d.get("names") or ([d["name"]] if d.get("name") else []))
    if not names:
        return jsonify(error="no names"), 400
    path = os.path.join(DASH, "backtests.js")
    tomb_p = os.path.join(DASH, "bt_deleted.json")
    import fcntl
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        txt = open(path).read()
        entries = json.JSONDecoder().raw_decode(
            txt[txt.index("=") + 1:].lstrip())[0]
        entries = [e for e in entries if e.get("name") not in names]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("window.BACKTESTS = ")
            json.dump(entries, f, default=float)
            f.write(";")
        os.replace(tmp, path)
        try:
            tomb = set(json.load(open(tomb_p))) if os.path.exists(tomb_p) else set()
        except Exception:
            tomb = set()
        tomb |= names
        json.dump(sorted(tomb), open(tomb_p, "w"))
    for name in names:
        _bt_meta_set(name, "rating", None)
        _bt_meta_set(name, "best", None)
        try:
            dp = os.path.join(DASH, "bt_detail", name + ".json")
            if os.path.exists(dp):
                os.remove(dp)
        except OSError:
            pass
    return jsonify(ok=True, deleted=len(names), remaining=len(entries))


if __name__ == "__main__":
    print("Control panel: http://127.0.0.1:8800")
    # PANEL_HOST=0.0.0.0 exposes the panel on the LAN (needed when the hub
    # runs on the mini and is browsed from other machines). No auth — keep it
    # on trusted networks only.
    app.run(host=os.environ.get("PANEL_HOST", "127.0.0.1"), port=8800,
            debug=False, threaded=True)
