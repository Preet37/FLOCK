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
| API docs for the frontend | `docs/API.md` (landing soon) |
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
| `preet/frontend-api` | Preet's agent | `/api/transactions`, risk score, `/api/metrics`, `/api/agents/<id>`, background `/api/run` jobs, `/api/health`, `docs/API.md` |
| `preet/release-qa` | Preet's agent | end-to-end release tests, QA report, demo-day checklist |
| `preet/gx10-run` | Preet's agent | GX10 models, speed + benchmark tables, fine-tuned judge, `docs/gx10_results.md` |
| `preet/docs-sync` | Preet's agent | CONTRACT.md / README / FLOCK_CONTEXT / STATUS.md synced to the code (done, merging) |

## Status log (append below; newest last)

- Sat 22:30 PT — Preet: Railway live with CREAO judges (verified: F-0001 judged by 3 CREAO agents, 0 fallbacks). Long `/api/run` requests hit Railway's proxy timeout (502) while the run continues server-side; fix (background jobs) coming in `preet/frontend-api`.
- Sat 22:30 PT — Preet: COORDINATION.md created. Everyone: follow "The loop" above.
