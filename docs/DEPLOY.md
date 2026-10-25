# Deploying Flock (Railway)

Two services can run from this repo. Both use simulated data only.

| Service | Start command | What it is |
| --- | --- | --- |
| **api** (default, `Procfile`) | `python -m flock.api --host 0.0.0.0` | Thabhelo's JSON API: findings, log, rules, score, agents, portal, `POST /api/run`. For CREAO agents and integrations. |
| **dashboard** (optional) | `python -m dashboard.app --host 0.0.0.0` | The full UI + its API, for judges. Its approve/reject buttons have no login, so only deploy it when that's acceptable for the demo. |

Both read `PORT` from Railway.

## Variables

| Variable | Why |
| --- | --- |
| `FLOCK_API_TOKEN` | **Required on any public deploy.** Every POST to the api must send `Authorization: Bearer <token>`. Reads stay open. |
| `FLOCK_CORS_ORIGIN` | Narrow CORS to the dashboard's origin (default `*`). |
| `FLOCK_OFFLINE=1` | Offline rules for every AI role until model keys are set. |
| `CREAO_API_KEY`, `FLOCK_*_PROVIDER=creao` | Run the AI roles as hosted CREAO agents (see README, "Run every AI role on CREAO"). |
| `ANTHROPIC_API_KEY` | Claude as reviewer (optional). |

## Persistent state

Findings, the audit log, the market ledger and agent status live in `data/state/`. Mount a Railway volume at
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
     -d '{"reset": true, "demo_approve": true}' https://<domain>/api/run   # first run fills the state
```
