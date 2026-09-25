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
TYPE_LIMIT = 1
TYPE_POST_ONLY = 2          # maker-only: cancelled instead of crossing
TYPE_MARKET = 5
ISOLATED, CROSS = 1, 2

# contract/detail is static per symbol — fetch once per process
_DETAIL = {}


def _order_id(res):
    """The order id out of whatever /order/create answered with: the id
    itself, or {'orderId': ..., 'ts': ...}. None when there is no id."""
    if isinstance(res, dict):
        for k in ("orderId", "order_id", "id"):
            if res.get(k) is not None:
                return res[k]
        return None
    return res


def _load_proxies(account=None):
    """Dedicated pool first (adaptive_trader/proxy_pool.json): each account
    gets its own stable exit IP (mexc1 -> first port, mexc2 -> second, ...).
    Falls back to the legacy single-proxy proxy_config.json."""
    try:
        pc = json.load(open(os.path.join(HERE, "proxy_pool.json")))
        ports = pc["ports"]
        idx = 0
        if account:
            import re as _re
            m = _re.search(r"(\d+)$", account)     # mexc1 -> 0, mexc2 -> 1
            if m:
                idx = (int(m.group(1)) - 1) % len(ports)
            else:
                import zlib
                idx = zlib.crc32(account.encode()) % len(ports)
        url = (f"http://{pc['username']}:{pc['password']}@"
               f"{pc['host']}:{ports[idx]}")
        return {"http": url, "https": url}
    except Exception:
        pass
    try:
        pc = json.load(open(PROXY_FILE))
        server = (pc.get("server") or "").replace("http://", "")
        if not server or "FILL" in str(pc.get("username", "")):
            return None
        url = f"http://{pc['username']}:{pc['password']}@{server}"
        return {"http": url, "https": url}
    except Exception:
        return None


# ---------------- proxy failover ----------------
# 2026-09-19 01:21-01:25: MEX2 Spot's FCFS close of ETH failed four times in a
# row — the account read through its pinned port got "Tunnel connection
# failed: 503" and a read timeout — and only went through on the 5th minute
# (~0.35% worse). Every pool exit IP is whitelisted on every key, so a request
# that could not even reach MEXC can simply be re-sent through another port.
#
# SAFETY RULE: fail over only when the request provably never reached MEXC
# (ConnectionError: proxy tunnel refused/503, DNS, connect timeout), or when it
# is idempotent (GET). A POST that READ-timed out may have executed — an order
# resent on another port could fill twice — so those are surfaced, not retried.
_EU_PORTS = {10005, 10007}        # ~450ms slower exits — never a failover target


def _alt_proxies(account, current):
    """A different pool port than `current`, for one retry. None if the pool
    has no other usable port."""
    try:
        pc = json.load(open(os.path.join(HERE, "proxy_pool.json")))
        ports = [int(p) for p in pc["ports"]]
    except Exception:
        return None
    cur = None
    try:
        cur = int(str((current or {}).get("https", "")).rsplit(":", 1)[1])
    except Exception:
        pass
    cands = [p for p in ports if p != cur and p not in _EU_PORTS] or \
            [p for p in ports if p != cur]
    if not cands:
        return None
    # deterministic per account so mexc1 and mexc2 don't both pile onto the
    # same spare port when a shared upstream blip hits them together
    import zlib
    p = cands[zlib.crc32((account or "").encode()) % len(cands)]
    url = f"http://{pc['username']}:{pc['password']}@{pc['host']}:{p}"
    return {"http": url, "https": url}


def _send_with_failover(api, send, retry_on_timeout, what=""):
    """send(proxies) -> requests.Response, tried once more on another pool
    port if the first attempt never reached MEXC (or timed out and
    `retry_on_timeout` says the call is idempotent)."""
    try:
        return send(api.proxies)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        # ConnectTimeout is both; a ReadTimeout on a non-idempotent call must
        # NOT be resent (the order may have gone through)
        if (isinstance(e, requests.exceptions.Timeout)
                and not isinstance(e, requests.exceptions.ConnectTimeout)
                and not retry_on_timeout):
            raise
        alt = _alt_proxies(api.account, api.proxies)
        if not alt or not api.proxies:
            raise
        try:
            port = alt["https"].rsplit(":", 1)[1]
            print(f"[mexc_api] {api.account}: {type(e).__name__} on the pinned "
                  f"proxy for {what or 'request'} — retrying once via port "
                  f"{port}", flush=True)
        except Exception:
            pass
        return send(alt)


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
        self.proxies = _load_proxies(self.account) if via_proxy else None
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
        r = _send_with_failover(
            self, lambda px: requests.get(BASE + path, params=params,
                                          headers=self._headers(pstr),
                                          proxies=px, timeout=self.timeout),
            retry_on_timeout=True, what=f"GET {path}")
        return self._out(r)

    def _post(self, path, body):
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if v is not None}
        raw = json.dumps(body)
        # orders: fail over only if the request never reached MEXC; a read
        # timeout is surfaced (the order may have executed)
        r = _send_with_failover(
            self, lambda px: requests.post(BASE + path, data=raw,
                                           headers=self._headers(raw),
                                           proxies=px, timeout=self.timeout),
            retry_on_timeout=False, what=f"POST {path}")
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

    def order_deals(self, symbol, page_size=20):
        """Recent futures fills (deal history) on a symbol."""
        return self._get("/api/v1/private/order/list/order_deals",
                         {"symbol": symbol, "page_num": 1,
                          "page_size": int(page_size)}) or []

    def history_positions(self, symbol=None, page_size=50):
        """Closed futures positions, newest first (MEXC 'Position History')."""
        return self._get("/api/v1/private/position/list/history_positions",
                         {"symbol": symbol, "page_num": 1,
                          "page_size": int(page_size)}) or []

    def transfer_records(self, page_size=50):
        """Transfers between THIS account's futures wallet and its spot
        ('MAIN') wallet, newest first: [{type: IN|OUT, amount, currency,
        state, createTime}]. Not external money — used by the panel's flow
        reader to tell which side of an internal account-to-account
        transfer was the sender."""
        d = self._get("/api/v1/private/account/transfer_record",
                      {"page_num": 1, "page_size": int(page_size)}) or {}
        return d.get("resultList") or []

    def history_orders(self, symbol=None, page_size=50):
        """Historical futures orders — filled/cancelled, newest first
        (MEXC 'Order & Trade History')."""
        return self._get("/api/v1/private/order/list/history_orders",
                         {"symbol": symbol, "page_num": 1,
                          "page_size": int(page_size)}) or []

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

    def place_limit(self, symbol, side, vol, price, leverage=None,
                    open_type=ISOLATED, otype=TYPE_LIMIT):
        """LIMIT order (rests on the book until filled or cancelled).
        otype=TYPE_POST_ONLY makes it maker-only.

        Returns the ORDER ID. The endpoint answers with
        {'orderId': ..., 'ts': ...} and callers stored that dict whole as the
        id, so every later cancel_orders([dict]) died on int() — a resting
        order the runner believed it had cancelled (2026-09-17). The price is
        snapped to the contract's tick here too: MEXC rejects an unrounded
        price with code 2015, which is what silently killed every resting TP.
        """
        res = self._post("/api/v1/private/order/create", dict(
            symbol=symbol, price=self.round_price(symbol, price),
            vol=float(vol),
            leverage=(int(leverage) if leverage else None), side=int(side),
            type=int(otype), openType=open_type))
        return _order_id(res)

    def open_orders(self, symbol=None, page_size=50):
        """Resting (unfilled) futures orders."""
        return self._get("/api/v1/private/order/list/open_orders" +
                         (f"/{symbol}" if symbol else ""),
                         {"page_num": 1, "page_size": int(page_size)}) or []

    def cancel_orders(self, ids):
        """Cancel futures orders by id list. Tolerates raw create-responses
        as well as ids — a cancel that throws leaves a live order resting."""
        out = []
        for i in ids:
            oid = _order_id(i)
            if oid is None:
                raise ValueError(f"cancel_orders: no order id in {i!r}")
            out.append(int(oid))
        return self._post("/api/v1/private/order/cancel", out)

    # ---------------- contract metadata (public, direct — no proxy) ----------
    def contract_detail(self, symbol):
        """Cached /contract/detail for one symbol. Public endpoint, so it
        goes DIRECT like klines do; only private calls need the proxy."""
        d = _DETAIL.get(symbol)
        if d is None:
            r = requests.get("https://contract.mexc.com/api/v1/contract/detail",
                             params={"symbol": symbol}, timeout=10)
            d = (r.json().get("data") or {}) if r.ok else {}
            if isinstance(d, list):          # some builds answer with a list
                d = next((x for x in d if x.get("symbol") == symbol), {})
            _DETAIL[symbol] = d
        return d

    def price_unit(self, symbol):
        """Tick size for the symbol's price, or None when unknown."""
        d = self.contract_detail(symbol)
        for k in ("priceUnit", "priceStep", "tickSize"):
            v = d.get(k)
            try:
                if v and float(v) > 0:
                    return float(v)
            except (TypeError, ValueError):
                pass
        sc = d.get("priceScale")
        try:
            if sc is not None:
                return 10.0 ** -int(sc)
        except (TypeError, ValueError):
            pass
        return None

    def round_price(self, symbol, price, direction=0):
        """Snap `price` to the contract's tick. direction>0 rounds UP, <0
        DOWN, 0 to nearest — a take-profit rounds AWAY from the market so the
        rounding can never make the target worse than the engine asked for.
        Unknown tick: return the price untouched rather than guess."""
        px = float(price)
        tick = self.price_unit(symbol)
        if not tick or tick <= 0:
            return px
        import math as _m
        n = px / tick
        n = (_m.ceil(n) if direction > 0
             else _m.floor(n) if direction < 0 else round(n))
        # tick can be 0.0001 — float division leaves 1.3005000000000002
        return round(n * tick, 12)

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
        self.proxies = _load_proxies(self.account) if via_proxy else None
        if via_proxy and not self.proxies:
            raise RuntimeError("via_proxy=true but proxy_config.json unusable")

    def _signed(self, method, path, params):
        params = {k: v for k, v in params.items() if v is not None}
        # 30s recvWindow: proxy round-trips (Tokyo egress) exceed the 5s default
        params["recvWindow"] = 30000
        params["timestamp"] = int(time.time() * 1000)
        # URL-encode BEFORE signing: MEXC verifies the HMAC over the encoded
        # query, so any value needing escaping must be signed escaped. The
        # raw join worked only because symbols/numbers never need it; the
        # multi-asset dust convert (asset=SUI,DOGE,XRP,BTC) was rejected with
        # 700002 "Signature for this request is not valid" (2026-09-25)
        # while the single-asset one on the other account went through.
        from urllib.parse import urlencode
        qs = urlencode(params)
        sig = hmac.new(self.sk.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE}{path}?{qs}&signature={sig}"
        # GET/DELETE are idempotent -> may be resent on a read timeout;
        # POST (orders) only when the request never reached MEXC
        r = _send_with_failover(
            self, lambda px: requests.request(method, url,
                                              headers={"X-MEXC-APIKEY": self.ak},
                                              proxies=px, timeout=self.timeout),
            retry_on_timeout=(method.upper() != "POST"),
            what=f"{method.upper()} {path}")
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

    def universal_transfer(self, asset, amount, from_type, to_type):
        """Internal transfer between THIS account's own wallets (SPOT and
        FUTURES). Not a withdrawal — funds never leave the MEXC account.
        Returns {'tranId': ...} on success."""
        assert from_type in ("SPOT", "FUTURES") and to_type in ("SPOT",
                                                                "FUTURES")
        return self._signed("POST", "/api/v3/capital/transfer",
                            dict(fromAccountType=from_type,
                                 toAccountType=to_type,
                                 asset=asset, amount=f"{float(amount):.8f}"))

    def capital_config(self):
        """Coin catalog with per-network deposit/withdraw availability."""
        return self._signed("GET", "/api/v3/capital/config/getall", {})

    def deposit_address(self, coin, network=None):
        """Deposit address(es) for a coin (+ optional network). READ-ONLY —
        shows where to send funds; the panel never initiates transfers.
        Network names contain spaces/parens ("Tron(TRC20)") which break the
        raw-string signer — pre-encode so signature and URL agree."""
        from urllib.parse import quote
        p = {"coin": coin}
        if network:
            p["network"] = quote(network, safe="")
        return self._signed("GET", "/api/v3/capital/deposit/address", p)

    def deposit_address_generate(self, coin, network):
        """Create the deposit address for a coin+network that has none yet
        (the website does this implicitly when you open its deposit page).
        Deposit-only: it mints a receiving address, nothing more."""
        from urllib.parse import quote
        return self._signed("POST", "/api/v3/capital/deposit/address",
                            {"coin": coin, "network": quote(network, safe="")})

    def internal_transfer_history(self, limit=50):
        """MEXC account-to-account ('internal') transfers involving THIS
        account: [{tranId, asset, amount, fromAccount, toAccount, status,
        timestamp}]. These appear in NEITHER deposit nor withdrawal history
        (2026-09-20: 700 USDT mexc1 -> mexc2 was invisible to /api/flows).
        Both parties get the same record with both emails masked alike, so
        direction is not in the payload."""
        d = self._signed("GET", "/api/v3/capital/transfer/internal",
                         {"limit": int(limit)})
        return (d or {}).get("data") or []

    def withdraw_history(self, coin=None, limit=20):
        """Recent withdrawals (status 7 = success)."""
        p = {"limit": int(limit)}
        if coin:
            p["coin"] = coin
        return self._signed("GET", "/api/v3/capital/withdraw/history", p)

    def deposit_history(self, coin=None, limit=20):
        """Recent deposits incl. IN-FLIGHT ones (status + confirmations)."""
        p = {"limit": limit}
        if coin:
            p["coin"] = coin
        return self._signed("GET", "/api/v3/capital/deposit/hisrec", p)

    def dust_convertible(self):
        """Assets MEXC will convert to MX (each worth < 5 USDT)."""
        return self._signed("GET", "/api/v3/capital/convert/list", {})

    def dust_convert(self, assets):
        """Convert small balances to MX. MEXC charges a 0.2% fee and allows
        up to 10 conversions per 24h; each asset must be worth < 5 USDT."""
        return self._signed("POST", "/api/v3/capital/convert",
                            {"asset": ",".join(assets)})

    _scale_cache = {}

    def quantity_scale(self, symbol):
        """Allowed decimal places for BASE quantity on this symbol.

        CAREFUL — two different fields, and confusing them costs money:
          * baseAssetPrecision  = DECIMAL PLACES allowed (HYPE: 2)
          * baseSizePrecision   = MINIMUM ORDER QUANTITY (HYPE: '0' = none)
        This used to read baseSizePrecision as decimals, so HYPE ('0') was
        treated as whole-units-only: sells floored 9.4496 -> 9 and 0.59 -> 0,
        stranding ~1 HYPE (~$60) per round trip and making dust unsellable.
        Use baseAssetPrecision for the scale; a fractional baseSizePrecision
        (e.g. SOL '0.01') also implies a step, so take the tighter of the two.
        """
        s = self.spot_symbol(symbol)
        if s in MexcSpotAPI._scale_cache:
            return MexcSpotAPI._scale_cache[s]
        r = requests.get(f"{BASE}/api/v3/exchangeInfo", params={"symbol": s},
                         proxies=self.proxies, timeout=self.timeout)
        info = (r.json().get("symbols") or [{}])[0]
        scale = int(info.get("baseAssetPrecision") or 2)
        bsp = str(info.get("baseSizePrecision") or "")
        if "." in bsp:                     # a real step like '0.01' -> 2 dp
            step_dp = len(bsp.split(".")[1].rstrip("0"))
            scale = min(scale, step_dp)
        MexcSpotAPI._scale_cache[s] = scale
        return scale

    def min_qty(self, symbol):
        """Exchange minimum order quantity (baseSizePrecision), 0 if none."""
        s = self.spot_symbol(symbol)
        k = s + ":minq"
        if k in MexcSpotAPI._scale_cache:
            return MexcSpotAPI._scale_cache[k]
        r = requests.get(f"{BASE}/api/v3/exchangeInfo", params={"symbol": s},
                         proxies=self.proxies, timeout=self.timeout)
        info = (r.json().get("symbols") or [{}])[0]
        try:
            v = float(info.get("baseSizePrecision") or 0)
        except (TypeError, ValueError):
            v = 0.0
        MexcSpotAPI._scale_cache[k] = v
        return v

    def floor_qty(self, symbol, qty):
        """FLOOR a base quantity to the symbol's allowed scale (never round up:
        you can't sell more than you hold)."""
        import math
        sc = self.quantity_scale(symbol)
        q = math.floor(float(qty) * 10 ** sc) / 10 ** sc
        return q, sc

    def price_scale(self, symbol):
        """Allowed decimal places for PRICE on this symbol (exchangeInfo
        quotePrecision)."""
        s = self.spot_symbol(symbol)
        key = s + ":px"
        if key in MexcSpotAPI._scale_cache:
            return MexcSpotAPI._scale_cache[key]
        r = requests.get(f"{BASE}/api/v3/exchangeInfo", params={"symbol": s},
                         proxies=self.proxies, timeout=self.timeout)
        info = (r.json().get("symbols") or [{}])[0]
        scale = int(info.get("quotePrecision")
                    or info.get("quoteAssetPrecision") or 4)
        MexcSpotAPI._scale_cache[key] = scale
        return scale

    def place_limit_buy(self, symbol, qty, price):
        """Resting LIMIT BUY of qty base at price (mirror of limit sell)."""
        q, _ = self.floor_qty(symbol, qty)
        return self._signed("POST", "/api/v3/order", dict(
            symbol=self.spot_symbol(symbol), side="BUY", type="LIMIT",
            quantity=f"{q:.{self.quantity_scale(symbol)}f}",
            price=f"{float(price):.{self.price_scale(symbol)}f}"))

    def place_limit_sell(self, symbol, qty, price):
        """Resting GTC LIMIT SELL — the exchange-side take-profit net. Sits on
        MEXC's books, so it executes even if our server is down."""
        import math
        q, _ = self.floor_qty(symbol, qty)
        psc = self.price_scale(symbol)
        px = math.floor(float(price) * 10 ** psc) / 10 ** psc
        return self._signed("POST", "/api/v3/order",
                            {"symbol": self.spot_symbol(symbol),
                             "side": "SELL", "type": "LIMIT",
                             "quantity": f"{q:.{self.quantity_scale(symbol)}f}",
                             "price": f"{px:.{psc}f}"})

    def cancel_order(self, symbol, order_id):
        return self._signed("DELETE", "/api/v3/order",
                            {"symbol": self.spot_symbol(symbol),
                             "orderId": str(order_id)})

    def query_order(self, symbol, order_id):
        return self._signed("GET", "/api/v3/order",
                            {"symbol": self.spot_symbol(symbol),
                             "orderId": str(order_id)})

    def my_trades(self, symbol, limit=20):
        """Recent fills on a spot symbol (newest last per API ordering)."""
        return self._signed("GET", "/api/v3/myTrades",
                            {"symbol": self.spot_symbol(symbol),
                             "limit": int(limit)})

    def all_orders(self, symbol, limit=20):
        """Order history on a spot symbol (filled + cancelled)."""
        return self._signed("GET", "/api/v3/allOrders",
                            {"symbol": self.spot_symbol(symbol),
                             "limit": int(limit)})

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
        q, sc = self.floor_qty(symbol, qty_base)
        if q <= 0:
            raise RuntimeError(
                f"sellable quantity is 0 after flooring {qty_base} to the "
                f"symbol's {sc}-decimal scale — the remainder is dust below "
                f"the exchange's minimum step")
        return self._signed("POST", "/api/v3/order", dict(
            symbol=self.spot_symbol(symbol), side="SELL", type="MARKET",
            quantity=f"{q:.{sc}f}"))


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
