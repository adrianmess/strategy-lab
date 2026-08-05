#!/bin/bash
# BOX B boot: REVERSE worker on the main plan, no hype watcher. Idempotent.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs $J --reverse 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_workers_b ran (jobs=$J)" >> ~/boot_workers.log
