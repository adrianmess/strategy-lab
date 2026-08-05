#!/bin/bash
# Run ON the EC2 box (Ubuntu 24.04) after the repo has been rsynced to ~/strategy-lab.
# Installs the python env the optimizer needs and smoke-tests imports.
set -e
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv rsync tmux htop > /dev/null
python3 -m venv "$HOME/venv"
source "$HOME/venv/bin/activate"
pip install -q --upgrade pip
# match the Mac's versions (numpy 2.4.6 / pandas 3.0.3 / numba 0.66.0 / pyarrow 24.0.0)
pip install -q "numpy==2.4.*" "pandas==3.0.*" "numba==0.66.*" "pyarrow==24.0.*" python-dotenv requests
cd "$HOME/strategy-lab/optimizer"
python3 - <<'EOF'
import numpy, pandas, numba, pyarrow
print("env ok:", numpy.__version__, pandas.__version__, numba.__version__, pyarrow.__version__)
import _bootstrap
import importlib
for m in ("optimize2_cli",):
    assert __import__("os").path.exists(m + ".py"), m
print("repo layout ok")
EOF
nproc
echo "BOOTSTRAP OK"
