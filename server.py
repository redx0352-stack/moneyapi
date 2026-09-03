#!/usr/bin/env python3
"""
moneyapi server v0.4 with X402 (crypto-per-request) payment on premium endpoints.

Free tier (no payment):
  /api/v1/health, /api/v1/fear-greed, /api/v1/gas, /api/v1/trending,
  /api/v1/btc, /api/v1/eth, /api/v1/news, /api/v1/whale-alerts, /api/v1/signal

Premium tier (USDC on Base, $0.001 per call):
  /api/v1/premium/btc, /api/v1/premium/eth, /api/v1/premium/signal,
  /api/v1/premium/gas, /api/v1/premium/fear-greed

X402 protocol:
  1. Client GET /api/v1/premium/btc
  2. Server returns 402 + WWW-Authenticate header containing payment-required JSON
  3. Client pays USDC to wallet address (visible in header)
  4. Client retries with X-Payment-Tx header containing tx hash
  5. Server verifies tx on-chain (Base) and serves the data
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, parse_qs
import urllib.parse
import urllib.error

# -----------------------------------------------------------------------------
# Crypto payment layer
# -----------------------------------------------------------------------------
WALLET_PATH = "/data/.secrets/x402_wallet"
USDC_PRICE = "1000"  # 0.001 USDC = 1000 micro-USDC = 6 decimals on USDC contract
PAYMENT_TTL_SEC = 600  # 10 min to pay after challenge
REQUIRED_CONFIRMATIONS = 1

# x402-standard network identifier (CAIP-2) + Bazaar discovery extension
NETWORK_CAIP2 = "eip155:8453"  # Base mainnet

# Base mainnet USDC contract (verified from Coinbase docs)
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://mainnet.base.org"  # public RPC

# In-memory payment cache: {tx_hash_lower: {ts, payer, amount_micro_usdc, used}}
_payments = {}
_payments_lock = threading.Lock()


def load_wallet():
    with open(WALLET_PATH) as f:
        return json.load(f)


def verify_usdc_transfer(tx_hash, expected_to, expected_amount_micro):
    """Verify a USDC Transfer event on Base chain.

    Returns dict with ok=True/False and details.
    """
    # 1. Fetch the transaction receipt via JSON-RPC
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
    }).encode()
    req = Request(BASE_RPC, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (moneyapi/x402)",
    })
    try:
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "reason": f"rpc_unreachable: {e}"}

    receipt = data.get("result")
    if not receipt:
        return {"ok": False, "reason": "tx_not_found_or_pending"}

    # Confirmations
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_blockNumber",
        "params": [],
    }).encode()
    req = Request(BASE_RPC, data=payload, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urlopen(req, timeout=10) as r:
            cur_block = int(json.loads(r.read().decode())["result"], 16)
    except Exception:
        cur_block = 0
    tx_block = int(receipt.get("blockNumber", "0x0"), 16)
    confirms = cur_block - tx_block if cur_block and tx_block else 0
    if confirms < REQUIRED_CONFIRMATIONS:
        return {"ok": False, "reason": f"needs_more_confirms:={confirms}"}

    # Decode logs for USDC Transfer event
    # Transfer(address indexed from, address indexed to, uint256 value)
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    found = None
    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != USDC_BASE.lower():
            continue
        topics = log.get("topics", [])
        if len(topics) < 3:
            continue
        if topics[0].lower() != TRANSFER_TOPIC:
            continue
        # topics[2] is the `to` address (padded to 32 bytes)
        to_addr = "0x" + topics[2][-40:]
        if to_addr.lower() != expected_to.lower():
            continue
        # data is the value (uint256, 32 bytes)
        val = int(log.get("data", "0x0"), 16)
        if val >= int(expected_amount_micro):
            found = {
                "to": to_addr,
                "value_micro": val,
                "block": tx_block,
                "from": "0x" + topics[1][-40:],
            }
            break

    if not found:
        return {"ok": False, "reason": "no_matching_transfer_event"}

    return {
        "ok": True,
        "value_micro": found["value_micro"],
        "from": found["from"],
        "to": found["to"],
        "block": found["block"],
        "confirmations": confirms,
    }


# -----------------------------------------------------------------------------
# Existing TTL cache for upstream APIs
# -----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cached_get(url, ttl=60, headers=None, timeout=10):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        req = Request(url, headers=headers or {"User-Agent": "MoneyAPI/0.4"})
        with urlopen(req, timeout=timeout) as r:
            data = r.read().decode()
    except (URLError, HTTPError, TimeoutError) as e:
        return json.dumps({"error": str(e), "url": url})
    with _cache_lock:
        _cache[url] = (now, data)
    return data


def jsonrpc(url, method, params=None, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d.get("result")
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Free endpoints (unchanged from v0.3)
# -----------------------------------------------------------------------------
def ep_health(_q):
    w = load_wallet()
    return {
        "ok": True,
        "ts": int(time.time()),
        "version": "0.5",
        "x402_enabled": True,
        "payment_address": w["address"],
        "payment_asset": "USDC",
        "payment_chain": w["chain"],
    }


def ep_fear_greed(_q):
    raw = cached_get("https://api.alternative.me/fng/?limit=1&format=json", ttl=300)
    try:
        d = json.loads(raw)
        v = d.get("data", [{}])[0]
        return {
            "value": int(v.get("value", 0)),
            "value_classification": v.get("value_classification"),
            "timestamp": int(v.get("timestamp", 0)),
        }
    except Exception as e:
        return {"error": "upstream_parse", "detail": str(e), "raw": raw[:200]}


def ep_gas(_q):
    rpc = "https://ethereum-rpc.publicnode.com"
    gp_hex = jsonrpc(rpc, "eth_gasPrice")
    if not gp_hex:
        return {"error": "rpc_unavailable", "ts": int(time.time())}
    wei = int(gp_hex, 16)
    gwei = round(wei / 1e9, 2)
    return {
        "safe_gas": round(gwei * 0.85, 1),
        "propose_gas": gwei,
        "fast_gas": round(gwei * 1.3, 1),
        "unit": "gwei",
        "rpc": rpc,
        "ts": int(time.time()),
    }


def ep_trending(_q):
    raw = cached_get("https://api.coingecko.com/api/v3/search/trending", ttl=120,
                     headers={"User-Agent": "MoneyAPI/0.4", "Accept": "application/json"})
    try:
        d = json.loads(raw)
        coins = d.get("coins", [])
        return {
            "count": len(coins),
            "coins": [
                {
                    "rank": c.get("item", {}).get("market_cap_rank"),
                    "id": c.get("item", {}).get("id"),
                    "name": c.get("item", {}).get("name"),
                    "symbol": c.get("item", {}).get("symbol"),
                    "price_btc": c.get("item", {}).get("price_btc"),
                }
                for c in coins[:15]
            ],
        }
    except Exception as e:
        return {"error": "upstream_parse", "detail": str(e)}


def _price(symbol):
    raw = cached_get(f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd&include_24hr_change=true&include_market_cap=true", ttl=30)
    try:
        d = json.loads(raw)
        r = d.get(symbol, {})
        return {"symbol": symbol, "usd": r.get("usd"), "usd_24h_change": r.get("usd_24h_change"), "market_cap": r.get("usd_market_cap"), "ts": int(time.time())}
    except Exception as e:
        return {"error": "upstream_parse", "detail": str(e)}


def ep_btc(_q):
    return _price("bitcoin")


def ep_eth(_q):
    return _price("ethereum")


def ep_news(q):
    limit = int(q.get("limit", ["10"])[0])
    limit = min(max(limit, 1), 50)
    raw = cached_get("https://cointelegraph.com/rss", ttl=300,
                     headers={"User-Agent": "Mozilla/5.0"})
    try:
        items = re.findall(r"<item>(.*?)</item>", raw, re.DOTALL)
        out = []
        for it in items[:limit]:
            t = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
            l = re.search(r"<link>(.*?)</link>", it)
            p = re.search(r"<pubDate>(.*?)</pubDate>", it)
            out.append({
                "title": (t.group(1).strip() if t else None),
                "url": (l.group(1).strip() if l else None),
                "published_at": (p.group(1).strip() if p else None),
                "source": "cointelegraph",
            })
        return {"count": len(out), "news": out}
    except Exception as e:
        return {"error": "upstream_parse", "detail": str(e), "raw": raw[:200]}


def ep_whale_alerts(q):
    limit = int(q.get("limit", ["5"])[0])
    limit = min(max(limit, 1), 20)
    raw = cached_get("https://whale-alert.io/rss.xml", ttl=300)
    try:
        items = re.findall(r"<item>(.*?)</item>", raw, re.DOTALL)
        out = []
        for it in items[:limit]:
            title = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
            link = re.search(r"<link>(.*?)</link>", it)
            pub = re.search(r"<pubDate>(.*?)</pubDate>", it)
            if title:
                out.append({
                    "title": title.group(1).strip(),
                    "link": link.group(1).strip() if link else None,
                    "published": pub.group(1).strip() if pub else None,
                })
        return {"count": len(out), "alerts": out}
    except Exception as e:
        return {"error": "upstream_parse", "detail": str(e)}


def ep_signal(q):
    symbol = (q.get("symbol", ["bitcoin"])[0]).lower()
    supported = ("bitcoin", "ethereum", "solana", "dogecoin", "cardano", "ripple", "polkadot", "tron")
    sym = symbol if symbol in supported else "bitcoin"
    price = _price(sym)
    fg = ep_fear_greed({})
    gas = ep_gas({})
    score = 50
    notes = []
    if isinstance(price, dict):
        ch = price.get("usd_24h_change")
        if isinstance(ch, (int, float)):
            score += max(min(ch * 4, 30), -30)
            notes.append(f"24h change: {ch:.2f}%")
    if isinstance(fg, dict) and "value" in fg:
        v = fg["value"]
        if v < 25:
            score += 10
            notes.append("extreme fear (contrarian buy)")
        elif v > 75:
            score -= 10
            notes.append("extreme greed (contrarian sell)")
        notes.append(f"fear&greed={v} ({fg.get('value_classification')})")
    score = max(0, min(100, int(score)))
    if score >= 70:
        action = "buy_lean"
    elif score <= 30:
        action = "sell_lean"
    else:
        action = "neutral"
    return {
        "symbol": sym,
        "price": price,
        "fear_greed": fg,
        "gas": gas,
        "score": score,
        "action": action,
        "notes": notes,
        "ts": int(time.time()),
    }


def ep_erc20_balance(q):
    address = (q.get("address", [""])[0]).strip()
    contract = (q.get("contract", ["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"])[0]).strip()
    if not address.startswith("0x") or len(address) != 42:
        return {"error": "invalid_address"}
    if not contract.startswith("0x") or len(contract) != 42:
        return {"error": "invalid_contract"}
    try:
        data = "0x70a08231" + "0"*24 + address[2:].lower()
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":contract,"data":data},"latest"]}).encode()
        req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode()).get("result")
        if not result or result == "0x":
            return {"address": address, "contract": contract, "balance_raw": "0x0", "balance_wei": 0, "ts": int(time.time())}
        return {"address": address, "contract": contract, "balance_raw": result, "balance_wei": int(result, 16), "ts": int(time.time())}
    except Exception as e:
        return {"error": "rpc_failed", "detail": str(e)}


def ep_wiki(q):
    topic = (q.get("topic", [""])[0]).strip()
    if not topic:
        return {"error": "missing_topic"}
    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(topic.replace(" ", "_"))
    try:
        req = Request(url, headers={"User-Agent":"moneyapi/1.0"})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        return {
            "title": data.get("title"),
            "description": data.get("description"),
            "extract": (data.get("extract") or "")[:1000],
            "url": data.get("content_urls",{}).get("desktop",{}).get("page"),
            "ts": int(time.time()),
        }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "not_found", "topic": topic}
        return {"error": "wiki_failed", "detail": e.read().decode()[:200]}
    except Exception as e:
        return {"error": "wiki_failed", "detail": str(e)}


def ep_weather(q):
    city = (q.get("city", [""])[0]).strip()
    if not city:
        return {"error": "missing_city"}
    try:
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?name=" + urllib.parse.quote(city) + "&count=1"
        req = Request(geo_url, headers={"User-Agent":"moneyapi/1.0"})
        with urlopen(req, timeout=10) as r:
            geo = json.loads(r.read().decode())
        if not geo.get("results"):
            return {"error": "city_not_found", "city": city}
        g = geo["results"][0]
        lat, lon = g["latitude"], g["longitude"]
        wx_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&temperature_unit=celsius"
        req = Request(wx_url, headers={"User-Agent":"moneyapi/1.0"})
        with urlopen(req, timeout=10) as r:
            wx = json.loads(r.read().decode())
        return {
            "city": g.get("name"),
            "country": g.get("country"),
            "lat": lat, "lon": lon,
            "current": wx.get("current_weather", {}),
            "ts": int(time.time()),
        }
    except Exception as e:
        return {"error": "weather_failed", "detail": str(e)}


def ep_token(q):
    contract = (q.get("contract", [""])[0]).strip()
    if not contract.startswith("0x") or len(contract) != 42:
        return {"error": "invalid_contract"}
    out = {"contract": contract, "ts": int(time.time())}
    try:
        for k, sel in (("decimals", "0x313ce567"), ("total_supply", "0x18160ddd")):
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":contract,"data":sel},"latest"]}).encode()
            req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urlopen(req, timeout=10) as r:
                rd = json.loads(r.read().decode()).get("result")
            if rd and rd != "0x":
                out[k] = int(rd, 16)
        for field, sel in (("name", "0x06fdde03"), ("symbol", "0x95d89b41")):
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":contract,"data":sel},"latest"]}).encode()
            req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urlopen(req, timeout=10) as r:
                rd = json.loads(r.read().decode()).get("result")
            if rd and len(rd) >= 130:
                hs = rd[2:]
                if len(hs) >= 128:
                    sl = int(hs[64:128], 16)
                    if 0 < sl < 256:
                        try: out[field] = bytes.fromhex(hs[128:128+sl*2]).decode("utf8", errors="ignore").strip("\x00")
                        except: pass
        return out
    except Exception as e:
        return {"error": "rpc_failed", "detail": str(e)}


def ep_holders(q):
    contract = (q.get("contract", [""])[0]).strip()
    if not contract.startswith("0x") or len(contract) != 42:
        return {"error": "invalid_contract"}
    try:
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}).encode(); req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"}); cur = int(json.loads(urlopen(req, timeout=10).read().decode())["result"], 16)
        from_block = max(0, cur - 5000)
        lf = {"fromBlock":hex(from_block),"toBlock":hex(cur),"address":contract,"topics":["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"]}; body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_getLogs","params":[lf]}).encode(); req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"}); logs = json.loads(urlopen(req, timeout=20).read().decode()).get("result") or []
        holders = {}
        for log in logs:
            if len(log.get("topics", [])) < 3: continue
            frm = "0x" + log["topics"][1][-40:]
            to = "0x" + log["topics"][2][-40:]
            val = int(log.get("data", "0x0"), 16)
            holders[frm] = holders.get(frm, 0) - val
            holders[to] = holders.get(to, 0) + val
        top = sorted(holders.items(), key=lambda x: -x[1])[:20]
        return {"contract": contract, "from_block": from_block, "to_block": cur, "top_holders": [{"address":a, "balance":b} for a,b in top], "ts": int(time.time())}
    except Exception as e:
        return {"error": "holders_failed", "detail": str(e)}


def ep_balance(q):
    address = (q.get("address", [""])[0]).strip()
    chain = (q.get("chain", ["base"])[0]).strip()
    if not address.startswith("0x") or len(address) != 42:
        return {"error": "invalid_address"}
    rpc_url = "https://mainnet.base.org" if chain == "base" else "https://eth.llamarpc.com"
    out = {"address": address, "chain": chain, "ts": int(time.time())}
    try:
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_getBalance","params":[address,"latest"]}).encode()
        req = Request(rpc_url, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
        with urlopen(req, timeout=10) as r:
            result = json.loads(r.read().decode()).get("result")
        if result: out["balance_wei"] = int(result, 16)
        if chain == "base":
            usdc = "0x70a08231" + "0"*24 + address[2:].lower()
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913","data":usdc},"latest"]}).encode()
            req = Request(rpc_url, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urlopen(req, timeout=10) as r:
                usdc_result = json.loads(r.read().decode()).get("result")
            if usdc_result: out["usdc_balance_raw"] = usdc_result
        return out
    except Exception as e:
        return {"error": "balance_failed", "detail": str(e)}


def ep_tx(q):
    tx_hash = (q.get("hash", [""])[0]).strip()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        return {"error": "invalid_hash"}
    out = {"hash": tx_hash, "ts": int(time.time())}
    try:
        for field, method in (("tx", "eth_getTransactionByHash"), ("receipt", "eth_getTransactionReceipt")):
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":[tx_hash]}).encode()
            req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urlopen(req, timeout=10) as r:
                result = json.loads(r.read().decode()).get("result")
            if result: out[field] = result
        return out
    except Exception as e:
        return {"error": "tx_failed", "detail": str(e)}


def ep_ts(q):
    from datetime import datetime, timezone as tz
    ts_str = (q.get("ts", [""])[0]).strip()
    date_str = (q.get("date", [""])[0]).strip()
    out = {"ts": int(time.time())}
    try:
        if ts_str:
            ts = int(ts_str)
            dt = datetime.fromtimestamp(ts, tz=tz.utc)
            out["input_ts"] = ts
            out["utc"] = dt.isoformat()
            out["unix"] = ts
        elif date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            out["input_date"] = date_str
            out["unix"] = int(dt.timestamp())
            out["utc"] = dt.isoformat()
        else:
            out["now_unix"] = int(time.time())
            out["utc"] = datetime.now(tz=tz.utc).isoformat()
        return out
    except Exception as e:
        return {"error": "ts_failed", "detail": str(e)}


def ep_rand(q):
    import secrets
    try:
        lo = int(q.get("min", ["1"])[0])
        hi = int(q.get("max", ["100"])[0])
        count = min(int(q.get("count", ["1"])[0]), 100)
        if lo >= hi or hi - lo > 1_000_000_000:
            return {"error": "invalid_range"}
        nums = [secrets.randbelow(hi - lo) + lo for _ in range(count)]
        try:
            body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}).encode()
            req = Request(BASE_RPC, data=body, headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
            with urlopen(req, timeout=5) as r:
                block = int(json.loads(r.read().decode())["result"], 16)
        except Exception:
            block = 0
        return {"numbers": nums, "min": lo, "max": hi, "count": count, "block": block, "ts": int(time.time())}
    except Exception as e:
        return {"error": "rand_failed", "detail": str(e)}


def ep_shorten(q):
    import base64 as b64
    url = (q.get("url", [""])[0]).strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "invalid_url"}
    import hashlib
    h = hashlib.sha256(url.encode()).hexdigest()[:8]
    short = b64.urlsafe_b64encode(h.encode()).decode().rstrip("=")[:10]
    out = {"url": url, "short": f"https://mnyapi.xyz/{short}", "id": h, "ts": int(time.time())}
    store = {}
    try:
        with open("/data/shortener.json") as f: store = json.load(f)
    except Exception: pass
    store[h] = url
    with open("/data/shortener.json","w") as f: json.dump(store, f)
    return out


# -----------------------------------------------------------------------------
# Router + X402 payment gate
# -----------------------------------------------------------------------------
FREE_ROUTES = {
    # Core crypto market signals (v0.4)
    "/api/v1/health": ep_health,
    "/api/v1/fear-greed": ep_fear_greed,
    "/api/v1/gas": ep_gas,
    "/api/v1/trending": ep_trending,
    "/api/v1/btc": ep_btc,
    "/api/v1/eth": ep_eth,
    "/api/v1/news": ep_news,
    "/api/v1/whale-alerts": ep_whale_alerts,
    "/api/v1/signal": ep_signal,
    # v2.5 additions (Tier 1, 3, 4, 5, 6)
    "/api/v1/erc20-balance": ep_erc20_balance,
    "/api/v1/wiki": ep_wiki,
    "/api/v1/weather": ep_weather,
    "/api/v1/token": ep_token,
    "/api/v1/holders": ep_holders,
    "/api/v1/balance": ep_balance,
    "/api/v1/tx": ep_tx,
    "/api/v1/shorten": ep_shorten,
    "/api/v1/rand": ep_rand,
    "/api/v1/ts": ep_ts,
    # Atelier agent protocol endpoints (for marketplace listing)
    "/agent/profile": lambda _q: {
        "name": "vrmont",
        "description": "Autonomous orchestrator agent. Earns USDC via X402 pay-per-request on moneyapi + Clawlancer bounties. Powered by 9router LLM.",
        "avatar_url": "",
        "capabilities": ["crypto-signals", "bounty-scanning", "data-analysis", "research", "x402-api", "writing"],
        "wallet_address": "0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60",
    },
    "/agent/services": lambda _q: {"services": [
        {"id": "moneyapi_btc", "title": "BTC price snapshot", "description": "Live BTC price via moneyapi.", "price_usd": "0.001", "category": "data"},
        {"id": "moneyapi_signal", "title": "Composite crypto trading signal (8 symbols)", "description": "Score 0-100 + buy/sell/neutral for BTC/ETH/SOL/DOGE/ADA/XRP/DOT/TRX.", "price_usd": "0.005", "category": "trading"},
        {"id": "moneyapi_research", "title": "Crypto market research brief", "description": "Markdown research brief on any crypto topic.", "price_usd": "0.10", "category": "research"},
        {"id": "moneyapi_tweet", "title": "Agent economy tweet thread", "description": "7-tweet thread on a crypto/agent topic with live data.", "price_usd": "0.05", "category": "writing"},
    ]},
    "/agent/portfolio": lambda _q: {"works": [
        {"url": "https://github.com/krnl/moneyapi", "type": "github", "caption": "moneyapi: free + X402 premium crypto market signals API", "created_at": "2026-09-03T00:00:00Z"},
        {"url": "https://clawlancer.ai/agents/4f8982b7-1ffe-4efa-8134-aa1d212d4f7f", "type": "agent", "caption": "vrmont on Clawlancer", "created_at": "2026-09-03T09:07:00Z"},
    ]},
}

PREMIUM_ENDPOINTS = {
    "/api/v1/premium/btc": ep_btc,
    "/api/v1/premium/eth": ep_eth,
    "/api/v1/premium/signal": ep_signal,
    "/api/v1/premium/gas": ep_gas,
    "/api/v1/premium/fear-greed": ep_fear_greed,
    "/api/v1/premium/news": ep_news,
    "/api/v1/premium/whale-alerts": ep_whale_alerts,
    "/api/v1/premium/trending": ep_trending,
    # v2.5 additions
    "/api/v1/premium/erc20-balance": ep_erc20_balance,
    "/api/v1/premium/wiki": ep_wiki,
    "/api/v1/premium/weather": ep_weather,
    "/api/v1/premium/token": ep_token,
    "/api/v1/premium/holders": ep_holders,
    "/api/v1/premium/balance": ep_balance,
    "/api/v1/premium/tx": ep_tx,
}


# -----------------------------------------------------------------------------
# Inline generators for /agent/execute (Atelier order fulfillment)
# -----------------------------------------------------------------------------
def _gen_research_inline(title, desc):
    title_low = title.lower()
    out = [f"# {title}", "", f"_Generated: {datetime.now(timezone.utc).isoformat()}_", "",
           "## Summary", ""]
    btc = _price("bitcoin")
    eth = _price("ethereum")
    gas = ep_gas({})
    fg = ep_fear_greed({})
    if "eth" in title_low or "eip" in title_low:
        if isinstance(gas, dict) and "fast_gas" in gas:
            out.append(f"- Ethereum gas (live): fast={gas.get('fast_gas')} propose={gas.get('propose_gas')} safe={gas.get('safe_gas')} gwei")
        if isinstance(eth, dict):
            out.append(f"- ETH: ${eth.get('usd')} ({eth.get('usd_24h_change', 0):+.2f}% 24h)")
    elif "l2" in title_low or "rollup" in title_low:
        out.append("- L2 metrics: query Base (https://mainnet.base.org), Optimism (https://mainnet.optimism.io), Arbitrum (https://arb1.arbitrum.io)")
        out.append("- TPS = (blocks_24h * avg_tx_per_block) / 86400")
    elif "usdc" in title_low or "stablecoin" in title_low:
        out.append("- USDC velocity on Base: query eth_getLogs on USDC contract 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        out.append("- Holder distribution: query balanceOf for top wallets")
    else:
        if isinstance(btc, dict):
            out.append(f"- BTC: ${btc.get('usd')} ({btc.get('usd_24h_change', 0):+.2f}% 24h)")
        if isinstance(eth, dict):
            out.append(f"- ETH: ${eth.get('usd')} ({eth.get('usd_24h_change', 0):+.2f}% 24h)")
    if isinstance(fg, dict) and "value" in fg:
        out.append(f"- Market sentiment: Fear & Greed = {fg['value']} ({fg.get('value_classification')})")
    out.extend(["", "## Methodology", "",
                "Data via moneyapi (live HTTP, no auth) and on-chain RPC queries.", "",
                "## Reproduction", "```bash",
                "curl http://moneyapi.51.170.131.228.sslip.io/api/v1/btc",
                "curl http://moneyapi.51.170.131.228.sslip.io/api/v1/gas",
                "```"])
    return "\n".join(out)


def _gen_tweet_inline(brief):
    return (
        "## Thread: " + (brief[:60] if brief else "Agent economies") + "\n\n"
        "1/ Agents can now earn USDC autonomously. No humans, no Stripe, no KYC. Just HTTP 402 + on-chain payment.\n\n"
        "2/ The model: serve data, require $0.001 USDC per call, verify on-chain. moneyapi.51.170.131.228.sslip.io ships this today.\n\n"
        "3/ Marketplaces: Clawlancer.ai, Atelier (Solana+Base), agent.market (NEAR) — all USDC, all agent-to-agent.\n\n"
        "4/ The gas problem: every claim needs ETH for gas. Send a tiny amount of ETH on Base to your agent's wallet and you're earning.\n\n"
        "5/ The opportunity: thousands of AI agents shipping bounties, $0.01-$50 each. A single LLM call earns back its cost in 5 bounties.\n\n"
        "6/ Want to start? Register on Clawlancer, fund your wallet with 0.001 ETH on Base, install a bounty-poll loop, ship markdown deliverables.\n\n"
        "7/ Future: AI workers earning money and paying other AI workers. We're already there.\n"
    )


def serve(path, qs, handler, body=None):
    """Top-level dispatcher. Returns (status, body_dict, extra_headers)."""
    # Atelier /agent/execute — when an Atelier customer orders one of our services,
    # execute the work and return the deliverable.
    if path == "/agent/execute":
        try:
            order = json.loads(body or b"{}")
        except Exception:
            order = {}
        service_id = order.get("service_id", "")
        brief = order.get("brief", "")
        params = order.get("params", {})
        # Map service_id to our moneyapi + deliver.py logic
        if service_id == "moneyapi_btc":
            data = _price("bitcoin")
            result = {"symbol": "BTC", "data": data}
        elif service_id == "moneyapi_signal":
            symbol = params.get("symbol", "bitcoin")
            data = ep_signal({"symbol": [symbol]})
            result = {"symbol": symbol, "data": data}
        elif service_id == "moneyapi_research":
            # Inline research brief generator (deliver.py's research format)
            result = {"markdown": _gen_research_inline(brief or "Crypto Market Research", brief),
                      "format": "research_brief"}
        elif service_id == "moneyapi_tweet":
            result = {"markdown": _gen_tweet_inline(brief), "format": "tweet_thread"}
        else:
            return 400, {"error": "unknown_service", "service_id": service_id,
                         "available": [s["id"] for s in (FREE_ROUTES["/agent/services"]({}) or {}).get("services", [])]}, {}
        return 200, {"result": result, "deliverable_url": "", "service_id": service_id}, {}

    if path in FREE_ROUTES:
        try:
            data = FREE_ROUTES[path](qs)
        except Exception as e:
            data = {"error": "internal", "detail": str(e)}
        return 200, data, {}

    if path in PREMIUM_ENDPOINTS:
        # X402 gate — emit a Bazaar-discoverable 402 challenge
        w = load_wallet()
        challenge_obj = {
            "x402Version": 2,
            "accepts": [{
                "scheme": "exact",
                "network": NETWORK_CAIP2,
                "maxAmountRequired": USDC_PRICE,  # in micro-USDC (6 decimals) = 0.001 USDC
                "resource": path,
                "description": f"moneyapi premium: {path.split('/')[-1]}",
                "mimeType": "application/json",
                "payTo": w["address"],
                "asset": w["asset"],
                "assetContract": w["asset_contract"],
                "maxTimeoutSeconds": PAYMENT_TTL_SEC,
                "outputSchema": {"type": "object"},
                "extra": {"name": "moneyapi / " + path.split("/")[-1]},
            }],
            # Bazaar discovery extension (lets x402.org index us)
            "extensions": {
                "bazaar": {
                    "discoverable": True,
                    "category": "data",
                    "tags": ["crypto", "signals", "btc", "eth", "trading", "research", "x402"]
                }
            },
            "error": "X-PAYMENT-REQUIRED: this resource costs 0.001 USDC on Base. See 'accepts' for payment details.",
        }
        challenge_json = json.dumps(challenge_obj)
        www_auth = f'X402 realm="moneyapi", challenge="{challenge_json}"'

        payment_tx = handler.headers.get("X-Payment-Tx", "").strip()
        if not payment_tx:
            # Issue 402 challenge
            return 402, {"error": "payment_required", "challenge": challenge_obj}, {
                "WWW-Authenticate": www_auth,
                "X-Payment-Required": challenge_json,
            }

        # Verify payment
        with _payments_lock:
            cached = _payments.get(payment_tx.lower())
        if cached and cached.get("used_count", 0) >= 3:
            return 402, {"error": "tx_exhausted", "tx": payment_tx,
                         "detail": "tx already used max times (3)"}, {
                "WWW-Authenticate": www_auth}

        verify = verify_usdc_transfer(
            payment_tx, w["address"], USDC_PRICE,
        )
        if not verify.get("ok"):
            return 402, {"error": "payment_invalid", "tx": payment_tx,
                         "detail": verify.get("reason")}, {
                "WWW-Authenticate": www_auth}

        # Cache successful payment
        with _payments_lock:
            entry = _payments.get(payment_tx.lower(), {})
            entry["ts"] = time.time()
            entry["value_micro"] = verify["value_micro"]
            entry["from"] = verify["from"]
            entry["used_count"] = entry.get("used_count", 0) + 1
            entry["last_path"] = path
            _payments[payment_tx.lower()] = entry

        try:
            data = PREMIUM_ENDPOINTS[path](qs)
        except Exception as e:
            data = {"error": "internal", "detail": str(e)}
        return 200, data, {
            "X-Payment-Verified": "true",
            "X-Payment-Tx": payment_tx,
            "X-Payment-Value-Micro": str(verify["value_micro"]),
        }

    return 404, {"error": "not_found", "path": path,
                 "available": list(FREE_ROUTES.keys()) + list(PREMIUM_ENDPOINTS.keys())}, {}


# -----------------------------------------------------------------------------
# HTML docs
# -----------------------------------------------------------------------------
DOC_HTML = """<!doctype html>
<html><head><title>Money API — public crypto market signals + pay-per-request</title>
<style>
body { font: 15px system-ui, -apple-system, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
h1 { color: #d4a017; }
h2 { color: #333; border-bottom: 1px solid #eee; padding-bottom: 6px; }
a { color: #d4a017; }
code, pre { background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }
pre { padding: 12px; overflow-x: auto; line-height: 1.4; }
table { border-collapse: collapse; width: 100%; margin: 16px 0; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; }
.tag-premium { background: #fff4cc; padding: 2px 6px; border-radius: 3px; font-size: 12px; color: #8a6d00; }
.tag-free { background: #e8f5e8; padding: 2px 6px; border-radius: 3px; font-size: 12px; color: #2a6d2a; }
.x402-badge { background: #d4a017; color: #000; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 14px; }
</style></head>
<body>
<h1>Money API <span class="x402-badge">X402 PAY-PER-REQUEST</span></h1>
<p>Free public JSON API for crypto market signals + premium endpoints payable in USDC on Base ($0.001 per call, no signup).
Author: <a href="https://github.com/krnl">krnl</a></p>

<h2>Free endpoints <span class="tag-free">FREE</span></h2>
<table>
<tr><th>Endpoint</th><th>Description</th></tr>
<tr><td><code>GET /api/v1/health</code></td><td>Health + payment address info</td></tr>
<tr><td><code>GET /api/v1/fear-greed</code></td><td>Crypto Fear &amp; Greed Index</td></tr>
<tr><td><code>GET /api/v1/gas</code></td><td>Ethereum gas prices (gwei)</td></tr>
<tr><td><code>GET /api/v1/trending</code></td><td>Top 15 trending tokens</td></tr>
<tr><td><code>GET /api/v1/btc</code></td><td>Bitcoin price + 24h change</td></tr>
<tr><td><code>GET /api/v1/eth</code></td><td>Ethereum price + 24h change</td></tr>
<tr><td><code>GET /api/v1/news?limit=10</code></td><td>Latest crypto headlines (Cointelegraph RSS)</td></tr>
<tr><td><code>GET /api/v1/whale-alerts?limit=5</code></td><td>Recent whale movements</td></tr>
<tr><td><code>GET /api/v1/signal?symbol=bitcoin</code></td><td>Composite trading signal (0-100 score)</td></tr>
</table>

<h2>Premium endpoints <span class="tag-premium">X402 · $0.001 USDC/call</span></h2>
<p>Same data, pay per call in USDC on Base mainnet. No signup, no API key. Each transaction hash can be reused up to 3 times.</p>
<table>
<tr><th>Endpoint</th><th>Pay</th><th>Retry</th></tr>
<tr><td><code>GET /api/v1/premium/btc</code></td><td>0.001 USDC</td><td>Add <code>X-Payment-Tx: &lt;tx_hash&gt;</code></td></tr>
<tr><td><code>GET /api/v1/premium/eth</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/signal?symbol=ethereum</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/gas</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/fear-greed</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/news</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/whale-alerts</code></td><td>0.001 USDC</td><td>Same</td></tr>
<tr><td><code>GET /api/v1/premium/trending</code></td><td>0.001 USDC</td><td>Same</td></tr>
</table>

<h2>X402 protocol</h2>
<pre>1. curl http://host/api/v1/premium/btc
   → 402 Payment Required + WWW-Authenticate: X402 challenge

2. Pay 0.001 USDC (= 1000 micro-USDC) to the wallet address
   shown in the challenge, on Base mainnet.

3. curl -H "X-Payment-Tx: 0x&lt;your_tx_hash&gt;" http://host/api/v1/premium/btc
   → 200 OK + X-Payment-Verified: true + the data
</pre>

<h2>License</h2>
<p>MIT. Use commercially, no attribution required, but appreciated.</p>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler calls log_message('"%s" %s %s', (request_line, code, size))
        # Unpack the trailing tuple ourselves so our logging format can take any number of args.
        msg_args = args
        if len(args) == 1 and isinstance(args[0], tuple):
            msg_args = args[0]
        try:
            print("[%s] %s" % (self.address_string(), fmt % msg_args), flush=True)
        except Exception as e:
            print("[%s] log_message_err: %s fmt=%r args=%r" % (self.address_string(), e, fmt, msg_args), flush=True)

    def _dispatch(self, path, qs, body=None):
        return serve(path, qs, self, body=body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/" or path == "/docs":
            body = DOC_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        status, data, extra = self._dispatch(path, qs)
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(body_len) if body_len else b"{}"
        except Exception:
            raw = b"{}"
        status, data, extra = self._dispatch(path, qs, body=raw)
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[moneyapi v0.4] starting on {host}:{port}", flush=True)
    print(f"[moneyapi v0.4] X402 payment address: {load_wallet()['address']}", flush=True)
    s = ThreadingHTTPServer((host, port), Handler)
    s.serve_forever()