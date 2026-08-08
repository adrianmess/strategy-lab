#!/bin/bash
# BOX B boot: REVERSE worker on the main plan, no hype watcher. Idempotent.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs $J --reverse 2>&1 | tee -a ~/worker.log"
# hype REVERSE alongside/after main (2026-08-08): small while main alive, full otherwise
tmux has-session -t hype 2>/dev/null || tmux new-session -d -s hype \
  "HJ=$J; tmux has-session -t gamut 2>/dev/null && HJ=4; . ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_ghype/plan.json --jobs \$HJ --reverse 2>&1 | tee -a ~/worker_hype.log"
echo "[$(date '+%F %T')] boot_workers_b ran (jobs=$J)" >> ~/boot_workers.log
