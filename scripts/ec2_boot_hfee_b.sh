#!/bin/bash
# BOX B boot for gamut_hfee_mh12 — the honest-fee re-search of the gorig_mh12
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
# CORE BUDGET — size from THIS box, never from whatever shipped in the repo
# bundle. Self-sufficient ON PURPOSE: a spot replacement boots from the LAUNCH
# TEMPLATE's embedded user-data, which is a SNAPSHOT — updating the copy in S3
# does not change what a replacement runs. On 2026-09-19/20 both boxes were
# replaced, came back without ~/ec2_size_cores.sh, the old `[ -x ... ] &&`
# guard skipped SILENTLY, and they ran ~17h and ~3h at 1/17th capacity at full
# spot price. So: fetch the sizer, and if that fails size inline anyway —
# never fall through to the inherited value.
[ -x ~/ec2_size_cores.sh ] || aws s3 cp \
    s3://gamut-sync-637309463295/code/ec2_size_cores.sh \
    ~/ec2_size_cores.sh >/dev/null 2>&1
chmod +x ~/ec2_size_cores.sh 2>/dev/null
if [ -x ~/ec2_size_cores.sh ]; then
  ~/ec2_size_cores.sh
else
  echo "{\"cores\": $(( J * 14 ))}" > ~/strategy-lab/optimizer/gamut_limits.json
  echo "[$(date '+%F %T')] size_cores MISSING — wrote cores=$(( J * 14 )) inline" \
    >> ~/boot_workers.log
fi
# self-heal every 10 min regardless of what the launch template installed
( crontab -l 2>/dev/null | grep -v ec2_size_cores
  echo "*/10 * * * * ~/ec2_size_cores.sh --quiet" ) | crontab -


tmux has-session -t keeper 2>/dev/null || tmux new-session -d -s keeper 'sleep infinity'
tmux has-session -t gamut 2>/dev/null || tmux new-session -d -s gamut \
  ". ~/venv/bin/activate && cd ~/strategy-lab/optimizer && python3 gamut_worker.py --plan campaigns/$CAMP/plan.json --jobs $J --reverse 2>&1 | tee -a ~/worker.log"
echo "[$(date '+%F %T')] boot_hfee ran REVERSE (jobs=$J)" >> ~/boot_workers.log
