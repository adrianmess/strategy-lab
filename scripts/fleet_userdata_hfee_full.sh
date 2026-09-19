#!/bin/bash
# Self-contained fleet user-data for gamut_hfee_mh12: takes a STOCK Ubuntu
# 22.04 AMI all the way to an armed worker, so the fleet doesn't have to wait
# on a baked AMI. ~10 minutes. Once gamut-worker-hfee AMI is available, point
# the launch template at it instead — this same script then short-circuits
# (the venv and data are already present) and the box arms in under a minute.
#
# Placeholders substituted at launch-template creation:
#   __EIPALLOC__   Elastic IP allocation id for this box
#   __DIRECTION__  fwd | rev
set -x
exec > /var/log/gamut-fleet.log 2>&1
B=s3://gamut-sync-637309463295
R=us-east-2

export DEBIAN_FRONTEND=noninteractive
# Fresh Ubuntu boots run unattended-upgrades, which holds the apt/dpkg locks.
# Without this wait, add-apt-repository below blocks for many minutes and the
# box sits idle at full spot price (box A lost this race on 2026-09-19 while
# box B won it and armed 15 minutes earlier).
systemctl stop unattended-upgrades 2>/dev/null || true
for i in $(seq 1 60); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1 || break
  sleep 5
done
apt-get update -y
apt-get install -y awscli tmux python3-venv rsync

IID=$(curl -s --max-time 5 http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 create-tags --region $R --resources "$IID" \
    --tags Key=Name,Value=gamut-hfee-__DIRECTION__ || true
aws ec2 associate-address --region $R --instance-id "$IID" \
    --allocation-id __EIPALLOC__ --allow-reassociation || true

# Ubuntu 22.04 ships python3.10; the pinned stack (numpy 2.4.6 / pandas 3.0.5
# / numba 0.66) needs >=3.11, so bring 3.11 in explicitly.
if ! command -v python3.11 >/dev/null 2>&1; then
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
  apt-get install -y python3.11 python3.11-venv python3.11-dev
fi

sudo -u ubuntu bash <<'USERPART'
set -x
cd /home/ubuntu
B=s3://gamut-sync-637309463295
echo __DIRECTION__ > ~/hfee_direction
echo gamut_hfee_mh12 > ~/PLAN_NAME
rm -f ~/TEARDOWN_FIRED

if [ ! -x ~/venv/bin/python3 ]; then
  python3.11 -m venv ~/venv
  ~/venv/bin/pip install --upgrade pip
  ~/venv/bin/pip install numpy==2.4.6 pandas==3.0.5 numba==0.66.0 \
      pyarrow==24.0.0 requests==2.34.2 python-dotenv==1.2.2 \
      python-dateutil==2.9.0.post0
fi

mkdir -p ~/strategy-lab && cd ~/strategy-lab
if [ ! -d optimizer ]; then
  aws s3 cp $B/code/repo.tgz /tmp/repo.tgz && tar -xzf /tmp/repo.tgz
fi
aws s3 cp $B/code/camp.tgz /tmp/camp.tgz && tar -xzf /tmp/camp.tgz -C optimizer
mkdir -p adaptive_trader/research/data
aws s3 sync $B/data/ adaptive_trader/research/data/ --only-show-errors
mkdir -p dashboard
[ -f dashboard/backtests.js ] || echo "window.BACKTESTS = [];" > dashboard/backtests.js

# done-markers from any previous box: never redo finished work
aws s3 sync $B/runs/ ~/strategy-lab/optimizer/runs/ --only-show-errors || true

cd /home/ubuntu
for f in ec2_boot_hfee.sh ec2_boot_hfee_b.sh box_s3_push.sh box_autoteardown.sh ec2_size_cores.sh; do
  aws s3 cp $B/code/$f ~/$f || true
done
cat > ~/boot_dispatch.sh <<'EOF'
#!/bin/bash
D=fwd; [ -f ~/hfee_direction ] && D=$(cat ~/hfee_direction)
if [ "$D" = "rev" ]; then ~/ec2_boot_hfee_b.sh; else ~/ec2_boot_hfee.sh; fi
EOF
chmod +x ~/*.sh

( crontab -l 2>/dev/null | grep -v "boot_dispatch\|box_s3_push\|box_autoteardown\|ec2_size_cores"
  echo "@reboot sleep 30 && ~/boot_dispatch.sh"
  echo "*/5 * * * * ~/box_s3_push.sh"
  echo "*/10 * * * * ~/box_autoteardown.sh"
  echo "*/10 * * * * ~/ec2_size_cores.sh --quiet" ) | crontab -

~/boot_dispatch.sh
USERPART

loginctl enable-linger ubuntu || true
timedatectl set-timezone America/Los_Angeles || true
echo "fleet userdata finished $(date)" >> /var/log/gamut-fleet.log
