#!/bin/bash
# Re-cost every stale published backtest at BOTH sides of the book, on this
# machine, submitting to the mini's panel. ~18h at 12 procs for all 4 shards.
#
#   scripts/run_recost.sh [procs] [shards...]
#   scripts/run_recost.sh 12 01 02 03 04     # the default
#
# Shards resume: each writes <shard>.done, so re-running skips finished work.
# The panel key is read from ~/.strategy_lab_panel_key (chmod 600) rather than
# passed on the command line, so it never shows up in ps.
set -u
L="$HOME/Code/strategy-lab"
PY="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
HUB="http://admns-Mac-mini.local:8800"
PROCS="${1:-12}"; shift 2>/dev/null || true
SHARDS=("$@"); [ ${#SHARDS[@]} -eq 0 ] && SHARDS=(01 02 03 04)

if [ ! -f "$HOME/.strategy_lab_panel_key" ]; then
  echo "missing ~/.strategy_lab_panel_key — off-box submits need it"; exit 1
fi
export PANEL_KEY="$(cat "$HOME/.strategy_lab_panel_key")"

cd "$L" || exit 1
echo "=== re-cost starting $(date '+%F %T') · ${PROCS} procs · shards ${SHARDS[*]} ==="
for s in "${SHARDS[@]}"; do
  f="dashboard/bt_refresh/fee_shard_${s}.json"
  [ -f "$f" ] || { echo "!! missing $f — skipping"; continue; }
  echo "--- shard $s starting $(date '+%F %T')"
  "$PY" scripts/refresh_backtests_worker.py \
      --shard "$f" --procs "$PROCS" --hub "$HUB"
  echo "--- shard $s finished $(date '+%F %T') rc=$?"
done
echo "=== re-cost done $(date '+%F %T') ==="
