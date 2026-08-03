#!/bin/bash
# Runs ON THE MAC. Waits for the (stopped, spot-interrupted) box to come back
# at its Elastic IP, then re-arms everything: boot script + @reboot cron on
# the box, workers started, sync loop restarted against the EIP.
# Usage: ./scripts/offload_watch.sh <EIP> <PEM>
set -u
IP="$1"; PEM="$2"
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -i $PEM -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
echo "[$(date '+%H:%M:%S')] waiting for $IP:22 …"
while ! nc -z -w 5 "$IP" 22 2>/dev/null; do sleep 60; done
echo "[$(date '+%H:%M:%S')] box is back — re-arming"
sleep 20   # let sshd/cloud-init settle
scp -i "$PEM" -o StrictHostKeyChecking=accept-new -q \
  "$LAB/scripts/ec2_boot_workers.sh" "ubuntu@$IP:~/" || exit 1
$SSH "ubuntu@$IP" 'chmod +x ~/ec2_boot_workers.sh;
  (crontab -l 2>/dev/null | grep -v ec2_boot_workers; echo "@reboot sleep 30 && ~/ec2_boot_workers.sh") | crontab -;
  ~/ec2_boot_workers.sh; tmux ls'
pkill -f offload_sync.sh 2>/dev/null
sleep 1
nohup "$LAB/scripts/offload_sync.sh" "$IP" "$PEM" gamut_g0801_2122 \
  > /tmp/offload_sync.log 2>&1 &
echo "[$(date '+%H:%M:%S')] re-armed: workers up, cron installed, sync loop on $IP"
