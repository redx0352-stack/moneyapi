# moneyapi

Free + **X402 pay-per-request** crypto market signals API. Built for AI agents and humans.

> "I Let an AI Run a Business Alone" playbook — but for $0. Real revenue, real on-chain payments, no Stripe needed.

**Live endpoint:** `https://moneyapi.51.170.131.228.sslip.io` (Coolify public URL)
**X402 wallet:** `0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60` (Base mainnet, USDC)
**Status:** Operational. All endpoints verified.

## What it does

| Endpoint | Free? | Description |
|---|---|---|
| `GET /api/v1/health` | yes | Health + payment address |
| `GET /api/v1/btc` | yes | Bitcoin price + 24h change + market cap |
| `GET /api/v1/eth` | yes | Ethereum price + 24h change + market cap |
| `GET /api/v1/gas` | yes | Ethereum gas prices (gwei) |
| `GET /api/v1/fear-greed` | yes | Crypto Fear & Greed Index |
| `GET /api/v1/trending` | yes | Top trending tokens (CoinGecko) |
| `GET /api/v1/news?limit=10` | yes | Latest crypto headlines (Cointelegraph RSS) |
| `GET /api/v1/whale-alerts?limit=5` | yes | Recent whale movements |
| `GET /api/v1/signal?symbol=bitcoin` | yes | Composite trading signal 0-100 |
| `GET /api/v1/premium/*` | $0.001 USDC | Same data, gated by X402 |
| `GET /agent/{profile,services,portfolio}` | yes | Atelier agent protocol |
| `POST /agent/execute` | yes | Order fulfillment for Atelier marketplace |

## X402 (pay-per-request)

Premium endpoints require **$0.001 USDC per call**, paid directly on Base mainnet. No accounts, no API keys.

```
1. curl https://moneyapi.51.170.131.228.sslip.io/api/v1/premium/btc
   -> 402 Payment Required + WWW-Authenticate: X402 challenge

2. Pay 0.001 USDC (= 1000 micro-USDC) to 0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60
   on Base mainnet.

3. curl -H "X-Payment-Tx: 0x<your_tx_hash>" https://moneyapi.51.170.131.228.sslip.io/api/v1/premium/btc
   -> 200 OK + the data
```

The 402 response includes the full Bazaar discovery extension so any X402-compatible client can find us automatically.

## Quick start (free tier)

```bash
# Get current BTC price
curl https://moneyapi.51.170.131.228.sslip.io/api/v1/btc

# Get a trading signal
curl "https://moneyapi.51.170.131.228.sslip.io/api/v1/signal?symbol=ethereum"

# Multi-symbol loop
for sym in bitcoin ethereum solana; do
  curl -s "https://moneyapi.51.170.131.228.sslip.io/api/v1/signal?symbol=$sym"
done
```

## Self-host

```bash
git clone https://github.com/redx0352-stack/moneyapi
cd moneyapi
pip install web3
python3 server.py   # Listens on :8787
```

The whole server is **one file**, ~600 lines, stdlib + web3 only.

## Architecture

- HTTP server: Python http.server (no Flask, no FastAPI)
- X402 verification: on-chain USDC transfer check via eth_getTransactionReceipt on Base mainnet
- Upstream data: CoinGecko, alternative.me, Cointelegraph RSS, eth.publicnode.com
- Caching: in-memory TTL (no Redis dependency)
- Bazaar-compatible: emits proper extensions.bazaar in 402 responses

## Used by

- Atelier (https://useatelier.ai) agent marketplace - listed as vrmont (4 services)
- Clawlancer (https://clawlancer.ai) bounty marketplace - registered agent vrmont

## Files

- server.py - single-file HTTP server (the whole API)
- examples/bounty_scan.py - GitHub Issues bounty scanner
- examples/clawlancer_loop.py - Clawlancer heartbeat
- examples/deliver.py - bounty deliverable generator
- examples/balance_check.py - USDC balance poller
- examples/money_loop.sh - orchestrator
- examples/start-moneyapi.sh - auto-start on container boot
- PLAN.md - original $0 to $1 plan

## License

MIT. Use commercially, no attribution required, but appreciated.

## Contact

- Telegram: @vrmont_bot (DM after /start)
- GitHub: redx0352-stack
- Wallet: 0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60 (Base)

## Related

- X402 protocol: https://x402.org
- Ben Awad AI runs a business series: https://www.youtube.com/@bawad (inspiration)
- x402 Bazaar: https://docs.x402.org
