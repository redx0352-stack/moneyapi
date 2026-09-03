#!/usr/bin/env python3
"""
clawlancer_loop.py - Autonomous Clawlancer bounty hunter.

Per Clawlancer's heartbeat protocol (sent in registration response):
1. Every 2 minutes: GET poll_url with auth header
2. Score each bounty vs my skills (0-100)
3. If score >= 80: POST to claim_url_template with bounty id
4. Do the work
5. POST deliver_url_template with the deliverable

Skill keywords weighted for vrmont's profile:
  python, agent, ai, llm, crypto, defi, web3, signals, bounty,
  data, research, writing, analysis, github, code, automation
"""
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

SECRETS = "/data/.secrets/clawlancer_agent.json"
LOG_PATH = "/data/clawlancer_loop.log"

# Skills + weights (high = critical)
SKILLS = {
    "python": 10, "agent": 8, "ai": 8, "llm": 8, "claude": 6,
    "crypto": 10, "defi": 8, "web3": 8, "signals": 10,
    "trading": 6, "research": 8, "data": 6, "analysis": 7,
    "writing": 5, "github": 5, "code": 5, "automation": 7,
    "api": 5, "x402": 8, "bounty": 8, "on-chain": 8,
}

# Anti-skills (negative weight) — bounties we'd do badly at
NEG_SKILLS = {
    "design": -8, "logo": -10, "graphic": -10, "video editing": -10,
    "translation": -5, "voice": -8, "audio": -8, "music": -10,
    "human-only": -100, "kyc": -100, "identity": -100,
}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def api_get(path, api_key):
    req = urllib.request.Request(f"https://clawlancer.ai{path}", headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "vrmont/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return None, str(e)


def api_post(path, api_key, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"https://clawlancer.ai{path}", data=data, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "vrmont/1.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return None, str(e)


def score_bounty(l):
    """Return (score 0-100, reasons)."""
    title = (l.get("title") or "").lower()
    desc = (l.get("description") or "").lower()
    cat = (l.get("category") or "").lower()
    text = f"{title} {desc} {cat}"

    score = 0
    reasons = []
    for skill, weight in SKILLS.items():
        if skill in text:
            score += weight
            reasons.append(f"+{weight} {skill}")
    for skill, weight in NEG_SKILLS.items():
        if skill in text:
            score += weight  # weight is already negative
            reasons.append(f"{weight} {skill}")

    # Bonus: high payout
    payout = l.get("price_usdc") or 0
    if not payout and l.get("price_wei"):
        payout = l.get("price_wei") / 1e6  # wei USDC → USDC
    if payout >= 100:
        score += 15
        reasons.append(f"+15 high_payout={payout:.0f}USDC")
    elif payout >= 10:
        score += 8
        reasons.append(f"+8 payout={payout:.0f}USDC")
    elif payout >= 1:
        score += 3
        reasons.append(f"+3 payout={payout:.2f}USDC")

    return max(0, min(100, score)), reasons


def main():
    if not os.path.exists(SECRETS):
        log("no secrets file yet — register first")
        return

    with open(SECRETS) as f:
        agent = json.load(f)
    api_key = agent["api_key"]
    agent_id = agent["agent"]["id"]

    log(f"clawlancer_loop started (agent {agent_id[:8]})")

    seen_ids_path = "/data/.clawlancer_seen.json"
    seen = set()
    if os.path.exists(seen_ids_path):
        try:
            with open(seen_ids_path) as f:
                seen = set(json.load(f).get("ids", []))
        except Exception:
            seen = set()

    while True:
        # 1. Poll new bounties
        s, data = api_get("/api/listings?listing_type=BOUNTY&status=active&sort=newest", api_key)
        if s != 200:
            log(f"poll failed: {s} {data}")
            time.sleep(120)
            continue
        listings = data.get("listings", []) if isinstance(data, dict) else []
        log(f"polled: {len(listings)} bounties")

        for L in listings:
            bid = L.get("id")
            if not bid or bid in seen:
                continue
            seen.add(bid)
            sc, why = score_bounty(L)
            log(f"  score={sc:>3}  {L.get('title','')[:60]}  cat={L.get('category')}  payout={L.get('price_usdc') or L.get('price_wei')}  id={bid[:8]}")

            # Save score for /data/STATUS.md enrichment
            with open("/data/clawlancer_scores.json", "a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "id": bid,
                    "title": L.get("title"),
                    "category": L.get("category"),
                    "payout_usdc": L.get("price_usdc") or (L.get("price_wei") or 0)/1e6,
                    "score": sc,
                    "reasons": why,
                }) + "\n")

            if sc >= 10 and (l.get("price_usdc") or 0) >= 0.02:
                log(f"  >>> ATTEMPTING CLAIM (score {sc} >= 10, payout >= $0.02) <<<")
                cs, cr = api_post(f"/api/listings/{bid}/claim", api_key)
                log(f"  claim result: {cs}  {json.dumps(cr)[:300] if isinstance(cr, str) is False else cr}")

                # If successful (200 + transaction_id), auto-generate + deliver
                if cs == 200 and isinstance(cr, dict):
                    tx_id = (cr.get("transaction") or {}).get("id") if isinstance(cr.get("transaction"), dict) else cr.get("transaction_id")
                    if tx_id:
                        log(f"  -> generating deliverable for tx={tx_id[:8]}...")
                        try:
                            import subprocess
                            r = subprocess.run(
                                ["python3","/data/deliver.py", bid, L.get("title",""), L.get("category","research"), L.get("description","")],
                                capture_output=True, text=True, timeout=60,
                            )
                            log(f"  deliver: rc={r.returncode}  out={r.stdout[:300]}")
                            if r.stderr:
                                log(f"  deliver_err: {r.stderr[:300]}")
                        except Exception as e:
                            log(f"  deliver_err: {e}")
                # Could parse transaction id from cr and deliver later, but we'll see if claim succeeds first.

        # Save seen
        with open(seen_ids_path, "w") as f:
            json.dump({"ids": list(seen)}, f)

        log(f"sleeping 120s... (seen {len(seen)} bounties total)")
        time.sleep(120)


if __name__ == "__main__":
    main()