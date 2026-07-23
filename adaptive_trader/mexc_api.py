#!/usr/bin/env python3
"""MEXC FUTURES API client — native order execution (retail futures API,
launched by MEXC on 2026-03-31).

Replaces the Playwright browser for lev instances: no captcha, no browser,
~instant orders, real leverage parameter on the order itself.

Signing (per the integration guide): HMAC-SHA256 over
  accessKey + timestamp + parameterString
with headers ApiKey / Request-Time / Signature. GET params sorted + joined
with '&'; POST signs the raw JSON body.

Keys: adaptive_trader/mexc_api_keys.json (gitignored):
  { "access_key": "...", "secret_key": "...", "via_proxy": true }
via_proxy routes all API calls through proxy_config.json — required when the
key is IP-linked to the Decodo egress IP (recommended: static, no 90-day
expiry, region-stable).

Fee note: API futures trades are maker 0.01% / taker 0.05% (overrides web
promo rates). The engines model 0.04% taker, so backtests are within 0.01%/side
of API reality.

Self-test (read-only, safe):  python3 mexc_api.py --test
"""
import argparse, hashlib, hmac, json, os, time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://api.mexc.com"
KEYS_FILE = os.path.join(HERE, "mexc_api_keys.json")
PROXY_FILE = os.path.join(os.path.dirname(HERE), "proxy_config.json")

# order sides (API constants)
OPEN_LONG, CLOSE_SHORT, OPEN_SHORT, CLOSE_LONG = 1, 2, 3, 4
TYPE_MARKET = 5
ISOLATED, CROSS = 1, 2


def _load_proxies():
    try:
        pc = json.load(open(PROXY_FILE))
        server = (pc.get("server") or "").replace("http://", "")
        if not server or "FILL" in str(pc.get("username", "")):
            return None
        url = f"http://{pc['username']}:{pc['password']}@{server}"
        return {"http": url, "https": url}
    except Exception:
        return None


def load_account(account=None):
    """Multi-account keys file:
      { "default": "mexc1",
        "accounts": { "mexc1": {access_key, secret_key, via_proxy},
                      "mexc2": {...} } }
    (a legacy flat {access_key, secret_key} file is treated as 'mexc1')."""
    k = json.load(open(KEYS_FILE))
    if "accounts" in k:
        name = account or k.get("default") or sorted(k["accounts"])[0]
        if name not in k["accounts"]:
            raise RuntimeError(f"API account '{name}' not in {KEYS_FILE}")
        acct = k["accounts"][name]
    else:
        name, acct = "mexc1", k
    if "PASTE" in str(acct.get("access_key", "")):
        raise RuntimeError(f"API account '{name}' has placeholder keys")
    return name, acct


class MexcFuturesAPI:
    def __init__(self, access_key=None, secret_key=None, via_proxy=None,
                 timeout=20, account=None):
        self.account = account or "(explicit keys)"
        if access_key is None:
            self.account, acct = load_account(account)
            access_key = acct["access_key"]
            secret_key = acct["secret_key"]
            if via_proxy is None:
                via_proxy = bool(acct.get("via_proxy", True))
        self.ak, self.sk = access_key, secret_key
        self.timeout = timeout
        self.proxies = _load_proxies() if via_proxy else None
        if via_proxy and not self.proxies:
            raise RuntimeError("via_proxy=true but proxy_config.json is not "
                               "usable — the IP-linked API key would be "
                               "rejected from the wrong egress IP")

    # ---------------- signing ----------------
    def _headers(self, param_str):
        ts = str(int(time.time() * 1000))
        sig = hmac.new(self.sk.encode(),
                       (self.ak + ts + param_str).encode(),
                       hashlib.sha256).hexdigest()
        return {"ApiKey": self.ak, "Request-Time": ts, "Signature": sig,
                "Content-Type": "application/json"}

    def _get(self, path, params=None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        pstr = "&".join(f"{k}={params[k]}" for k in sorted(params))
        r = requests.get(BASE + path, params=params,
                         headers=self._headers(pstr),
                         proxies=self.proxies, timeout=self.timeout)
        return self._out(r)

    def _post(self, path, body):
        body = {k: v for k, v in body.items() if v is not None}
        raw = json.dumps(body)
        r = requests.post(BASE + path, data=raw,
                          headers=self._headers(raw),
                          proxies=self.proxies, timeout=self.timeout)
        return self._out(r)

    @staticmethod
    def _out(r):
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        if not j.get("success", False):
            raise RuntimeError(f"API error code={j.get('code')}: "
                               f"{j.get('message')}")
        return j.get("data")

    # ---------------- read-only ----------------
    def assets(self):
        return self._get("/api/v1/private/account/assets")

    def spot_api_symbols(self):
        """SPOT v3 (Binance-style signing): the symbols THIS key may API-trade.
        The key-creation UI lets you whitelist any pair, but MEXC gates spot
        API order placement per symbol server-side — this is the ground truth."""
        ts = int(time.time() * 1000)
        qs = f"timestamp={ts}"
        sig = hmac.new(self.sk.encode(), qs.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{BASE}/api/v3/defaultSymbols?{qs}&signature={sig}",
                         headers={"X-MEXC-APIKEY": self.ak},
                         proxies=self.proxies, timeout=self.timeout)
        j = r.json()
        if isinstance(j, dict) and j.get("data") is not None:
            return j["data"]
        raise RuntimeError(f"spot symbols query failed: {str(j)[:300]}")

    def open_positions(self, symbol=None):
        return self._get("/api/v1/private/position/open_positions",
                         {"symbol": symbol})

    # ---------------- trading ----------------
    def place_market(self, symbol, side, vol, leverage=None, price=None,
                     open_type=ISOLATED, external_oid=None):
        """Market order. price is still required by the endpoint — pass the
        current mark/last price as a reference value."""
        return self._post("/api/v1/private/order/create", dict(
            symbol=symbol, price=float(price or 0) or None, vol=float(vol),
            leverage=(int(leverage) if leverage else None), side=int(side),
            type=TYPE_MARKET, openType=open_type, externalOid=external_oid))

    def open_long(self, symbol, vol, leverage, price):
        return self.place_market(symbol, OPEN_LONG, vol, leverage, price)

    def open_short(self, symbol, vol, leverage, price):
        return self.place_market(symbol, OPEN_SHORT, vol, leverage, price)

    def close_position(self, symbol, price=None):
        """Close every open position on the symbol with market orders."""
        out = []
        for p in (self.open_positions(symbol) or []):
            hold = float(p.get("holdVol") or 0)
            if hold <= 0:
                continue
            ptype = int(p.get("positionType") or 1)   # 1 long, 2 short
            side = CLOSE_LONG if ptype == 1 else CLOSE_SHORT
            out.append(self.place_market(symbol, side, hold,
                                         price=price,
                                         open_type=int(p.get("openType") or 1)))
        return out or [{"note": "no open position"}]


class MexcSpotAPI:
    """SPOT v3 client (Binance-style): the signature is HMAC-SHA256 of the
    query/body string itself, sent as a 'signature' parameter, with the key in
    the X-MEXC-APIKEY header. Symbols have NO underscore (SOLUSDT)."""
    def __init__(self, access_key=None, secret_key=None, via_proxy=None,
                 timeout=20, account=None):
        self.account = account or "(explicit keys)"
        if access_key is None:
            self.account, acct = load_account(account)
            access_key = acct["access_key"]
            secret_key = acct["secret_key"]
            if via_proxy is None:
                via_proxy = bool(acct.get("via_proxy", True))
        self.ak, self.sk = access_key, secret_key
        self.timeout = timeout
        self.proxies = _load_proxies() if via_proxy else None
        if via_proxy and not self.proxies:
            raise RuntimeError("via_proxy=true but proxy_config.json unusable")

    def _signed(self, method, path, params):
        params = {k: v for k, v in params.items() if v is not None}
        # 30s recvWindow: proxy round-trips (Tokyo egress) exceed the 5s default
        params["recvWindow"] = 30000
        params["timestamp"] = int(time.time() * 1000)
        qs = "&".join(f"{k}={params[k]}" for k in params)
        sig = hmac.new(self.sk.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE}{path}?{qs}&signature={sig}"
        r = requests.request(method, url,
                             headers={"X-MEXC-APIKEY": self.ak},
                             proxies=self.proxies, timeout=self.timeout)
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code != 200 or (isinstance(j, dict) and j.get("code")
                                    not in (None, 200, 0)):
            raise RuntimeError(f"spot API error: {str(j)[:300]}")
        return j

    @staticmethod
    def spot_symbol(symbol):
        return symbol.replace("_", "")          # SOL_USDT -> SOLUSDT

    # ---------------- read-only ----------------
    def account_info(self):
        return self._signed("GET", "/api/v3/account", {})

    def balance(self, asset):
        for b in self.account_info().get("balances", []):
            if b.get("asset") == asset:
                return float(b.get("free", 0))
        return 0.0

    def open_orders(self, symbol=None):
        """Pending (unfilled) spot orders."""
        p = {"symbol": self.spot_symbol(symbol)} if symbol else {}
        return self._signed("GET", "/api/v3/openOrders", p)

    def ticker_price(self, symbol):
        """Public last price (no auth)."""
        r = requests.get(f"{BASE}/api/v3/ticker/price",
                         params={"symbol": self.spot_symbol(symbol)},
                         proxies=self.proxies, timeout=self.timeout)
        return float(r.json().get("price"))

    # ---------------- trading ----------------
    def market_buy_quote(self, symbol, quote_usdt):
        """Market BUY spending quote_usdt of USDT. Returns the order (with
        executedQty = base filled) — spot has no leverage, ever."""
        return self._signed("POST", "/api/v3/order", dict(
            symbol=self.spot_symbol(symbol), side="BUY", type="MARKET",
            quoteOrderQty=f"{quote_usdt:.2f}"))

    def market_sell(self, symbol, qty_base):
        return self._signed("POST", "/api/v3/order", dict(
            symbol=self.spot_symbol(symbol), side="SELL", type="MARKET",
            quantity=f"{qty_base:.4f}"))


def _test(account=None):
    api = MexcFuturesAPI(account=account)
    print(f"account: {api.account} | egress via proxy: {bool(api.proxies)}")
    a = api.assets()
    usdt = next((x for x in a if x.get("currency") == "USDT"), None)
    print("USDT asset:", json.dumps(usdt, indent=1) if usdt else a)
    p = api.open_positions("SOL_USDT")
    print("SOL_USDT open positions:", json.dumps(p, indent=1))
    print("FUTURES SELF-TEST OK — key, signature, IP link and region working")
    try:
        syms = api.spot_api_symbols()
        sol = "SOLUSDT" in syms
        print(f"spot API-tradable symbols: {len(syms)} | SOLUSDT: "
              f"{'YES — spot API trading available!' if sol else 'NO — spot stays on the browser executor'}")
    except Exception as e:
        print(f"spot symbol probe failed (futures unaffected): {e}")
    try:
        spot = MexcSpotAPI(account=account)
        usdt_free = spot.balance("USDT")
        sol_free = spot.balance("SOL")
        print(f"SPOT wallet: {usdt_free:.2f} USDT free, {sol_free:.4f} SOL free")
        print("SPOT SELF-TEST OK")
    except Exception as e:
        print(f"spot account probe failed: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="read-only self-test (assets + positions + spot probe)")
    ap.add_argument("--account", default=None, help="key account name (mexc1…)")
    args = ap.parse_args()
    if args.test:
        _test(args.account)
    else:
        print(__doc__)
