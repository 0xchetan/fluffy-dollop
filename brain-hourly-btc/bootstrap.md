# Brain Hourly BTC — Bootstrap

Run these steps in order on first boot. Ask each question, wait for the user's answer, then proceed.

## Step 1: Check Balance

```
uv run "$SKILL_DIR/scripts/pm_client.py" balance
```

If `trading_balance > 0`: proceed to Step 2.

If `trading_balance == 0` but `wallet_balance > 0`:

> You have $[WALLET_BALANCE] in your wallet but it's not available for trading yet. Running `approve-trading` to authorize the exchange contract...

```
uv run "$SKILL_DIR/scripts/pm_client.py" approve-trading
```

If both are zero:

> No funds detected. Deposit USDC.e on Polygon to your agent wallet: `[EVM_WALLET_ADDRESS]`
>
> Let me know once you've sent the funds.

Wait for user confirmation, then re-check balance.

## Step 2: Max Bet

> What's the maximum bet per trade? (in USD, e.g., $25)

> Recommended: 5% of your balance ($[BALANCE * 0.05]). This caps any single trade regardless of Kelly sizing.

## Step 3: Trading Mode

> How should I handle trades?
> 1. **Autonomous** — I trade within your limits, notify you after
> 2. **Confirm** — I present each setup and wait for your approval
> 3. **Paper** — I log predictions and sizing but don't place real orders

## Step 4: First Prediction Demo

```
uv run $SUPERAGENT_PLAYBOOK/scripts/btc_hourly.py predict
```

> Brain says BTC will go [DIRECTION] with [CONFIDENCE]% confidence. Reasoning: [REASONING].
>
> Target market: [MARKET_SLUG], outcome: [OUTCOME].
> At current settings (cold start — flat bet), this would be a $[AMOUNT] trade.

## Step 5: Set Up Schedules

```python
schedule_recurring("hourly btc cycle: uv run $SUPERAGENT_PLAYBOOK/scripts/run_hourly.py --db /data/state/trades.db --max-bet <MAX_BET>", "57 * * * *")
schedule_recurring("redeem cycle: uv run $SUPERAGENT_PLAYBOOK/scripts/run_redeem.py --db /data/state/trades.db", "0 */2 * * *")
schedule_recurring("daily btc performance summary", "0 0 * * *")
```

## Step 6: Go Live

Write configuration to memory.md under `## Configuration`:
- Initial balance (from Step 1)
- Max bet per trade
- Rolling window: 50
- Trading mode

Write stats tracker to memory.md under `## Stats`:
```
Total trades: 0
Resolved: 0
Win rate: N/A (cold start)
Kelly tier: cold_start
P&L: $0.00
Current streak: 0
```

> Brain Hourly BTC is live. Balance: $[BALANCE], max bet: $[MAX_BET], mode: [MODE]. Cold start — flat bets at $[MAX_BET/2] until 50 trades resolve, then Kelly auto-escalates. Cycle fires at :57 each hour.
