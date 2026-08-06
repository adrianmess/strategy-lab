#!/bin/bash
# Runs ON THE MAC. Waits for a box to answer at its (Elastic) IP, then
# re-arms it: boot script + @reboot cron on the box, workers started, and
# the multi-box sync loop restarted.
# Usage: ./scripts/offload_watch.sh <IP> <PEM> [BOOT_SCRIPT] [SYNC_IP...]
set -u
IP="$1"; PEM="$2"
BOOT="${3:-ec2_boot_workers.sh}"
shift $(( $# > 3 ? 3 : 2 ))
SYNC_IPS=("$@"); [ ${#SYNC_IPS[@]} -eq 0 ] && SYNC_IPS=("$IP")
LAB="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -i $PEM -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"
echo "[$(date '+%H:%M:%S')] waiting for $IP:22 …"
while ! nc -z -w 5 "$IP" 22 2>/dev/null; do sleep 60; done
echo "[$(date '+%H:%M:%S')] box is back — re-arming with $BOOT"
sleep 20
scp -i "$PEM" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -q \
  "$LAB/scripts/$BOOT" "ubuntu@$IP:~/ec2_boot_workers.sh" || exit 1
$SSH "ubuntu@$IP" 'chmod +x ~/ec2_boot_workers.sh;
  (crontab -l 2>/dev/null | grep -v ec2_boot_workers; echo "@reboot sleep 30 && ~/ec2_boot_workers.sh") | crontab -;
  ~/ec2_boot_workers.sh; tmux ls'
pkill -f offload_sync.sh 2>/dev/null
sleep 1
nohup "$LAB/scripts/offload_sync.sh" "$PEM" g0801_2122 "${SYNC_IPS[@]}" \
  > /tmp/offload_sync.log 2>&1 &
echo "[$(date '+%H:%M:%S')] re-armed: workers up, cron installed, sync on ${SYNC_IPS[*]}"
