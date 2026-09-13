#!/bin/bash
# BOX A boot for the gspot_newpairs campaign (cron @reboot, fleet user-data,
# recovery). Idempotent; session-existence guards. ~1 spec per 11 vCPUs.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
# the live core-budget file OVERRIDES --jobs, and the AMI carries a stale
# MacBook value ({"cores":12}) — a fleet replacement ran a 192-core box at
# 12 for hours (2026-09-10). Always reset it to this machine's true width.
echo "{\"cores\": $(nproc)}" > ~/strategy-lab/optimizer/gamut_limits.json
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_gspot_newpairs/plan.json --jobs $J 2>&1 | tee -a ~/worker.log"
# gorig_mh12 rides ALONGSIDE the gspot tail (repurposed A/B, 2026-09-13):
# both workers share the live core budget; gspot's tail claims little
printf 'gamut_gspot_newpairs\ngamut_gorig_mh12\n' > ~/PLAN_NAME
tmux has-session -t gamut2 2>/dev/null || tmux new-session -d -s gamut2 \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/gamut_gorig_mh12/plan.json --jobs $J 2>&1 | tee -a ~/worker_orig.log"
echo "[$(date '+%F %T')] boot_workers_spotnp ran (jobs=$J, + gorig)" >> ~/boot_workers.log
