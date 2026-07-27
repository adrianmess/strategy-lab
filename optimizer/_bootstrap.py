"""Path bootstrap for the optimizer CLIs.

Adds the research code to sys.path and switches the CWD into a run directory
so every run keeps its own caches/artifacts. Import this FIRST in every CLI.
"""
import os, sys

OPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(OPT_DIR)
RESEARCH = os.path.join(REPO, "adaptive_trader", "research")
RESEARCH2 = os.path.join(REPO, "adaptive_trader", "research2")
DASHBOARD = os.path.join(REPO, "dashboard")

sys.path.insert(0, RESEARCH2)
sys.path.insert(0, RESEARCH)

# ONE shared precompute cache for every CLI (optimizer, backtests, metax).
# Mixing cache locations mixes data snapshots: segment lengths then disagree
# across families and per-bar regime/variant indexing breaks (shape-mismatch
# crashes, or silently misaligned sims).
os.environ.setdefault("WF2_CACHE_DIR", os.path.join(OPT_DIR, "cache"))


def safe_name(name: str) -> str:
    """Run names become folder names: strip path separators & odd characters."""
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "run"


def enter_run_dir(name: str) -> str:
    run_dir = os.path.join(OPT_DIR, "runs", safe_name(name))
    os.makedirs(run_dir, exist_ok=True)
    os.chdir(run_dir)
    return run_dir
