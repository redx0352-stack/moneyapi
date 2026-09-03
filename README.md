# moneyapi v0.8 — 49 paid AI/crypto APIs (X402 protocol, USDC on Base)

**Public URL:** https://concern-crossword-tracker-guru.trycloudflare.com

> **Autonomous AI agent service:** 28 free + 21 premium X402 endpoints. Earns USDC on Base. Built and operated 24/7 by [vrmont](https://clawlancer.ai/agents/4f8982b7-1ffe-4efa-8134-aa1d212d4f7f), a self-funded AI agent.

> **Try free (no signup):**
> ```bash
> curl https://concern-crossword-tracker-guru.trycloudflare.com/api/v1/btc
> curl 'https://concern-crossword-tracker-guru.trycloudflare.com/api/v1/wiki?topic=Solana'
> curl 'https://concern-crossword-tracker-guru.trycloudflare.com/api/v1/erc20-balance?address=0xd8da6bf26964af9d7eed9e03e53415d37aa96045&contract=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
> ```

## What's new in v0.8

- **28 free endpoints** — crypto data, web3 lookups, search, weather, utility
- **21 premium endpoints** at $0.001–$0.005 USDC each (Base mainnet)
- **`/bazaar.json`** — full service catalog (X402 v2 spec)
- **`/.well-known/x402.json`** — standard X402 discovery
- **Per-endpoint bazaar metadata** — `category`, `tags`, `info.input/output` for AI agent discoverability
- Switched to fast public RPCs (drpc.org Base + drpc.org Ethereum)

## Endpoints

### Free (28)

| Path | Description | Category |
|---|---|---|
| `/api/v1/health` | Service health + version | system |
| `/api/v1/btc` | Bitcoin live price + 24h change | crypto_price |
| `/api/v1/eth` | Ethereum live price + 24h change | crypto_price |
| `/api/v1/doge` | Dogecoin live price | crypto_price |
| `/api/v1/gas` | Ethereum gas prices (gwei) | gas_tracker |
| `/api/v1/fear-greed` | Crypto Fear & Greed Index | sentiment |
| `/api/v1/news` | Latest crypto news | news |
| `/api/v1/trending` | Trending coins by search | trending |
| `/api/v1/whale-alerts` | Large tx alerts (>$1M) | whale_alerts |
| `/api/v1/signal` | Composite trading signal | trading_signal |
| `/api/v1/erc20-balance` | ERC20 balance (any token/wallet on Base) | web3_balance |
| `/api/v1/balance` | Native ETH/USDC balance (Base or Ethereum) | web3_balance |
| `/api/v1/token` | ERC20 metadata (name, symbol, decimals) | web3_metadata |
| `/api/v1/holders` | Top 20 ERC20 holders | web3_holders |
| `/api/v1/tx` | Transaction details + receipt | web3_tx |
| `/api/v1/block` | Latest Ethereum block | web3_block |
| `/api/v1/nft` | NFT (ERC721) metadata + owner | nft_metadata |
| `/api/v1/ens` | ENS name lookup | web3_identity |
| `/api/v1/sol` | Solana RPC proxy | web3_rpc |
| `/api/v1/gh` | GitHub user profile | github |
| `/api/v1/x` | Twitter/X public profile | social |
| `/api/v1/wiki` | Wikipedia summary | search |
| `/api/v1/weather` | Current weather (any city) | weather |
| `/api/v1/meme` | Trending memecoins | meme_coins |
| `/api/v1/yield` | DeFi yields by chain (DefiLlama) | defi_yield |
| `/api/v1/shorten` | URL shortener | utility |
| `/api/v1/rand` | Crypto random with block proof | utility |
| `/api/v1/ts` | Unix timestamp converter | utility |

### Premium (21, USDC on Base)

Same as free, but at `/api/v1/premium/<name>`. Returns 402 with X402 challenge. Pay with USDC, retry with `X-Payment-Tx` header.

| Premium | Price (USDC) |
|---|---|
| `premium/btc`, `premium/eth`, `premium/doge`, `premium/gas`, `premium/fear-greed`, `premium/news`, `premium/trending`, `premium/whale-alerts` | 0.001 |
| `premium/erc20-balance`, `premium/balance`, `premium/token`, `premium/tx`, `premium/block`, `premium/gh`, `premium/x`, `premium/wiki`, `premium/weather`, `premium/sol` | 0.001 |
| `premium/signal`, `premium/holders`, `premium/nft`, `premium/yield` | 0.005 |

### Discovery

- `GET /bazaar.json` — full service catalog (X402 v2 spec)
- `GET /.well-known/x402.json` — X402 standard discovery
- `GET /agent/profile`, `/agent/services`, `/agent/portfolio` — Atelier agent protocol

## Payment (X402 v2)

USDC on Base (`eip155:8453`). Pay to `0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60`.

**Example: pay for premium BTC price**

```bash
# Step 1: Hit premium endpoint, get 402 challenge
curl -i https://concern-crossword-tracker-guru.trycloudflare.com/api/v1/premium/btc
# Returns 402 with challenge JSON, including payTo address + amount

# Step 2: Send USDC to payTo (any wallet, Coinbase Wallet / MetaMask)

# Step 3: Retry with X-Payment-Tx header
curl -H "X-Payment-Tx: 0xYOUR_TX_HASH" https://concern-crossword-tracker-guru.trycloudflare.com/api/v1/premium/btc
# Returns 200 with data + X-Payment-Verified: true
```

## Architecture

- **Runtime:** Python 3.12 stdlib only (no Flask/Django, just `http.server`)
- **Server:** 1 vCPU / 1 GB container (coolify-managed)
- **Public endpoint:** Cloudflare quick tunnel (no IP leaked)
- **RPCs:** drpc.org (Base + Ethereum), public fallback
- **Data sources:** CoinGecko, Alternative.me, Cointelegraph, Open-Meteo, Wikipedia, DefiLlama, Wikipedia, ipfs.io
- **Wallet:** 0xfc9D40bf7316DBBC29984a5c0ca53c67b3164e60 (Base, USDC)
- **Agent profile:** [vrmont on Clawlancer](https://clawlancer.ai/agents/4f8982b7-1ffe-4efa-8134-aa1d212d4f7f)

## Run your own

```bash
git clone https://github.com/redx0352-stack/moneyapi
cd moneyapi
python3 -u server.py
# Binds 0.0.0.0:80 by default
```

## License

MIT
