#!/bin/bash
# One-time setup for the Mac mini (M4 Pro) as the second search box.
# Run FROM THE MAIN MAC:  ./scripts/mini_bootstrap.sh <mini-user>@<mini-ip>
# Prereq: Remote Login enabled on the mini (System Settings -> General ->
# Sharing -> Remote Login). You'll be asked for the mini's password once;
# after that the ssh key takes over.
set -euo pipefail
MINI="$1"
LAB="$(cd "$(dirname "$0")/.." && pwd)"

# 1) ssh key (passwordless from here on)
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id -i ~/.ssh/id_ed25519 "$MINI"

# 2) code + data (no caches — they rebuild on the mini; no runs — synced later)
ssh "$MINI" 'mkdir -p ~/strategy-lab'
rsync -az --exclude '.git' --exclude 'optimizer/runs' --exclude '*cache*' \
      --exclude 'dashboard/bt_detail' --exclude 'dashboard/backtests.js' \
      "$LAB/" "$MINI:strategy-lab/"

# 3) python env (Apple Silicon wheels)
ssh "$MINI" 'cd strategy-lab && python3 -m venv ~/venv &&
  ~/venv/bin/pip -q install numpy pandas numba pyarrow requests python-dotenv &&
  echo "env ready: $(~/venv/bin/python3 -V)"'

# 4) smoke test: import the engine
ssh "$MINI" 'cd strategy-lab/optimizer && ~/venv/bin/python3 -c "import _bootstrap, optimize2_cli" 2>&1 | tail -1; echo smoke ok'

echo
echo "mini ready. Start a plan on it with:"
echo "  ssh $MINI 'cd strategy-lab/optimizer && nohup ~/venv/bin/python3 gamut_worker.py --plan campaigns/<camp>/plan.json --jobs 10 --reverse > ~/worker.log 2>&1 &'"
echo "and start the LAN sync loop here with:"
echo "  nohup ./scripts/offload_sync.sh ~/.ssh/id_ed25519 <camp> <mini-ip> > /tmp/mini_sync.log 2>&1 &"
