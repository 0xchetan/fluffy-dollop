# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Deterministic redeem/reconciliation cycle for Brain Hourly BTC.

Reconciles trade DB with exchange positions, redeems resolved winners,
marks losers, and detects orphan positions.

Steps:
  1. Get pending trades from DB
  2. Get current positions from exchange
  3. For each pending trade: resolve market, match position, update DB
  4. Detect orphan positions (exchange positions not in DB)

Zero external dependencies — stdlib only.
Calls btc_hourly.py and pm_client.py as subprocesses via `uv run`.

Usage:
    uv run run_redeem.py
    uv run run_redeem.py --dry-run
    uv run run_redeem.py --db /data/state/trades.db --max-age-hours 48
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
        # history command returns a bare list
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, list):
                return {"success": True, "trades": parsed}
        except json.JSONDecodeError:
            pass
        return {"success": False, "error": f"{label}: invalid JSON", "raw": stdout[:500]}


def resolve_paths(args) -> tuple[str, str]:
    """Resolve btc_hourly.py and pm_client.py paths."""
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


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp string, return None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def alt_slug(slug: str) -> str:
    """Flip a slug between with-year and without-year format.

    Polymarket hourly BTC events alternate between:
      bitcoin-up-or-down-april-3-8am-et          (no year)
      bitcoin-up-or-down-april-3-2026-8am-et     (with year)
    """
    # With year → without: strip the 4-digit year segment
    m = re.match(r"^(bitcoin-up-or-down-\w+-\d+)-(\d{4})-(\d+\w+-et)$", slug)
    if m:
        return f"{m.group(1)}-{m.group(3)}"
    # Without year → with: insert current year
    m = re.match(r"^(bitcoin-up-or-down-\w+-\d+)-(\d+\w+-et)$", slug)
    if m:
        year = datetime.now(timezone.utc).year
        return f"{m.group(1)}-{year}-{m.group(2)}"
    return ""


def slug_to_fingerprint(slug: str) -> tuple[str, str, str] | None:
    """Extract (month, day, time) from a market slug for fuzzy matching.

    Handles both formats:
      bitcoin-up-or-down-april-3-10pm-et        → (april, 3, 10pm)
      bitcoin-up-or-down-april-3-2026-10pm-et   → (april, 3, 10pm)
    """
    m = re.match(
        r"bitcoin-up-or-down-(\w+)-(\d+)(?:-\d{4})?-(\d{1,2}(?:am|pm))-et$",
        slug, re.IGNORECASE,
    )
    if m:
        return m.group(1).lower(), m.group(2), m.group(3).lower()
    return None


def match_trade_to_position(
    trade: dict, positions: list[dict],
) -> dict | None:
    """Match a pending trade to a position by title when resolve fails.

    Position titles look like:
      "Bitcoin: Up or Down? - April 3, 10PM ET"
      "Will the price of Bitcoin go up or down? April 3 10PM ET"
    We extract month/day/time from the trade slug and match against the
    title, plus compare outcome (Up/Down).
    """
    slug = trade.get("market_slug", "")
    outcome = (trade.get("outcome_bought") or "").lower()
    fp = slug_to_fingerprint(slug)
    if not fp:
        return None

    month, day, time_str = fp  # e.g. ("april", "3", "10pm")

    for pos in positions:
        title = (pos.get("title") or "").lower()
        pos_outcome = (pos.get("outcome") or "").lower()

        # Must match outcome
        if outcome and pos_outcome and outcome != pos_outcome:
            continue

        # Title must contain month, day, and time
        if month not in title:
            continue
        # Day check: look for the number as a standalone token (avoid "13" matching "3")
        if not re.search(rf"\b{day}\b", title):
            continue
        if time_str not in title:
            continue

        return pos

    return None


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def run_redeem(args) -> dict:
    """Execute the full redeem/reconciliation cycle."""
    btc_hourly, pm_client = resolve_paths(args)
    db = args.db
    now = datetime.now(timezone.utc)

    output: dict = {
        "timestamp": now.isoformat(),
        "dry_run": args.dry_run,
        "pending_total": 0,
        "pending_active": 0,
        "resolved_count": 0,
        "resolutions": [],
        "orphans": [],
        "errors": [],
    }

    # Step 1: Get pending trades from DB
    history_result = run_cmd(
        ["uv", "run", btc_hourly, "history", "--db", db, "--limit", "200"],
        "history",
    )

    # Parse trades — history returns a list or {"trades": [...]}
    all_trades: list[dict] = []
    if isinstance(history_result, list):
        all_trades = history_result
    elif isinstance(history_result, dict):
        if isinstance(history_result.get("trades"), list):
            all_trades = history_result["trades"]
        elif history_result.get("success") is False:
            output["errors"].append({"step": "get_history", "error": history_result.get("error", "Failed to get history")})
            return output

    pending_trades = []
    for trade in all_trades:
        if trade.get("status") != "pending":
            continue
        ts = parse_timestamp(trade.get("timestamp"))
        if ts:
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours > args.max_age_hours:
                continue  # Too old, skip
        pending_trades.append(trade)

    output["pending_total"] = len(pending_trades)

    if not pending_trades:
        return output

    # Step 2: Get current positions from exchange
    positions_result = run_cmd(
        ["uv", "run", pm_client, "positions"],
        "positions",
    )

    positions: list[dict] = []
    if positions_result.get("success"):
        positions = positions_result.get("positions", [])

    # Build condition_id → position lookup
    position_by_condition: dict[str, dict] = {}
    for pos in positions:
        cid = pos.get("condition_id", "")
        if cid:
            position_by_condition[cid] = pos

    # Track which condition_ids we matched from DB
    matched_condition_ids: set[str] = set()

    # Step 3: For each pending trade, resolve and reconcile
    output["pending_active"] = len(pending_trades)

    for trade in pending_trades:
        trade_id = trade.get("id")
        market_slug = trade.get("market_slug", "")

        if not market_slug:
            output["errors"].append({
                "trade_id": trade_id,
                "error": "No market_slug in trade record",
            })
            continue

        # --- Strategy A: resolve slug → condition_id → match position ---
        condition_id = ""
        end_date: datetime | None = None
        position: dict | None = None
        match_method = ""

        # Try stored slug, then alt format (year ↔ no-year)
        for slug in [market_slug, alt_slug(market_slug)]:
            if not slug:
                continue
            resolve_result = run_cmd(
                ["uv", "run", pm_client, "resolve", "--market-slug", slug],
                f"resolve-{slug}",
            )
            if resolve_result.get("success") and resolve_result.get("resolved"):
                market_info = resolve_result.get("market", {})
                condition_id = market_info.get("condition_id", "")
                end_date = parse_timestamp(market_info.get("end_date", ""))
                break

        if condition_id:
            position = position_by_condition.get(condition_id)
            if position:
                match_method = "resolve"
                matched_condition_ids.add(condition_id)

        # --- Strategy B: direct title matching against positions ---
        if not position:
            position = match_trade_to_position(trade, positions)
            if position:
                condition_id = position.get("condition_id", "")
                match_method = "title_match"
                matched_condition_ids.add(condition_id)

        # --- Evaluate position ---
        if position:
            current_price = position.get("current_price", 0.5)
            resolved = position.get("resolved", False)
            redeemable = position.get("redeemable", False)

            # Determine outcome from current_price — don't require
            # resolved/redeemable flags since Polymarket can lag on these.
            if current_price >= 0.99:
                won = True
            elif current_price <= 0.01:
                won = False
            else:
                # Still mid-range, genuinely unresolved
                continue

            buy_price = trade.get("price", 0.50)
            shares = position.get("size", trade.get("shares", 0))

            if won:
                pnl = round(shares * (1.0 - buy_price), 2)
            else:
                pnl = round(-(shares * buy_price), 2)

            resolution: dict = {
                "trade_id": trade_id,
                "market_slug": market_slug,
                "condition_id": condition_id,
                "match_method": match_method,
                "result": "won" if won else "lost",
                "pnl": pnl,
                "shares": shares,
                "current_price": current_price,
                "redeemable": redeemable,
            }

            # Attempt on-chain redeem for winners — don't trust the
            # redeemable flag, just try it and let the contract decide.
            # Polymarket lags on flipping redeemable for hourly markets.
            if won and condition_id:
                if args.dry_run:
                    resolution["redeem"] = {"dry_run": True, "redeemable_flag": redeemable}
                else:
                    redeem_result = run_cmd(
                        ["uv", "run", pm_client, "redeem", "--condition-id", condition_id],
                        f"redeem-{condition_id}",
                    )
                    resolution["redeem"] = {
                        "success": redeem_result.get("success", False),
                        "attempted": True,
                        "redeemable_flag": redeemable,
                        "error": redeem_result.get("error") if not redeem_result.get("success") else None,
                    }

            # Update DB regardless — accounting shouldn't wait for on-chain redemption
            if args.dry_run:
                resolution["db_update"] = {"dry_run": True}
            else:
                won_flag = "--won" if won else "--lost"
                update_result = run_cmd(
                    ["uv", "run", btc_hourly, "update",
                     "--db", db,
                     "--trade-id", str(trade_id),
                     won_flag,
                     "--pnl", str(pnl)],
                    f"update-{trade_id}",
                )
                resolution["db_update"] = {"success": update_result.get("success", True)}

            output["resolutions"].append(resolution)
            output["resolved_count"] += 1

        elif condition_id and end_date and (now - end_date).total_seconds() > 7200:
            # Resolved via API but no position — market ended >2hrs ago,
            # likely unfilled or already redeemed to 0
            buy_price = trade.get("price", 0.50)
            shares = trade.get("shares", 0)
            pnl = round(-(shares * buy_price), 2)

            resolution = {
                "trade_id": trade_id,
                "market_slug": market_slug,
                "condition_id": condition_id,
                "match_method": "no_position",
                "result": "lost",
                "pnl": pnl,
                "reason": "No position found, market ended >2hrs ago",
            }

            if args.dry_run:
                resolution["db_update"] = {"dry_run": True}
            else:
                update_result = run_cmd(
                    ["uv", "run", btc_hourly, "update",
                     "--db", db,
                     "--trade-id", str(trade_id),
                     "--lost",
                     "--pnl", str(pnl),
                     "--notes", "No position found after market end"],
                    f"update-{trade_id}",
                )
                resolution["db_update"] = {"success": update_result.get("success", True)}

            output["resolutions"].append(resolution)
            output["resolved_count"] += 1

        elif not position and not condition_id:
            output["errors"].append({
                "trade_id": trade_id,
                "market_slug": market_slug,
                "error": "Could not resolve market or match to any position",
            })

    # Step 4: Orphan detection
    for pos in positions:
        title = (pos.get("title") or "").lower()
        cid = pos.get("condition_id", "")
        if "bitcoin" in title and cid and cid not in matched_condition_ids:
            output["orphans"].append({
                "condition_id": cid,
                "title": pos.get("title"),
                "outcome": pos.get("outcome"),
                "size": pos.get("size"),
                "current_price": pos.get("current_price"),
            })

    return output


def main():
    parser = argparse.ArgumentParser(description="Brain Hourly BTC — redeem/reconciliation cycle")
    parser.add_argument("--db", default="/data/state/trades.db", help="Path to trade database")
    parser.add_argument("--pm-client", default="", help="Path to pm_client.py (default: $SKILL_DIR/scripts/pm_client.py)")
    parser.add_argument("--max-age-hours", type=float, default=48, help="Max age of pending trades to process")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without redeeming or updating")
    args = parser.parse_args()

    try:
        result = run_redeem(args)
    except Exception as e:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": args.dry_run,
            "pending_total": 0,
            "pending_active": 0,
            "resolved_count": 0,
            "resolutions": [],
            "orphans": [],
            "errors": [{"step": "main", "error": str(e)}],
        }

    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
