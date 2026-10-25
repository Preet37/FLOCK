# Flock API (for the frontend)

The deployed API is `flock/api.py` (stdlib `http.server`, no framework). Live at
**https://web-production-812b4.up.railway.app** (Railway, `Procfile`: `python -m flock.api --host 0.0.0.0`,
`FLOCK_OFFLINE=1`, a volume at `/app/data/state`). Run it locally with `python -m flock.api` (port 8787).

All data is **simulated** (a fictional 56-store bagel chain from `generator/generate.py`, or the hand-made fixtures).
Every example below is a real response captured from a local run (`POST /api/run {"dataset": "demo", "seed": 2025,
"profile": "ca_bagel_chain", "stores": 12, "weeks": 2}`), trimmed where marked `...`.

## Basics

| | |
| --- | --- |
| Base URL | `https://web-production-812b4.up.railway.app` (local: `http://127.0.0.1:8787`) |
| Auth | Reads (GET) are open. Every POST needs `Authorization: Bearer <FLOCK_API_TOKEN>`; without it: `401`. |
| CORS | `Access-Control-Allow-Origin: *` (or `FLOCK_CORS_ORIGIN`), headers `Content-Type, Authorization`, methods `GET, POST, OPTIONS`. Preflight `OPTIONS` answers `204` with no body (`Content-Length: 0`). |
| Errors | Always JSON: `{"error": "..."}`. `400` bad input, `401` no/wrong token, `404` not found, `409` a run is in progress or the state machine refused the move, `500` a bug (the message says what broke). Never an HTML traceback. Bodies are strict JSON: a `NaN` or infinity is sent as `null` (`JSON.parse` rejects `NaN`), on this server and on the dashboard's mirror. |
| Runs | One at a time. A second `POST /api/run`, or a decision while a run is going, gets `409` with `"running": "<job id>"`. |
| Money | Dollars, rounded to cents. Gaps are computed in plain Python by the checkers; no model touches a dollar. |
| Logs | The server writes one stderr line per request: `api  GET /api/metrics  200  9ms`. A run's own output goes to its job (`output_tail`), never to the terminal or to another request: stdout is a per-thread proxy installed once at startup, so concurrent requests and the background job each capture only their own prints. |

## Which endpoint feeds which panel

| Panel (Himaja's layout) | Endpoint(s) | Poll |
| --- | --- | --- |
| (1) Live audit log column | `GET /api/log` (newest last), `GET /api/progress` while a run is going | 2-5 s |
| (2) Discrepancies list with risk; click -> expected vs actual + resolve | `GET /api/findings?sort=risk`, `GET /api/findings/<id>`, `POST /api/findings/<id>/approve` or `/reject` (lands in the audit log) | 5 s |
| (3) Transactions / invoices table | `GET /api/transactions?channel=&store=&status=&limit=&offset=` | on page change |
| (4) Agent chain across the top; click -> activity / live screen | `GET /api/agents` (the chain, in pipeline order), `GET /api/agents/<agent_id>`, `GET /api/screens/<file>` (Clicky only) | 5 s |
| (5) Bottom metrics row | `GET /api/metrics` (four cards) | after each run |
| Run button / status | `POST /api/run?wait=0` (fixtures) or `POST /api/run` with `dataset: "demo"` (always a job), then poll `GET /api/jobs/<id>`; `GET /api/health` | 1-2 s while running |
| (6) Lump sums: list of deposits; click -> waterfall, stores, findings inside | `GET /api/payouts`, `GET /api/payouts/<payout_id>` | after each run / decision |
| (7) Agent theater: judge columns, balance ticker, human lane, rule banner | `GET /api/stream?since=<cursor>` (live) or `GET /api/stream/replay?run=last&speed=N` (stage) | 1 s live; replay: `next_in_ms` |

## Reads

### `GET /api/health`
Railway health check. Cheap; safe to poll.
```json
{
  "status": "ok",
  "version": "1906bfa891fa",
  "store": "JsonStore",
  "state_dir": "state",
  "at": "2026-10-25T05:24:58+00:00",
  "auth_required_for_post": true,
  "last_run": {"run_id": "RUN-90b38f45", "label": "live demo: seed 2025, ca_bagel_chain, 12 stores x 14 days",
               "source": "generated: ca_bagel_chain seed 2025, 12 stores x 2 weeks", "finished_at": "2026-10-25T05:24:42+00:00", "seconds": 2.293},
  "counts": {"findings": 73, "needs_human": 4, "log_entries": 350, "transactions": 97},
  "running_job": null
}
```
`version` is `RAILWAY_GIT_COMMIT_SHA` (or `git rev-parse`), `null` if neither is available. `last_run.source` names the data: `fixtures/fake_findings.json`, `generated: <profile> seed <n>, <stores> stores x <weeks> weeks` for a dataset run, or `real data (<std folder>)`.

### `GET /api/findings`
Every finding, each with a **risk score**. Query: `state`, `outcome`, `channel`, `check_type`, `risk` (`low|med|high`),
`sort=risk` (highest first, ties by `finding_id`). The default order is the work queue: findings with
`needs_human` first, then by state (`JUDGED`, `APPROVED`, `FILED`, `CHECKED`, `NEW`, `RECOVERED`, `REJECTED`), then
risk (highest first), then `finding_id`, so the same data always comes back in the same order. `needs_human` = waiting
on a person (a conflict, or a drafted dispute ready to approve): show the resolve buttons when it is true.
```json
[
  {
    "finding_id": "F-8b28ee81",
    "order_ids": ["ORD-11618", "b155-4ff0-497a"],
    "channel": "ubereats",
    "check_type": "missing_order",
    "expected": 0.0,
    "actual": 6.02,
    "gap": 6.02,
    "evidence": [{"source_file": "ubereats/ubereats_statement_2026-02-09.csv", "row": 394,
                  "what": "payout line ubereats:b155-4ff0-497a gross 26.35, net 20.33"}],
    "outcome": "conflict",
    "confidence": 0.7,
    "reason": "Judges disagree or aren't confident enough; sending to a human. judge.strict: conflict (80%) | ...",
    "votes": [{"judge": "judge.strict", "outcome": "conflict", "confidence": 0.8, "weight": 0.3486,
               "reason": "No contract, amendment or second source proves this gap."}, "..."],
    "attention": {"value": 6.02, "rank": 1, "budget": 10, "status": "queued"},
    "state": "REJECTED",
    "risk": 49,
    "risk_label": "med",
    "risk_factors": {"dollars": 0.422, "uncertainty": 0.3, "authority": 1.0, "attention": 0.241},
    "needs_human": false
  }
]
```
Expected vs actual for the detail pane: `expected`, `actual`, `gap` (+ `evidence` rows, `reason`, `votes`,
`dispute_draft`, `case_file` when present).

#### Risk score (on every finding)
Defined in `flock/frontend.py` (`risk`, `RISK_FORMULA`); a pure function of the finding, computed on read.

```
risk = round(45*D + 25*U + 20*A + 10*V)      always 0..100
D  dollars      = ln(1 + |gap|) / ln(1 + 100), capped at 1          ($100+ maxes it)
U  uncertainty  = 1 - panel confidence (0.5 before the panel votes)
A  authority    = 1 if the panel sent it to a human (conflict);
                  0.5 if any evidence comes from a source without authority (unauth_email, call_note); else 0
V  attention    = attention value / $25, capped at 1; attention value = |gap| x (1 - balance-weighted panel
                  confidence) (flock/market.py), the same number the attention budget ranks conflicts by
label: low < 34 <= med < 67 <= high
```
`risk_factors` returns D, U, A, V so the UI can explain the number.

### `GET /api/findings/<id>`
```json
{
  "finding": {"finding_id": "F-8b28ee81", "...": "same fields as the list, incl. risk", "needs_human": false},
  "log": [
    {"at": "2026-10-25T05:24:40+00:00", "actor": "checkers", "role": "agent", "action": "check",
     "subject_id": "F-8b28ee81", "subject_type": "finding", "allowed": true, "before": "NEW", "after": "CHECKED",
     "diff": {"state": ["NEW", "CHECKED"]}, "reason": "Checker computed expected vs actual in code",
     "evidence": ["..."]}
  ],
  "risk_formula": "risk = 45*D + 25*U + 20*A + 10*V, rounded, 0-100. ..."
}
```

### `GET /api/transactions`
One row per payout per store (platform x store x payout_id), plus one row per **sold-never-paid** order (a register
order that is in no payout at all). Built once per real-data run into `data/state/transactions.json`
(`flock/transactions.py`), so a request only filters and pages a file.

Query: `channel` (`doordash|ubereats|grubhub|direct_app`), `store` (`S01`...), `status` (matches any of the row's
`flags`), `limit` (default 50, max 500), `offset` (default 0). Sorted newest payout first.

`status` is the most important flag, decided at read time from the findings' **current** state:
`sold_never_paid > missing_deposit > short / over > disputed > needs_review > recovered > matched`.

| status | means |
| --- | --- |
| `matched` | the store's share of the bank deposit equals its statement net within $0.05, and no open finding |
| `short` / `over` | the deposit is less / more than the statements say. A platform deposit covers every store, so it is split across the stores by statement net (exact to the cent, the same split the journal uses): a deposit-level shortfall shows on every store in that payout, pro rata |
| `missing_deposit` | no bank deposit was found for this payout |
| `disputed` | a linked finding is money we expect back (recoverable / approved / filed, not rejected) |
| `needs_review` | a linked finding is a conflict waiting for a human |
| `recovered` | every open linked finding was paid back |
| `sold_never_paid` | a register order in no payout (`kind: "order"`) |

```json
{
  "total": 20, "limit": 2, "offset": 0,
  "rows": [
    {
      "txn_id": "grubhub:GH-PO-20260216:S12",
      "kind": "payout",
      "channel": "grubhub", "platform": "Grubhub", "store_id": "S12",
      "payout_id": "GH-PO-20260216", "payout_date": "2026-02-24",
      "gross": 706.95,
      "deductions": {"commission": 127.26, "promo": 0.0, "error_charges": 0.0, "tax_withheld": 1.46,
                     "other_fees": 47.3, "total": 176.02},
      "net": 530.93,
      "deposit": {"deposit_id": "BNK-7312888", "amount": 12932.41, "store_share": 530.93, "date": "2026-02-25",
                  "covers_payouts": ["GH-PO-20260209", "GH-PO-20260216"], "match_confidence": 1.0,
                  "source_file": "bank/bank_deposits.csv", "source_row": 7},
      "deposit_gap": 0.0,
      "deposit_status": "matched",
      "finding_ids": ["F-4a96b23e", "F-4d765dc0", "F-673dadf1", "F-73f5e7bb", "F-995d0f1d", "F-a41ad0f0", "F-ac403ec6"],
      "source": {"lines": 43, "files": [{"file": "grubhub/grubhub_statement.xlsx#Detail", "first_row": 547,
                                          "last_row": 1088}]},
      "status": "disputed",
      "flags": ["disputed", "needs_review"]
    }
  ],
  "summary": {
    "rows": 97, "payouts": 96, "unpaid_orders": 1,
    "by_status": {"sold_never_paid": 1, "missing_deposit": 0, "short": 12, "over": 0, "disputed": 7,
                  "needs_review": 0, "recovered": 0, "matched": 77},
    "by_channel": {"direct_app": 24, "doordash": 25, "grubhub": 24, "ubereats": 24},
    "stores": 12, "gross": 183156.9, "deductions": 31297.52, "net": 151859.38, "deposited": 151806.83,
    "with_findings": 44
  },
  "filtered_summary": {"rows": 20, "...": "same shape, over the filtered rows"},
  "data": {"stores": 12, "days": 14, "weeks": 2, "store_weeks": 24, "orders": 9723, "payout_lines": 9727,
           "deposits": 7, "period_start": "2026-02-09", "period_end": "2026-02-22"},
  "source": "real data (...)",
  "built_at": "2026-10-25T05:24:40+00:00"
}
```
`summary.by_status` counts each row once (by `status`); `?status=` matches any flag, so its `total` can be larger.
A sold-never-paid row:
```json
{"txn_id": "order:ORD-12346", "kind": "order", "channel": "doordash", "platform": "DoorDash", "store_id": "S12",
 "payout_id": null, "payout_date": null, "placed_at": "2026-02-12T06:26:09", "order_id": "ORD-12346",
 "gross": 7.9, "deductions": {"commission": 0.0, "promo": 0.0, "error_charges": 0.0, "tax_withheld": 0.0,
 "other_fees": 0.0, "total": 0.0}, "net": 0.0, "deposit": null, "deposit_gap": -7.9,
 "deposit_status": "missing_payout", "finding_ids": ["F-bb3c73ea"],
 "source": {"lines": 1, "files": [{"file": "pos/pos_orders.csv", "first_row": 1237, "last_row": 1237}]},
 "status": "sold_never_paid", "flags": ["sold_never_paid", "disputed"]}
```
After a fixtures run (the default `POST /api/run`) there are no payout tables, so `total` is 0 and `note` says so.
Year scale is ~12k payout rows; at the hosted cap (56 stores x 8 weeks) about 1,800.

Each payout row also carries `orders` (distinct order ids on its lines) and `terms`: the deal terms in effect for that
store and period, one brief per deduction type (`{"value", "authority", "source_file", "quote"}`), absent when no deal
document sets that term. `GET /api/payouts` uses them to say which expected figures it can't compute.

### `GET /api/payouts`
Feeds **(6) Lump sums**. One row per **lump sum that hit the bank**: platform x payout period (`channel:payout_id`).
Built from the run's own files (`transactions.json` "payouts", written once per real-data run by
`flock/transactions.py`; the findings' current state is applied on read by `flock/payouts.py`). All math in cents, no
model, no invented numbers.

Query: `channel`, `status` (matches any of the row's `flags`). Sorted newest first. After a fixtures run (hand-made
findings, no payout tables) the list is empty and `note` says why.

| field | means |
| --- | --- |
| `deposit_amount`, `deposit_date`, `deposit_id` | the bank deposit (the whole deposit; `covers_payouts` lists every payout it pays) |
| `received` | this payout's share of that deposit (one deposit covering two payouts is split by statement net, exactly); 0 if none |
| `stores`, `orders`, `payout_lines` | how many stores, orders and statement lines are in it |
| `gross`, `deductions {commission, promo, error_charges, tax_withheld, other_fees}`, `deductions_total`, `net` | the statement's figures; `adjustments` = net - (gross - deductions) (anything else on the statement) |
| `expected_net` | **per contract**: gross - expected deductions + adjustments + other gaps owed. A line's expected = statement figure - the gaps owed back on it (findings recoverable or human-approved; each gap computed in code by a checker from the deal term). `expected_net_computable` is false when a store was charged for a line with no deal term behind it (`not_computable` lists those lines) |
| `shortfall` | `expected_net - received` |
| `deposit_shortfall` | `net - received` |
| `owed` | `open_gaps` (counted, not yet recovered) + `deposit_owed` (deposit shortfall over $0.05 whose deposit finding is neither rejected nor recovered) |
| `in_question` | gaps on conflicts still waiting for a human: shown, not counted until a human approves |
| `findings`, `findings_by_counts_as` | finding ids linked to it, by how each counts: `owed`, `recovered`, `in_question`, `contract_stands` (a human rejected it), `fair`, `deposit` |
| `status` / `flags` | `missing_deposit > short_deposit > owed > needs_review > recovered > matched` |

```json
{"total": 8,
 "summary": {"payouts": 8, "gross": 123014.1, "deductions": 21511.26, "net": 101502.84, "expected_net": 101595.85,
             "received": 101465.96, "shortfall": 129.89, "owed": 129.89, "open_gaps": 93.01, "deposit_owed": 36.88,
             "recovered": 0.0, "in_question": 12.1,
             "by_status": {"missing_deposit": 0, "short_deposit": 1, "owed": 6, "needs_review": 0, "recovered": 0,
                           "matched": 1},
             "by_channel": {"direct_app": 2, "doordash": 2, "grubhub": 2, "ubereats": 2}},
 "unpaid_orders": {"count": 1, "gross": 17.95, "note": "orders in the register that are in no payout at all ..."},
 "source": "generated: ca_bagel_chain seed 2025, 8 stores x 2 weeks", "built_at": "2026-10-25T08:01:04+00:00",
 "payouts": [
  {"payout_id": "UE-PO-20260209", "payout_key": "ubereats:UE-PO-20260209", "channel": "ubereats",
   "platform": "Uber Eats", "payout_date": "2026-02-17", "deposit_id": "BNK-9362204", "deposit_date": "2026-02-18",
   "deposit_amount": 9938.53, "covers_payouts": ["UE-PO-20260209"], "received": 9938.53,
   "stores": 8, "orders": 803, "payout_lines": 803, "gross": 13574.35,
   "deductions": {"commission": 2714.87, "promo": 281.82, "error_charges": 0.0, "tax_withheld": 0.0,
                  "other_fees": 602.25},
   "deductions_total": 3598.94, "adjustments": 0.0, "net": 9975.41,
   "expected_net": 9984.41, "expected_net_computable": true, "shortfall": 45.88, "deposit_shortfall": 36.88,
   "open_gaps": 9.0, "deposit_owed": 36.88, "owed": 45.88, "recovered": 0.0, "in_question": 0.0,
   "findings": 14, "findings_by_counts_as": {"owed": 12, "deposit": 1, "contract_stands": 1},
   "status": "short_deposit", "flags": ["short_deposit", "owed"]},
  "..."],
 "formula": {"expected_net": "...", "shortfall": "...", "owed": "...", "...": "..."}}
```
(Totals reconcile: 101595.85 expected - 101465.96 received = 129.89 shortfall = 93.01 open gaps + 36.88 short deposit.
The one sold-never-paid order is in no lump sum, so it is counted under `unpaid_orders`, not `owed`.)

### `GET /api/payouts/<payout_id>`
Feeds the **(6)** detail. `<payout_id>` is the bare id (`UE-PO-20260209`) or the key (`ubereats:UE-PO-20260209`).
Everything in the list row, plus:

| field | means |
| --- | --- |
| `waterfall` | ordered steps for a chart: `gross` -> each deduction (`amount` = statement, `expected` = contract, `gap`, `in_question`, `computable`, `note`, `basis`, `finding_ids`) -> `adjustments` (if any) -> `statement_net` -> `other` gaps (if any) -> `expected_net` -> `deposit` -> `shortfall`. Each step has `running` (statement side) and `running_expected` (contract side) for floating bars |
| `lines` | per deduction type: `actual`, `expected`, `gap`, `owed`, `recovered`, `in_question`, `finding_ids`, `computable`, `basis` (the term, e.g. "commission rate 0.2 (contract, contracts/ubereats_contract.json)"), `note` (why it can't be computed), `terms`; plus `other` (sold-never-paid, paid-without-register-order, anomalies) |
| `stores_detail` | per store: `orders`, `gross`, `deductions`, `net`, `received` (its share), `deposit_gap`, `findings`, `open_gaps`, `share_of_gross` |
| `findings_detail` | every finding inside it, riskiest first: `finding_id, check_type, line, store_id (null for a deposit finding), gap, expected, actual, outcome, state, counts_as, risk, risk_label, portal_ref, attention, needs_human, reason` |
| `owed_breakdown` | `{open_gaps, deposit_shortfall, total, formula, deposit_closed_by}` |
| `deposit`, `deposit_findings` | the bank deposit row and the deposit finding(s) |

`404` for an unknown payout. Example (trimmed to two lines, two waterfall steps, two stores, two findings):
```json
{"payout_id": "UE-PO-20260209", "payout_key": "ubereats:UE-PO-20260209", "status": "short_deposit",
 "gross": 13574.35, "net": 9975.41, "expected_net": 9984.41, "received": 9938.53, "shortfall": 45.88, "owed": 45.88,
 "...": "...every list field...",
 "lines": [
  {"key": "commission", "label": "Commission", "actual": 2714.87, "expected": 2714.87, "gap": 0.0, "owed": 0.0,
   "recovered": 0.0, "in_question": 0.0, "findings": 0, "finding_ids": [], "computable": true,
   "basis": "commission rate 0.2 (contract, contracts/ubereats_contract.json)", "note": null,
   "terms": [{"value": 0.2, "authority": "contract", "source_file": "contracts/ubereats_contract.json",
              "quote": "Merchant is enrolled in the Standard (simulated) plan. Uber Eats (simulated coun..."}]},
  {"key": "other_fees", "label": "Other fees", "actual": 602.25, "expected": 593.25, "gap": 9.0, "owed": 9.0,
   "recovered": 0.0, "in_question": 0.0, "findings": 12, "finding_ids": ["F-0799505e", "F-13ff429f", "..."],
   "computable": true, "basis": "fee 0.75 (contract, contracts/ubereats_contract.json)", "note": null,
   "terms": [{"value": 0.75, "authority": "contract", "source_file": "contracts/ubereats_contract.json",
              "quote": "Merchant shall pay a Marketing Fee of $0.75 per completed order. Fee changes or ..."}]},
  "..."],
 "waterfall": [
  {"key": "gross", "label": "Gross sales on the statement", "kind": "total", "amount": 13574.35,
   "running": 13574.35, "running_expected": 13574.35},
  {"key": "commission", "label": "Commission", "kind": "deduction", "amount": -2714.87, "expected": -2714.87,
   "gap": 0.0, "in_question": 0.0, "computable": true, "note": null,
   "basis": "commission rate 0.2 (contract, contracts/ubereats_contract.json)", "finding_ids": [],
   "running": 10859.48, "running_expected": 10859.48},
  "...",
  {"key": "statement_net", "label": "Net per statement", "kind": "total", "amount": 9975.41, "running": 9975.41,
   "running_expected": 9984.41},
  {"key": "expected_net", "label": "Expected net per contract", "kind": "total", "amount": 9984.41,
   "computable": true, "running": 9975.41, "running_expected": 9984.41},
  {"key": "deposit", "label": "Deposit received (BNK-9362204)", "kind": "total", "amount": 9938.53,
   "running": 9938.53, "running_expected": 9984.41},
  {"key": "shortfall", "label": "Shortfall (expected - received)", "kind": "result", "amount": 45.88,
   "running": 9938.53, "running_expected": 9984.41}],
 "owed_breakdown": {"open_gaps": 9.0, "deposit_shortfall": 36.88, "total": 45.88,
                    "formula": "owed = open recoverable gaps (not yet recovered) + deposit shortfall still open",
                    "deposit_closed_by": []},
 "stores_detail": [
  {"store_id": "S01", "txn_id": "ubereats:UE-PO-20260209:S01", "orders": 76, "gross": 1210.9, "deductions": 326.9,
   "net": 884.0, "received": 880.73, "deposit_gap": -3.27, "findings": 13, "open_gaps": 9.0, "share_of_gross": 0.0892},
  "..."],
 "findings_detail": [
  {"finding_id": "F-8985179a", "check_type": "missing_order", "line": "other", "store_id": "S02",
   "order_ids": ["9bcb-18e6-83a4", "ORD-12719"], "expected": 0.0, "actual": 2.6, "gap": 2.6, "outcome": "conflict",
   "state": "REJECTED", "counts_as": "contract_stands", "risk": 41, "risk_label": "med", "portal_ref": null,
   "attention": {"value": 2.6, "rank": 2, "budget": 10, "status": "queued"}, "needs_human": false, "reason": "..."},
  {"finding_id": "F-ec9e9ba8", "check_type": "deposit", "line": "deposit", "store_id": null,
   "order_ids": ["UE-PO-20260209"], "expected": 9975.41, "actual": 9938.53, "gap": 36.88, "outcome": "recoverable",
   "state": "FILED", "counts_as": "deposit", "risk": 41, "risk_label": "med", "portal_ref": "SBX-3E5A05", "...": "..."},
  "..."],
 "deposit": {"deposit_id": "BNK-9362204", "amount": 9938.53, "date": "2026-02-18",
             "covers_payouts": ["UE-PO-20260209"], "match_confidence": 1.0,
             "source_file": "bank/bank_deposits.csv", "source_row": 4},
 "deposit_findings": ["F-ec9e9ba8"]}
```

### `GET /api/stream?since=<cursor>`
Feeds **(7) Agent theater**. One ordered feed of what the agents do, built from what the run already writes
(`agent_status.jsonl`, `audit_log.jsonl`, `market_ledger.jsonl`, `proposed_rules.json`; `flock/theater.py`). Poll it
every second with the `cursor` you got back; the files are tailed by byte offset, so a poll reads only what is new.

Query: `since` (default 0 = from the start of the current run), `limit` (default 200, max 1000).
Response: `run`, `cursor` (pass it back), `first_cursor` (the current run's first event), `latest`, `more` (true: poll
again right away), `reset`, `balances` (each judge's balance now, for the ticker), `events`.

- **Cursor**: an integer that only goes up while the server lives. An event never changes cursor or moves.
- **`reset: true`**: your cursor is from an earlier run (a new run replaced the files) or an earlier server process.
  Drop what you have and render these events (they start at the new run's `run_started`).
- **Every event**: `cursor, at, type, lane (agents|judges|market|human|learn), actor, text` (one readable sentence)
  plus structured fields by type:

| type | lane | fields |
| --- | --- | --- |
| `run_started` | agents | `run` |
| `agent_spawned` / `agent_working` / `agent_done` / `agent_failed` | agents | `role, agent_id, subject, summary` (checker, Clicky, journal, learner; one per worker when a batch has 8 or fewer) |
| `workers` | agents | `count, stores` (+ `findings, rows` when done): workers aggregated |
| `checked` | agents | `count, by_type, gap_total`: the checkers' findings, aggregated |
| `case_file` | agents | `finding_id, trigger, why, facts, next_step` (an investigator's case file) |
| `vote` | judges | `finding_id, judge, outcome, confidence, model, round, reason, weight` |
| `revision` | judges | `finding_id, judge, from, to, confidence, changed` (the debate round) |
| `second_opinion` | judges | `finding_id, judge (reviewer), outcome, confidence, model` (audit sample) |
| `verdict` | judges | `finding_id, outcome, confidence, agree, gap, check_type, channel, reason` |
| `memo` | judges | `finding_id, memo` (the reviewer's note to the human on a split) |
| `rule_applied` | learn | `finding_id, rule_id, outcome` (an approved rule decided it: no panel, no bets) |
| `escalation` | human | `finding_id, rank, budget, value, status (queued|deferred), gap, check_type, channel` |
| `bet_staked` | market | `finding_id, judge, outcome, confidence, stake, to_win, at_risk, bet_id, balances` |
| `bet_settled` | market | `finding_id, judge, payout, result (won|lost|abstained), truth, called, balance, balances` |
| `dispute_drafted` | agents | `finding_id, gap, channel` |
| `human_decision` | human | `finding_id, decision (approve|reject|recover), human, role, reason, gap` |
| `filed` | agents | `finding_id, portal_ref` (Clicky's sandbox filing reference) |
| `denied` | human | `finding_id, attempted, by, role, reason` (the gatekeeper refused a move) |
| `rule_proposed` | learn | `rule_id, description, evidence_count, precedents, outcome` |
| `rule_approved` | learn | `rule_id, human, description` |
| `journal`, `ranked` | agents | `count` |
| `judged_bulk`, `bets_bulk`, `settled_bulk`, `decided_bulk`, `drafted_bulk`, `filed_bulk`, `cases_bulk` | | `count` + totals (`by_outcome`, `stake`, `by_judge`, `balances`, ...) |

**Light at year scale.** A finding is told in full (votes, revisions, verdict, memo, escalation, bets, decision,
settlements) when it is the first of its kind (check type x outcome), one of the first 6, one of the first 6
conflicts past those, or it had a debate or a memo (60 per run at most). Every other escalation (60 per run) still pops
into the human lane with its rank and decision. Everything else is counted into one `*_bulk` event per batch; workers,
checks, ranks and journal entries are always aggregated. 3,000 judged findings make under 600 events.

**Order.** Sources are merged by the second they happened in, then by pipeline stage; bets are anchored to their move:
stakes right after the finding's verdict, settlements right after the human decision.

Example (`since=0`, one event of each kind picked from the 310 of this run):
```json
{"run": "RUN-8be3137e", "cursor": 310, "first_cursor": 1, "latest": 310, "reset": true, "more": false,
 "balances": {"strict": 388.0, "operator": 273.6, "skeptic": 346.4}, "count": 310, "poll_ms": 1000,
 "events": [
  {"cursor": 1, "at": "2026-10-25T08:01:04.769+00:00", "type": "run_started", "lane": "agents", "actor": "pipeline",
   "run": "RUN-8be3137e", "text": "Run RUN-8be3137e started: the flock is waking up"},
  {"cursor": 4, "type": "workers", "lane": "agents", "actor": "workers", "count": 8,
   "stores": ["S01", "S02", "S03", "S04", "S05", "S06", "S07", "S08"],
   "text": "8 workers spawned, one per store (S01..S08): matching orders to payouts", "at": "..."},
  {"cursor": 14, "type": "checked", "lane": "agents", "actor": "checkers", "count": 73,
   "by_type": {"missing_order": 2, "error_charge": 4, "fee": 60, "tax": 1, "promo": 2, "commission": 1, "deposit": 2,
               "anomaly": 1}, "gap_total": 175.29,
   "text": "Checkers logged 73 findings (60 fee, 4 error charge, 2 missing order, 2 promo): $175.29 in gaps, computed in code from the deal terms", "at": "..."},
  {"cursor": 15, "type": "case_file", "lane": "agents", "actor": "investigator:F-8985179a",
   "finding_id": "F-8985179a", "trigger": "missing_order_unsupported", "why": "missing order with no second source",
   "facts": ["Payout UE-PO-20260209 (2026-02-17) has 803 lines, net $9,975.41.",
             "First ubereats missing order finding for store S02."],
   "next_step": "Look for a register order under another id or time for this charge; ...",
   "text": "Investigator opened a case file on F-8985179a: missing order with no second source. Next: ...", "at": "..."},
  {"cursor": 16, "type": "vote", "lane": "judges", "actor": "judge.strict", "finding_id": "F-6b175438",
   "judge": "strict", "outcome": "recoverable", "confidence": 0.9, "model": "offline rules",
   "reason": "Payout breaks a term backed by a contract or authorized source.", "weight": 0.3785, "round": null,
   "text": "strict votes recoverable (90%) on F-6b175438, doordash missing order $17.95", "at": "..."},
  {"cursor": 19, "type": "verdict", "lane": "judges", "actor": "panel", "finding_id": "F-6b175438",
   "outcome": "recoverable", "confidence": 0.9, "gap": 17.95, "check_type": "missing_order", "channel": "doordash",
   "agree": 3, "reason": "All 3 judges agree ...", "text": "Panel: F-6b175438 is recoverable, 3 of 3 agree (90%)",
   "at": "..."},
  {"cursor": 20, "type": "bet_staked", "lane": "market", "actor": "judge.strict", "finding_id": "F-6b175438",
   "judge": "strict", "outcome": "recoverable", "confidence": 0.9, "stake": 9, "to_win": 9.6, "at_risk": 22.4,
   "bet_id": "B-15609abd", "balances": {"strict": 301.6, "operator": 222.4, "skeptic": 272.8},
   "text": "strict stakes 9 on recoverable (F-6b175438): wins up to +9.6, risks 22.4", "at": "..."},
  {"cursor": 27, "type": "escalation", "lane": "human", "actor": "panel", "finding_id": "F-8985179a", "rank": 2,
   "budget": 10, "value": 2.6, "status": "queued", "gap": 2.6, "check_type": "missing_order", "channel": "ubereats",
   "text": "F-8985179a (ubereats missing order $2.60) escalated to a human: attention #2 of 10 (worth $2.60 of a look)",
   "at": "..."},
  {"cursor": 212, "type": "judged_bulk", "lane": "judges", "actor": "panel", "count": 36,
   "by_outcome": {"recoverable": 36}, "by_rule": 0, "gap_total": 36.34,
   "text": "Judges decided 36 more findings: 36 recoverable", "at": "..."},
  {"cursor": 232, "type": "human_decision", "lane": "human", "actor": "demo-controller", "finding_id": "F-6b175438",
   "decision": "approve", "human": "demo-controller", "role": "human", "reason": "Evidence checks out", "gap": 17.95,
   "text": "demo-controller approved F-6b175438: \"Evidence checks out\"", "at": "..."},
  {"cursor": 233, "type": "bet_settled", "lane": "market", "actor": "judge.strict", "finding_id": "F-6b175438",
   "judge": "strict", "payout": 9.6, "result": "won", "truth": "recoverable", "called": "recoverable",
   "balance": 311.2, "bet_id": "B-15609abd", "balances": {"strict": 311.2, "operator": 222.4, "skeptic": 272.8},
   "text": "strict won +9.6 on F-6b175438 (truth: recoverable); balance 311.2", "at": "..."},
  {"cursor": 293, "type": "filed", "lane": "agents", "actor": "clicky", "finding_id": "F-6b175438",
   "portal_ref": "SBX-C69986", "gap": 17.95, "text": "Clicky filed F-6b175438 in the sandbox portal: SBX-C69986",
   "at": "..."},
  {"cursor": 310, "type": "rule_proposed", "lane": "learn", "actor": "learner", "rule_id": "R-001",
   "description": "grubhub fee backed by contract, unauth_email: accept, do not dispute", "evidence_count": 8,
   "precedents": ["F-079af078", "F-0ba349b4", "..."], "outcome": "correct",
   "text": "Learner proposes R-001: grubhub fee backed by contract, unauth_email: accept, do not dispute (learned from 8 human decisions)",
   "at": "..."}]}
```
(Balances carry over between runs until `--forget`, which is why they start above 100 here.)

### `GET /api/stream/sse?since=<cursor>`
The same events as Server-Sent Events: `id: <cursor>`, `event: <type>`, `data: <event JSON>`. Live by default (new
events every 0.5 s, a `: keep-alive` comment every 15 s, closes after 10 min; `EventSource` reconnects with
`Last-Event-ID`). An `event: reset` comes first when the run changed. `&replay=1&speed=N` streams the replay below and
ends with `event: done`. Some proxies buffer SSE; the polling endpoint always works, so use SSE only as a nicety.

### `GET /api/stream/replay?run=last&speed=N`
The last run's theater, re-emitted with stage pacing, so the show runs on stage without re-running the pipeline.
Events are rebuilt from the files (cursors 1..N; `live_cursor` is the event's cursor in the live feed) and each gets
`delay_ms` by type (a vote 500 ms, an escalation 1.4 s, a rule 1.8 s, a stake 220 ms; `pace_ms` lists them) divided by
`speed` (0.25-20, default 1), clamped to `[60, 3000] / speed` ms, and compressed so the whole replay fits in
`150 s / speed` (the floor gives way only when even the floor would not fit). `bounds` says the numbers in effect.

- **Polling (same loop as live):** first call without `start`: you get `start` (server ms) and the events due at t=0.
  Then call `?run=last&speed=N&since=<cursor>&start=<start>` every ~250 ms (or after `next_in_ms`); you get the events
  whose time has come. `done: true` after the last one.
- **All at once:** `&all=1` returns the whole schedule (`t_ms` per event, `duration_ms`); the client plays it.
- `run` must be `last` (anything else: `400`). `speed` that is not a number: `400`.

```json
{"mode": "replay", "run": "RUN-8be3137e", "speed": 2.0, "duration_ms": 75000, "count": 310,
 "bounds": {"min_gap_ms": 30, "max_gap_ms": 1500, "max_total_ms": 75000},
 "start": 1792915282335, "elapsed_ms": 0, "cursor": 1,
 "events": [{"cursor": 1, "at": "2026-10-25T08:01:04.769+00:00", "type": "run_started", "lane": "agents",
             "actor": "pipeline", "run": "RUN-8be3137e", "text": "Run RUN-8be3137e started: the flock is waking up",
             "live_cursor": 1, "delay_ms": 0, "t_ms": 0}],
 "done": false, "next_in_ms": 108}
```

### `GET /api/metrics`
The four bottom cards. Each card: `id`, `title`, `value`, `unit`, `formula` (plain English), `inputs`, `sources`
(each labelled `MEASURED`, `ASSUMPTION`, `SIMULATED`, `PROJECTION` or `MISSING`), `notes`. Nothing is invented: a value
that can't be measured is `null` and a note says why. Also `run`: the last run's id, label, wall time and data volume.

| Card | Value | How | Measured vs assumption |
| --- | --- | --- | --- |
| `agent_time` | `agent_hours`, `human_hours`, `hours_saved`, `multiple` | agent = wall time of the last pipeline run (`last_run.json`, `time.monotonic` around `pipeline.run`; generating the simulated data is not counted). human = store-weeks x minutes per store-week / 60. | agent time and store-weeks: **MEASURED**. Minutes per store-week: **MEASURED** if `data/benchmarks/results.json` has a human contestant (`scripts/benchmark.py --human ... --human-minutes`), else `FLOCK_HUMAN_MINUTES_PER_STORE_WEEK`, else **ASSUMPTION: 60 min** = "reconciling one location can take an hour" (Lunchbox, vendor claim, https://lunchbox.io/third-party-disputes-the-silent-restaurant-revenue-killer), applied per week as in FLOCK_CONTEXT.md §7, which itself marks the per-week cadence unverified. Conflicts still sent to a human are counted on neither side (the card says how many). |
| `accuracy` | `accuracy`, `caught`, `total`, `missed`, `false_alarms` | `scripts/score.py` against the run's answer key: `data/state/answer_key.csv` after a dataset run (kept by the run), else `data/answer_key.csv` (fixtures). Plus `benchmark`: per-contestant accuracy from `data/benchmarks/results.json` when present (`null` on the hosted box). | **SIMULATED**: the key comes from whoever planted the traps. |
| `cost` | `usd`, `usd_known_part`, `complete`, `credits`, `model_calls`, `live_judge_budget` (the run's `judging` record, see "Judging speed and the live-model budget"), `usd_per_1000_findings`, `credits_per_1000_findings` | local model calls x $0 + server-reported tokens x list price (`flock/llm.py LIST_PRICES`) + CREAO-reported tokens x the Developer Console per-model price (`llm.CREAO_USD_PER_M`, what CREAO /v2 bills) + CREAO /v1 credits x `FLOCK_CREAO_USD_PER_CREDIT`. | calls, tokens and CREAO usage are **MEASURED** per run (`llm.call_stats()` saved in `last_run.json`). Anything we cannot price (v1 credits with no rate, a model with no price, agent.creao.ai app calls that report no usage) makes `usd` `null`; `usd_known_part` is what we can price and `unknown` lists the rest. Offline rules = 0 calls = $0. The live box runs the AI roles on CREAO, so expect CREAO usage there. |
| `money_saved` | `usd` (= `recovered + filed + approved + pending`), `due_from_platform_booked`, `annualized_projection` | sum of `|gap|` over findings that are money we expect back (the journal's test: recoverable, approved, filed or recovered, never rejected), split by state. `due_from_platform_booked` = debits to finding-linked "Due from Platform" lines in the drafted journal entries. Projection = usd x 52 / weeks in the data. | dollars and receivables **MEASURED** from findings and journal entries. Projection is a **PROJECTION** (the multiplier is in `inputs.projection_multiplier`) and describes the simulation's trap rate, not a real chain. Undecided conflicts are listed, not counted. Year-scale runs (> 50k rows) skip the journal, so `due_from_platform_booked` is 0 there with a note. |

Real response (12 stores x 2 weeks), trimmed to one card in full:
```json
{
  "run": {"run_id": "RUN-90b38f45", "label": "live demo: seed 2025, ca_bagel_chain, 12 stores x 14 days",
          "seconds": 2.293, "findings": 73,
          "data": {"stores": 12, "days": 14, "weeks": 2, "store_weeks": 24, "orders": 9723, "payout_lines": 9727,
                   "deposits": 7, "period_start": "2026-02-09", "period_end": "2026-02-22"}, "...": "..."},
  "cards": [
    {
      "id": "agent_time", "title": "Agent hours vs human hours", "unit": "hours",
      "value": {"agent_hours": 0.000637, "human_hours": 24.0, "hours_saved": 24.0, "multiple": 37677},
      "formula": "agent hours = measured wall time of the last pipeline run / 3600. human hours = store-weeks in the run's data x minutes a person needs per store-week / 60. hours saved = human - agent; multiple = human / agent.",
      "inputs": {"agent_seconds": 2.293, "stores": 12, "weeks": 2, "store_weeks": 24, "minutes_per_store_week": 60.0,
                 "human_baseline": "ASSUMPTION", "sent_to_a_human": 14},
      "sources": [
        {"what": "agent_seconds", "label": "MEASURED", "source": "state/last_run.json: pipeline wall time (time.monotonic)"},
        {"what": "store_weeks", "label": "MEASURED", "source": "distinct stores x weeks spanned by std/orders.csv placed_at (flock/transactions.py)"},
        {"what": "minutes_per_store_week", "minutes_per_store_week": 60.0, "label": "ASSUMPTION",
         "claim": "Reconciling one location can take an hour (vendor claim). FLOCK_CONTEXT.md §7 applies it as ~1 hour per location per week and marks the per-week cadence as unverified ([need: per week? verify]).",
         "source": "https://lunchbox.io/third-party-disputes-the-silent-restaurant-revenue-killer",
         "cited_in": "FLOCK_CONTEXT.md §6 (Lunchbox) and §7 (worked example)"}
      ],
      "notes": ["14 finding(s) still go to a human for a decision; that review time is counted on neither side.",
                "Data is simulated (generator/generate.py)."]
    },
    {"id": "accuracy", "value": {"accuracy": 1.0, "caught": 236, "total": 236, "missed": 0, "false_alarms": 0},
     "inputs": {"answer_key": "data/state/answer_key.csv", "findings": 73, "settled_by_rules": 0, "sent_to_a_human": 14},
     "benchmark": null, "...": "..."},
    {"id": "cost", "value": {"usd": 0.0, "usd_known_part": 0.0, "complete": true, "credits": 0.0, "model_calls": 0,
                             "usd_per_1000_findings": 0.0, "credits_per_1000_findings": null},
     "notes": ["Zero model calls: every AI role ran on offline rules (FLOCK_OFFLINE=1), which costs $0."], "...": "..."},
    {"id": "money_saved", "value": {"usd": 164.5, "recovered": 0.0, "filed": 128.5, "approved": 0.0, "pending": 36.0,
                                    "due_from_platform_booked": 122.95, "annualized_projection": 4277.0},
     "inputs": {"findings_counted": 57, "undecided_conflicts": 4, "undecided_conflict_usd": 4.4,
                "journal_entries_with_receivable": 19, "weeks": 2, "projection_multiplier": 26.0},
     "sources": ["...", {"what": "annualized_projection", "label": "PROJECTION",
                         "source": "x 26.0 = 52 weeks / 2 week(s) in the data; assumes every week leaks like these did"}],
     "...": "..."}
  ]
}
```
At the hosted cap (56 stores x 8 weeks, seed 2025) a local run measured: agent 16.7 s vs 448 store-weeks x 60 min =
448 human hours (ASSUMPTION), 1447/1447 traps, 0 model calls, $1,031.69 found.

### `GET /api/agents`
The chain for the top row, in pipeline order: `agents[]` with `actor` (the id to pass to `/api/agents/<id>`), `name`,
`owner`, `job`, `live` (roles it shows up as in the live feed), `actions` (audit-log entries), `last_at`; plus
`judges`, `humans`, `transitions` (the state machine).

### `GET /api/agents/<agent_id>`
`agent_id` is a roster actor (`collector`, `formatter`, `matcher`, `deal_keeper`, `checkers`, `anomaly`,
`investigator`, `panel`, `ranker`, `dispute_writer`, `gatekeeper`, `clicky`, `journal`, `learner`) or a live agent
from the feed (`worker:S01`, `checker:deposits`, `judge:F-8b28ee81`, `writer:F-...`, `investigator:F-...`, pooled
`judge*`). Returns `activity` (newest first: audit-log entries `kind: "audit"` and agent events `kind: "agent"`),
`subject`, `parent`, `children`, `case_file` (a link to the finding it worked on, with the investigator's case file
when there is one), `has_screen`, `screens`.

**Only Clicky has a screen.** It drives a browser in the sandbox portal. In browser mode
(`FLOCK_CLICKY_MODE=browser`) it saves one screenshot per filing to `data/screens/filing-<ref>.png`, and a recorded
human demonstration (`python -m flock.agents.clicky record`) leaves step screenshots in `data/demos/<id>/`. The hosted
box files in file mode (no browser), so `screens.filings` is empty there. Every other agent is plain Python with no
UI: show its `activity` timeline as its "live screen".
```json
{
  "agent_id": "clicky", "kind": "roster", "name": "Clicky", "owner": "Preet", "role": "agent",
  "job": "Files approved disputes in the sandbox portal. The gatekeeper records the filing.",
  "live_roles": ["clicky"], "run": "RUN-90b38f45", "subject": null, "parent": null,
  "children": [{"agent_id": "clicky", "role": "clicky", "subject": "sandbox portal", "state": "done",
                "summary": "filed 9 disputes in the sandbox portal", "started": "2026-10-25T05:24:41.095+00:00",
                "ended": "2026-10-25T05:24:41.097+00:00"}],
  "children_total": 1, "actions": 9,
  "activity": [{"at": "2026-10-25T05:24:41.097+00:00", "kind": "agent", "action": "done",
                "subject_id": "sandbox portal", "agent_id": "clicky", "summary": "filed 9 disputes in the sandbox portal"},
               "..."],
  "activity_total": 12, "case_file": null,
  "has_screen": true,
  "screen_note": "Only Clicky has a screen: it drives a browser in the sandbox portal. Every other agent is plain Python with no UI; its live screen is its activity timeline.",
  "screens": {"filings": [], "demonstration": null, "latest": null}
}
```
With screenshots: `"screens": {"filings": [{"file": "filing-SBX-1A2B3C.png", "url": "/api/screens/filing-SBX-1A2B3C.png"}],
"demonstration": {"demo_id": "demo-...", "steps": ["/api/screens/demos/demo-.../step-01.png"], "video": null},
"latest": "/api/screens/filing-SBX-1A2B3C.png"}`.

A live judge:
```json
{
  "agent_id": "judge:F-8b28ee81", "kind": "live", "name": "Panel", "role": "judge",
  "job": "Three judges vote on each finding. Agreement decides alone; disagreement goes to a human.",
  "state": "done", "summary": "conflict: strict conflict, operator recoverable, skeptic conflict",
  "subject": "F-8b28ee81",
  "parent": {"agent_id": "worker:S10", "role": "worker", "subject": "S10", "state": "done",
             "summary": "827 orders matched, 24 findings"},
  "children": [], "activity": ["..."], "activity_total": 7,
  "case_file": {"finding_id": "F-8b28ee81", "url": "/api/findings/F-8b28ee81", "has_case_file": true,
                "case_file": {"trigger": "missing_order_unsupported", "why": "missing order with no second source",
                              "next_step": "Look for a register order under another id or time for this charge; ...",
                              "...": "..."}},
  "has_screen": false, "screens": null, "screen_note": "..."
}
```

### `GET /api/screens/<file>` and `GET /api/screens/demos/<demo_id>/<file>`
Binary (`image/png`, `image/jpeg`, `video/webm`). Only files directly in `data/screens/` or `data/demos/<demo_id>/`.
Every path segment must be a plain file name (letters, digits, `_ . -`, no leading dot, no `..`, no encoded slashes);
the resolved path must stay inside its folder. Anything else: `404` JSON.

### Other reads
| Endpoint | Returns |
| --- | --- |
| `GET /api/log` | The whole audit log, oldest first (findings, journal entries, denied attempts). Each entry: `at, actor, role, action, subject_id, subject_type, allowed, before, after, diff, reason, evidence`. A resolve shows up here as `role: "human"`, `action: "approve" / "reject"`. |
| `GET /api/score` | Scoreboard vs the run's answer key: `caught, total, missed, false_alarms, recovered, to_human, by_rule, traps[], runs[], text`. |
| `GET /api/rules` | `approved` and `proposed` learned rules. |
| `GET /api/portal` | Disputes Clicky filed in the sandbox portal. |
| `GET /api/progress` | `{"status": "running" / "done" / "idle", "stage", "done", "total", "unit", "rate", "elapsed", ...}` |
| `GET /api/jobs`, `GET /api/jobs/<id>` | Background runs (below). |
| `GET /api` | Route list. |

## Writes (token required)

### `POST /api/findings/<id>/approve` and `/reject`
Body `{"actor": "himaja", "reason": "Evidence checks out"}`. The gatekeeper decides whether the move is legal (only a
JUDGED finding can be approved / rejected); a refused move is logged and returns `409`. Approve also has Clicky file the
dispute. Response: `finding` (with risk), `filed` (portal refs), `proposed` (rules the Learner now proposes), `was`,
`log_entry` (the audit-log line for this decision, so the log column can append it at once).

### `POST /api/rules/<id>/approve`
Body `{"actor": "himaja"}`.

### `POST /api/run`
| Body | What happens |
| --- | --- |
| `{"reset": true, "demo_approve": true}` (no `dataset`) | The fixtures run (6 hand-made findings). **On the hosted box always call `POST /api/run?wait=0`**: it returns `202` with a job at once. Without `?wait=0` it stays synchronous for old callers (returns `{"output", "findings"}`), but with the AI roles on CREAO it outlasts Railway's proxy timeout and the caller sees a `502` while the run continues server-side. |
| `{"dataset": "demo", "seed": 2025, "profile": "ca_bagel_chain", "weeks": 2, "stores": 56}` | `flock.demo`: generate -> collect -> format -> match -> check -> judge -> rank -> draft -> demo human -> Clicky -> journal -> learner, into the server's state, kept. Returns **`202`** with a job right away; add `?wait=1` to block instead. |

Dataset run details:
- Caps on the hosted box: `stores` 1..56 (`FLOCK_HOSTED_MAX_STORES`), `weeks` 1..8 (`FLOCK_HOSTED_MAX_WEEKS`), `seed`
  0..2^31-1, `profile` one of `profiles/*.json` (`ca_bagel_chain`, `default`). Defaults: seed 2025, ca_bagel_chain,
  2 weeks, the profile's store count (56). Out of range: `400`.
- `demo_approve` (default `true`): the demo human approves recoverables and rejects conflicts, so Clicky files and the
  log fills. Send `false` to leave decisions for the UI. `forget: true` starts with no learned rules.
- **The unseen statement layout is auto-confirmed** (`confirm=True`): locally a person answers y/N at the terminal
  when the Formatter meets a layout it has never seen; the hosted box has no terminal, so the dataset run accepts the
  Formatter's proposed mapping. The job result says so (`auto_confirmed_unseen_layout: true`).
- The generated answer key is kept at `data/state/answer_key.csv`, so `/api/score` and `/api/metrics` score against
  it. The next fixtures run (`pipeline --reset`) removes it and the committed fixtures key is used again.
- Timing measured locally: 12 stores x 2 weeks ~3 s; 56 x 8 (190k orders, split detection over worker processes) ~17-30 s.

```json
{"job_id": "JOB-71bb2996", "kind": "demo",
 "params": {"seed": 2025, "profile": "ca_bagel_chain", "weeks": 2, "stores": 12, "demo_approve": true, "forget": false},
 "status": "running", "created_at": "2026-10-25T05:24:39+00:00", "started_at": "2026-10-25T05:24:39+00:00",
 "finished_at": null, "seconds": null, "result": null, "error": null, "poll": "/api/jobs/JOB-71bb2996"}
```

### `GET /api/jobs/<id>`
`status`: `running | done | failed`. While running: `progress` (same as `/api/progress`). Always: `output_tail` (last
40 lines of the run's printout). When done:
```json
{"job_id": "JOB-71bb2996", "kind": "demo", "status": "done", "seconds": 2.86,
 "result": {"params": {"...": "..."}, "auto_confirmed_unseen_layout": true,
            "score": {"caught": 236, "total": 236, "missed": 0, "false_alarms": 0, "recovered": 161.73,
                      "to_human": 14, "orders": 9724, "seconds": 2.8},
            "findings": 73,
            "transactions": {"stores": 12, "days": 14, "weeks": 2, "store_weeks": 24, "orders": 9723,
                             "payout_lines": 9727, "deposits": 7, "period_start": "2026-02-09", "period_end": "2026-02-22"}},
 "error": null,
 "output_tail": ["human hand-offs by run: Week 1: 0 | Week 2: 14", "", "kept: the dashboard shows this run ...", "done in 2.8s"]}
```
Jobs live in memory: a restart forgets the job list (the run's results stay in `data/state`).

## Concurrency

The server is a `ThreadingHTTPServer`. Reads during a run are safe: findings are read under the store's lock and the
JSON files are replaced atomically; JSONL readers (audit log, agent feed, portal) skip a line still being written or
damaged. One run at a time (`409`), and human decisions are refused with `409` while a run is going (a run resets the
findings). Two decisions on the same finding at once: exactly one wins, the others get `409` from the gatekeeper.

### Judging speed and the live-model budget

With live judges (CREAO, Groq, ...) each finding costs 3 judge calls (+ a debate round on a split), so a 78-finding
dataset judged one finding at a time takes about 2 hours on CREAO. Two env knobs, read at the start of each run:

| Variable | Default | Effect |
| --- | --- | --- |
| `FLOCK_JUDGE_CONCURRENCY` | `1` offline, `6` when any judge is live | Findings judged at once (a thread pool around `panel.decide`). CREAO run creates are still paced to `FLOCK_CREAO_RPM` across all threads. Only the panel calls run in the pool: gatekeeper transitions, market stakes and progress stay in one thread in the same order, so results are identical to the sequential run (tested offline). |
| `FLOCK_LIVE_JUDGE_BUDGET` | unset = every finding | The N riskiest findings (the risk formula above before the panel votes: `|gap|`, weak authority, `|gap|` x uncertainty; ties by `|gap|`, then `finding_id`) get the live judges. The rest are judged by offline rules, no model call: each of their votes has `"budget": "offline"`, the finding has `"live_budget": "outside"`, and its reason ends with `(offline rules: outside this run's live-model budget)`. Findings an approved rule settles never use the budget. Offline (no live judge) the budget is not applied. |

The run prints `judges: live models on 25 of 78 findings (FLOCK_LIVE_JUDGE_BUDGET=25; 53 on offline rules), 6 at a
time`, `last_run.json` keeps `judging: {live, concurrency, budget, used, outside, judged, by_rule}`, and the `cost`
card of `GET /api/metrics` shows it as `value.live_judge_budget` (plus a note when a budget was applied). For a hosted
demo: `FLOCK_JUDGE_CONCURRENCY=6`, `FLOCK_LIVE_JUDGE_BUDGET=25`.

## Smoke test

```bash
python scripts/smoke_hosted.py https://web-production-812b4.up.railway.app                 # every GET + shapes
python scripts/smoke_hosted.py https://web-production-812b4.up.railway.app \
    --token-file ~/.flock_token --run --stores 12 --weeks 2                                # + a dataset run, polled
```
Prints a pass/fail table (every GET, a finding detail, a payout detail, the payout identities (shortfall = expected -
received, owed = open gaps + deposit owed), stream cursors strictly increasing and continuing from `since`, replay
gaps and length inside `bounds`, `run=other` -> 400, traversal -> 404, unknown route -> JSON 404, POST without the
token -> 401, and with `--run` the job and the transactions it wrote); exits 1 on any failure.

## The dashboard mirrors

`python -m dashboard.app` serves the same read models: `/api/transactions`, `/api/payouts`, `/api/payouts/<id>`,
`/api/stream`, `/api/stream/replay`, `/api/metrics`, `/api/agents/<agent_id>`, `/api/screens/...`, and risk on
`/api/findings` and `/api/findings/<id>`. (Its own `/api/agents` is the live flock snapshot, not the roster.) It has no
`/api/run`, jobs, health or SSE. Its **Lump sums** tab (`#/payouts`, `#/payout/<key>`: list, waterfall chart with
statement vs contract bars, line-by-line table, stores, findings linked) and **Theater** tab (`#/theater`: three judge
columns with votes and bets, a balance ticker, the human lane, a rule-learned banner; Live or "Replay last run" at
1-8x) are the backup UI for the demo.

## Proposed CONTRACT.md addition (not applied)

A transaction row (`flock/transactions.py`), for anyone building against it:
`txn_id, kind (payout|order), channel, platform, store_id, payout_id, payout_date, gross,
deductions {commission, promo, error_charges, tax_withheld, other_fees, total}, net,
deposit {deposit_id, amount, store_share, date, covers_payouts, match_confidence, source_file, source_row} | null,
deposit_gap, deposit_status (matched|short|over|missing_deposit|missing_payout), finding_ids,
source {lines, files [{file, first_row, last_row}]}, orders, terms {<deduction>: {value, authority, source_file, quote}}`
plus read-time `status` and `flags`.
And on a finding, read-time `risk` (0-100), `risk_label`, `risk_factors`.
A lump sum (`transactions.json` "payouts", `flock/payouts.py` on read): the fields of `GET /api/payouts` above.
A theater event (`flock/theater.py`): `cursor, at, type, lane, actor, text` + the per-type fields above.
