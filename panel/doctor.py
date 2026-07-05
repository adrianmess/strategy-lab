#!/usr/bin/env python3
"""Environment doctor — checks everything the site needs and says exactly
what's wrong and how to fix it. Used by /api/doctor and runnable directly:
    python3 panel/doctor.py
"""
import glob
import importlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

MIN_VERSIONS = dict(numpy=(1, 26), pandas=(2, 0), numba=(0, 59), flask=(2, 0))


def _vtuple(s):
    out = []
    for part in s.split(".")[:3]:
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num or 0))
    return tuple(out)


def check_imports():
    results = []
    for mod in ["numpy", "pandas", "pyarrow", "numba", "flask", "requests"]:
        try:
            m = importlib.import_module(mod)
            try:
                from importlib.metadata import version as _ver
                v = _ver(mod)
            except Exception:
                v = getattr(m, "__version__", "?")
            ok = True
            note = f"v{v}"
            if mod in MIN_VERSIONS and _vtuple(v) < MIN_VERSIONS[mod]:
                ok = False
                note = f"v{v} is too old (need >= {'.'.join(map(str, MIN_VERSIONS[mod]))})"
            results.append(dict(check=f"import {mod}", ok=ok, note=note))
        except Exception as e:
            results.append(dict(check=f"import {mod}", ok=False,
                                note=f"{type(e).__name__}: {str(e)[:200]}"))
    return results


def check_numpy_health():
    """The classic 'C-extensions failed' problem shows up here."""
    try:
        import numpy as np
        a = np.arange(10, dtype=np.float64)
        assert float(a.sum()) == 45.0
        return [dict(check="numpy C-extensions", ok=True, note="working")]
    except Exception as e:
        return [dict(check="numpy C-extensions", ok=False,
                     note=f"{str(e)[:200]} — fix: python3 -m pip install --upgrade --force-reinstall numpy pandas pyarrow numba")]


def check_numba_jit():
    try:
        from numba import njit
        import numpy as np

        @njit(cache=False)
        def f(x):
            s = 0.0
            for i in range(len(x)):
                s += x[i]
            return s
        assert f(np.arange(5.0)) == 10.0
        return [dict(check="numba JIT compile", ok=True, note="working")]
    except Exception as e:
        return [dict(check="numba JIT compile", ok=False,
                     note=f"{str(e)[:200]} — often a numpy/numba version mismatch; try: pip install -U numba")]


def find_caches():
    pats = ["adaptive_trader/research/*.pkl", "adaptive_trader/research2/*.pkl",
            "optimizer/runs/*/*.pkl", "optimizer/runs/_backtest_tmp/*.pkl"]
    out = []
    for p in pats:
        out.extend(glob.glob(os.path.join(REPO, p)))
    return sorted(set(out))


def check_caches():
    """Caches pickled under a different numpy major version fail to load.
    They're all rebuildable, so a failing cache is a warning, not an error —
    the loaders auto-rebuild now — but clearing them avoids rebuild-on-first-use
    surprises inside long jobs."""
    import pickle
    results = []
    bad = []
    for path in find_caches():
        try:
            with open(path, "rb") as f:
                pickle.load(f)
        except Exception as e:
            bad.append(path)
    if bad:
        results.append(dict(check=f"pickle caches ({len(find_caches())} found)", ok=False,
                            note=f"{len(bad)} unreadable under this numpy — will auto-rebuild on use, "
                                 f"or clear them now with the button / doctor.py --fix-caches",
                            bad_caches=[os.path.relpath(b, REPO) for b in bad]))
    else:
        results.append(dict(check=f"pickle caches ({len(find_caches())} found)", ok=True,
                            note="all readable"))
    return results


def check_data():
    results = []
    for f in ["sol_3min.parquet", "sol_1min.parquet"]:
        p = os.path.join(REPO, "adaptive_trader", "research", "data", f)
        if os.path.exists(p):
            results.append(dict(check=f"data {f}", ok=True,
                                note=f"{os.path.getsize(p)//1024//1024} MB"))
        else:
            results.append(dict(check=f"data {f}", ok=False,
                                note="missing — run 'Refresh CoinAPI data' or research/download_data.py"))
    return results


def run_all():
    checks = []
    checks += check_imports()
    checks += check_numpy_health()
    checks += check_numba_jit()
    checks += check_data()
    checks += check_caches()
    checks.append(dict(check="python", ok=sys.version_info >= (3, 9),
                       note=f"{sys.version.split()[0]} @ {sys.executable}"))
    return dict(ok=all(c["ok"] for c in checks), checks=checks)


def fix_caches():
    removed = []
    import pickle
    for path in find_caches():
        try:
            with open(path, "rb") as f:
                pickle.load(f)
        except Exception:
            try:
                os.remove(path)
                removed.append(os.path.relpath(path, REPO))
            except OSError as e:
                removed.append(f"FAILED {path}: {e}")
    return removed


if __name__ == "__main__":
    if "--fix-caches" in sys.argv:
        for r in fix_caches():
            print("removed:", r)
    r = run_all()
    for c in r["checks"]:
        print(("OK  " if c["ok"] else "FAIL"), c["check"], "—", c["note"])
    print("\nOVERALL:", "HEALTHY" if r["ok"] else "PROBLEMS FOUND (see FAIL lines)")
    sys.exit(0 if r["ok"] else 1)
