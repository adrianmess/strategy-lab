#!/bin/zsh
# One-time MacBook worker setup for the optimize offload (2026-09-10).
# Verifies prerequisites, writes ~/.strategy_lab_worker.json, and installs
# the launchd agent that keeps scripts/macbook_dispatch.py polling the mini.
# Safe to re-run (idempotent). Uninstall:
#   launchctl bootout gui/$UID/com.strategylab.macworker
#   rm ~/Library/LaunchAgents/com.strategylab.macworker.plist
set -e

REPO="$HOME/Code/strategy-lab"
PY="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
KEY="$HOME/.ssh/lab_auto_ed25519"
REMOTE="admn@admns-Mac-mini.local"
HUB="http://admns-Mac-mini.local:8800"
PLIST="$HOME/Library/LaunchAgents/com.strategylab.macworker.plist"
LOG="$HOME/Library/Logs/strategy-lab-worker.log"
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

echo "== strategy-lab MacBook worker setup =="

# 1. prerequisites
[ -d "$REPO/optimizer" ] || { echo "FAIL: repo not at $REPO"; exit 1; }
[ -x "$PY" ] || { echo "FAIL: python not at $PY"; exit 1; }
"$PY" -c "import numba, pandas, numpy" \
  || { echo "FAIL: deps missing ($PY -m pip install numba pandas numpy pyarrow)"; exit 1; }
command -v rsync >/dev/null || { echo "FAIL: rsync missing"; exit 1; }
[ -f "$KEY" ] || { echo "FAIL: ssh key $KEY missing"; exit 1; }
${=SSH} "$REMOTE" true || { echo "FAIL: cannot ssh to the mini"; exit 1; }
echo "ok: repo, python, deps, rsync, ssh"

# 2. panel key (fetched over ssh, never typed)
PANEL_KEY=$(${=SSH} "$REMOTE" "python3 -c \"import json;print(json.load(open('strategy-lab/panel/panel_key.json'))['key'])\"")
[ -n "$PANEL_KEY" ] || { echo "FAIL: could not read panel key from the mini"; exit 1; }
CORES=$(( $(sysctl -n hw.ncpu) - 2 ))
cat > "$HOME/.strategy_lab_worker.json" <<EOF
{"hub": "$HUB", "panel_key": "$PANEL_KEY", "repo": "$REPO",
 "python": "$PY", "procs": $CORES,
 "ssh_key": "$KEY", "remote": "$REMOTE", "worker": "macbook"}
EOF
chmod 600 "$HOME/.strategy_lab_worker.json"
echo "ok: config written (procs=$CORES)"

# 3. panel reachable with that key
curl -sf -H "X-Panel-Key: $PANEL_KEY" "$HUB/api/remote/status" >/dev/null \
  || { echo "FAIL: $HUB/api/remote/status not reachable (panel updated?)"; exit 1; }
echo "ok: panel remote API reachable"

# 4. launchd agent (RunAtLoad + KeepAlive: survives reboots and crashes;
#    the poller is idle-cheap — the heavy process only exists during a job)
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.strategylab.macworker</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$REPO/scripts/macbook_dispatch.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
launchctl bootout "gui/$UID/com.strategylab.macworker" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
sleep 3
launchctl print "gui/$UID/com.strategylab.macworker" | grep -q "state = running" \
  || { echo "FAIL: agent not running — see $LOG"; exit 1; }
echo "ok: launchd agent running (log: $LOG)"

# 5. mini should see the heartbeat within ~20s
sleep 20
curl -sf -H "X-Panel-Key: $PANEL_KEY" "$HUB/api/remote/status" \
  | python3 -c "import json,sys; w=json.load(sys.stdin)['workers']; \
assert 'macbook' in w and w['macbook']['ago']<60, w; \
print('ok: mini sees the MacBook worker (heartbeat %ss ago, %s cores)' \
% (w['macbook']['ago'], w['macbook']['cores']))"

echo "== done — the 'MacBook' option in the Optimize pages is now live =="
