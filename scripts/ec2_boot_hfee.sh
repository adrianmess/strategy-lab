#!/bin/bash
# BOX A boot for gamut_hfee_mh12 — the honest-fee re-search of the gorig_mh12
# grid. Idempotent; safe from cron @reboot, fleet user-data or by hand.
# Jobs scale with the box: ~1 spec per 11 vCPUs (192->17, 96->8, min 4).
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin:$HOME/.local/bin
CAMP=gamut_hfee_mh12

# ---- FEE GUARD -------------------------------------------------------------
# This campaign exists ONLY to re-search at the fees the account actually pays.
# A box whose fees.json still carries MEXC's advertised web rates would burn
# hours reproducing the exact error we are replacing, and it would look like a
# successful run. Refuse to start instead.
~/venv/bin/python3 - <<'CHK'
import json, sys
d = json.load(open('/home/ubuntu/strategy-lab/adaptive_trader/fees.json'))
fut = d.get('fut', {})
bad = [k for k, v in fut.items() if (v.get('taker') or 0) < 0.0008
       or (v.get('maker') or 0) < 0.0006]
sys.exit(1 if (bad or not fut) else 0)
CHK
if [ $? -ne 0 ]; then
  echo "[$(date '+%F %T')] FEE GUARD TRIPPED — fees.json is not the corrected one; workers NOT started" >> ~/boot_workers.log
  exit 1
fi

J=$(( $(nproc) / 11 )); [ "$J" -lt 4 ] && J=4
tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/$CAMP/plan.json --jobs $J 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_hfee ran FORWARD (jobs=$J)" >> ~/boot_workers.log
