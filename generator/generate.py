"""Data generator (owner: Maja). Builds a fake franchise group's week with planted traps and an answer key.

  python generator/generate.py --seed 42 --stores 12 --weeks 2 --out generator/out

Approach (see FLOCK_CONTEXT.md section 13):
  1. Build the TRUE world first: stores, channels, contracts (commission tier, promo split, fees, dates),
     one amendment, 3 rep emails, and every order with what Dave's SHOULD be paid.
  2. Render each source in its own messy format:
       pos_orders.csv            cleanest, but missing app-side refunds
       doordash_payouts.csv      own column names; error charges as separate lines
       ubereats_statement.csv    "$12.50" as text, different date format
       grubhub_statement.xlsx    summary sheet + detail sheet
       direct_app_orders.csv + refund_log.csv
       bank_deposits.csv         weekly lumps per platform, no order detail
       contracts/*.json|pdf, emails/*.txt
  3. Plant the 17 traps and write data/answer_key.csv:
       order_id, trap_type, expected_outcome (correct|recoverable|conflict), amount
  4. Different --seed = a fresh week. Generate one live in front of judges.

Use Claude only for text (rep emails, contract wording). Never let an LLM write the numbers.

FIRST MILESTONE: 1 store x 1 day with 3 traps, so Thabhelo can start matching against real files.
"""
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stores", type=int, default=12)
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--out", default="generator/out")
    ap.parse_args()
    raise SystemExit("TODO (Maja): generator not built yet. Pipeline uses fixtures/fake_findings.json meanwhile.")


if __name__ == "__main__":
    main()
