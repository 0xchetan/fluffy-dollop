# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Kelly criterion calculator for binary prediction markets.

At even money (buy_price=0.50), Kelly simplifies to: f* = 2p - 1
where p is the estimated win probability (rolling win rate).

Auto-Kelly: fraction escalates with edge strength + sample confidence.
  cold start (< window trades):  flat bet
  win_rate 50-55%:               quarter Kelly (0.25)
  win_rate 55-60% + 2x window:   half Kelly (0.50)
  win_rate 60%+ + 2x window:     full Kelly (1.0)

Usage:
    uv run kelly.py --win-rate 0.55 --bankroll 500 --max-bet 25
"""

from __future__ import annotations

import argparse
import json


def kelly_fraction(win_rate: float, buy_price: float = 0.50) -> float:
    """Calculate raw Kelly fraction f* for a binary bet.

    For a binary market bought at `buy_price`:
    - Win payout per dollar risked: (1 - buy_price) / buy_price
    - Loss: lose the stake
    - f* = (p * b - q) / b  where b = odds, p = win prob, q = 1-p
    - At buy_price=0.50: b=1, so f* = p - q = 2p - 1
    """
    if win_rate <= 0 or win_rate > 1:
        return 0.0
    if buy_price <= 0 or buy_price >= 1:
        return 0.0

    b = (1 - buy_price) / buy_price  # odds (payout ratio)
    q = 1 - win_rate
    f_star = (win_rate * b - q) / b

    return max(f_star, 0.0)


def auto_kelly_multiplier(win_rate: float, sample_size: int, window: int) -> tuple[float, str]:
    """Pick Kelly multiplier automatically based on edge strength + sample confidence.

    Returns (multiplier, tier_name).
    """
    if sample_size < window:
        return 0.0, "cold_start"

    if win_rate <= 0.50:
        return 0.0, "no_edge"

    # Need 2x window samples for higher tiers
    confident = sample_size >= window * 2

    if win_rate >= 0.60 and confident:
        return 1.0, "full"
    elif win_rate >= 0.55 and confident:
        return 0.5, "half"
    else:
        return 0.25, "quarter"


def adjusted_kelly(
    win_rate: float,
    buy_price: float,
    multiplier: float,
    bankroll: float,
    max_bet: float,
) -> float:
    """Calculate dollar bet amount using fractional Kelly.

    Args:
        win_rate: Estimated win probability from rolling history.
        buy_price: Market price of the outcome being bought.
        multiplier: Kelly fraction (0.25 = quarter Kelly).
        bankroll: Total capital allocated to this strategy.
        max_bet: Hard cap on any single bet.

    Returns:
        Dollar amount to bet.
    """
    f_star = kelly_fraction(win_rate, buy_price)
    if f_star <= 0:
        return 0.0

    bet = f_star * multiplier * bankroll
    return min(bet, max_bet)


def rolling_win_rate(
    results: list[bool], window: int = 50
) -> tuple[float, int]:
    """Calculate win rate over the most recent `window` results.

    Args:
        results: List of booleans (True=win, False=loss), oldest first.
        window: Number of recent results to consider.

    Returns:
        (win_rate, sample_size) tuple.
    """
    if not results:
        return 0.0, 0

    recent = results[-window:]
    sample = len(recent)
    wins = sum(recent)
    return wins / sample, sample


def main():
    parser = argparse.ArgumentParser(description="Kelly criterion calculator")
    parser.add_argument("--win-rate", type=float, required=True, help="Rolling win rate (0-1)")
    parser.add_argument("--buy-price", type=float, default=0.50, help="Market buy price (default: 0.50)")
    parser.add_argument("--bankroll", type=float, required=True, help="Total bankroll in USD")
    parser.add_argument("--max-bet", type=float, required=True, help="Maximum bet per trade in USD")
    parser.add_argument("--sample-size", type=int, default=0, help="Number of resolved trades")
    parser.add_argument("--window", type=int, default=50, help="Rolling window size")
    args = parser.parse_args()

    multiplier, tier = auto_kelly_multiplier(args.win_rate, args.sample_size, args.window)
    f_star = kelly_fraction(args.win_rate, args.buy_price)
    bet = adjusted_kelly(
        args.win_rate, args.buy_price, multiplier,
        args.bankroll, args.max_bet,
    )

    result = {
        "win_rate": args.win_rate,
        "buy_price": args.buy_price,
        "kelly_fraction_raw": round(f_star, 4),
        "kelly_tier": tier,
        "kelly_multiplier": multiplier,
        "kelly_fraction_adjusted": round(f_star * multiplier, 4),
        "bankroll": args.bankroll,
        "max_bet": args.max_bet,
        "bet_amount": round(bet, 2),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
