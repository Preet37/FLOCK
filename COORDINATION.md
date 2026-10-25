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
- Sun 02:45 PT — Preet's agent: DONE `preet/gx10-run` (not pushed). GX10 has 3 judges + a LoRA-tuned Qwen2.5-7B judge in Ollama. `docs/gx10_results.md`: speed (GX10 panel 3.3x lower p50, 2.8x throughput vs the Mac), held-out benchmark (seeds 101-103, pre-deal-sources generator bca3166), tuned 916/916 = rules vs base 910/916. Code: `FLOCK_LOCAL_REASONING` in llm.py (gx10.sh env sets auto), `local:<model>` contestants in benchmark.py, `scripts/train/`. Test: `FLOCK_OFFLINE=1 pytest -q` (546 passed).
- Sun 01:00 PT — Preet's agent: STARTED `preet/lumpsum-theater`: GET /api/payouts (lump sums, waterfall), GET /api/stream (agent theater, replay), dashboard tabs. Owns flock/api.py + dashboard/ for this.
- Sun 02:00 PT — Preet's agent: DONE `preet/lumpsum-theater` (not pushed). New: flock/payouts.py (GET /api/payouts, /api/payouts/<id>: expected net per contract vs deposit, shortfall, owed, waterfall, per store, findings inside), flock/theater.py (GET /api/stream?since=, /api/stream/sse, /api/stream/replay?run=last&speed=N), transactions.json gains per-row orders + deal terms and a "payouts" list, dashboard tabs Lump sums + Theater (backup UI), docs/API.md panels (6) and (7) with real examples, smoke_hosted covers the new GETs. Test: `FLOCK_OFFLINE=1 pytest -q tests/test_payouts_theater.py`; `python scripts/smoke_hosted.py http://127.0.0.1:8787` after a dataset run. Needs a dataset run on the hosted box to fill /api/payouts (fixtures have no payouts).
- Sun 01:10 PT — Preet's agent: STARTED `preet/showtime`: generator `--scenario demo_month` (one calendar month, 56 stores, ~100K orders, planted big DoorDash commission error), `python -m flock.showtime prepare|status|reset|restore`, vendor follow-up drafts (gatekeeper `follow_up` step), docs/DEMO.md. Not touching flock/api.py or dashboard/.
- Sun 01:25 PT — Preet's agent: DONE `preet/showtime` (not pushed). `python generator/generate.py --scenario demo_month` (Sep 2026, 56 stores, ~103K orders -> 20 lump-sum deposits; DoorDash keeps 25% at 3 stores after a 20% amendment + 7 small incidents; $ impact computed into scenario.json). `python -m flock.showtime prepare|status|approve|restore|reset` (~50 s offline: 4 prior months with the demo human + Learner, hand-offs 5,4,3,1,1; headline left JUDGED for the presenter; cheat sheet data/state/showtime.md). Opt-in `pipeline.run(rollup=True)` (flock/rollup.py: one finding per broken term per weekly payout). Vendor follow-ups: FILED-only gatekeeper annotations follow_up / approve_follow_up / reject_follow_up / send_follow_up, finding field `follow_up`, `python -m flock.agents.follow_up`, `clicky.send_follow_ups`. Run sheet: docs/DEMO.md. Test: `FLOCK_OFFLINE=1 pytest -q tests/test_showtime.py tests/test_follow_up.py` (563 pass). For api.py/dashboard owners: rolled-up findings carry hundreds of order_ids (show `rollup.orders` instead), and follow-up approve/send has no HTTP route yet.
- Sun 01:40 PT — Preet: merged `preet/gx10-run` (GX10 results: tuned 7B judge 916/916 on held-out seeds, docs/gx10_results.md), `preet/lumpsum-theater` (GET /api/payouts, /api/stream + replay, dashboard Lump sums + Theater tabs), `preet/showtime` (python -m flock.showtime prepare|status|restore|reset, demo_month scenario, vendor follow-ups, docs/DEMO.md). GX10 default judge is now flock-judge-v1; `scripts/gx10.sh env --hybrid` = judges on GX10 + reviewer/writer/mapper/reader on CREAO (verified live). Hosted data refreshed (8 lump sums). main: 578 tests pass.
- Sun 03:00 PT — Preet's agent: STARTED `preet/follow-up-api` (on top of `preet/showtime`): follow-up HTTP routes, rollup display, dashboard follow-up card. Owns flock/api.py + dashboard/ for this.
- Sun 03:40 PT — Preet's agent: DONE `preet/follow-up-api` (not pushed). POST /api/findings/<id>/follow_up (agent drafts), /follow_up/approve, /follow_up/reject ({actor, reason}), /follow_up/send (gatekeeper via Clicky, returns portal_ref); token-gated, 409 out of order / during a run, agent ids refused and logged; mirrored in dashboard/app.py (403 on refusal). GET /api/findings/<id> has `follow_up`; list items have `has_follow_up`, `orders_label`, `order_count` (rollups: "457 orders at S28, S38, S41", ids cut to 10, full in detail). Payouts: a rollup counts once, split per store via new `rollup.by_store`. Theater: `follow_up` events, rollup counts in texts. Dashboard finding page: Vendor follow-up card. Docs: API.md, DEMO.md (button replaces the CLI step), showtime cheat sheet. Test: `FLOCK_OFFLINE=1 pytest -q tests/test_follow_up_api.py` (590 pass total).
- Sun 02:30 PT — Preet → Thabhelo: NEW `POST /api/connectors` (token) + `GET /api/connectors` on the live API: a CREAO Slack/Gmail watcher posts the connector payload; Flock keeps it, reads it next to the contracts and replies with each claim, its quote, sender and whether that sender is authorized (binding). Your part (CREAO side) is the 'Plan for the live Slack demo' section in docs/connectors.md; test payload: docs/connector_example.json.
- Sun 03:30 PT — Preet: PR #9 (Himaja's frontend) merged. Hosted site has the SHOWTIME demo state (GET /api/showtime = cheat sheet; headline F-6a673b72, DoorDash DD-PO-20260901 short $549.33). POST /api/connectors live and verified: the example Slack batch -> auth_email/binding for S06 (contracts kept on the volume). Himaja: new endpoints for the UI = /api/payouts (+/<id>), /api/stream (+/replay), /api/showtime, /api/connectors, follow-up routes; see docs/API.md. Please don't POST /api/run on the hosted site now (it replaces the demo state).
- Sun 03:50 PT — Preet's agent: STARTED `preet/ui-integration` (on top of origin/himaja/frontend 37e2217): flock/api.py serves frontend/ at / ; Himaja's redesigned dashboard wired to every endpoint (lump sums + waterfall, theater, finding detail + approve/reject/try-as-agent, follow-up, connectors, showtime banner, metrics, scoreboard, market). Owns flock/api.py + frontend/ for this.
- Sun 04:40 PT — Preet's agent: DONE `preet/ui-integration` (not pushed). Himaja's console (frontend/) is the product UI: flock/api.py serves it at / (and /frontend/*, traversal-safe, no auth for GETs); same-origin API when served by the API, ?api= and the Railway default otherwise. Wired: lump sums + waterfall + zoomed gaps + per-store, finding drawer (evidence/authority, votes, memo, case file, audit trail, approve files via Clicky, reject, try-as-agent refused with the logged entry), vendor follow-up card, theater live/replay 1-8x, Sources (connectors), showtime banner, metrics with How we got this, scoreboard + new GET /api/market. Agents can't approve through the human route any more (role agent / agent ids -> 409 + log_entry). Tailwind/Lucide vendored (offline stage). Docs: README UI, DEMO.md clicks, API.md, frontend/README.md, showtime cheat sheet. Test: `FLOCK_TEST_BROWSER=1 pytest -q tests/test_frontend_e2e.py` (screens in results/ui/); 601 pass offline.
