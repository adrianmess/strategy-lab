#!/bin/bash
# BOX B variant: REVERSE worker on the main plan (meet-in-the-middle with
# box A), jobs=6 (96 vCPU box), no hype watcher (box A owns that).
export PATH=/usr/bin:/bin:/usr/local/bin
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
if ! pgrep -xf "python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs 6 --reverse" >/dev/null; then
  tmux kill-session -t gamut 2>/dev/null
  tmux new-session -d -s gamut '. ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs 6 --reverse 2>&1 | tee -a ~/worker.log'
fi
echo "[$(date '+%F %T')] boot_workers_b ran" >> ~/boot_workers.log
