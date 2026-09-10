#!/bin/bash
# BOX A boot for the gspot_newpairs campaign (cron @reboot, fleet user-data,
# recovery). Idempotent; session-existence guards. ~1 spec per 11 vCPUs.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_gspot_newpairs/plan.json --jobs $J 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_workers_spotnp ran (jobs=$J)" >> ~/boot_workers.log
