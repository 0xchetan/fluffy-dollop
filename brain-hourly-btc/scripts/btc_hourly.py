# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
#     "pydantic>=2.0",
# ]
# ///
"""Brain Hourly BTC — main strategy script.

Fetches BTC price from Binance, asks Gigabrain Brain for direction,
constructs the next hour's event slug, and outputs everything the agent
needs to pick a market and place a trade.

Event slug pattern (ET timezone):
  bitcoin-above-on-{month}-{day}-{year}-{hour}{ampm}-et

The agent uses the event slug to look up available markets via pm_client.py,
picks the one closest to 50c, and places a limit order.

Commands:
    predict  — BTC price + Brain direction + event slug → JSON
    size     — Calculate bet amount from rolling win rate + auto-Kelly
    record   — Log a trade after placing an order
    update   — Mark a trade won/lost (during redeem cycle)
    status   — Rolling stats: win rate, P&L, streak, sample size
    history  — Full trade history as JSON

Usage:
    uv run btc_hourly.py predict
    uv run btc_hourly.py size --db trades.db --balance 500 --max-bet 25
    uv run btc_hourly.py record --db trades.db --btc-price 66600 --direction up --confidence 0.7 --market-slug <slug> --outcome Yes
    uv run btc_hourly.py update --db trades.db --trade-id 1 --won --pnl 12.50
    uv run btc_hourly.py status --db trades.db
    uv run btc_hourly.py history --db trades.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

# Add scripts dir to path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from kelly import adjusted_kelly, auto_kelly_multiplier, kelly_fraction, rolling_win_rate
from trade_log import get_history, get_rolling_results, get_stats, init_db, record_prediction, resolve_trade

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Event slug construction (ET timezone)
# ---------------------------------------------------------------------------

def next_hour_et() -> datetime:
    """Get the start of the next hour in ET."""
    now = datetime.now(ET)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def construct_event_slug(target_et: datetime, with_year: bool = False) -> str:
    """Construct event slug for Polymarket hourly BTC markets.

    Polymarket has used two formats:
      - bitcoin-up-or-down-april-3-8am-et          (no year)
      - bitcoin-up-or-down-april-3-2026-8am-et     (with year)
    The format flips periodically. Callers should try both.
    """
    month = target_et.strftime("%B").lower()
    day = target_et.day
    hour_12 = target_et.strftime("%I").lstrip("0")
    ampm = target_et.strftime("%p").lower()
    if with_year:
        year = target_et.year
        return f"bitcoin-up-or-down-{month}-{day}-{year}-{hour_12}{ampm}-et"
    return f"bitcoin-up-or-down-{month}-{day}-{hour_12}{ampm}-et"


# ---------------------------------------------------------------------------
# Brain API + Binance
# ---------------------------------------------------------------------------

class BrainPrediction(BaseModel):
    direction: str  # "up" or "down"
    confidence: float
    reasoning: str


async def fetch_btc_price() -> float:
    """Fetch current BTC/USDT price from Binance."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
        )
        resp.raise_for_status()
        return float(resp.json()["price"])


async def _brain_query(api_url: str, headers: dict, payload: dict) -> str:
    """Query Brain API via SSE streaming. Always uses stream=true."""
    payload["stream"] = True
    content_parts: list[str] = []

    async with httpx.AsyncClient(timeout=600, headers=headers) as client:
        async with client.stream(
            "POST", f"{api_url}/v1/chat", json=payload,
        ) as resp:
            resp.raise_for_status()
            event_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line:
                    event_lines.append(line)
                    continue
                for event_line in event_lines:
                    if not event_line.startswith("data: "):
                        continue
                    try:
                        evt = json.loads(event_line[6:])
                    except json.JSONDecodeError:
                        continue
                    if evt.get("event") == "RunResponseContent":
                        content_parts.append(evt.get("content", ""))
                    elif evt.get("event") == "error":
                        raise RuntimeError(evt.get("message", "Stream error"))
                event_lines = []

    return "".join(content_parts)


def _extract_json(text: str) -> dict:
    """Extract JSON from Brain response (may be wrapped in markdown code block)."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


async def ask_brain(btc_price: float) -> BrainPrediction:
    """Ask Gigabrain Brain for a BTC direction call."""
    api_url = os.environ.get("GIGABRAIN_API_URL", "")
    api_key = os.environ.get("GIGABRAIN_API_KEY", "")

    if not api_url:
        print(json.dumps({"success": False, "error": "GIGABRAIN_API_URL not set"}))
        sys.exit(1)

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    prompt = (
        f"BTC is currently at ${btc_price:,.2f}. "
        f"Will it be higher or lower in 1-2 hours? "
        f"Respond ONLY as JSON with exactly these fields: "
        f'direction (string: "up" or "down"), '
        f"confidence (float: 0.0 to 1.0), "
        f"reasoning (string: 1-2 sentences)"
    )

    payload: dict = {"message": prompt}

    model = os.environ.get("GIGABRAIN_MODEL")
    model_provider = os.environ.get("GIGABRAIN_MODEL_PROVIDER", "")
    if model:
        payload["model"] = model
    if model_provider:
        payload["model_provider"] = model_provider

    content = await _brain_query(api_url.rstrip("/"), headers, payload)

    if not content:
        raise RuntimeError("Empty response from Brain API")

    parsed = _extract_json(content)
    return BrainPrediction(
        direction=parsed["direction"].lower(),
        confidence=float(parsed["confidence"]),
        reasoning=parsed.get("reasoning", ""),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_predict(args):
    """Fetch BTC price, get Brain prediction, construct event slug."""
    btc_price = await fetch_btc_price()
    prediction = await ask_brain(btc_price)

    target_et = next_hour_et()
    event_slug = construct_event_slug(target_et, with_year=False)
    event_slug_alt = construct_event_slug(target_et, with_year=True)

    now_utc = datetime.now(timezone.utc)
    mins_until = (target_et.astimezone(timezone.utc) - now_utc).total_seconds() / 60

    print(json.dumps({
        "btc_price": btc_price,
        "direction": prediction.direction,
        "confidence": prediction.confidence,
        "reasoning": prediction.reasoning,
        "event_slug": event_slug,
        "event_slug_alt": event_slug_alt,
        "target_time_et": target_et.strftime("%Y-%m-%d %I:%M %p ET"),
        "minutes_until": round(mins_until, 1),
    }, indent=2))


async def cmd_size(args):
    """Calculate bet amount — uses trading balance as bankroll, Kelly auto-escalates."""
    conn = init_db(args.db)
    results = get_rolling_results(conn, args.window)
    conn.close()

    win_rate, sample_size = rolling_win_rate(results, args.window)
    balance = args.balance

    if balance <= 0:
        print(json.dumps({
            "mode": "no_balance",
            "balance": balance,
            "bet_amount": 0,
            "reason": "No trading balance — deposit USDC.e",
        }, indent=2))
        return

    # Auto Kelly: pick multiplier based on edge + sample confidence
    multiplier, tier = auto_kelly_multiplier(win_rate, sample_size, args.window)

    if tier == "cold_start":
        bet = min(round(args.max_bet * 0.5, 2), balance)
        print(json.dumps({
            "mode": "cold_start",
            "sample_size": sample_size,
            "window": args.window,
            "balance": balance,
            "bet_amount": bet,
            "reason": f"Only {sample_size}/{args.window} resolved trades — flat bet",
        }, indent=2))
        return

    if tier == "no_edge":
        print(json.dumps({
            "mode": "paused",
            "sample_size": sample_size,
            "win_rate": round(win_rate, 4),
            "balance": balance,
            "bet_amount": 0,
            "reason": f"Win rate {win_rate:.1%} <= 50% over {sample_size} trades — no edge",
        }, indent=2))
        return

    f_star = kelly_fraction(win_rate, args.buy_price)
    bet = adjusted_kelly(
        win_rate, args.buy_price, multiplier,
        balance, args.max_bet,
    )

    print(json.dumps({
        "mode": "kelly",
        "kelly_tier": tier,
        "kelly_multiplier": multiplier,
        "kelly_raw": round(f_star, 4),
        "sample_size": sample_size,
        "win_rate": round(win_rate, 4),
        "balance": balance,
        "max_bet": args.max_bet,
        "bet_amount": round(bet, 2),
    }, indent=2))


async def cmd_record(args):
    """Record a trade to the database after placing an order."""
    conn = init_db(args.db)
    trade_id = record_prediction(
        conn,
        btc_price=args.btc_price,
        direction=args.direction,
        confidence=args.confidence,
        reasoning=args.reasoning,
        market_slug=args.market_slug,
        outcome_bought=args.outcome,
        shares=args.shares,
        price=args.price,
        order_id=args.order_id,
        status="pending",
    )
    conn.close()
    print(json.dumps({"trade_id": trade_id, "status": "recorded"}))


async def cmd_update(args):
    """Update a trade's status during redeem cycle.

    --settled: mark as settled_won/settled_lost (outcome known, not yet redeemed)
    without --settled: mark as won/lost (final, cash collected)
    """
    conn = init_db(args.db)
    resolve_trade(conn, args.trade_id, args.won, args.pnl, notes=args.notes, settled=args.settled)
    conn.close()
    if args.settled:
        status = "settled_won" if args.won else "settled_lost"
    else:
        status = "won" if args.won else "lost"
    print(json.dumps({"trade_id": args.trade_id, "status": status, "pnl": args.pnl}))


async def cmd_status(args):
    """Show rolling stats."""
    conn = init_db(args.db)
    stats = get_stats(conn, args.window)
    conn.close()
    print(json.dumps(stats, indent=2))


async def cmd_history(args):
    """Show full trade history."""
    conn = init_db(args.db)
    history = get_history(conn, args.limit)
    conn.close()
    print(json.dumps(history, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="Brain Hourly BTC strategy")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("predict", help="BTC price + Brain prediction + target market slug")

    p = sub.add_parser("size", help="Calculate bet amount (auto-Kelly, bankroll tracks P&L)")
    p.add_argument("--db", default="trades.db", help="Path to trade database")
    p.add_argument("--balance", type=float, required=True, help="Current trading balance in USD (from pm_client.py balance)")
    p.add_argument("--max-bet", type=float, required=True, help="Maximum bet per trade in USD")
    p.add_argument("--buy-price", type=float, default=0.50, help="Market buy price (default: 0.50)")
    p.add_argument("--window", type=int, default=50, help="Rolling window size")

    p = sub.add_parser("record", help="Record a trade after placing an order")
    p.add_argument("--db", default="trades.db", help="Path to trade database")
    p.add_argument("--btc-price", type=float, required=True)
    p.add_argument("--direction", required=True, choices=["up", "down"])
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--reasoning", default="")
    p.add_argument("--market-slug", required=True)
    p.add_argument("--outcome", required=True, choices=["Up", "Down"])
    p.add_argument("--shares", type=float, default=0)
    p.add_argument("--price", type=float, default=0.50)
    p.add_argument("--order-id", default="")

    p = sub.add_parser("update", help="Mark a trade won/lost or settled (during redeem cycle)")
    p.add_argument("--db", default="trades.db", help="Path to trade database")
    p.add_argument("--trade-id", type=int, required=True)
    p.add_argument("--won", action="store_true")
    p.add_argument("--lost", action="store_true")
    p.add_argument("--settled", action="store_true", help="Mark as settled (outcome known, not yet redeemed)")
    p.add_argument("--pnl", type=float, required=True)
    p.add_argument("--notes", default="")

    p = sub.add_parser("status", help="Show rolling stats")
    p.add_argument("--db", default="trades.db", help="Path to trade database")
    p.add_argument("--window", type=int, default=50, help="Rolling window size")

    p = sub.add_parser("history", help="Show trade history")
    p.add_argument("--db", default="trades.db", help="Path to trade database")
    p.add_argument("--limit", type=int, default=50, help="Number of trades to show")

    args = parser.parse_args()

    # Parse --won/--lost into a boolean for cmd_update
    if args.command == "update":
        if args.won == args.lost:
            parser.error("Specify exactly one of --won or --lost")
        args.won = args.won  # True if --won, False if --lost

    handler = {
        "predict": cmd_predict,
        "size": cmd_size,
        "record": cmd_record,
        "update": cmd_update,
        "status": cmd_status,
        "history": cmd_history,
    }[args.command]

    try:
        asyncio.run(handler(args))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, default=str))
        sys.exit(1)


if __name__ == "__main__":
    main()
