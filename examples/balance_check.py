#!/usr/bin/env python3
"""
balance_check.py - Poll moneyapi's X402 wallet for incoming USDC payments on Base.
Logs every received transfer to /data/revenue.json and appends to STATUS.md.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WALLET_PATH = "/data/.secrets/x402_wallet"
BASE_RPC = "https://mainnet.base.org"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STATE_PATH = "/data/.balance_state.json"
REVENUE_LOG = "/data/revenue.json"


def rpc(method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(BASE_RPC, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (moneyapi/balance)",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("result")
    except Exception:
        return None


def get_token_balance(holder, token, block="latest"):
    data = "0x70a08231" + "0"*24 + holder[2:].lower()
    return rpc("eth_call", [{"to": token, "data": data}, block])


def scan_transfers(wallet, from_block, to_block):
    padded = "0x" + "0"*24 + wallet[2:].lower()
    logs = rpc("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": USDC_BASE,
        "topics": [TRANSFER_TOPIC, None, padded],
    }])
    return logs or []


def main():
    with open(WALLET_PATH) as f:
        wallet = json.load(f)["address"]

    # State: last block scanned + known tx hashes
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)
    else:
        state = {"last_block": 0, "known_txs": []}

    # Current block
    cur_hex = rpc("eth_blockNumber")
    if not cur_hex:
        print("[balance_check] RPC unreachable")
        return
    cur_block = int(cur_hex, 16)
    print(f"[balance_check] current block: {cur_block}")

    # Scan range: from last+1 to current
    start = max(state["last_block"] + 1, cur_block - 2000)  # cap at 2000 blocks
    if cur_block < start:
        print(f"[balance_check] already scanned through block {state['last_block']}")
    else:
        print(f"[balance_check] scanning blocks {start}..{cur_block}")
        new_txs = scan_transfers(wallet, start, cur_block)
        for log in new_txs:
            tx = log.get("transactionHash", "")
            if tx in state["known_txs"]:
                continue
            value = int(log.get("data", "0x0"), 16) / 1e6
            block_num = int(log.get("blockNumber", "0x0"), 16)
            from_addr = "0x" + log["topics"][1][-40:]
            print(f"  + {value:.6f} USDC  block={block_num}  from={from_addr}  tx={tx}")

            # Append to revenue log
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "block": block_num,
                "from": from_addr,
                "value_usdc": value,
                "tx": tx,
            }
            revenue = []
            if os.path.exists(REVENUE_LOG):
                try:
                    with open(REVENUE_LOG) as f:
                        revenue = json.load(f)
                except Exception:
                    revenue = []
            revenue.append(entry)
            with open(REVENUE_LOG, "w") as f:
                json.dump(revenue, f, indent=2)
            state["known_txs"].append(tx)

        state["last_block"] = cur_block

    # Current balance
    raw = get_token_balance(wallet, USDC_BASE)
    balance = int(raw, 16) / 1e6 if raw else 0
    print(f"[balance_check] current balance: {balance:.6f} USDC")

    # Persist state
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)

    # Update STATUS.md revenue section
    update_status_balance(balance)


def update_status_balance(balance_usdc):
    status_path = "/data/STATUS.md"
    if not os.path.exists(status_path):
        return
    with open(status_path) as f:
        md = f.read()
    # Find or insert revenue section after moneyapi status
    lines = md.splitlines()
    new_lines = []
    inserted = False
    for i, ln in enumerate(lines):
        new_lines.append(ln)
        if not inserted and ln.startswith("- **Status:**") and "moneyapi" not in ln:
            new_lines.append(f"- **Wallet balance:** {balance_usdc:.6f} USDC (Base) — wallet 0xfc9D40...4e60")
            new_lines.append(f"- **Premium endpoint:** `/api/v1/premium/*` (X402, $0.001 USDC/call)")
            inserted = True
    if not inserted:
        # Just append at top
        new_lines = [f"- **Wallet balance:** {balance_usdc:.6f} USDC", ""] + lines
    with open(status_path, "w") as f:
        f.write("\n".join(new_lines))


if __name__ == "__main__":
    main()