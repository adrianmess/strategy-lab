#!/bin/bash
# Runs ON the mini: stop worker, quarantine invalid msw_* runs, clear state,
# restart the reverse worker on the (already-pushed, fixed) plan.
pkill -f "gamut[_]worker" 2>/dev/null
pkill -f "optimize2[_]cli" 2>/dev/null
sleep 2
cd ~/strategy-lab/optimizer || exit 1
mkdir -p runs_invalid
echo "quarantining: $(ls runs | grep -c '^msw_')"
for d in runs/msw_*; do [ -e "$d" ] && mv "$d" runs_invalid/ 2>/dev/null; done
echo "left: $(ls runs | grep -c '^msw_')"
rm -f campaigns/msweep_0810/worker_state*.json ~/worker.log
nohup caffeinate -i ~/venv/bin/python3 gamut_worker.py --plan campaigns/msweep_0810/plan.json --jobs 1 --reverse >> ~/worker.log 2>&1 &
sleep 5
tail -2 ~/worker.log
