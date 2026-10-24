# Dashboard (owner: Ira)

A small Flask server (`dashboard/app.py`) serving one plain HTML/JS page (`dashboard/static/`). No Node, no build step.
Reads through the `Store` protocol (`flock.store.get_store()`, picked by env `FLOCK_STORE`, JSON by default).

```bash
export FLOCK_OFFLINE=1
python -m flock.pipeline --reset --forget --pace 0.4   # agents run slowly enough to watch the counters tick
python -m dashboard.app                                # open http://127.0.0.1:5050  (--port to change)
```

Run the pipeline in one terminal and the dashboard in another; the page polls once a second.
Without `--demo-approve`, conflicts and drafted disputes wait in the Human queue for you.

Screens, in demo order:
1. **Live board** (`#/`): a progress bar while a run is going (stage, done/total, rows per second, from
   `GET /api/progress`; `python -m flock.demo` and big `--weeks 52` runs show it best), findings, dollars found, sent to a human, awaiting a decision, audit entries + denied attempts;
   a count per state; the findings table; the latest agent activity.
2. **Human queue** (`#/queue`): conflicts and drafted disputes with Approve / Reject and a reason box. "Acting as" in
   the top bar is your name. Then "File N approved via Clicky", and the Learner's proposed rules with "Approve rule".
3. **Finding detail, the PR view** (`#/finding/F-0003`): numbers (computed in code), sources and claims with their
   authority, rule applied or the 3 judges' votes and confidences, dispute draft, what was filed, who decided, and the
   full audit trail with diffs. Denied attempts show in red. "Try as an agent" shows the gatekeeper refusing an agent.
4. **Journal** (`#/journal`, `#/journal/<je_id>`): draft journal entries per store and week (real data only), each
   with debits, credits, a balanced check, disputed amounts booked as "Due from Platform", Approve (human), and
   "Export N approved to the GL import" (the gatekeeper posts). The entry view shows every line with its finding link
   and clickable source rows, plus the entry's audit trail and "Try as an agent".
5. **Scoreboard** (`#/score`): `scripts/score.py` (caught, missed, false alarms, dollars, each trap vs the answer key)
   plus human hand-offs by week from `data/state/runs.jsonl`.
6. **Flock** (`#/flock`): every agent of the latest run with its job, colored by role: counts per role (active /
   done / failed), who is working right now, and the tree of who spawned whom (worker per store, then a judge per
   finding, then the writer or investigator under it; leaves shown as chips that link to the finding). Polls
   `GET /api/agents?since=<cursor>` every second and redraws only when an agent changed. A finding's page shows its
   investigator's **case file** (summary, facts, the payout, nearby deposits, every claim for the term) and the agents
   that worked on it.
7. **Judge market** (`#/market`): judge balances with sparkline history and weight share, recent bets and settlements
   from `data/state/market_ledger.jsonl`, attention budget used / remaining, and deferred escalations. Deferred
   conflicts also show (dashed) at the bottom of the Human queue; a FILED finding's page has "Mark recovered".

## The rule it keeps

The dashboard never writes a finding. Approve / reject call `gatekeeper.transition(store, id, action, Actor(name, role))`
(role defaults to `human`; anything else is denied and logged). Filing calls `clicky.file_approved()`, which moves
findings as the gatekeeper.

## API

| Method | Path | What |
| --- | --- | --- |
| GET | `/api/summary` | counters, count per state, latest 15 log entries |
| GET | `/api/findings` | all findings (+ `needs_human`) |
| GET | `/api/queue` | JUDGED findings waiting on a person (conflicts first, by attention rank; deferred last) |
| GET | `/api/findings/<id>` | finding, its log, the human decision, denied attempts, rule applied, portal entry |
| POST | `/api/findings/<id>/approve`, `/reject` or `/recover` | body `{"actor": "ira", "reason": "...", "role": "human"}`. 403 + logged if denied. Allowed moves settle the judges' bets |
| GET | `/api/agents?since=N` | the live flock: `counts` per role, `active`, `agents` changed since cursor N (all on a new run: `reset`), `cursor` |
| GET | `/api/market` | judge leaderboard (balance, weight, history, record), recent ledger entries, attention budget + deferred |
| POST | `/api/file-approved` | Clicky files APPROVED disputes (gatekeeper transition) |
| GET | `/api/rules` | proposed + approved learned rules |
| POST | `/api/rules/<rule_id>/approve` | body `{"actor": "ira"}` |
| GET | `/api/score` | scoreboard, per-trap results, run history |
| GET | `/api/meta` | store backend, states, state machine (findings + journal entries), `airgap` (enforced?, last run line) |
| GET | `/api/airgap` | air-gap evidence: last run's model calls by host ("air-gapped: N model calls, 0 left this network", offending hosts), each role's route and whether it stays on this network (`FLOCK_AIRGAP=1`) |
| GET | `/api/progress` | current run: `status` (idle/running/done/failed), `stage`, `done`/`total`, `rate` (rows/s), finished `stages` |
| GET | `/api/journal` | journal entries (no source rows), count per state, unbalanced count, total debits / credits |
| GET | `/api/journal/<je_id>` | one entry with its lines, sources and audit log |
| POST | `/api/journal/<je_id>/approve` or `/reject` | body `{"actor": "ira", "reason": "...", "role": "human"}`. 403 + logged if denied (agents, unbalanced entries) |
| POST | `/api/journal/export` | the gatekeeper posts every APPROVED entry to `data/exports/journal_entries_<time>.csv` |
| GET | `/api/source?file=data/...csv&row=N` | one raw CSV row (header + values) under `data/`, for a journal line's source link |

## SpacetimeDB

`FLOCK_STORE=spacetime` selects `SpacetimeStore` in `flock/store.py`, a stub that raises `NotImplementedError`.
Its docstring has the plan: tables `findings`, `audit_log` (insert-only), `agent_status`; reducers `put_new`,
`commit` (finding + log entry in one transaction), `reset`; the page subscribes to those tables instead of polling.
Implement the six `Store` methods and nothing else changes.
