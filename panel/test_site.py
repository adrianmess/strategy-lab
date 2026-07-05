#!/usr/bin/env python3
"""Automated test of every site endpoint and UI-triggered action.

    python3 panel/test_site.py          (starts its own server on port 8801)

Exercises: pages, doctor, status, trader config editing, configs/runs listings,
param space round-trip, backtest job end-to-end, optimize2 job end-to-end,
backtests delete, webhook status, job stop. Prints PASS/FAIL per check.
"""
import json, os, subprocess, sys, time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BASE = "http://127.0.0.1:8801"
results = []


def req(path, body=None, method=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=180) as resp:
        raw = resp.read().decode()
        try:
            return resp.status, json.loads(raw)
        except Exception:
            return resp.status, raw


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
    except Exception as e:
        results.append((name, False, str(e)[:200]))
        print(f"FAIL  {name}: {str(e)[:200]}")


def wait_job(jid, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, jobs = req("/api/jobs")
        j = next((x for x in jobs if x["id"] == jid), None)
        if j and j["status"] != "running":
            assert "done (0)" in j["status"], f"job ended badly: {j['status']} log={j['log'][-3:]}"
            return j
        time.sleep(3)
    raise TimeoutError(f"job {jid} still running after {timeout}s")


def main():
    group = sys.argv[1] if len(sys.argv) > 1 else "all"
    env = dict(os.environ)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                            stdout=open("/tmp/test_site_server.log", "w"),
                            stderr=subprocess.STDOUT, env=env,
                            cwd=HERE)
    # patch port via env? server hardcodes 8800; run tests against 8800 unless busy
    global BASE
    BASE = "http://127.0.0.1:8800"
    time.sleep(3)
    try:
      if group in ("all", "quick"):
        check("page /", lambda: req("/")[0] == 200 or (_ for _ in ()).throw(AssertionError))
        for p in ["optimize.html", "docs.html", "backtests.html", "index.html"]:
            check(f"page /dashboard/{p}", lambda p=p: (
                (lambda s, b: None if (s == 200 and "nav" in str(b)) else (_ for _ in ()).throw(AssertionError(f"status {s}")))(*req(f"/dashboard/{p}"))))
        check("GET /api/doctor", lambda: (
            (lambda s, b: None if s == 200 and "checks" in b else (_ for _ in ()).throw(AssertionError(b)))(*req("/api/doctor"))))
        check("GET /api/status", lambda: (
            (lambda s, b: None if s == 200 and "running" in b else (_ for _ in ()).throw(AssertionError))(*req("/api/status"))))
        check("GET /api/configs", lambda: (
            (lambda s, b: None if s == 200 and isinstance(b, list) and len(b) >= 3 else (_ for _ in ()).throw(AssertionError(len(b) if isinstance(b, list) else b)))(*req("/api/configs"))))
        check("GET /api/runs2", lambda: (
            (lambda s, b: None if s == 200 and isinstance(b, list) and any(r.get("holdout") for r in b) else (_ for _ in ()).throw(AssertionError))(*req("/api/runs2"))))
        check("GET /api/webhook/status", lambda: (
            (lambda s, b: None if s == 200 else (_ for _ in ()).throw(AssertionError))(*req("/api/webhook/status"))))

        def cfg_roundtrip():
            _, before = req("/api/trader_config?file=config.json")
            s, r = req("/api/trader_config?file=config.json", {"equity_usdt": 1234.0})
            assert r.get("ok")
            _, after = req("/api/trader_config?file=config.json")
            assert after["equity_usdt"] == 1234.0
            req("/api/trader_config?file=config.json", {"equity_usdt": before["equity_usdt"]})
        check("trader_config GET/POST round-trip", cfg_roundtrip)

        def space_roundtrip():
            _, sp = req("/api/param_space")
            assert "v7" in sp
            old = sp["v7"]["continuous"]["zL"]["range"][0]
            sp["v7"]["continuous"]["zL"]["range"][0] = -3.1
            s, r = req("/api/param_space", sp)
            assert r.get("ok")
            _, sp2 = req("/api/param_space")
            assert sp2["v7"]["continuous"]["zL"]["range"][0] == -3.1
            sp2["v7"]["continuous"]["zL"]["range"][0] = old
            req("/api/param_space", sp2)
        check("param_space GET/POST round-trip", space_roundtrip)

      if group in ("all", "jobs", "bt", "opt", "misc"):
        def backtest_e2e():
            s, r = req("/api/jobs/backtest",
                       {"config": "../adaptive_trader/research2/final_config_v6_spot_vol3.json",
                        "name": "__uitest_bt"})
            wait_job(r["id"])
            _, txt = req("/dashboard/backtests.js")
            assert "__uitest_bt" in str(txt)
            s, r = req("/api/backtests/delete", {"name": "__uitest_bt"})
            assert r.get("ok")
        if group in ("all", "jobs", "bt"):
            check("backtest job end-to-end (+publish +delete)", backtest_e2e)

        def optimize2_e2e():
            s, r = req("/api/jobs/optimize2",
                       {"algo": "random", "mode": "spot", "method": "none",
                        "procs": 2, "total": 60, "batch": 15, "name": "__uitest_opt"})
            wait_job(r["id"])
            _, runs = req("/api/runs2")
            assert any(x["name"] == "__uitest_opt" for x in runs)
        if group in ("all", "jobs", "opt"):
            check("optimize2 job end-to-end (+runs listing)", optimize2_e2e)

        def job_stop():
            s, r = req("/api/jobs/optimize2",
                       {"algo": "random", "mode": "spot", "method": "none",
                        "procs": 2, "hours": 1, "batch": 500, "name": "__uitest_stop"})
            time.sleep(2)
            s, r2 = req(f"/api/jobs/{r['id']}/stop", {})
            assert r2.get("ok")
        if group in ("all", "jobs", "misc"):
            check("job stop", job_stop)
            check("POST /api/doctor/fix_caches", lambda: (
                (lambda s, b: None if s == 200 and "removed" in b else (_ for _ in ()).throw(AssertionError))(*req("/api/doctor/fix_caches", {}))))

    finally:
        proc.terminate()
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} checks passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
