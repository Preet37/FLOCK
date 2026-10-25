# Deploying Flock (Railway)

Two services can run from this repo. Both use simulated data only.

| Service | Start command | What it is |
| --- | --- | --- |
| **api** (default, `Procfile`) | `python -m flock.api --host 0.0.0.0` | The JSON API (findings, log, rules, score, agents, portal, payouts, theater, `POST /api/run`) **and the console** (`frontend/`) at `/`: the hosted URL's root is the UI judges use. POSTs need the token. |
| **dashboard** (optional backup) | `python -m dashboard.app --host 0.0.0.0` | The older Flask UI + its API. Its approve/reject buttons have no login, so only deploy it when that's acceptable for the demo. |

Both read `PORT` from Railway.

## Variables

| Variable | Why |
| --- | --- |
| `FLOCK_API_TOKEN` | **Required on any public deploy.** Every POST to the api must send `Authorization: Bearer <token>`. Reads stay open. |
| `FLOCK_CORS_ORIGIN` | Narrow CORS to the dashboard's origin (default `*`). |
| `FLOCK_OFFLINE=1` | Offline rules for every AI role until model keys are set. |
| `CREAO_API_KEY`, `FLOCK_*_PROVIDER=creao` | Run the AI roles as hosted CREAO agents (see README, "Run every AI role on CREAO"). |
| `ANTHROPIC_API_KEY` | Claude as reviewer (optional). |
| `FLOCK_JUDGE_CONCURRENCY` | Findings judged at once (default 1 offline, 6 with a live judge). CREAO pacing (`FLOCK_CREAO_RPM`) still applies across threads; results are the same as one at a time. Hosted demo: `6`. |
| `FLOCK_LIVE_JUDGE_BUDGET` | Live judges for only the N riskiest findings per run; the rest use offline rules and say so on each finding (default: all). Hosted demo: `25` (a 78-finding CREAO job goes from ~2h to minutes). See docs/API.md, "Judging speed and the live-model budget". |
| `FLOCK_HOSTED_MAX_STORES`, `FLOCK_HOSTED_MAX_WEEKS` | Caps for `POST /api/run {"dataset": "demo"}` (default 56 stores, 8 weeks). |
| `FLOCK_CREAO_USD_PER_CREDIT` | Optional. Without it `GET /api/metrics` reports CREAO credits but no dollars for them. |
| `FLOCK_HUMAN_MINUTES_PER_STORE_WEEK` | Optional override of the human baseline in `GET /api/metrics` (labelled ASSUMPTION; default 60, cited in docs/API.md). |

Health check path: `/api/health` (set it in Railway's service settings). The frontend's reference is `docs/API.md`.

## Persistent state

Findings, the audit log, the market ledger, agent status, `transactions.json`, `last_run.json` (metrics) and a
dataset run's `answer_key.csv` live in `data/state/`. Mount a Railway volume at
`/app/data/state` so they survive restarts and redeploys. (Don't mount over `/app/data`: it would hide
`data/answer_key.csv`.) Learned rules (`rules.json`) and the sandbox portal file are not persistent on the host.

## Commands

```bash
railway login
railway init                         # new project "flock"
railway up                           # build + deploy from this folder
railway variables --set FLOCK_API_TOKEN=<random> --set FLOCK_OFFLINE=1
railway volume add --mount-path /app/data/state
railway domain                       # public https URL
curl https://<domain>/api/findings   # reads are open
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     -d '{"reset": true, "demo_approve": true}' "https://<domain>/api/run?wait=0"   # first run fills the state (a job; poll /api/jobs/<id>)
# real volume: generated data (simulated chain), a background job; poll the job until "done"
curl -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
     -d '{"dataset": "demo", "seed": 2025, "profile": "ca_bagel_chain", "stores": 56, "weeks": 8}' https://<domain>/api/run
curl https://<domain>/api/jobs/<job_id>
python scripts/smoke_hosted.py https://<domain>                                        # every GET, pass/fail table
python scripts/smoke_hosted.py https://<domain> --token-file ~/.flock_token --run     # + a dataset run, polled
```

The dataset run auto-confirms the unseen statement layout (no terminal on the host to answer y/N) and keeps its
answer key next to the state; the next fixtures run puts the fixtures key back. Jobs are in memory: a restart forgets
the job list, not the results.
