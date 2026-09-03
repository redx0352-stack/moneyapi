#!/bin/bash
# money_loop.sh - autonomous overnight runner.
# Scans bounties every BOUNTY_INTERVAL (default 4h),
# refreshes status every STATUS_INTERVAL (default 1h),
# checks wallet balance every BALANCE_INTERVAL (default 5 min),
# restarts moneyapi if dead.
# Designed to run forever via setsid.
set -e

cd /data
export PYTHONPATH=/data:$PYTHONPATH

BOUNTY_INTERVAL=${BOUNTY_INTERVAL:-14400}   # 4h
STATUS_INTERVAL=${STATUS_INTERVAL:-3600}    # 1h
BALANCE_INTERVAL=${BALANCE_INTERVAL:-300}   # 5 min
BOUNTY_MIN_PAYOUT=${BOUNTY_MIN_PAYOUT:-50}

echo "[money_loop] starting  bounty_every=${BOUNTY_INTERVAL}s  status_every=${STATUS_INTERVAL}s  balance_every=${BALANCE_INTERVAL}s"

last_bounty=0
last_status=0
last_balance=0

while true; do
    now=$(date +%s)

    # Balance check (most frequent)
    if [ $((now - last_balance)) -ge $BALANCE_INTERVAL ]; then
        echo "[money_loop] $(date -u +%FT%TZ) balance_check..."
        python3 /data/balance_check.py >> /data/money_loop.log 2>&1 || echo "[money_loop] balance_check failed"
        last_balance=$now
    fi

    # Scan bounties
    if [ $((now - last_bounty)) -ge $BOUNTY_INTERVAL ]; then
        echo "[money_loop] $(date -u +%FT%TZ) bounty_scan..."
        MIN_PAYOUT=$BOUNTY_MIN_PAYOUT python3 /data/bounty_scan.py >> /data/money_loop.log 2>&1 || echo "[money_loop] bounty_scan failed"
        last_bounty=$now
    fi

    # Update status
    if [ $((now - last_status)) -ge $STATUS_INTERVAL ]; then
        echo "[money_loop] $(date -u +%FT%TZ) status_update..."
        python3 /data/status_update.py >> /data/money_loop.log 2>&1 || echo "[money_loop] status_update failed"
        last_status=$now
    fi

    # Ensure moneyapi is still up; restart if dead
    if ! (echo > /dev/tcp/127.0.0.1/8787) 2>/dev/null; then
        echo "[money_loop] $(date -u +%FT%TZ) moneyapi DOWN, restarting..."
        cd /data/moneyapi && setsid python3 -u server.py </dev/null >> /data/moneyapi/access.log 2>&1 &
        cd /data
        sleep 2
    fi

    sleep 30   # check cadence
done