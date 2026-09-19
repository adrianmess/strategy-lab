#!/bin/bash
# Fleet launch-template user-data for gamut_hfee_mh12 (honest-fee re-search).
# Makes every fleet replacement self-arming: grab the box's Elastic IP, pull
# the latest code + done-markers from S3 so a dead box's completions are never
# redone, then start the worker in the right direction.
#
# Two placeholders are substituted when the launch template is created:
#   __EIPALLOC__   the Elastic IP allocation id for this box
#   __DIRECTION__  fwd | rev   (box A walks the plan forward, box B backward)
set -x
exec > /var/log/gamut-fleet.log 2>&1
B=s3://gamut-sync-637309463295
R=us-east-2

IID=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 create-tags --region $R --resources "$IID" \
    --tags Key=Name,Value=gamut-hfee-__DIRECTION__ || true
aws ec2 associate-address --region $R --instance-id "$IID" \
    --allocation-id __EIPALLOC__ --allow-reassociation || true

sudo -u ubuntu bash <<'USERPART'
set -x
cd /home/ubuntu
B=s3://gamut-sync-637309463295
echo __DIRECTION__ > ~/hfee_direction
echo gamut_hfee_mh12 > ~/PLAN_NAME
rm -f ~/TEARDOWN_FIRED

# refresh the worker + boot scripts in case they changed since the AMI
aws s3 cp $B/code/gamut_worker.py ~/strategy-lab/optimizer/gamut_worker.py || true
for f in ec2_boot_hfee.sh ec2_boot_hfee_b.sh box_s3_push.sh box_autoteardown.sh ec2_size_cores.sh; do
  aws s3 cp $B/code/$f ~/$f || true
done
chmod +x ~/*.sh

# done-markers from any previous box: a replacement must not redo finished work
aws s3 sync $B/runs/ ~/strategy-lab/optimizer/runs/ --only-show-errors || true
aws s3 sync $B/state/ ~/strategy-lab/optimizer/campaigns/gamut_hfee_mh12/ \
    --exclude "*" --include "worker_state*.json" --only-show-errors || true

mkdir -p ~/strategy-lab/dashboard
[ -f ~/strategy-lab/dashboard/backtests.js ] || \
    echo "window.BACKTESTS = [];" > ~/strategy-lab/dashboard/backtests.js

( crontab -l 2>/dev/null | grep -v "boot_dispatch\|box_s3_push\|box_autoteardown\|ec2_size_cores"
  echo "@reboot sleep 30 && ~/boot_dispatch.sh"
  echo "*/5 * * * * ~/box_s3_push.sh"
  echo "*/10 * * * * ~/box_autoteardown.sh"
  echo "*/10 * * * * ~/ec2_size_cores.sh --quiet" ) | crontab -

~/boot_dispatch.sh
USERPART

loginctl enable-linger ubuntu || true
echo "fleet userdata finished $(date)" >> /var/log/gamut-fleet.log
