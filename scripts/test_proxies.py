#!/usr/bin/env python3
"""Verify the 10 dedicated Decodo ISP proxies end-to-end (READ-ONLY):
  1. exit IP of each port (must match the whitelisted list)
  2. MEXC public contract API through each
  3. MEXC AUTHENTICATED futures call (account assets) through each
Never places orders. Prints no secrets."""
import json, os, sys, time
import requests

LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(LAB, "adaptive_trader"))

# parse the proxy file: host:port:user:pass lines + a comma list of exit IPs
lines = [l.strip() for l in open(os.path.join(LAB, "ip_proxies_APIs"))
         if l.strip()]
creds = [l for l in lines if ":" in l and "." in l.split(":")[0]]
expected_ips = set()
for l in lines:
    if "," in l and ":" not in l:
        expected_ips = {x.strip() for x in l.split(",") if x.strip()}
host = creds[0].split(":")[0]
user, pw = creds[0].split(":")[2], creds[0].split(":")[3]
ports = list(range(10001, 10011))          # test the full range incl. 10009

from mexc_api import MexcFuturesAPI  # noqa: E402
keys = json.load(open(os.path.join(LAB, "adaptive_trader",
                                   "mexc_api_keys.json")))
accounts = sorted(keys.get("accounts", {"mexc1": None}))
print(f"accounts found: {accounts}")
print(f"expected exit IPs ({len(expected_ips)}): {sorted(expected_ips)}\n")

seen_ips = {}
ok_pub = ok_auth = 0
for n, port in enumerate(ports):
    url = f"http://{user}:{pw}@{host}:{port}"
    px = {"http": url, "https": url}
    row = [f"port {port}"]
    try:                                   # 1. exit IP
        ip = requests.get("https://ip.decodo.com/json", proxies=px,
                          timeout=20).json().get("proxy", {}).get("ip") \
            or requests.get("https://api.ipify.org", proxies=px,
                            timeout=20).text.strip()
        seen_ips[port] = ip
        row.append(f"exit={ip}" + (" ✓" if ip in expected_ips else " (NOT in list!)"))
    except Exception as e:
        row.append(f"exit FAILED: {str(e)[:60]}")
        print("  ".join(row)); continue
    try:                                   # 2. MEXC public
        r = requests.get("https://contract.mexc.com/api/v1/contract/ping",
                         proxies=px, timeout=20).json()
        ok = bool(r.get("success", r.get("data")))
        row.append("public ✓" if ok else f"public? {str(r)[:40]}")
        ok_pub += ok
    except Exception as e:
        row.append(f"public FAILED: {str(e)[:50]}")
    try:                                   # 3. authenticated (rotate accounts)
        acct = accounts[n % len(accounts)]
        api = MexcFuturesAPI(account=acct, via_proxy=False)
        api.proxies = px
        api.assets()
        row.append(f"auth[{acct}] ✓")
        ok_auth += 1
    except Exception as e:
        row.append(f"auth[{accounts[n % len(accounts)]}] FAILED: {str(e)[:70]}")
    print("  ".join(row))
    time.sleep(0.4)

print(f"\nsummary: {len(seen_ips)}/10 reachable, {ok_pub}/10 public OK, "
      f"{ok_auth}/10 authenticated OK")
missing = expected_ips - set(seen_ips.values())
if missing:
    print(f"whitelisted IPs never seen: {sorted(missing)}")
extra = set(seen_ips.values()) - expected_ips
if extra:
    print(f"exit IPs NOT in the whitelist file: {sorted(extra)}")
