#!/bin/bash
# Runs ON a box via cron (*/10). When EVERY spec in the campaign plan has a
# durable done-marker, do a final S3 push and tear THIS box down — the fix
# for the August bill, where finished boxes idled at $3/hr until noticed.
#
# Fleet instances: delete our own fleet (maintain-mode would otherwise just
# replace a stopped instance). Plain spot: cancel the persistent request
# first (else it relaunches forever), then terminate.
#
# Needs on the gamut-box role: ec2:DescribeInstances, ec2:DescribeTags,
# ec2:DeleteFleets, ec2:CancelSpotInstanceRequests, ec2:TerminateInstances
# (docs/ec2/gamut_box_iam_policy.json). EIPs/AMI/S3 stay — released by hand
# per the runbook teardown checklist.
export PATH=/usr/bin:/bin:/usr/local/bin:/snap/bin
# which campaign THIS box answers to: ~/PLAN_NAME holds the campaign dir
# (written by the fleet user-data). Fallback = the original gspot campaign
# so the already-running boxes keep working unchanged.
CAMPDIRS=$(cat "$HOME/PLAN_NAME" 2>/dev/null || echo gamut_gspot_newpairs)
GUARD="$HOME/TEARDOWN_FIRED"
LOG="$HOME/autoteardown.log"
R=us-east-2

[ -f "$GUARD" ] && exit 0

# ~/PLAN_NAME may list SEVERAL campaigns (one per line) — the box tears
# down only when EVERY spec of EVERY listed plan has a durable marker
LEFT=$(python3 - $CAMPDIRS <<'EOF'
import json, os, sys
runs = os.path.expanduser('~/strategy-lab/optimizer/runs')
left = 0
for camp in sys.argv[1:]:
    p = os.path.expanduser(f'~/strategy-lab/optimizer/campaigns/{camp}/plan.json')
    if not os.path.exists(p):
        left += 1          # plan not even synced yet — definitely not done
        continue
    plan = json.load(open(p))
    left += sum(1 for s in plan['specs']
                if not (os.path.exists(os.path.join(runs, s['name'], 'best_config.json'))
                        or os.path.exists(os.path.join(runs, s['name'], 'no_survivor.json'))))
print(left)
EOF
)
if [ "$LEFT" != "0" ]; then
  echo "[$(date '+%F %T')] $LEFT specs remain" >> "$LOG"
  exit 0
fi

echo "[$(date '+%F %T')] campaign COMPLETE — final push then self-teardown" >> "$LOG"
touch "$GUARD"
"$HOME/box_s3_push.sh"
sleep 5
"$HOME/box_s3_push.sh"          # second pass: catch files written during the first

TOK=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/instance-id)
FLEET=$(aws ec2 describe-tags --region $R \
  --filters "Name=resource-id,Values=$IID" "Name=key,Values=aws:ec2:fleet-id" \
  --query 'Tags[0].Value' --output text 2>>"$LOG")
if [ -n "$FLEET" ] && [ "$FLEET" != "None" ]; then
  echo "[$(date '+%F %T')] deleting fleet $FLEET (terminates this instance)" >> "$LOG"
  aws ec2 delete-fleets --region $R --fleet-ids "$FLEET" \
    --terminate-instances >> "$LOG" 2>&1
else
  SIR=$(aws ec2 describe-instances --region $R --instance-ids "$IID" \
    --query 'Reservations[0].Instances[0].SpotInstanceRequestId' --output text 2>>"$LOG")
  if [ -n "$SIR" ] && [ "$SIR" != "None" ]; then
    echo "[$(date '+%F %T')] cancelling spot request $SIR" >> "$LOG"
    aws ec2 cancel-spot-instance-requests --region $R \
      --spot-instance-request-ids "$SIR" >> "$LOG" 2>&1
  fi
  echo "[$(date '+%F %T')] terminating self ($IID)" >> "$LOG"
  aws ec2 terminate-instances --region $R --instance-ids "$IID" >> "$LOG" 2>&1
fi
