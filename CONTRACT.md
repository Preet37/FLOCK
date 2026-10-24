# CONTRACT.md

Everyone builds to this. If you need to change it, tell the team first.

## Folders

```
data/raw/<source>/   untouched files (pos, doordash, ubereats, grubhub, direct_app, bank, contracts, emails)
data/std/            standardized tables (CSV)
data/answer_key.csv  planted traps + correct outcome
data/state/          findings.json + audit_log.jsonl (written by the store)
```

## Standard order row (register / direct app)
`order_id, store_id, channel, placed_at (ISO), subtotal, tax, promo_code, source_file, source_row`

## Standard payout line (one per order per platform)
`payout_line_id, channel, store_id, platform_order_id, order_id (if known), gross, commission, promo_merchant_funded, error_charge, tax_withheld_by_platform, other_fees, net, payout_id, payout_date, source_file, source_row`

## Bank deposit
`deposit_id, channel, amount, deposit_date, memo, source_file, source_row`

## Deal term (output of Deal Keeper)
`term_id, channel, store_id (or "all"), term_type (commission|promo_split|fee|tax_rule), value, effective_from, effective_to, authority (contract|amendment|auth_email|unauth_email|call_note), quote, source_file`

Authority ranking, strongest first: contract, amendment, auth_email, unauth_email, call_note.
Stronger wins automatically. Equal strength or unknown authority = conflict.

## Finding (one per discrepancy)
`finding_id, order_ids[], channel, check_type (commission|promo|tax|error_charge|deposit|missing_order), expected, actual, gap, evidence[] (source_file + row or quote), outcome (correct|recoverable|conflict), confidence, reason (plain English), votes[], rank_score, dispute_draft, state`

## States (the only allowed moves)

| From | Action | To | Who may do it |
| --- | --- | --- | --- |
| NEW | check | CHECKED | agent |
| CHECKED | judge | JUDGED | agent |
| JUDGED | approve | APPROVED | human |
| JUDGED | reject | REJECTED | human |
| APPROVED | file | FILED | gatekeeper |
| FILED | recover | RECOVERED | human or gatekeeper |

## Log entry (append-only, never edited)
`at, actor, role, action, subject_id, allowed (true/false), before, after, reason, evidence[]`

## The one rule
Any change to a finding goes through `flock.gatekeeper.transition()`. No agent edits a finding directly. Denied attempts are logged too.

## AI vs code
AI only for messy formats, reading documents, and judgment. All money math is plain Python.
