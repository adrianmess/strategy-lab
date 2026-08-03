#!/bin/bash
# Runs ON the box (cron @reboot + recovery). Starts whatever isn't running.
# Safe to run repeatedly.
export PATH=/usr/bin:/bin:/usr/local/bin
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
if ! pgrep -xf "python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs 13" >/dev/null; then
  tmux kill-session -t gamut 2>/dev/null
  tmux new-session -d -s gamut 'source ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs 13 2>&1 | tee -a ~/worker.log'
fi
tmux has-session -t hype 2>/dev/null || tmux new-session -d -s hype 'while pgrep -xf "python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs 13" >/dev/null; do sleep 120; done; source ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_ghype/plan.json --jobs 13 2>&1 | tee -a ~/worker_hype.log'
tmux has-session -t btrebuild 2>/dev/null || tmux new-session -d -s btrebuild '~/rebuild_missing_bts.sh 2>&1 | tee -a ~/rebuild_bts.log'
echo "[$(date '+%F %T')] boot_workers ran" >> ~/boot_workers.log
