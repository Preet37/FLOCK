# CONTRACT.md

Everyone builds to this. It describes what the code on `main` does today (synced Oct 24, 2026). If you need to
change it, tell the team first, then change this file and `flock/models.py` together.

Contents: [Rules](#the-rules) · [Folders](#folders) · [Standard tables](#standard-tables-datastd) ·
[Deal terms](#deal-term-output-of-the-deal-keeper) · [Finding](#finding-one-per-discrepancy) ·
[State machines](#state-machines-who-may-do-each-move) · [Log entry](#log-entry-append-only-never-edited) ·
[Journal entry](#journal-entry) · [Workflow](#workflow-clickys-learned-steps) · [Market ledger](#judge-market-ledger) ·
[Agent status](#agent-status-event) · [Connectors](#connector-payload) · [Traps 1-22](#traps-1-22-the-answer-key) ·
[HTTP](#http-surfaces)

## The rules

1. **Money math is code.** Every dollar goes through `flock.models.money()` (rounded to cents). No model computes,
   changes or "corrects" a number. Models only map messy columns, read documents and give judgment.
2. **Agents propose, humans approve.** Only a `human` actor may approve or reject a finding, a journal entry, a
   learned workflow or a learned rule.
3. **Only the gatekeeper files and posts.** Filing a dispute (`file`) and posting a journal entry (`post`) are done by
   the `gatekeeper` actor, never by an agent or a person directly.
4. **One door.** Every change to a finding, journal entry or workflow goes through `flock.gatekeeper.transition()`.
   No agent edits one directly. Denied attempts are logged too, then raise `Denied`.
5. **Every row keeps its source.** Every standardized row has `source_file` + `source_row` (the row number you'd see
   in Excel; the header is its own row). Every evidence item and every journal line points back to a source row, a
   quote, or a finding.
6. **Cleaning never deletes a disagreement.** Only exact duplicate rows are removed, and each removal is recorded.

Roles (`Actor.role`): `agent`, `human`, `gatekeeper`. Actor ids seen in the log include `checkers`, `panel`,
`ranker`, `dispute_writer`, `investigator`, `journal`, `clicky`, `clicky.learner`, `gatekeeper`, `demo-controller` (the
simulated human of `--demo-approve`), `cli-human`, and people's names from the dashboard.

## Folders

```
data/raw/<source>/          untouched copies; sources: pos, doordash, ubereats, grubhub, direct_app, bank,
                            contracts, emails (.json, .txt, .eml), slack/ (Slack export layout: users.json,
                            channels.json, <channel>/<YYYY-MM-DD>.json), notes/ (.md meeting notes), unknown/
data/raw/manifest.csv       source, file, sha256, copied_at (Collector; re-running copies nothing new)
data/std/                   standardized tables (CSV, below)
data/answer_key.csv         the committed demo key (6 rows); generated keys go to generator/out/answer_key.csv
data/state/                 written by the store:
                              findings.json, audit_log.jsonl, agent_status.jsonl, market_ledger.jsonl,
                              journal_entries.json, proposed_rules.json, runs.jsonl, progress.json, airgap.json,
                              answer_key.csv (only after `flock.demo --keep`)
data/inbox/connectors/      connector drop folder (+ processed/, rejected/ with <name>.errors.txt)
data/exports/               journal_entries_<UTC stamp>.csv: the GL import file the gatekeeper posts
data/demos/<demo_id>/       a recorded human demonstration: demo.json, step-NN.png, video.webm, trace.zip
data/workflows/             file_dispute.json (current learned workflow), archive/, audit_log.jsonl (survives --reset)
data/sandbox_portal/        disputes.jsonl: what Clicky filed in the FAKE localhost portal
data/benchmarks/            seeds/ (cached prepared seeds), results.json, results.csv
data/training/              judge_train.jsonl, judge_val.jsonl, manifest.json (tuned-judge training set)
results/                    judge_load_<provider>_<stamp>.json (scripts/judge_load.py)
rules.json                  approved learned rules (repo root)
mappings.json               human-confirmed column maps per report layout
chart_of_accounts.json      GL accounts for the Journal Entry Drafter
creao_agents.json           CREAO agent ids per role (ids, not secrets)
profiles/<name>.json        company profiles for the generator (all simulated)
```

Everything under `data/` except `data/answer_key.csv` is gitignored, as are `rules.json` and `results/*`.

## Standard tables (data/std/)

Written by the Formatter. Deductions are stored as positive numbers, so `net = gross - deductions`. Dates are ISO.
Column order below is the exact CSV header order (`tests/test_data.py` checks it against this file).

## Standard order row (register / direct app)

`data/std/orders.csv`:

`order_id, store_id, channel, placed_at (ISO), subtotal, tax, promo_code, source_file, source_row`

## Standard payout line (one per order per platform)

`payout_line_id, channel, store_id, platform_order_id, order_id (if known), gross, commission, promo_merchant_funded, error_charge, tax_withheld_by_platform, other_fees, net, payout_id, payout_date, source_file, source_row`

`data/std/payout_lines.csv` has those columns, then three extras the checkers read: `promo_total, promo_code,
placed_at` (`promo_total` is the promo's full cost before the split). DoorDash error charges stay as their own
payout lines (gross 0, net = -charge) and also appear in `error_charges.csv`.

## Bank deposit

`data/std/bank_deposits.csv`:

`deposit_id, channel, amount, deposit_date, memo, source_file, source_row`

## Sidecar tables

| File | Columns | Used for |
| --- | --- | --- |
| `error_charges.csv` | `error_charge_id, channel, store_id, platform_order_id, order_id, amount, delivered_at, complaint_at, reason, payout_id, source_file, source_row` | the 72-hour rule, duplicate charges |
| `refunds.csv` | `refund_id, channel, order_id, amount, status, initiated_at, settled_at, source_file, source_row` | refunds stuck at "initiated" |
| `payout_summaries.csv` | `payout_id, channel, period_start, period_end, order_count, gross, net, payout_date, source_file, source_row` | Grubhub summary sheet |
| `duplicates_removed.csv` | `table, source_file, source_row, duplicate_of` | what cleaning dropped, and the row it duplicated |

Channels: `doordash, ubereats, grubhub, direct_app`.

## Deal term (output of the Deal Keeper)

| Field | Values |
| --- | --- |
| `term_id` | `T-001`, ... |
| `channel` | a channel, or `all` |
| `store_id` | `S01`, ... or `all` |
| `term_type` | `commission` (fraction of gross), `promo_split` (merchant's share of the promo), `fee` (flat $ per order, compared to `other_fees`), `tax_rule` (`platform_remits` or `merchant_remits`), `error_window` (hours; default 72) |
| `value` | number or string, as written in the source |
| `effective_from`, `effective_to` | ISO date; blank = open-ended |
| `authority` | `contract`, `amendment`, `auth_email`, `unauth_email`, `call_note`, or `null` (unknown) |
| `quote` | exact text from the source (asserted verbatim for messages) |
| `source_file` | path under `data/` |
| optional `promo_code` | the promo the term applies to |
| optional `sender` | who sent the message, e.g. `Sam Takahashi <sam@ubereats.example>` |
| optional `locator` | where in the file: `line N` (email, note) or `ts=... [thread_ts=...] [(edited)]` (Slack) |
| optional `medium` | `email`, `slack`, `call_note` |
| optional `authority_basis` | why that authority, e.g. "sender is on the contract's authorized contacts" |
| optional `edited`, `slack_channel`, `subject` | Slack edit flag, `#channel`, email subject |

When the checkers ask for a term (`deal_keeper.terms_for`), the winning term also carries `conflicts` (claims a human
should see) and `unresolved: true` when nobody wins.

### Who has authority

Contracts and amendments list `authorized_contacts` (emails and `@slack_handles`, strings or `{email, slack}`;
`schedule_b_authorized_contacts` emails count too). A written message (email or Slack) gets its authority from its
sender, never from its text or a model:

| Sender | Authority |
| --- | --- |
| on that channel's contract's authorized contacts | `auth_email` (binding) |
| same company domain, not on the list | `unauth_email` (non-binding) |
| anyone else | `null`, unknown (conflict) |
| contract has no list (old files) | the message's `sender_authorized` flag, else unknown |
| meeting / call notes | always `call_note` (non-binding) |

`auth_email` / `unauth_email` cover any written message; `medium` says whether it was email or Slack.

### Thread correction

In a Slack thread, a later **binding** message about the same channel + store + term_type (+ promo_code) replaces
the earlier ones in that thread before `effective()` runs. The replaced claims are kept in `superseded` with
`superseded_by: <locator>`. In an email, quoted reply lines (`>`) are history, never new claims.

### Which term is in effect (newest binding wins)

- **Binding:** `contract`, `amendment`, `auth_email`. These can change the deal.
- **Non-binding:** `unauth_email`, `call_note`, unknown. These never change the deal on their own.

To pick the term for one channel + store + term_type (+ promo_code) on one day (`deal_keeper.effective()`):

1. Keep only claims active that day (`effective_from <= day <= effective_to`; blank = open-ended).
2. If any binding claim is active, only binding claims can win:
   1. A store-specific claim beats an `all`-stores claim.
   2. The latest `effective_from` wins (that's what an amendment or an authorized waiver is for).
   3. Same `effective_from`: contract > amendment > auth_email. Same rank with different values = conflict, no winner.
3. Non-binding claims whose value differs from the winner are returned as `conflicts`. They go into the finding's
   evidence (with `conflict: true`) and a human decides. They never change the expected amount silently.
4. If only non-binding claims are active: unauth_email > call_note. If the strongest is unknown authority, or two of
   the same rank disagree, nobody wins: the checker computes with the first claim and marks it `unresolved`, and a
   human decides.

Rule-based patterns read messages first. A line with deal words the rules can't parse goes to the `reader` model
role; its claim is kept only if its quote appears verbatim in the message. Offline, that line is listed as unread
(`<file>#<locator>`), never guessed.

## Finding (one per discrepancy)

| Field | Meaning |
| --- | --- |
| `finding_id` | `F-xxxxxxxx` (fixtures use `F-0001`) |
| `order_ids[]` | orders involved. Deposit findings carry payout ids; trap 4 carries the platform order id |
| `channel` | the channel |
| `check_type` | see the table below |
| `expected`, `actual`, `gap` | dollars, computed in code. Positive gap = the store is owed money |
| `evidence[]` | `{source_file, row, what}` for a data row, or `{source_file, quote, authority, locator?, conflict?}` for a deal claim |
| `outcome` | `correct`, `recoverable`, `conflict` (set by the panel) |
| `confidence` | 0..1, the panel's (balance-weighted) mean |
| `reason` | plain English |
| `votes[]` | see below |
| `rank_score` | `|gap| x win_probability - effort` (ranker) |
| `dispute_draft` | the message to the platform (dispute writer) |
| `state` | see the state machine |
| optional `review_memo` | the reviewer's memo for a human when the judges split (it explains, never decides) |
| optional `case_file` | investigator's case file: `trigger, why, store_id, payouts, nearby_deposits, history, claims, votes?, facts, next_step, summary, summary_by` |
| optional `attention` | `{value, rank, budget, status}`; status `queued` or `deferred` (still a conflict, still waiting) |
| optional `portal_ref` | the sandbox portal's `SBX-` reference, written at `file` |

**Vote** (one per judge per round): `judge` (`judge.strict`, `judge.operator`, `judge.skeptic`, or `reviewer`),
`outcome`, `confidence`, `reason`, plus when they apply: `model` (provider:model that answered), `fallback` (the error
that sent it to offline rules), `round` (`1` or `2` after a debate; `"audit"` for the reviewer's second opinion),
`revised` (round 2 changed the call), `weight` (judge's market balance share), `debate_error`.

### Check types

| `check_type` | Emitted by | What it compares |
| --- | --- | --- |
| `commission` | Checkers | commission charged vs the `commission` term |
| `promo` | Checkers | merchant-funded promo vs `promo_split` and the promo's date window |
| `tax` | Checkers | tax withheld vs `tax_rule` (`platform_remits` = must not be withheld) |
| `error_charge` | Checkers | 72-hour window (`error_window`), duplicate charges, legit charges (gap 0), and refunds stuck at "initiated" |
| `fee` | Checkers | `other_fees` vs the `fee` term (rep waivers, traps 14, 15, 19-22) |
| `deposit` | Checkers (deposit reconciler) | payout statement net vs the bank deposit (one deposit may cover several payouts) |
| `missing_order` | Matcher | sold never paid, or paid with no register order |
| `anomaly` | Anomaly Detector | a store x channel's fee, commission or payout lag vs its own learned baseline, only where no deal term governs it |

### How the panel sets `outcome`

1. An approved learned rule that matches decides it (`confidence 1.0`, no votes).
2. Otherwise three judges vote in parallel. On a split, one debate round (live models only).
3. All agree and the mean confidence (unrounded) is at least 0.75: that outcome. Otherwise `conflict`.
4. Code guardrails on top: a gap within $0.05 is `correct`; a `recoverable` with weak evidence becomes `conflict`
   (a commission / promo / tax / error_charge finding with no contract, amendment or auth_email behind it, or any
   other finding with fewer than two evidence items).
5. Audit sample: `FLOCK_AUDIT_RATE` (default 10%, by a hash of `finding_id`) of the panel's own calls get a reviewer
   second opinion; if it disagrees the finding becomes `conflict`.

## State machines (who may do each move)

`transition()` checks the move exists, checks the actor's role, runs the subject's guard, keeps field-limited
annotations to their fields, strips `state` and the id from any patch, and writes the log entry in the same commit.

### Finding (`models.FINDING`)

| From | Action | To | Who |
| --- | --- | --- | --- |
| NEW | check | CHECKED | agent |
| CHECKED | judge | JUDGED | agent |
| JUDGED | approve | APPROVED | human |
| JUDGED | reject | REJECTED | human |
| APPROVED | file | FILED | gatekeeper |
| FILED | recover | RECOVERED | human or gatekeeper |

Annotations change fields but not state (still logged):

| State | Action | Who | May write |
| --- | --- | --- | --- |
| CHECKED, JUDGED | investigate | agent | only `case_file` (anything else in the patch = denied) |
| JUDGED | rank | agent | any field (used for `rank_score`) |
| JUDGED, APPROVED | draft | agent | any field (used for `dispute_draft`) |

After an allowed `approve`, `reject` or `recover` on a finding, the judge market settles its bets on it.

### Journal entry (`models.JOURNAL`)

| From | Action | To | Who |
| --- | --- | --- | --- |
| NEW | draft | DRAFT | agent |
| DRAFT | approve | APPROVED | human |
| DRAFT | reject | REJECTED | human |
| APPROVED | post | POSTED | gatekeeper |

Guard: `approve` is refused unless the lines balance to the cent (recomputed, never trusted) and there is at least
one line.

### Workflow (`workflow.WORKFLOW`, Clicky's learned form steps)

| From | Action | To | Who |
| --- | --- | --- | --- |
| NEW | learn | DRAFT | agent |
| DRAFT | approve | APPROVED | human |
| DRAFT | reject | REJECTED | human |
| APPROVED | stale | STALE | agent or gatekeeper (a step's element is gone) |
| DRAFT, APPROVED, STALE, REJECTED | retire | RETIRED | agent (a newer demonstration replaced it) |

Guard on `approve`: the steps must still hash to what Clicky learned, and some step must take the order ids and the
amount from the finding. At replay Clicky refuses a workflow whose steps no longer hash to the approved hash.

### Learned rule (`flock/agents/learner.py`)

PROPOSED (written by the Learner to `proposed_rules.json`) -> APPROVED (only a human, via `learner.approve()`;
copied into `rules.json` with `approved_by`, `approved_at`). Rules without `approved_by` never match. This move is
not a gatekeeper transition, but it is logged (`action: approve_rule`).

## Log entry (append-only, never edited)

`at, actor, role, action, subject_id, subject_type, allowed, before, after, diff, reason, evidence[]`

- `subject_type`: `finding`, `journal_entry` or `workflow`.
- `before` / `after`: states. `diff`: `{field: [before, after]}` for every changed field; `{}` when denied.
- Denied: `allowed: false`, `after == before`, and `reason` starts with `DENIED:`.
- Findings and journal entries share `data/state/audit_log.jsonl`. Workflows log to
  `data/workflows/audit_log.jsonl` and also to the base store's log when one is given.
- `approve_rule` entries (from the Learner) have no `subject_type`; `before` is `PROPOSED`, `after` is `APPROVED`.

## Journal entry

One balanced entry per store x channel x payout, drafted by `flock/agents/journal.py`.

`je_id (JE-<store>-<channel>-<payout_id>), journal_no (FLK00001...), date, week, store_id, channel, payout_id,
payout_date, deposit_id, tax_rule, memo, lines[], total_debit, total_credit, balanced, finding_ids[], notes[],
drafted_by, state`; posting adds `export_file, posted_at`.

**Line:** `line_no, account, account_name, account_type, debit, credit, memo, finding_id, sources[]` where each
source is `{source_file, rows[]}`. Every line carries sources and/or a `finding_id`.

What gets booked: Dr Cash (the store's share of the bank deposit, exact to the cent), Dr commission / promotions /
fees / error charges, Dr Due from Platform (Disputed) per open finding instead of expense, Cr Delivery Sales, sales
tax by `tax_rule`, and anything a payout line can't explain to Payout Clearing. Export columns: `Journal No, Date,
Account, Account Name, Debit, Credit, Memo, Location, Class, Flock JE, Finding, Source`.

## Workflow (Clicky's learned steps)

`workflow_id (WF-file_dispute-v<N>), name, version, state, learned_from (demo id), learned_at, portal_layout,
portal_url, shown_finding, labeled_by, steps[], fields, constants, unused_fields, video, trace, hash, warnings`;
approve adds `approved_by, approved_at, approved_hash`; stale adds `stale_reason, stale_step, stale_at,
stale_submitted`; retire adds `retired_by`.

**Step:** `n, phase (login|file), action (goto|fill|select|click|read_ref), path, target (locator), source,
navigates, next_path, pattern, demo_value, screenshot`. `source` is `{kind: "finding", field, format}`,
`{kind: "constant", value}` or `{kind: "env", name}`; the portal password is never stored (`FLOCK_PORTAL_PASS` at
replay). Finding fields a step can take: `finding_id, order_ids, amount, channel, category, dispute_draft`.

## Judge market ledger

`data/state/market_ledger.jsonl`, append-only. Balances start at 100 and are derived from it (never stored).

| `kind` | Fields |
| --- | --- |
| `stake` | `at, bet_id, run_id, finding_id, judge, outcome, confidence, p_recoverable, stake, to_win, at_risk` |
| `settle` | `at, bet_id, finding_id, judge, called, confidence, truth, source (approve/reject/recover), decided_by, payout, balance, result (won/lost/abstained)` |
| `void` | `at, bet_id, finding_id, judge, reason` (findings wiped by `--reset`) |
| `reset` | `at, reason` (`--forget`: balances back to 100; history kept) |

Forecast: `recoverable` at c -> p = c; `correct` at c -> p = 1 - c; `conflict` -> 0.5. Truth: human approve =
recoverable, human reject = correct, RECOVERED = recoverable. Payout = `10 x (1 - 4(p - y)^2)`; a judge never loses
more than its balance. A bet settles only if the audit log holds the matching allowed move.

## Agent status event

`data/state/agent_status.jsonl` (`store.agent_event()` / `store.agents(since)`; both have no-op defaults in the
Store protocol).

`seq, at, run, event (spawn|working|done|failed), agent_id, role, parent_id, subject, started, ended, summary`, plus
extras some agents add (`outcome`, `trigger`, `filed`, `drafted`, `proposed`). Pooled agents (past the first 150 of
a role in a run) add `pooled: true, count, done, failed`.

Roles: `worker` (one per store), `checker` (deposit reconciler), `judge`, `investigator`, `writer`, `clicky`,
`learner`, `journal`.

## Store protocol

Every backend implements `get, all, put_new, commit, log, reset` (plus `agent_event, agents` for the live feed).
`put_new` only takes state NEW; `commit(item_or_None, log_entry)` saves the change and its log line atomically
(`None` = a denied attempt). `FLOCK_STORE=json` (default) or `spacetime` (stub, not implemented). The journal and
workflow stores implement the same six methods so the gatekeeper can move them.

## Connector payload

Outside connectors (the intended one is a CREAO agent) drop JSON into `data/inbox/connectors/`;
`python -m flock.connectors ingest` validates it and writes `.eml`, Slack export or `.md` files through the
Collector. Version 1: `flock_connector: "1"`, `source` (`email|slack|note`), `connector`, optional `simulated`,
`items` (1 to 5000). Full item shapes: [`docs/connectors.md`](docs/connectors.md). A connector reports what was
said; it never decides authority or extracts terms.

## Traps 1-22 (the answer key)

`generator/out/answer_key.csv`: `order_id, trap_type, expected_outcome, amount`. Deposit traps (5, 6) use the
payout id as `order_id`; trap 4 uses the platform order id. A `correct` trap counts as caught when Flock disputes
nothing for it.

| # | trap_type | Expected | How Flock gets it |
| --- | --- | --- | --- |
| 1 | duplicate_rows | correct | Formatter drops exact duplicates (`duplicates_removed.csv`) |
| 2 | unseen_layout | correct | Formatter proposes a mapping, a human confirms once |
| 3 | sold_never_paid | recoverable | `missing_order` (Matcher) |
| 4 | payout_without_pos_order | conflict | `missing_order`, one source only, so a human decides |
| 5 | deposit_covers_two_weeks | correct | `deposit`: totals agree across both payouts |
| 6 | payout_deposit_mismatch | recoverable | `deposit` |
| 7 | commission_wrong_tier | recoverable | `commission` |
| 8 | bogo_wrong_split | recoverable | `promo` |
| 9 | promo_outside_dates | recoverable | `promo` |
| 10 | error_charge_after_72h | recoverable | `error_charge` |
| 11 | duplicate_error_charge | recoverable | `error_charge` |
| 12 | tax_withheld_platform_collected | recoverable | `tax` |
| 13 | refund_initiated_no_money | conflict | `error_charge` (refund stuck at "initiated") |
| 14 | authorized_rep_waiver | recoverable | `fee`, waiver from an authorized rep |
| 15 | unauthorized_rep_promise | conflict | `fee` with the rep's claim as conflict evidence |
| 16 | legit_error_charge | correct | `error_charge` with gap 0, never disputed |
| 17 | penny_rounding | correct | within the $0.05 tolerance |
| 18 | processing_fee_creep | recoverable | `anomaly`: no flat fee term exists, only history catches it |
| 19 | slack_authorized_waiver | recoverable | `fee`, waiver posted in Slack by an authorized contact |
| 20 | slack_unauthorized_promise | conflict | `fee`, Slack promise from a rep not on the list |
| 21 | slack_thread_correction | recoverable | `fee`, the thread's correction moved the waiver to this day |
| 22 | slack_thread_retracted_day | correct | the corrected-away day is billed as normal; nothing disputed |

Traps 19-22 are planted only when Slack is among the deal sources (`--sources ...slack...`, the default for
`ca_bagel_chain`). `--mini` plants 3, 7, 16. Each trap is planted `max(1, round(trap_rate x weight x store_weeks /
100))` times.

## HTTP surfaces

Both serve JSON; writes are human decisions that still go through the gatekeeper.

| Server | Start | Reads | Writes |
| --- | --- | --- | --- |
| `flock.api` (stdlib, port 8787, `Procfile`) | `python -m flock.api` | `/api/agents` (roster + action counts), `/api/findings`, `/api/findings/<id>`, `/api/log`, `/api/rules`, `/api/score`, `/api/portal` | `POST /api/findings/<id>/approve`, `/reject`, `/api/rules/<id>/approve`, `/api/run`. With `FLOCK_API_TOKEN` set, POSTs need `Authorization: Bearer <token>`. Denied = 409 |
| `dashboard.app` (Flask, port 5050) | `python -m dashboard.app` | `/api/summary`, `/api/findings`, `/api/queue`, `/api/findings/<id>`, `/api/market`, `/api/agents?since=N` (live feed), `/api/rules`, `/api/score`, `/api/progress`, `/api/airgap`, `/api/meta`, `/api/journal`, `/api/journal/<je_id>`, `/api/clicky`, `/api/clicky/demos/<id>/<file>`, `/api/source` | `POST /api/findings/<id>/approve|reject|recover`, `/api/file-approved`, `/api/rules/<id>/approve`, `/api/journal/<je_id>/approve|reject`, `/api/journal/export`, `/api/clicky/approve|reject`. Denied = 403 |

Note: `GET /api/agents` means different things on the two servers (roster vs live feed).
