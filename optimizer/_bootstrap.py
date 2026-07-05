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


def enter_run_dir(name: str) -> str:
    run_dir = os.path.join(OPT_DIR, "runs", name)
    os.makedirs(run_dir, exist_ok=True)
    os.chdir(run_dir)
    return run_dir
