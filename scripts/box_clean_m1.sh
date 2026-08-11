#!/bin/bash
# Runs ON the box: stop worker, quarantine invalid m1w_* runs (no-holdout bug),
# clear worker state, restart from the (already-pushed, fixed) plan.
tmux kill-session -t m1 2>/dev/null
pkill -f gamut_worker 2>/dev/null
sleep 2
cd ~/strategy-lab/optimizer || exit 1
mkdir -p runs_invalid
echo "quarantining: $(ls runs | grep -c m1w_)"
for d in runs/m1w_*; do [ -e "$d" ] && mv "$d" runs_invalid/; done
rm -f campaigns/m1sweep_0810/worker_state*.json ~/worker.log
~/boot_m1.sh
sleep 6
tail -3 ~/worker.log
