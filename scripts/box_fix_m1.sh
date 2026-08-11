#!/bin/bash
# Runs ON the box: clean restart of the m1sweep worker with plan reset.
chmod +x ~/boot_m1.sh
rm -f ~/worker.log
tmux kill-server 2>/dev/null
pkill -f gamut_worker 2>/dev/null
sleep 1
python3 - <<'PY'
import json
p = "strategy-lab/optimizer/campaigns/m1sweep_0810/plan.json"
d = json.load(open(p))
n = 0
for s in d["specs"]:
    if s.get("status") != "pending":
        s["status"] = "pending"; s.pop("try", None); n += 1
json.dump(d, open(p, "w"), indent=1)
print("reset", n, "specs")
PY
echo "runs synced: $(ls strategy-lab/optimizer/runs | wc -l)"
( crontab -l 2>/dev/null | grep -v boot_m1; echo "@reboot sleep 40 && /home/ubuntu/boot_m1.sh" ) | crontab -
~/boot_m1.sh
sleep 25
tmux ls
tail -5 ~/worker.log
