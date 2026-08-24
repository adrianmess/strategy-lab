#!/bin/bash
# Keep the control-panel server alive on the hub (Mac mini).
# Installed in cron: "@reboot" starts it at boot, "* * * * *" restarts it
# within a minute of any crash. Running instance found -> exit quietly.
# (Pattern is anchored so webhook_server.py etc. don't count as the panel.)
# NOTE: the venv python EXECs the framework binary, so the process shows as
# ".../MacOS/Python server.py" — match on " server.py" at end of line (the
# leading space keeps webhook_server.py from counting as the panel).
#
# QoS: the panel is handed to LAUNCHD (launchctl submit) instead of being
# forked from cron. macOS clamps every cron descendant to background QoS —
# efficiency cores only — and the clamp is inherited by EVERYTHING the panel
# spawns (traders, gamut, optimizer workers) and cannot be lifted afterwards
# (taskpolicy -B/-t/-l/-d were all tried on live pids and at spawn: no
# effect). Measured 2026-08-24: the same optimizer generation took 0.7s via
# ssh/launchd and 17-21s via cron — a 25x penalty that made a gamut look
# stalled. launchctl submit escapes the clamp: same benchmark 0.85s.
pgrep -f " server\.py$" >/dev/null && exit 0
cd "$HOME/strategy-lab/panel" || exit 1
echo "[$(date '+%F %T')] panel not running — watchdog starting it (via launchd)" >> panel.log
# the label may still be registered from the previous run; remove is a no-op
# when it is not (we only get here when NO panel process exists, so remove
# never kills a live panel)
launchctl remove com.strategylab.panel 2>/dev/null
sleep 1
launchctl submit -l com.strategylab.panel -- /bin/bash -c \
  "cd $HOME/strategy-lab/panel && PANEL_HOST=0.0.0.0 exec $HOME/venv/bin/python3 server.py >> panel.log 2>&1"
