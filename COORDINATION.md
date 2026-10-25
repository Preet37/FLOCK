# COORDINATION — read this first, every time

**For every person AND every AI agent working in this repo.** This is how we avoid stepping on each other tonight.

## The loop (do this every ~5 minutes while you work)

```bash
git fetch origin && git log --oneline HEAD..origin/main   # anything new on main?
git pull --rebase origin main                              # on your own branch: git rebase origin/main
```
Then re-read the **Status log** at the bottom of this file.

**When you start a task:** add a line to the Status log ("STARTED"), commit, push.
**When you finish:** add a line ("DONE" + branch + what changed + how to test), commit, push.
Keep each line short. Newest at the bottom. Never delete other people's lines.

## Rules

1. **Branch per task**, PR (or ask Preet) into `main`. Don't push big changes straight to `main`.
2. Before merging: the Quickstart prints **caught 6/6, 2 then 0 sent to a human**, and `FLOCK_OFFLINE=1 pytest -q` passes.
3. **Never commit secrets** (API keys, tokens, `.env`) or generated data (`data/state`, `data/raw/*`, `data/std/*`, `rules.json`, `results/`). Keys live in Railway variables or local files outside the repo.
4. `main` auto-deploys to Railway. A broken `main` breaks the live site.
5. CONTRACT.md is the shared contract. Changing a data shape = tell the team here first.
6. Money math is code. Agents propose, humans approve, only the gatekeeper files/posts.

## Where things are

| What | Where |
| --- | --- |
| Live API (Railway) | https://web-production-812b4.up.railway.app — reads open, POST needs `Authorization: Bearer <FLOCK_API_TOKEN>` (ask Preet) |
| AI roles on the live API | CREAO (7 hosted agents, Thabhelo's account) |
| API docs for the frontend | `docs/API.md` |
| Plan, owners, timeline | `docs/PLAN.md` |
| Contract (data shapes, state machines, rules) | `CONTRACT.md` |
| Deploy guide | `docs/DEPLOY.md` |
| GX10 (local models + training) | `scripts/gx10.sh`, `docs/gx10_results.md` (landing soon) |

## Who owns what right now

| Who | Owns | Don't touch without asking |
| --- | --- | --- |
| Preet (+ Preet's agents) | hosting (Railway), ML + benchmarking, merges into `main` | `flock/api.py` (being extended right now), `scripts/benchmark.py`, `scripts/gx10.sh` |
| Thabhelo | CREAO (agents, connectors, CREAO on the live API) | `scripts/creao_setup.py`, `creao_agents.json` |
| Himaja | the UI (her frontend folder) | — build against the live API; don't use Claude artifacts for live data (they can't call our API) |
| Ira | pitch, video, Devpost (confirm) | — |

**Feature freeze: Sunday 3:00 AM. Submit: Sunday 9:00 AM.**

## In flight (branches not on main yet)

| Branch | Owner | What |
| --- | --- | --- |
| `preet/gx10-run` | Preet's agent | GX10 models, speed + benchmark tables, fine-tuned judge, `docs/gx10_results.md` |
| PR #9 `himaja/frontend` | Himaja | live console in `frontend/` wired to the API — tested clean, waiting for Preet's merge |

Merged into main tonight: `preet/docs-sync`, `preet/frontend-api`, `preet/release-qa`, `preet/api-fixes`, PRs #6-#8 (CREAO).

**Merging into `main` restarts the live Railway server** (auto-deploy) and kills any running `/api/run` job. Before merging, check `GET /api/health` → `running_job` is null, or say so in the Status log first.

## Status log (append below; newest last)

- Sat 22:30 PT — Preet: Railway live with CREAO judges (verified: F-0001 judged by 3 CREAO agents, 0 fallbacks). Long `/api/run` requests hit Railway's proxy timeout (502) while the run continues server-side; fix (background jobs) coming in `preet/frontend-api`.
- Sat 22:30 PT — Preet: COORDINATION.md created. Everyone: follow "The loop" above.
- Sat 22:40 PT — Preet: DONE merged `preet/docs-sync` into main: CONTRACT.md rewritten to match the code (all shapes, state machines, traps 1-22, deal-term rule), README/FLOCK_CONTEXT updated (Noah's, current owners), new docs/STATUS.md. Docs only; 472 tests pass.
- Sat 23:20 PT — Preet's agent: DONE `preet/frontend-api` (not pushed, ready to merge): flock/api.py adds GET /api/transactions (paged, one row per payout x store), risk 0-100 on findings, GET /api/metrics (4 cards, formula + sources, assumptions labelled), GET /api/agents/<id>, GET /api/screens/<file>, GET /api/health, GET /api/jobs/<id>. POST /api/run runs as a background job (dataset=demo always; fixtures with `?wait=0`), one at a time (409): the fix for the Railway 502. Docs: docs/API.md. Test: `FLOCK_OFFLINE=1 pytest -q tests/test_api.py`; `python scripts/smoke_hosted.py <url> [--token-file F --run]`. After deploy: set Railway health check to /api/health; the frontend should call `POST /api/run?wait=0`.
- Sat 23:55 PT — Preet's agent: DONE `preet/release-qa` (not pushed). tests/test_release.py (end-to-end release scenarios a-h; slow ones behind FLOCK_SLOW=1), 11 bug fixes outside api.py/dashboard (run-history leak, model-written $ in disputes, portal partial reads, stage CLI tracebacks, learner rule ids, read_by, refund money_moved, ranker odds, fee weak-evidence), docs/QA_REPORT.md with open api.py bugs + demo-day checklist. Test: `FLOCK_SLOW=1 FLOCK_TEST_BROWSER=1 pytest -q` (526 pass).
- Sun 00:00 PT — Preet's agent: DONE `preet/api-fixes` (on top of `preet/release-qa`, not pushed). QA bugs A2 (strict JSON, NaN -> null), A3 (per-thread stdout proxy: job output / score text no longer lost or mixed, server logs stay), A5 (bodyless 204), A6 (hmac token compare; /api/findings default = needs_human, state, risk desc), A7 (dashboard run history follows a temp store); dataset `source` is a readable label. Judging: `FLOCK_JUDGE_CONCURRENCY` (default 1 offline, 6 live; identical results) and `FLOCK_LIVE_JUDGE_BUDGET=N` (N riskiest get live judges, rest offline rules, marked; shown in /api/metrics cost card + run summary). Hosted demo: CONCURRENCY=6, BUDGET=25. Docs: API.md, DEPLOY.md, QA_REPORT.md. Test: `FLOCK_OFFLINE=1 pytest -q` (tests/test_api.py, tests/test_judge_concurrency.py).
- Sun 00:20 PT — Preet: DONE merged `preet/frontend-api`, `preet/release-qa`, `preet/api-fixes` into main and deployed. Live site passes `scripts/smoke_hosted.py` 19/19. Railway now has FLOCK_JUDGE_CONCURRENCY=6, FLOCK_LIVE_JUDGE_BUDGET=25, AI roles on CREAO, health check /api/health, volume at /app/data/state (verified it survives redeploys). main: 535 tests pass.
- Sun 00:20 PT — Preet: Himaja's PR #9 (frontend) tested: merges clean, frontend/ only, JS valid, uses /api/agents /findings /log /metrics /score /transactions. Waiting for Preet's merge. Thabhelo: PR #8 merge restarted the server mid-job; please check /api/health running_job before merging.
- Sun 00:45 PT — Preet: hosted site re-filled with a clean run (8 stores x 2 weeks, bagel chain, seed 2025): 222/223 traps, 0 false alarms, 112 agents, 120 CREAO calls (budget 25, concurrency 6), 14 min. /api/metrics now has real values (human time is a labelled ASSUMPTION until the human baseline is timed; CREAO cost shows calls but no USD yet). Frontend can build against this data now. Don't POST /api/run casually: it replaces this data.
- Sun 00:40 PT — Thabhelo: DONE `thabhelo/spacetime` (PR to follow). `FLOCK_STORE=spacetime` is real: Rust module in `spacetimedb/` (tables finding, audit_log, agent_status; reducers put_new, commit, agent_event, reset), `SpacetimeStore` in flock/store.py over SpacetimeDB's HTTP API. Quickstart on it prints 6/6, 2 then 0. Dashboard works unchanged. JSON stays default (Railway untouched). Test: `FLOCK_OFFLINE=1 pytest -q tests/test_spacetime.py tests/test_store.py`; live: `spacetime start`, publish per spacetimedb/README.md, `FLOCK_SPACETIME_LIVE=1`.
