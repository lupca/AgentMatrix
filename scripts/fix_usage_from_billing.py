#!/usr/bin/env python3
"""Fix llm_usage records using actual billing data from SiliconFlow."""

import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from app.db.base import SessionLocal
from app.db.models import LLMUsage


def parse_billing_file(filepath: str) -> list[dict]:
    """Parse SiliconFlow billing file into API call records."""
    with open(filepath, "r") as f:
        content = f.read().replace("\r", "")

    # Pattern to extract transaction data
    # Each API call has 3 billing entries: cached-input, input, output
    # They share the same transaction ID prefix (before the _0, _1, _2 suffix)

    calls = {}

    # Find all lines with token data
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for transaction lines
        if "B20260727" in line:
            # Extract transaction ID
            match = re.search(r"B\d+_(\d)", line)
            if match:
                suffix = match.group(1)
                tx_base = re.search(r"(B\d+)", line).group(1)

                # Extract timestamp (format: HH:MM:00)
                time_match = re.search(r"(\d{2}:\d{2}:\d{2})", line)
                if time_match:
                    usage_time = time_match.group(1)

                # Extract token count and cost
                # Token count is the first decimal number after the type
                numbers = re.findall(r"(\d+\.?\d*)", line)

                if tx_base not in calls:
                    calls[tx_base] = {
                        "time": usage_time,
                        "cached_tokens": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_cost": Decimal("0"),
                        "input_cost": Decimal("0"),
                        "output_cost": Decimal("0"),
                    }

                # Determine type from previous line or current line
                prev_line = lines[i-1] if i > 0 else ""
                combined = prev_line + line

                # Find the token amount (first reasonable decimal in the data area)
                # The format is: "tokens <amount> 0 <amount> tokens <price> <subtotal>"
                tokens_match = re.search(r"tokens?\s+(\d+\.?\d*)\s+0\s+(\d+\.?\d*)", line)
                cost_match = re.search(r"(\d+\.\d{6})\s+0\s+0\s*(\d+\.\d+)$", line)

                if tokens_match:
                    tokens = float(tokens_match.group(1)) * 1000  # K tokens to tokens

                    if "cached-" in combined or "cached-input" in combined:
                        calls[tx_base]["cached_tokens"] = int(tokens)
                        if cost_match:
                            calls[tx_base]["cached_cost"] = Decimal(cost_match.group(2))
                    elif "output-" in combined:
                        calls[tx_base]["output_tokens"] = int(tokens)
                        if cost_match:
                            calls[tx_base]["output_cost"] = Decimal(cost_match.group(2))
                    elif "input-" in combined and "cached" not in combined:
                        calls[tx_base]["input_tokens"] = int(tokens)
                        if cost_match:
                            calls[tx_base]["input_cost"] = Decimal(cost_match.group(2))
        i += 1

    # Convert to list and add total input
    result = []
    for tx_id, data in calls.items():
        # Total input = uncached input + cached input
        data["total_input_tokens"] = data["input_tokens"] + data["cached_tokens"]
        data["total_cost"] = data["cached_cost"] + data["input_cost"] + data["output_cost"]
        data["tx_id"] = tx_id
        result.append(data)

    # Sort by time
    result.sort(key=lambda x: x["time"])
    return result


def parse_billing_simple(filepath: str) -> list[dict]:
    """Simpler parsing - group by transaction ID pattern."""
    with open(filepath, "r") as f:
        content = f.read().replace("\r", "")

    calls = {}

    # Find all cost entries (they end with the cost amount)
    pattern = re.compile(
        r"(\d{2}:\d{2}:\d{2})(B\d+_\d)\s+(\d{2}:\d{2}:\d{2})(\S+)\s+"
        r"(\d+\.?\d*)\s+\d+\s+(\d+\.?\d*)\s*tokens\s*"
        r"(\d+\.?\d*)\s*(\d+\.?\d*)\s+\d+\s+\d+\s*(\d+\.?\d*)"
    )

    lines = content.split("\n")
    for i, line in enumerate(lines):
        # Check previous line for token type
        prev_line = lines[i-1] if i > 0 else ""

        # Extract numbers from line
        if re.search(r"tokens\s+\d+\.\d+\s+0", line):
            # Get transaction base ID
            tx_match = re.search(r"B(\d{14})_(\d)", line)
            if tx_match:
                tx_base = f"B{tx_match.group(1)}"
                suffix = tx_match.group(2)

                # Get time
                time_match = re.search(r"(\d{2}:\d{2}:\d{2})", line)
                time_str = time_match.group(1) if time_match else "00:00:00"

                # Get token count (in K)
                tokens_match = re.search(r"(\d+\.?\d*)\s+0\s+(\d+\.?\d*)\s*tokens", line)
                if tokens_match:
                    ktokens = float(tokens_match.group(1))
                    tokens = int(ktokens * 1000)

                    # Get cost (last number before final 0s)
                    cost_match = re.search(r"(\d+\.\d+)\s*$", line.strip())
                    cost = Decimal(cost_match.group(1)) if cost_match else Decimal("0")

                    if tx_base not in calls:
                        calls[tx_base] = {
                            "time": time_str,
                            "cached_tokens": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_cost": Decimal("0"),
                        }

                    combined = prev_line + line
                    if "cached-" in combined:
                        calls[tx_base]["cached_tokens"] = tokens
                    elif "output-" in combined:
                        calls[tx_base]["output_tokens"] = tokens
                    elif "input-" in combined:
                        calls[tx_base]["input_tokens"] = tokens

                    calls[tx_base]["total_cost"] += cost

    result = []
    for tx_id, data in calls.items():
        data["total_input_tokens"] = data["input_tokens"] + data["cached_tokens"]
        data["tx_id"] = tx_id
        result.append(data)

    result.sort(key=lambda x: x["time"])
    return result


def main():
    billing_file = "/home/lupca/Downloads/bills_d8dhuo90hh4s73edje4g_bet_cNuTZFesTtun_20260727170000_20260728165959.txt"

    print("Parsing billing file...")
    billing_calls = parse_billing_simple(billing_file)
    print(f"Found {len(billing_calls)} API calls in billing")

    # Calculate totals from billing
    total_cached = sum(c["cached_tokens"] for c in billing_calls)
    total_input = sum(c["input_tokens"] for c in billing_calls)
    total_output = sum(c["output_tokens"] for c in billing_calls)
    total_cost = sum(c["total_cost"] for c in billing_calls)

    print(f"\nBilling totals:")
    print(f"  Cached: {total_cached:,}")
    print(f"  Input (uncached): {total_input:,}")
    print(f"  Output: {total_output:,}")
    print(f"  Total input: {total_cached + total_input:,}")
    print(f"  Total cost: ${total_cost:.6f}")

    # Get DB records
    db = SessionLocal()
    try:
        records = db.query(LLMUsage).filter(
            LLMUsage.created_at >= "2026-07-27 17:00:00",
            LLMUsage.created_at < "2026-07-28 00:00:00",
            LLMUsage.model == "zai-org/GLM-5.2"
        ).order_by(LLMUsage.created_at).all()

        print(f"\nDB records: {len(records)}")
        db_cached = sum(r.cached_tokens or 0 for r in records)
        db_input = sum(r.input_tokens or 0 for r in records)
        db_output = sum(r.output_tokens or 0 for r in records)
        db_cost = sum(r.cost_usd or 0 for r in records)

        print(f"DB totals:")
        print(f"  Cached: {db_cached:,}")
        print(f"  Input: {db_input:,}")
        print(f"  Output: {db_output:,}")
        print(f"  Cost: ${float(db_cost):.6f}")

        # Strategy: We have fewer DB records than billing calls
        # This is because context compaction wasn't tracked
        # We'll scale the existing records proportionally

        print("\n--- Fixing DB records ---")

        # Calculate scaling factors
        if db_cached > 0:
            cached_scale = total_cached / db_cached
        else:
            cached_scale = 1

        if db_output > 0:
            output_scale = total_output / db_output
        else:
            output_scale = 1

        print(f"Cached scale: {cached_scale:.2f}x")
        print(f"Output scale: {output_scale:.2f}x")

        # Instead of scaling, let's just update the totals to match billing
        # We'll distribute the difference across records proportionally

        missing_cached = total_cached - db_cached
        missing_output = total_output - db_output
        missing_cost = float(total_cost) - float(db_cost)

        print(f"\nMissing from DB:")
        print(f"  Cached: {missing_cached:,}")
        print(f"  Output: {missing_output:,}")
        print(f"  Cost: ${missing_cost:.6f}")

        if input("Apply fixes? (y/n): ").lower() != "y":
            print("Aborted.")
            return

        # Pricing for recalculation
        input_price = Decimal("1.302")  # per MTok
        output_price = Decimal("4.092")  # per MTok
        cached_price = Decimal("0.26")   # per MTok

        # Update each record proportionally
        for record in records:
            old_cached = record.cached_tokens or 0
            old_output = record.output_tokens or 0

            # Add proportional share of missing tokens
            if db_cached > 0 and old_cached > 0:
                add_cached = int(missing_cached * (old_cached / db_cached))
                record.cached_tokens = old_cached + add_cached

            if db_output > 0 and old_output > 0:
                add_output = int(missing_output * (old_output / db_output))
                record.output_tokens = old_output + add_output

            # Recalculate cost
            uncached_input = record.input_tokens - record.cached_tokens
            if uncached_input < 0:
                uncached_input = 0
                record.cached_tokens = record.input_tokens

            cost = (
                Decimal(uncached_input) * input_price
                + Decimal(record.cached_tokens) * cached_price
                + Decimal(record.output_tokens) * output_price
            ) / Decimal(1_000_000)
            record.cost_usd = cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

        db.commit()
        print("Records updated!")

        # Verify
        new_cached = sum(r.cached_tokens or 0 for r in records)
        new_output = sum(r.output_tokens or 0 for r in records)
        new_cost = sum(r.cost_usd or 0 for r in records)

        print(f"\nNew DB totals:")
        print(f"  Cached: {new_cached:,} (target: {total_cached:,})")
        print(f"  Output: {new_output:,} (target: {total_output:,})")
        print(f"  Cost: ${float(new_cost):.6f} (target: ${float(total_cost):.6f})")

    finally:
        db.close()


if __name__ == "__main__":
    main()
