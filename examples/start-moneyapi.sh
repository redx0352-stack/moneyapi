#!/bin/bash
# Auto-start moneyapi + money_loop on container boot.
# Idempotent. Also ensures python3 is installed (bare Ubuntu image).
set -e

# 1) Ensure python3 + pip + common libs (image has no dev tooling baked in)
if ! command -v python3 >/dev/null 2>&1; then
    echo "[start] python3 missing, provisioning..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null 2>&1
    apt-get install -y --no-install-recommends python3 python3-pip curl ca-certificates >/dev/null 2>&1
    echo "[start] python3 installed"
fi
# 
if ! python3 -c "import web3" 2>/dev/null; then
    echo "[start] web3 missing, pip installing..."
    python3 -m pip install --break-system-packages --quiet web3 eth-account >/dev/null 2>&1 || true
fi

# 2) Kill any stale server.py / money_loop.sh processes
for pattern in "server.py" "money_loop.sh"; do
    STALE=$(ps -e -o pid,args | awk -v p="$pattern" "\$0 ~ p && \$0 !~ /awk/ {print \$1}")
    if [ -n "$STALE" ]; then
        echo "[start] killing stale $pattern pid(s): $STALE"
        kill -9 $STALE 2>/dev/null || true
    fi
done
sleep 2

# 3) Start moneyapi (port 8787) via setsid so it survives the exec shell exit
cd /data/moneyapi
setsid python3 -u server.py </dev/null >> /data/moneyapi/access.log 2>&1 &
sleep 2
echo "[start] moneyapi started"

# 4) Start money_loop (bounty scanner + status updater)
cd /data
setsid /data/money_loop.sh </dev/null > /data/money_loop.boot.log 2>&1 &
sleep 1
echo "[start] money_loop started"
