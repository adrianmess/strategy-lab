#!/bin/zsh
# rerun fetch_pair (resumable) until every 1min file reaches the present
for i in $(seq 1 60); do
  python3 fetch_pair.py btc eth doge xrp sui >> fetch_pairs.log 2>&1
  ok=$(python3 - <<'PY'
import pandas as pd, time
done=0
for c in ["btc","eth","doge","xrp","sui"]:
    try:
        t=pd.read_parquet(f"data/{c}_1min.parquet")["t"].max()
        if (pd.Timestamp.now(tz="UTC")-t).total_seconds() < 86400: done+=1
    except Exception: pass
print(done)
PY
)
  echo "pass $i: $ok/5 pairs current" >> fetch_pairs.log
  [ "$ok" = "5" ] && break
  sleep 45
done
echo "FETCH LOOP DONE" >> fetch_pairs.log
