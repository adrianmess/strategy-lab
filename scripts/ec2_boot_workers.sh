#!/bin/bash
# BOX A boot (cron @reboot, fleet user-data, recovery). Idempotent.
# Session-existence guards (sessions die with their process — no pgrep traps).
# Jobs scale with the box: ~1 spec per 11 vCPUs (192->17, 96->8, min 4).
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_g0801_2122/plan.json --jobs $J 2>&1 | tee -a ~/worker.log"
# hype runs ALONGSIDE main (user 2026-08-07): small width while the main
# worker is alive, full width once it's gone (monitor bumps width after
# main completes if this boot raced the main worker's quick exit)
tmux has-session -t hype 2>/dev/null || tmux new-session -d -s hype \
  "HJ=$J; tmux has-session -t gamut 2>/dev/null && HJ=5; . ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_ghype/plan.json --jobs \$HJ 2>&1 | tee -a ~/worker_hype.log"
echo "[$(date '+%F %T')] boot_workers ran (jobs=$J)" >> ~/boot_workers.log
