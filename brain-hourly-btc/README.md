# Brain Hourly BTC

Hourly BTC direction bets on Polymarket using Gigabrain Brain API analysis and Kelly criterion position sizing.

## How It Works

Every hour at :57:

1. Fetch BTC price from Binance
2. Ask Gigabrain Brain: "Will BTC be higher or lower in 1-2 hours?"
3. Construct the event slug from time, look up markets, pick the one near 50c
4. Size the bet using auto-Kelly from rolling historical win rate
5. Place a maker limit order (zero fees on Polymarket)
6. Redeem resolved positions every 2-4 hours

## Why This Works

- **Zero maker fees** on Polymarket means any edge > 0% is profitable
- **~8,760 bets/year** (hourly) gives the law of large numbers room to work
- **Kelly criterion sizing** from actual win rate (not model confidence) optimizes growth
- **Cold start protection**: flat bets until enough data to estimate edge

## Scripts

| Script | Purpose |
|--------|---------|
| `btc_hourly.py` | Main strategy: predict, size, resolve, status, history |
| `kelly.py` | Kelly criterion math: fraction, adjusted sizing, rolling win rate |
| `trade_log.py` | SQLite trade log: record, resolve, query trades |

## Commands

```bash
# Get BTC prediction from Brain
uv run scripts/btc_hourly.py predict

# Calculate bet size (auto-Kelly from rolling win rate)
uv run scripts/btc_hourly.py size --db trades.db --balance 500 --max-bet 25

# Record a trade after placing order
uv run scripts/btc_hourly.py record --db trades.db --btc-price 84000 --direction up --confidence 0.7 --market-slug <slug> --outcome Yes

# Mark trade won/lost during redeem cycle
uv run scripts/btc_hourly.py update --db trades.db --trade-id 1 --won --pnl 12.50

# View rolling stats
uv run scripts/btc_hourly.py status --db trades.db

# Full trade history
uv run scripts/btc_hourly.py history --db trades.db
```

## Environment Variables

- `GIGABRAIN_API_URL` — Brain API base URL (required)
- `GIGABRAIN_API_KEY` — Brain API key (required)

Polymarket trading also requires `EVM_PRIVATE_KEY` and `EVM_WALLET_ADDRESS` (configured via the polymarket skill).

## Deploy

Install as a playbook on a Gigabrain daemon agent. The bootstrap wizard (`bootstrap.md`) walks through configuration on first boot.
