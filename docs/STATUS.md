# Flock status (Sat Oct 24, 2026, late)

One page for teammates and judges: what's built, what's measured, what's simulated, what's open.
Rules and data shapes: [`CONTRACT.md`](../CONTRACT.md). Every command below works with `export FLOCK_OFFLINE=1` and no
API key unless it says otherwise.

## What's built

| Piece | See it |
| --- | --- |
| Full pipeline: collect, format, match, deal terms, check, judge, rank, draft, human, file, journal, learn | `python -m flock.pipeline --reset --forget --demo-approve && python scripts/score.py` |
| Self-improving: a human approves a learned rule, week 2 sends fewer items to a human | `python -m flock.agents.learner --approve R-001 && python -m flock.pipeline --reset --demo-approve && python scripts/score.py` |
| Gatekeeper + append-only audit log (denied attempts too) for findings, journal entries, workflows | dashboard finding page, "Try as an agent"; `data/state/audit_log.jsonl` |
| Generator: traps 1-22 + answer key, company profiles (default, `ca_bagel_chain`) | `python generator/generate.py --profile ca_bagel_chain --weeks 2` |
| Live seed demo (a judge picks the number) | `python -m flock.demo --seed 4821 --profile ca_bagel_chain --weeks 2 --confirm` |
| Year scale: per-store parallel match + check, progress bar | `python -m flock.demo --seed 42 --profile ca_bagel_chain --weeks 52 --confirm` |
| Deal sources: `.eml`, Slack export, `.md` call notes; authority from `authorized_contacts`; thread corrections | `python -m flock.demo --seed 4821 --sources email,slack,notes --confirm` |
| Connector drop folder (for a CREAO Slack/Gmail watcher) | `python -m flock.connectors check payload.json`, `python -m flock.connectors ingest` |
| Anomaly Detector (learned baselines, trap 18) | in every real-data run; findings with `check_type: anomaly` |
| Judge panel: 3 judges, debate round, reviewer memo, 10% audit sample | live with any provider; offline rules otherwise |
| Judge market: Brier-scored bets, balance-weighted votes, attention budget | `python -m flock.market` |
| Live flock: agent status feed, investigators with case files | dashboard "Flock" tab, `GET /api/agents?since=0` |
| Journal Entry Drafter: balanced entries, human approves, gatekeeper posts a GL CSV | dashboard "Journal" tab (real data, week scale) |
| Clicky: files in a FAKE localhost portal; learns the form from one human demonstration; goes STALE when the form changes | `FLOCK_CLICKY_MODE=browser python -m flock.pipeline --reset --demo-approve`; `python -m flock.agents.clicky record` |
| Models per role: local (Ollama/vLLM), Groq, NVIDIA, Claude, CREAO hosted agents, CREAO app (Flock Judge) | `python scripts/llm_check.py` (needs keys / a local server) |
| Air gap: refuses off-network providers, counts every call by host | `eval "$(scripts/gx10.sh env --airgap)"`, then any run; `GET /api/airgap` |
| GX10 tooling: SSH tunnel, model pulls, status; judge load test | `scripts/gx10.sh status`, `python scripts/judge_load.py` |
| Benchmark harness on held-out seeds 101-110 | `python scripts/benchmark.py` |
| Tuned-judge training set (held-out seeds locked out) | `python scripts/judge_dataset.py` |
| SpacetimeDB store (`FLOCK_STORE=spacetime`): 3 tables, 4 reducers, gatekeeper transactions | `spacetime start`, `spacetime publish flock --server local --module-path spacetimedb --yes`, then the Quickstart |
| Dashboard (Flask, one page) and JSON API (stdlib, Railway `Procfile`) | `python -m dashboard.app` (:5050), `python -m flock.api` (:8787) |

## What's measured

| Number | What | Where it comes from |
| --- | --- | --- |
| **6/6 traps, 2 then 0 sent to a human** | Quickstart, week 1 vs week 2 after one approved rule | Quickstart commands above, run Oct 24 on this branch |
| **3,676 / 3,676 traps, 0 false alarms, 173 to a human, ~53 s** | Rules-only benchmark, traps 1-22, seeds 101-110, bagel chain 56 stores x 2 weeks | `python scripts/benchmark.py`, run Oct 24 -> `data/benchmarks/results.json` |
| **1,241,381 orders, 9,655 / 9,655 answer-key rows, 0 false alarms, 342 to a human, 124 s** | A full year of the bagel chain, traps 1-22, offline, on a laptop | `python -m flock.demo --seed 42 --profile ca_bagel_chain --weeks 52 --confirm`, run Oct 24 |
| 55/55, 163/163, 517/517 | Trap 18 (fee creep) caught by the Anomaly Detector: default company 2 weeks; bagel chain 8 stores; default company 1 year | commit 37749ff message |
| p50 88.4 s, p95 126.0 s, 1.48 findings/min, 0 fallbacks | Judge load test: MacBook Air, Ollama `qwen2.5-coder:14b` as all three judges, 6 findings, air-gapped | README "GX10" section (commit a6db0b9) |
| 23 calls, outcomes matched the answer key, ~15 s per call | Fixture pipeline with every AI role on CREAO hosted agents (`claude-haiku-4-5`) | commit 69cea7f message |
| 7/7 agents answer usable JSON | CREAO hosted agents after the `FLOCK_ANSWER:` marker fix | commit d509d0c message |
| recoverable at 0.81 in 78 s, no fallbacks | Flock Judge on agent.creao.ai (`creaoapp`), three judges + debate on F-0001 | README "CREAO track" |
| 472 passed, 2 skipped | `FLOCK_OFFLINE=1 pytest -q` | run Oct 24 on this branch |

How to read the trap counts: the answer key has one row per affected order, so a trap that touches many orders
counts many times (in the year run, `processing_fee_creep` is 7,696 of the 9,655 rows). Every trap type is listed
with its own count in the scoreboard's "by trap" line.

## Simulated vs real

| Simulated | Real |
| --- | --- |
| All company data: orders, platform statements, bank deposits, contracts, rep emails, Slack, call notes. Target company is Noah's NY Bagels (~56 CA stores); the generated files use the made-up brand "Ringtide Bagels". No real Noah's records, people or terms | The code: every check, match, journal line and dollar is computed by it, the same on simulated or real files |
| The dispute portal (`flock/portal.py`, localhost only, labeled SANDBOX) | Browser automation: Playwright really logs in, types and submits in that portal, and records a real demonstration |
| The human in `--demo-approve` (`demo-controller`) | Human decisions from the dashboard / CLI, logged under the person's name |
| Answer keys (made by the generator alongside the data) | Model calls when keys are set: Claude, Groq, NVIDIA, Ollama on the GX10, CREAO hosted agents and the Flock Judge (runs recorded above) |
| The generator's `default` profile (the original 12-store hot chicken company) | The sourced research in `FLOCK_CONTEXT.md` section 6 (job postings, the bakery case, leakage figures) |

## Open items

- **GX10 numbers:** judge load and an air-gapped run with three local models on the GX10 are not recorded in the repo yet (only the laptop run above).
- **Benchmark contestants beyond rules:** Claude, local, local-tuned, CREAO and the human contestant have no recorded results yet.
- **Tuned judge:** step 1 (training set) is done; LoRA training, serving and scoring are not (`scripts/train/` named in `docs/PLAN.md` does not exist).
- **SpacetimeDB:** done on `thabhelo/spacetime`: module in `spacetimedb/`, `SpacetimeStore` over the HTTP API, the Quickstart prints 6/6 then 2 -> 0 with `FLOCK_STORE=spacetime` (local server). Also published to Maincloud (`flock` under @thabhelo) and the Quickstart passed against it. Moving the sidecar files (market ledger, runs, journal) into tables is open.
- **Frontend:** Himaja's frontend folder is not merged; `dashboard/` is the UI that runs today.
- **Hosting:** Railway config is ready (`Procfile`, `docs/DEPLOY.md`); no public URL is recorded in the repo.
- **Journal entries at year scale:** skipped (split detection keeps no order stories); run a week at a time.
- **Owners to confirm:** Ira on pitch / video / Devpost.
- **Pitch fact:** a sourced revenue figure for Noah's NY Bagels before quoting one on stage.
- Code/doc mismatches found while syncing the docs are listed in the PR / report for this change, not fixed here.
