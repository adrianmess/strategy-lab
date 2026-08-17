#!/bin/bash
# Keep the control-panel server alive on the hub (Mac mini).
# Installed in cron: "@reboot" starts it at boot, "* * * * *" restarts it
# within a minute of any crash. Running instance found -> exit quietly.
# (Pattern is anchored so webhook_server.py etc. don't count as the panel.)
# NOTE: the venv python EXECs the framework binary, so the process shows as
# ".../MacOS/Python server.py" — match on " server.py" at end of line (the
# leading space keeps webhook_server.py from counting as the panel).
pgrep -f " server\.py$" >/dev/null && exit 0
cd "$HOME/strategy-lab/panel" || exit 1
echo "[$(date '+%F %T')] panel not running — watchdog starting it" >> panel.log
PANEL_HOST=0.0.0.0 nohup "$HOME/venv/bin/python3" server.py >> panel.log 2>&1 &
