# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Deterministic hourly trading cycle for Brain Hourly BTC.

Runs the full 9-step trading cycle as a single script:
  1. Cancel stale orders (>2hrs)
  2. Risk check (win rate + loss streak)
  3. Balance check
  4. Predict (Brain direction call)
  5. Find market (event slug → market)
  6. Duplicate check (already traded this market?)
  7. Size (Kelly bet sizing)
  8. Buy (place limit order)
  9. Record (log trade to DB)

Zero external dependencies — stdlib only.
Calls btc_hourly.py and pm_client.py as subprocesses via `uv run`.

Usage:
    uv run run_hourly.py --max-bet 25
    uv run run_hourly.py --max-bet 25 --dry-run
    uv run run_hourly.py --max-bet 25 --db /data/state/trades.db
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_cmd(args_list: list[str], label: str, timeout: int = 120) -> dict:
    """Run a subprocess, parse JSON stdout, return result dict."""
    try:
        result = subprocess.run(
            args_list,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"{label}: timed out after {timeout}s"}
    except FileNotFoundError:
        return {"success": False, "error": f"{label}: command not found: {args_list[0]}"}

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        # Try to parse JSON error from stdout first
        if stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return {
            "success": False,
            "error": f"{label}: exit code {result.returncode}",
            "stderr": stderr or None,
            "stdout": stdout or None,
        }

    if not stdout:
        return {"success": False, "error": f"{label}: no output"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"success": False, "error": f"{label}: invalid JSON", "raw": stdout[:500]}


def resolve_paths(args) -> tuple[str, str]:
    """Resolve btc_hourly.py and pm_client.py paths.

    Returns (btc_hourly_path, pm_client_path).
    """
    script_dir = Path(__file__).resolve().parent
    btc_hourly = str(script_dir / "btc_hourly.py")

    if args.pm_client:
        pm_client = args.pm_client
    else:
        skill_dir = os.environ.get("SKILL_DIR", "")
        if skill_dir:
            pm_client = os.path.join(skill_dir, "scripts", "pm_client.py")
        else:
            pm_client = str(script_dir.parent.parent.parent / "skills" / "polymarket" / "scripts" / "pm_client.py")

    return btc_hourly, pm_client


def step_result(step: str, success: bool, data: dict | None = None, error: str | None = None) -> dict:
    """Build a step result dict."""
    r: dict = {"step": step, "success": success}
    if data:
        r["data"] = data
    if error:
        r["error"] = error
    return r


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_cancel_stale(pm_client: str) -> tuple[bool, dict, dict]:
    """Cancel orders older than 2 hours. Best-effort, never bails."""
    orders_result = run_cmd(
        ["uv", "run", pm_client, "my-orders", "--raw"],
        "my-orders",
    )

    canceled = []
    if orders_result.get("success") and orders_result.get("orders"):
        now = datetime.now(timezone.utc)
        for order in orders_result["orders"]:
            # Try to parse order timestamp
            created = order.get("created_at") or order.get("timestamp") or ""
            if not created:
                continue
            try:
                order_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = (now - order_time).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            if age_hours > 2:
                order_id = order.get("id") or order.get("order_id", "")
                if order_id:
                    cancel = run_cmd(
                        ["uv", "run", pm_client, "cancel-order", "--order-id", str(order_id)],
                        f"cancel-{order_id}",
                    )
                    canceled.append({"order_id": order_id, "age_hours": round(age_hours, 1), "result": cancel.get("success", False)})

    data = {"canceled": canceled, "total_orders": len(orders_result.get("orders", []))}
    return True, data, step_result("cancel_stale", True, data)


def step_risk_check(btc_hourly: str, db: str, window: int, loss_streak_limit: int) -> tuple[bool, dict, dict]:
    """Check risk rules: win rate and loss streak."""
    result = run_cmd(
        ["uv", "run", btc_hourly, "status", "--db", db, "--window", str(window)],
        "status",
    )

    if not result.get("total_trades") and not result.get("resolved"):
        # No trades yet — cold start, continue
        return True, result, step_result("risk_check", True, {"status": "cold_start", "resolved": 0})

    resolved = result.get("resolved", 0)
    win_rate = result.get("rolling_win_rate")
    streak = result.get("current_streak", 0)
    streak_type = result.get("current_streak_type")

    # Check negative edge with sufficient sample
    if win_rate is not None and win_rate <= 0.50 and resolved >= window:
        return False, result, step_result(
            "risk_check", False,
            error=f"Win rate {win_rate:.1%} <= 50% over {resolved} resolved trades — paused",
        )

    # Check consecutive loss streak
    if streak_type == "lost" and streak >= loss_streak_limit:
        return False, result, step_result(
            "risk_check", False,
            error=f"{streak} consecutive losses (limit {loss_streak_limit}) — paused",
        )

    return True, result, step_result("risk_check", True, {
        "resolved": resolved,
        "rolling_win_rate": win_rate,
        "streak": streak,
        "streak_type": streak_type,
    })


def step_balance(pm_client: str) -> tuple[bool, dict, dict]:
    """Get trading balance."""
    result = run_cmd(
        ["uv", "run", pm_client, "balance"],
        "balance",
    )

    if not result.get("success"):
        return False, result, step_result("balance", False, error=result.get("error", "Balance check failed"))

    balance = result.get("trading_balance", 0)
    if balance <= 0:
        return False, result, step_result(
            "balance", False,
            error=f"Trading balance is ${balance:.2f} — no funds",
        )

    return True, result, step_result("balance", True, {"trading_balance": balance})


def step_predict(btc_hourly: str) -> tuple[bool, dict, dict]:
    """Get Brain prediction + event slug."""
    result = run_cmd(
        ["uv", "run", btc_hourly, "predict"],
        "predict",
        timeout=300,  # Brain API can be slow
    )

    if result.get("success") is False:
        return False, result, step_result("predict", False, error=result.get("error", "Prediction failed"))

    btc_price = result.get("btc_price")
    direction = result.get("direction")
    event_slug = result.get("event_slug")

    if not all([btc_price, direction, event_slug]):
        return False, result, step_result(
            "predict", False,
            error=f"Missing fields: btc_price={btc_price}, direction={direction}, event_slug={event_slug}",
        )

    return True, result, step_result("predict", True, {
        "btc_price": btc_price,
        "direction": direction,
        "confidence": result.get("confidence"),
        "event_slug": event_slug,
    })


def step_find_market(pm_client: str, event_slug: str, event_slug_alt: str = "") -> tuple[bool, dict, dict]:
    """Find the market closest to 0.50 for the event.

    Tries event_slug first, falls back to event_slug_alt if no events found.
    Polymarket flips between with-year and without-year slug formats.
    """
    slugs_to_try = [event_slug]
    if event_slug_alt:
        slugs_to_try.append(event_slug_alt)

    events: list[dict] = []
    used_slug = event_slug
    last_error = ""

    for slug in slugs_to_try:
        result = run_cmd(
            ["uv", "run", pm_client, "events", "--slug", slug],
            "events",
        )
        if result.get("success") and result.get("events"):
            events = result["events"]
            used_slug = slug
            break
        last_error = result.get("error", f"No events for {slug}")

    if not events:
        tried = " and ".join(slugs_to_try)
        return False, {}, step_result("find_market", False, error=f"No events found for {tried}: {last_error}")

    markets = events[0].get("markets", [])
    if not markets:
        return False, {}, step_result("find_market", False, error=f"No markets in event: {used_slug}")

    # Find market with Up price closest to 0.50
    best_market = None
    best_distance = float("inf")

    for market in markets:
        # Look for Up price in tokens or outcomes
        up_price = None
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").lower() == "up":
                up_price = token.get("price")
                break

        if up_price is None:
            # Fallback: yes_price might be Up price
            up_price = market.get("yes_price")

        if up_price is not None:
            distance = abs(up_price - 0.50)
            if distance < best_distance:
                best_distance = distance
                best_market = market
                best_market["_up_price"] = up_price

    if best_market is None:
        return False, {}, step_result("find_market", False, error="Could not determine Up price for any market")

    market_slug = best_market.get("market_slug") or best_market.get("slug", "")
    up_price = best_market["_up_price"]

    data = {
        "market_slug": market_slug,
        "up_price": up_price,
        "question": best_market.get("question", ""),
    }
    return True, data, step_result("find_market", True, data)


def step_duplicate_check(btc_hourly: str, db: str, market_slug: str) -> tuple[bool, dict, dict]:
    """Check if we already traded this market."""
    result = run_cmd(
        ["uv", "run", btc_hourly, "history", "--db", db, "--limit", "100"],
        "history",
    )

    if not isinstance(result, list):
        # history returns a list directly, or may be wrapped
        trades = result if isinstance(result, list) else result.get("trades", result if isinstance(result, list) else [])
        if isinstance(result, dict) and not result.get("success", True) is False:
            trades = result if isinstance(result, list) else []
            # It may just be the list
            if isinstance(result, list):
                trades = result
    else:
        trades = result

    for trade in trades:
        if trade.get("market_slug") == market_slug and trade.get("status") in (
            "pending", "won", "lost", "settled_won", "settled_lost",
        ):
            return False, {}, step_result(
                "duplicate_check", False,
                error=f"Already traded market {market_slug} (trade #{trade.get('id')}, status={trade.get('status')})",
            )

    return True, {}, step_result("duplicate_check", True, {"market_slug": market_slug, "checked": len(trades)})


def step_size(
    btc_hourly: str, db: str, balance: float, max_bet: float, buy_price: float,
    window: int, min_shares: int,
) -> tuple[bool, dict, dict]:
    """Calculate bet size via Kelly."""
    result = run_cmd(
        ["uv", "run", btc_hourly, "size",
         "--db", db,
         "--balance", str(balance),
         "--max-bet", str(max_bet),
         "--buy-price", str(buy_price),
         "--window", str(window)],
        "size",
    )

    bet_amount = result.get("bet_amount", 0)
    mode = result.get("mode", "")

    if mode == "paused" or bet_amount <= 0:
        return False, result, step_result(
            "size", False,
            error=result.get("reason", f"Sizing returned $0 (mode={mode})"),
        )

    # Check minimum shares
    if buy_price > 0:
        shares = bet_amount / buy_price
        if shares < min_shares:
            return False, result, step_result(
                "size", False,
                error=f"Shares {shares:.1f} < minimum {min_shares} (bet=${bet_amount:.2f}, price={buy_price})",
            )

    return True, result, step_result("size", True, {
        "bet_amount": bet_amount,
        "mode": mode,
        "kelly_tier": result.get("kelly_tier") or result.get("mode"),
    })


def step_buy(
    pm_client: str, market_slug: str, outcome: str, price: float,
    amount_usd: float, dry_run: bool,
) -> tuple[bool, dict, dict]:
    """Place a buy order (or skip in dry-run mode)."""
    if dry_run:
        data = {
            "dry_run": True,
            "market_slug": market_slug,
            "outcome": outcome,
            "price": price,
            "amount_usd": amount_usd,
        }
        return True, data, step_result("buy", True, data)

    result = run_cmd(
        ["uv", "run", pm_client, "buy",
         "--market-slug", market_slug,
         "--outcome", outcome,
         "--price", str(price),
         "--amount-usd", str(amount_usd)],
        "buy",
        timeout=180,
    )

    if not result.get("success"):
        return False, result, step_result("buy", False, error=result.get("error", "Order rejected"))

    order_id = result.get("order_id", "")
    data = {
        "order_id": order_id,
        "market_slug": market_slug,
        "outcome": outcome,
        "price": price,
        "amount_usd": amount_usd,
    }
    return True, data, step_result("buy", True, data)


def step_record(
    btc_hourly: str, db: str, btc_price: float, direction: str,
    confidence: float, reasoning: str, market_slug: str, outcome: str,
    shares: float, price: float, order_id: str, dry_run: bool,
) -> tuple[bool, dict, dict]:
    """Record the trade to DB. Best-effort, never bails."""
    if dry_run:
        data = {"dry_run": True, "trade_id": None}
        return True, data, step_result("record", True, data)

    result = run_cmd(
        ["uv", "run", btc_hourly, "record",
         "--db", db,
         "--btc-price", str(btc_price),
         "--direction", direction,
         "--confidence", str(confidence),
         "--reasoning", reasoning or "",
         "--market-slug", market_slug,
         "--outcome", outcome,
         "--shares", str(shares),
         "--price", str(price),
         "--order-id", order_id or ""],
        "record",
    )

    trade_id = result.get("trade_id")
    if trade_id:
        return True, result, step_result("record", True, {"trade_id": trade_id})

    # Best-effort — log error but don't fail the whole run
    return True, result, step_result("record", False, error=result.get("error", "Record failed"))


def step_exchange_duplicate_check(pm_client: str, market_slug: str) -> tuple[bool, dict, dict]:
    """Check if there are already open orders for this market on the exchange."""
    result = run_cmd(
        ["uv", "run", pm_client, "my-orders", "--raw"],
        "my-orders-dup-check",
    )

    if not result.get("success") or not result.get("orders"):
        # Can't check or no open orders — proceed
        return True, {}, step_result("exchange_dup_check", True, {"open_orders": 0})

    for order in result["orders"]:
        order_market = order.get("market_slug") or order.get("market", "")
        if order_market == market_slug:
            order_id = order.get("id") or order.get("order_id", "")
            return False, {}, step_result(
                "exchange_dup_check", False,
                error=f"Open order already exists for {market_slug} (order_id={order_id})",
            )

    return True, {}, step_result("exchange_dup_check", True, {"open_orders": len(result["orders"])})


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_hourly(args) -> dict:
    """Execute the full hourly trading cycle."""
    btc_hourly, pm_client = resolve_paths(args)
    db = args.db
    steps: list[dict] = []

    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "steps": steps,
        "action": "error",
    }

    # Step 1: Cancel stale orders
    can_continue, _, sr = step_cancel_stale(pm_client)
    steps.append(sr)

    # Step 2: Risk check
    can_continue, status_data, sr = step_risk_check(btc_hourly, db, args.window, args.loss_streak_limit)
    steps.append(sr)
    if not can_continue:
        output["action"] = "paused"
        output["reason"] = sr.get("error", "Risk check failed")
        return output

    # Step 3: Balance
    can_continue, balance_data, sr = step_balance(pm_client)
    steps.append(sr)
    if not can_continue:
        output["action"] = "paused"
        output["reason"] = sr.get("error", "Balance check failed")
        return output

    trading_balance = balance_data.get("trading_balance", 0)

    # Step 4: Predict
    can_continue, predict_data, sr = step_predict(btc_hourly)
    steps.append(sr)
    if not can_continue:
        output["action"] = "error"
        output["reason"] = sr.get("error", "Prediction failed")
        return output

    btc_price = predict_data["btc_price"]
    direction = predict_data["direction"]
    confidence = predict_data.get("confidence", 0)
    reasoning = predict_data.get("reasoning", "")
    event_slug = predict_data["event_slug"]
    event_slug_alt = predict_data.get("event_slug_alt", "")

    # Step 5: Find market (tries both slug formats — Polymarket flips between them)
    can_continue, market_data, sr = step_find_market(pm_client, event_slug, event_slug_alt)
    steps.append(sr)
    if not can_continue:
        output["action"] = "skipped"
        output["reason"] = sr.get("error", "No market found")
        return output

    market_slug = market_data["market_slug"]
    up_price = market_data["up_price"]

    # Determine outcome and buy price — fixed at 50¢ limit
    buy_price = 0.50
    if direction == "up":
        outcome = "Up"
    else:
        outcome = "Down"

    # Step 6: Duplicate check (DB + open exchange orders)
    can_continue, _, sr = step_duplicate_check(btc_hourly, db, market_slug)
    steps.append(sr)
    if not can_continue:
        output["action"] = "skipped"
        output["reason"] = sr.get("error", "Duplicate trade")
        return output

    # Step 6b: Exchange-level duplicate check — catch any open orders for this market
    can_continue, _, sr = step_exchange_duplicate_check(pm_client, market_slug)
    steps.append(sr)
    if not can_continue:
        output["action"] = "skipped"
        output["reason"] = sr.get("error", "Duplicate open order")
        return output

    # Step 7: Size
    can_continue, size_data, sr = step_size(
        btc_hourly, db, trading_balance, args.max_bet, buy_price, args.window, args.min_shares,
    )
    steps.append(sr)
    if not can_continue:
        output["action"] = "paused"
        output["reason"] = sr.get("error", "Sizing returned 0")
        return output

    bet_amount = size_data.get("bet_amount", 0)
    shares = round(bet_amount / buy_price, 2) if buy_price > 0 else 0

    # Step 8: Buy
    can_continue, buy_data, sr = step_buy(
        pm_client, market_slug, outcome, buy_price, bet_amount, args.dry_run,
    )
    steps.append(sr)
    if not can_continue:
        output["action"] = "error"
        output["reason"] = sr.get("error", "Order rejected")
        return output

    order_id = buy_data.get("order_id", "")

    # Step 9: Record
    _, record_data, sr = step_record(
        btc_hourly, db, btc_price, direction, confidence, reasoning,
        market_slug, outcome, shares, buy_price, order_id, args.dry_run,
    )
    steps.append(sr)

    output["action"] = "traded"
    output["trade"] = {
        "btc_price": btc_price,
        "direction": direction,
        "confidence": confidence,
        "event_slug": event_slug,
        "market_slug": market_slug,
        "outcome": outcome,
        "buy_price": buy_price,
        "bet_amount": bet_amount,
        "shares": shares,
        "order_id": order_id,
        "trade_id": record_data.get("trade_id"),
        "kelly_tier": size_data.get("kelly_tier"),
    }

    return output


def main():
    parser = argparse.ArgumentParser(description="Brain Hourly BTC — deterministic trading cycle")
    parser.add_argument("--max-bet", type=float, required=True, help="Maximum bet per trade in USD")
    parser.add_argument("--db", default="/data/state/trades.db", help="Path to trade database")
    parser.add_argument("--pm-client", default="", help="Path to pm_client.py (default: $SKILL_DIR/scripts/pm_client.py)")
    parser.add_argument("--dry-run", action="store_true", help="Run all steps except actual buy")
    parser.add_argument("--window", type=int, default=50, help="Rolling window size for Kelly")
    parser.add_argument("--min-shares", type=int, default=5, help="Minimum shares to place a trade")
    parser.add_argument("--loss-streak-limit", type=int, default=5, help="Consecutive losses before pause")
    args = parser.parse_args()

    # Acquire exclusive file lock to prevent concurrent runs (duplicate trades).
    # Lock file sits next to the DB so it shares the same persistent volume.
    lock_path = Path(args.db).parent / "run_hourly.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "steps": [],
            "action": "skipped",
            "reason": "Another run_hourly instance is already running (lock held)",
        }
        print(json.dumps(result, indent=2))
        lock_fd.close()
        sys.exit(0)

    try:
        result = run_hourly(args)
    except Exception as e:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "steps": [],
            "action": "error",
            "reason": str(e),
        }
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    print(json.dumps(result, indent=2))

    # Always exit 0 — action field indicates outcome
    sys.exit(0)


if __name__ == "__main__":
    main()
