# Flock

Flock does the boring job a restaurant pays an accountant to do by hand: it takes the messy money reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, sorts them into one place, and checks them, faster and cheaper than a person can.

> **Team:** [`docs/STATUS.md`](docs/STATUS.md) is the one page on what's built, measured and simulated.
> [`docs/PLAN.md`](docs/PLAN.md) has the plan to submission (Sun Oct 25, 9 AM), the demo script and who's on what.

**The company: Noah's NY Bagels**, about 56 locations across California, a mid-market chain. All data in this repo is **simulated** (`--profile ca_bagel_chain`); no real Noah's records or contract terms are used.
- **What:** the stores get paid by delivery apps (DoorDash, Uber Eats, Grubhub) and their own ordering channel, and each one sends money and reports in a different, messy way.
- **Why:** each app has its own deal (commission tiers, promos, refund rules), and none of them talk to each other or to the register, bank or contracts.
- **When:** every week, when the payouts land as lump sums that never match what the stores actually sold.
- **Where:** on a staff accountant's desk, stuck in app portals, the register, the bank and a pile of spreadsheets.
- **Why it matters:** someone gets paid to sort it all by hand. It's slow, and mistakes mean the company loses money it already earned.

**On this page:** [Quickstart](#quickstart-works-right-now-no-api-key-needed) ·
[Models](#models-local-first-claude-as-reviewer) · [GX10 + air gap](#gx10-local-models-over-tailscale-nothing-changed-on-the-box) ·
[CREAO track](#creao-track-the-agent-built-on-agentcreaoai) · [CREAO for every role](#run-every-ai-role-on-creao) ·
[Judge market](#judge-market-bets-that-settle-against-reality-human-attention-as-a-budget) ·
[Live flock + investigators](#the-flock-live-agents-each-with-a-job) · [Data + deal sources](#data-generator---collector---formatter) ·
[Anomaly Detector](#anomaly-detector-learned-not-written) · [Benchmark](#benchmark-the-proof-table) ·
[Tuned judge](#tuned-judge-the-training-set-step-1) · [Live seed demo](#live-seed-demo-a-judge-picks-the-number) ·
[Clicky + learning](#clicky-demo-agent-files-disputes-in-a-sandbox-portal) · [Deploy](#deploy-railway) ·
[Pipeline + journal](#pipeline) · [Owners](#who-owns-what-updated-saturday-oct-24-details-in-docsplanmd) ·
[Rules](#rules-for-everyone) · [Repo map](#repo-map)

## Read first

| File | What's in it |
| --- | --- |
| `CONTRACT.md` | **Build to this.** The rules, folders, every table and row shape, the state machines (findings, journal entries, workflows) with who may do each move, the deal-term rule, check types, traps 1-22. |
| `docs/STATUS.md` | One page: what's built (and the command to see it), what's measured, what's simulated vs real, open items. |
| `FLOCK_CONTEXT.md` | Background: problem, money flow, sourced research, numbers, pitch, agents, edge cases, team split, demo plan. Paste it into Claude at the start of a session. |
| `docs/PLAN.md` | The plan: benchmark design, timeline, demo script, who's on what. |
| `docs/connectors.md` | How Slack, Gmail and call notes get into Flock (drop-folder JSON contract for a CREAO connector). |
| `docs/DEPLOY.md` | Hosting the API (and optionally the dashboard) on Railway. |
| `docs/API.md` | The deployed JSON API for the frontend: every endpoint, real example responses, auth, CORS, which panel each feeds, how the metrics are computed. |
| `docs/benchmark_schema.md` | Result file schema for the judge load test. |
| `dashboard/README.md` | Dashboard screens and its API. |
| `docs/hyphenate-track.md`, `docs/sponsor-tracks.md` | What the judges want; sponsor rules and the submission checklist. **Submit by Sun Oct 25, 9:00 AM.** |
| `docs/TEAM_BRIEF.md` | The Discord team brief. `docs/pipeline-sketch.png`: the original hand-drawn pipeline. |

## Quickstart (works right now, no API key needed)

```bash
python3 -m venv .venv && source .venv/bin/activate          # Railway runs Python 3.12
pip install -r requirements.txt
export FLOCK_OFFLINE=1                                      # offline rules: fast, free, deterministic

python -m flock.pipeline --reset --forget --demo-approve    # WEEK 1: day one, no learned rules
python scripts/score.py                                     # caught 6/6, 2 sent to a human
python -m flock.agents.learner --approve R-001              # human approves the rule the Learner proposed
python -m flock.pipeline --reset --demo-approve             # WEEK 2: same traps, 0 sent to a human
python scripts/score.py                                     # also prints Week 1: 2 | Week 2: 0
FLOCK_OFFLINE=1 pytest -q                                   # all tests (offline, models mocked)
python -m dashboard.app                                     # dashboard at http://127.0.0.1:5050
```

Dashboard details (screens, API, the live `--pace` demo) are in `dashboard/README.md`.
Storage is picked by `FLOCK_STORE` through `flock.store.get_store()`: `json` (default, `data/state/`); `spacetime` is a stub that raises until SpacetimeStore is written.
The Quickstart runs on `fixtures/fake_findings.json` (6 hand-written findings). The company story is Noah's NY Bagels, simulated with `--profile ca_bagel_chain` (see Data and Live seed demo); the generator's `default` profile is still the original 12-store hot chicken company.

That week 1 vs week 2 drop is the "self-improving" demo moment.
With nothing set (or `FLOCK_OFFLINE=1`) everything runs on offline rules, exactly as above.

## Models (local first, Claude as reviewer)

The judges, the dispute writer and the column mapper can each run on a different provider. All of them speak
the OpenAI chat API over plain HTTP (no extra dependency), except Claude:

| Provider | What | Env |
| --- | --- | --- |
| `local` | vLLM or Ollama on our NVIDIA GX10 (or a laptop) | `FLOCK_LOCAL_URL` (default `http://127.0.0.1:11434/v1`), `FLOCK_LOCAL_MODEL` (default `llama3.1:8b`), `FLOCK_LOCAL_API_KEY` (optional) |
| `groq` | Groq cloud | `GROQ_API_KEY`, `FLOCK_GROQ_MODEL` (default `llama-3.3-70b-versatile`) |
| `nvidia` | build.nvidia.com | `NVIDIA_API_KEY`, `FLOCK_NVIDIA_MODEL` (default `meta/llama-3.3-70b-instruct`), `FLOCK_NVIDIA_URL` |
| `anthropic` | Claude | `ANTHROPIC_API_KEY`, `FLOCK_MODEL` (default `claude-sonnet-5-5`) |
| `creao` | CREAO Developer Platform /v2: every AI role as a hosted CREAO agent (see below) | `CREAO_API_KEY` (project key, USD wallet), `creao_agents.json` from `scripts/creao_setup.py --create`, optional `FLOCK_CREAO_LLM`, `FLOCK_CREAO_RPM`, `FLOCK_CREAO_MODE=async` |
| `creaoapp` | An agent built on agent.creao.ai (the Flock Judge, CREAO track) through its app API Trigger | `CREAO_APP_KEY` (`capi_...`), app id from `creao_agents.json` (`apps`), optional `FLOCK_CREAOAPP_URL` |
| `offline` | never call anything | |

| Role | Env | Default |
| --- | --- | --- |
| Judges | `FLOCK_JUDGE_PROVIDERS` comma list in panel order (strict, operator, skeptic), e.g. `local,local,groq`; one entry = all three. `FLOCK_JUDGE_MODELS` optionally gives each judge its own model | `FLOCK_PROVIDER` |
| Reviewer | `FLOCK_REVIEWER_PROVIDER`, `FLOCK_REVIEWER_MODEL` | `anthropic` |
| Dispute writer | `FLOCK_WRITER_PROVIDER`, `FLOCK_WRITER_MODEL` | `FLOCK_PROVIDER` |
| Column mapper | `FLOCK_MAPPER_PROVIDER`, `FLOCK_MAPPER_MODEL` | `FLOCK_PROVIDER` |
| Document reader (Deal Keeper: PDF text, free-text emails) | `FLOCK_READER_PROVIDER`, `FLOCK_READER_MODEL` | `FLOCK_PROVIDER` |

`FLOCK_PROVIDER` defaults to `anthropic`, which is offline without a key. So setting only `ANTHROPIC_API_KEY`
still means "Claude everywhere", as before. A provider without its key falls back to offline rules.
`FLOCK_LLM_TIMEOUT` is the per-call timeout in seconds (default 30). `FLOCK_AIRGAP=1` refuses any provider off this network (see GX10 below). If a call fails or times out, that judge
falls back to the offline rules and the finding's reason says so. Each live vote records the `model` that cast it.

How a live panel decides:
1. Round 1: three judges vote independently, in parallel.
2. If they disagree: one debate round. Each judge sees the other two votes and reasons and may revise. Both rounds stay in `votes` (`round: 1`, `round: 2`).
3. Still split: the finding is a `conflict` and goes to a human. The reviewer does **not** break the tie; it writes `review_memo` on the finding (each side, and what evidence would settle it).
4. Audit sample: `FLOCK_AUDIT_RATE` (default `0.1`, picked by a hash of `finding_id`, so the same finding is always in or out) of the findings the panel decided on its own get a reviewer second opinion. If the reviewer disagrees, the finding becomes a `conflict`, the reason starts with "Audit sample: ...", and a human decides.

Offline the debate and the reviewer do nothing. Approved rules, the tolerance and weak-evidence guardrails, and
"agents propose, humans approve" all still apply on top of any model.

## GX10 (local models over Tailscale, nothing changed on the box)

The GX10's Ollama listens on `127.0.0.1:11434` only and the machine is shared, so we don't reconfigure it: an SSH
tunnel maps it to `127.0.0.1:11435` on the laptop. [`scripts/gx10.sh`](scripts/gx10.sh) does the plumbing
(`ssh gx10` alias, see the top of the script for knobs):

```bash
scripts/gx10.sh tunnel                  # terminal 1: keeps ssh -N -L 11435:127.0.0.1:11434 gx10 up, reconnects on drop
eval "$(scripts/gx10.sh env)"           # judges = gpt-oss:20b, qwen3:30b, gemma3:27b; writer + mapper local
eval "$(scripts/gx10.sh env --airgap)"  # same + local reviewer + FLOCK_AIRGAP=1 (the Wi-Fi-off demo)
scripts/gx10.sh status                  # tailscale line for gx10, tunnel up?, models on disk (/api/tags), loaded (/api/ps)
scripts/gx10.sh pull small              # ollama pull ON the GX10 under nohup, log ~/flock-pulls.log: qwen3:8b gemma3:12b llama3.1:8b
scripts/gx10.sh pull large              # gpt-oss:20b qwen3:30b gemma3:27b  (scripts/gx10.sh log to watch)
python scripts/llm_check.py             # pings every configured route: latency, which model answered, models served
```

**Air gap (`FLOCK_AIRGAP=1`).** `flock/llm.py` then only calls providers whose URL host is on this network:
loopback, private ranges, link-local, Tailscale (`100.64.0.0/10`, `*.ts.net`, or a one-word MagicDNS name that
resolves there). Anthropic, Groq and NVIDIA's hosted API become offline with a reason ("FLOCK_AIRGAP=1: groq would
call api.groq.com, which is off this network") and their roles use offline rules. Every model call is counted by host,
air-gapped or not; the pipeline prints `air-gapped: N model calls, 0 left this network` (or names the hosts that were
called) and saves it to `data/state/airgap.json`; the dashboard serves it at `GET /api/airgap` (also in `/api/meta`).
That's the evidence behind "Wi-Fi off": a count, not a claim.

**Judge load test.** [`scripts/judge_load.py`](scripts/judge_load.py) pushes N findings through the real panel at a
chosen concurrency and writes `results/judge_load_<provider>_<timestamp>.json` (p50 / p95 seconds per finding,
findings/min, per-judge model + latency, fallbacks, offline, cost). Schema for the benchmark:
[`docs/benchmark_schema.md`](docs/benchmark_schema.md). `results/` is gitignored.

```bash
python scripts/judge_load.py --provider local --models qwen2.5-coder:14b --n 6 --concurrency 3   # laptop Ollama
eval "$(scripts/gx10.sh env --airgap)"; python scripts/judge_load.py --n 60 --concurrency 8      # GX10, 3 models
python scripts/judge_load.py --source seed --seed 4821 --n 40     # findings from a fresh generated seed (temp dir)
FLOCK_OFFLINE=1 python scripts/judge_load.py --n 100              # rules-only baseline
```

First real number (MacBook Air, Ollama, `qwen2.5-coder:14b` as all three judges, 6 fixture findings, concurrency 3,
air-gapped): p50 88.4s, p95 126.0s per finding, 1.48 findings/min, 21 model calls all to 127.0.0.1, 0 fallbacks.
A laptop serializes the requests; the GX10 numbers go next to it.

Serving with vLLM instead (OpenAI-compatible on `:8000`): point `FLOCK_LOCAL_URL` at it (tunnel or the Tailscale
address) and set `FLOCK_LOCAL_MODEL` / `FLOCK_JUDGE_MODELS` to the ids it reports.

## SpacetimeDB as the store

`FLOCK_STORE=spacetime` swaps the JSON files for SpacetimeDB with no other change: findings, the append-only audit log
and the live agent feed become three public tables in `spacetimedb/src/lib.rs`, and every write is a reducer call,
so a finding change and its audit entry land in one transaction (`commit`). Reads are SQL over SpacetimeDB's HTTP
API; nothing else in Flock touches storage. Sidecar files (market ledger, run history, journal entries) stay on disk
under `data/state/spacetime/` for now.

```bash
curl -sSf https://install.spacetimedb.com | sh                       # once
spacetime start                                                      # local server, leave it running
spacetime publish flock --server local --module-path spacetimedb --yes
export FLOCK_STORE=spacetime FLOCK_OFFLINE=1
python -m flock.pipeline --reset --forget --demo-approve && python scripts/score.py   # 6/6, 2 to a human
python -m flock.agents.learner --approve R-001
python -m flock.pipeline --reset --demo-approve && python scripts/score.py            # 6/6, 0 to a human
python -m dashboard.app                                              # same dashboard, reading SpacetimeDB
spacetime sql flock "SELECT finding_id, state FROM finding" --server local
```

Maincloud: the module is published there as `flock` under Thabhelo's account (https://spacetimedb.com/@thabhelo/flock, 3 tables, 4 reducers). Point the store at it with `SPACETIME_URL=https://maincloud.spacetimedb.com` and
`SPACETIME_TOKEN=<your CLI token>` (`spacetime login`, then `spacetime login show --token`; never commit it). The
Quickstart ran against Maincloud on Oct 25: 6/6, 2 then 0 sent to a human, rows visible with `spacetime sql flock
"SELECT finding_id, state FROM finding" --server maincloud`. Tests: `tests/test_spacetime.py` runs against an in-process
fake; `FLOCK_SPACETIME_LIVE=1` runs the same checks against the real server. JSON stays the default, so the hosted
site is unaffected until someone flips the variable there.

## CREAO track: the agent built on agent.creao.ai

The track's one requirement: at least one CREAO agent built on agent.creao.ai does the core work of the project, its
agent ID and a share link to one of its runs are in the README, and it runs in the demo. Ours is the judge.

| | |
| --- | --- |
| Agent | **Flock Judge**, built on agent.creao.ai (team: Thabhelo's team, thabhelo.duve@talladega.edu) |
| Agent ID (app id) | `9f48f380-fcd8-44d3-8387-ab9f85cd1c50` |
| Agent page | https://agent.creao.ai/apps/9f48f380-fcd8-44d3-8387-ab9f85cd1c50 |
| Run share link | https://agent.creao.ai/share/db6b2917902c5799ac9fee5906942a58 (expires 2026-11-23; a judge run of the strict auditor on F-0001) |
| What it does | The panel's three judges (strict auditor, operator, skeptic) each run as this agent with their own persona prompt; it returns the `{outcome, confidence, reason}` verdict the gatekeeper records |
| How Flock calls it | provider `creaoapp` in `flock/llm.py`: app-scoped API Trigger, `POST /api/v1/apps/{appId}/runs` with `inputs: {instructions, request}`, polled until completed, paced to the key's 30 requests a minute |
| Switch it on | `export CREAO_APP_KEY=capi_... FLOCK_JUDGE_PROVIDERS=creaoapp` and `unset FLOCK_OFFLINE` (the app id is read from `creao_agents.json`) |
| First real run | three judges plus a debate round on fixture finding F-0001: recoverable at 0.81 in 78 s, no fallbacks |

The seven hosted agents on the Developer Platform (next section) are the same roles on the API side; the track
requirement is met by this one, which lives in the chat product where the judges can open it.

## Run every AI role on CREAO

[CREAO](https://creao.ai) hosts agents created from a plain-language description and runs them through its Developer
Platform `/v2` (`developer.creao.ai`, project keys, a USD wallet). `scripts/creao_setup.py` creates one hosted CREAO
agent per Flock AI role (the three judges, reviewer, dispute writer, column mapper, document reader) and the `creao`
provider in `flock/llm.py` runs them. Flock keeps its prompts and all money math in its own code: each agent is created
with one contract, "treat `instructions` as your system prompt, answer `request`, reply with only the JSON asked for",
so the audit trail, the gatekeeper and the offline fallback are unchanged.

```bash
export CREAO_API_KEY=cr_sk_...                      # PROJECT key: Developer Console > API key. Fund the USD wallet first.
python scripts/creao_setup.py --create              # 7 agents (NL create, polled); writes creao_agents.json
python scripts/creao_setup.py --check               # one tiny run per agent: reply, tokens, estimated USD
python scripts/creao_setup.py --env                 # the export lines below, filled in
export FLOCK_JUDGE_PROVIDERS=creao FLOCK_REVIEWER_PROVIDER=creao FLOCK_WRITER_PROVIDER=creao \
       FLOCK_MAPPER_PROVIDER=creao FLOCK_READER_PROVIDER=creao
unset FLOCK_OFFLINE
python -m flock.pipeline --reset --forget            # the whole flock judged, drafted and read on CREAO
python scripts/benchmark.py --contestants rules,creao   # CREAO on the proof table, USD per 1,000 findings
```

How a call works: `POST /v2/realtime/runs` with `{agent_id, input: {instructions, request}}`; the reply is the NDJSON
stream's text and `usage.models` (per-model tokens) is turned into USD with the console's rates (`llm.creao_usage()`).
If realtime is refused the provider polls `POST /v2/runs` + `GET /v2/runs/{id}` (`FLOCK_CREAO_MODE=async` forces it).
CREAO allows 30 run creates per account per minute, so the provider paces itself to `FLOCK_CREAO_RPM` (default 28) and
waits for `Retry-After` on a 429: a 42-finding week is about five minutes of judging. `FLOCK_CREAO_LLM` picks the
catalog model the agents run on (default `anthropic/claude-sonnet-5-5`; `anthropic/claude-haiku-4-5` is the cheap one).
A role whose agent is missing stays on offline rules and says why. `FLOCK_AIRGAP=1` keeps CREAO offline like any other
internet host. `FLOCK_CREAO_API=v1` switches to the CLI routes for an account key.

## Judge market (bets that settle against reality, human attention as a budget)

Each judge's vote is a bet: a forecast p = P(recoverable) ('conflict' = 0.5, no lean). Bets settle when ground truth
arrives through the gatekeeper: human approve = recoverable, human reject = correct, RECOVERED (the platform paid) =
recoverable for any bet still open. Payout is Brier-scored, `10 x (1 - 4(p - y)^2)`: up to +10 for a confident right
call, 0 for "a human should decide", down to -30 for a confident wrong one, so honest confidence pays best. Judges start
at 100 credits; the panel weights each judge's confidence by its balance share (unanimity and the guardrails still apply).
Balances persist across runs and reset with `--forget`. Each run, conflicts are ordered by `|gap| x (1 - weighted
confidence)`; past `FLOCK_ATTENTION_BUDGET` (default 10) they're marked deferred: still conflicts, still waiting for a person.

```bash
python -m flock.market                     # leaderboard, open bets, attention used / deferred
python -m flock.market --recover F-0001    # a human marks a filed dispute paid; open bets settle
FLOCK_ATTENTION_BUDGET=1 python -m flock.pipeline --reset --demo-approve   # see F-0004 deferred, not decided
```

Ledger: `data/state/market_ledger.jsonl` (append-only; balances are derived from it). Dashboard: the "Judge market" tab.

## The flock (live agents, each with a job)

Every agent writes spawn / working / done events to an append-only feed through the store
(`store.agent_event()`, `data/state/agent_status.jsonl`; `flock/agents_live.py`): a **worker** per store (match +
check), the deposit **checker**, a **judge** panel per finding, a **writer** per drafted dispute, **Clicky**, the
**journal** drafter and the **learner**. An **investigator** sub-agent is spawned only when a finding can't be
explained: a deposit or missing-order gap with a single source behind it (child of the store's worker), or a conflict
sent to a human with no review memo (child of its judge). It gathers the payout's other lines, nearby bank deposits,
the store's history for that check and every deal claim for the term (binding or not), and attaches a short
`case_file` through the gatekeeper (`investigate`, an annotation that may only write `case_file`: it never changes an
outcome or a dollar). Summary by the `writer` model route when configured, else a template. At most
`FLOCK_INVESTIGATORS` (default 4) run at once. Past 150 agents of one role the rest are pooled into one counter, so a
year of data stays a few hundred feed lines. Dashboard: the "Flock" tab (`GET /api/agents?since=<cursor>`), and the
case file on a finding's page.

## Data (generator -> collector -> formatter)

```bash
export FLOCK_OFFLINE=1
python generator/generate.py --mini                   # first milestone: 1 store x 1 day, traps 3, 7, 16
python generator/generate.py --seed 42 --stores 12 --weeks 2     # full run, traps 1-18 -> generator/out/ (19-22 need --sources slack)
python -m flock.agents.collector                      # copy generator/out/* untouched into data/raw/<source>/ + manifest.csv
python -m flock.agents.formatter                      # data/raw -> data/std/*.csv; asks y/N for the unseen layout (trap 2)
python -m flock.agents.formatter --confirm            # same, accepting proposed mappings without asking
python generator/generate.py --profile ca_bagel_chain --weeks 52   # another (simulated) company, a full year
```

- Same `--seed` and args = byte-identical files. `--traps 3,7,16` picks traps, `--days N` overrides `--weeks`. Traps 2 and 5 need `--weeks 2`.
- **Company profiles** (`profiles/*.json`, `--profile NAME`): name, stores and cities, channels, orders per store per day by channel, menu / average ticket, weekday + monthly seasonality, dayparts, catering, and the contract terms per channel. `default` is the original 12-store company (same bytes as before profiles existed). `ca_bagel_chain` is a **fictional, simulated** 56-store California bagel chain (morning-heavy, weekend peaks, catering orders; ~1.2M orders a year); no real company, people or contract terms. `--stores` defaults to the profile's store count.
- **Traps across a year:** each trap is planted `max(1, round(trap_rate x weight x store_weeks / 100))` times, spread across the period (`--trap-rate`, default 2 per 100 store-weeks; a 2-week demo still gets exactly one of each, 12 stores x 52 weeks gets 12 of most, 56 x 52 gets 58). Waiver traps 14/15 are weighted lower, the unseen layout (2) happens once. The answer key scales with it.
- **Year scale** (offline, laptop, full chain `generate -> collect -> format -> pipeline -> score`, all traps caught, 0 false alarms): 12 stores x 2 weeks (3.2k orders) **2s**; 12 x 52 weeks (83k orders) **9s**; bagel chain 56 x 52 weeks (1.24M orders, 2.5M std rows): **124s** with traps 1-22 (9,655/9,655 answer-key rows, 342 sent to a human; `python -m flock.demo --seed 42 --profile ca_bagel_chain --weeks 52 --confirm`, Oct 24). Before traps 18-22 existed the same run was 84s with 1,126 rows. The formatter streams and converts column-wise, the pipeline splits the std tables by store and runs Matcher + Checkers in parallel processes (`flock/scan.py`, `FLOCK_WORKERS`), deal terms are cached per store/day, and the JSON store batches writes. The pipeline reuses `data/std/` when the raw files and mappings haven't changed.
- **Progress:** the formatter and pipeline draw a terminal progress bar (stage, done/total, rows/sec) and write `data/state/progress.json`; the dashboard's live board shows it (`GET /api/progress`).
- A statement whose layout nobody has confirmed yet is skipped, and "sold, never paid" for that channel is held back (not disputed) until a human says yes, because its orders were never read.
- The answer key goes to `generator/out/answer_key.csv`. `data/answer_key.csv` (the demo scoreboard) is only overwritten with `--write-answer-key`; restore it with `git checkout data/answer_key.csv`. The collector never copies the answer key.
- `data/std/` gets `orders.csv`, `payout_lines.csv`, `bank_deposits.csv` (CONTRACT.md columns; payout lines add `promo_total, promo_code, placed_at`), plus `error_charges.csv`, `refunds.csv`, `payout_summaries.csv`, `duplicates_removed.csv`.
- Column maps live in `mappings.json`. Unknown layouts get a proposed mapping (Claude, or a header-name heuristic offline) that a human confirms once; then it's saved and automatic. Run with `--mappings /tmp/m.json` if you want to rehearse the demo moment without touching the committed file.
- `data/raw/` and `data/std/` are gitignored. To get back to a fixtures-only Quickstart: `rm -rf data/raw/*/ data/raw/manifest.csv data/std/*.csv`.
- **Deal changes scattered across email, Slack and calls** (`--sources email,slack,notes`, on by default for
  `ca_bagel_chain`; the default profile stays JSON-only and byte-identical): emails as `.eml` (with a quoted reply
  chain), a real-layout Slack export in `slack/` (shared `#<platform>-partnership` channels with the platform's reps,
  `#ops-promos`, threads, edits, reactions, noise), meeting notes in `notes/*.md`, and contracts that list
  `authorized_contacts` (emails and `@handles`). The Deal Keeper reads all of them with one authority rule: the sender
  is matched against that list (on it = binding; same company, not on it = conflict; unknown = conflict). Every claim
  keeps an exact quote (asserted verbatim) and a locator (Slack `ts`, email/note line); in a thread the latest binding
  message wins. Traps 19-22 live only in Slack: an authorized waiver (recoverable), an unauthorized promise (conflict),
  and a thread whose second message moves the waiver (new day recoverable, retracted day correct). Outside connectors
  (a CREAO agent watching Slack/Gmail) hand off through a drop folder: [`docs/connectors.md`](docs/connectors.md),
  `python -m flock.connectors ingest`. `python -m flock.demo --seed 4821 --sources email,slack,notes --confirm`.

## Anomaly Detector (learned, not written)

`flock/agents/anomaly.py` runs next to the Checkers on the same stories. For each store x channel it fits the normal
fee line (`other_fees ~ a x gross + b`), commission rate and payout timing from earlier weeks, then flags a later week
whose orders break the pattern by more than the baseline noise. It only watches a metric when **no deal term covers
it**: a fee or commission term makes that the Checkers' job, so nothing is reported twice. Findings carry
`check_type: anomaly`, cite the baseline and the offending rows, and go through the panel like everything else.

The generator's trap 18, `processing_fee_creep`, is the proof: the direct app's processor quietly raises its rate for one
store-week. Its contract states a percentage plus a fixed amount, so the Deal Keeper has no flat fee to compare and no
rule fires. The scoreboard shows the detector catching every creeping order (2 weeks: 55/55 on the default company,
year scale through the parallel path too) with no innocent order flagged.

## Benchmark (the proof table)

`scripts/benchmark.py` runs every contestant on the SAME findings from held-out seeds and writes one results table.
Prepare (generate, collect, format, match, check) is all code, cached per seed under `data/benchmarks/seeds/`; only the
panel differs between contestants. A live contestant with no key is recorded as skipped, never silently run on the rules.

```bash
export FLOCK_OFFLINE=1
python scripts/benchmark.py                                        # rules only, seeds 101-110, bagel chain, 2 weeks (~50s)
python scripts/benchmark.py --contestants rules,claude,local       # Claude needs ANTHROPIC_API_KEY; local needs FLOCK_LOCAL_URL
python scripts/benchmark.py --contestants local-tuned --tuned-model <id the server reports>
python scripts/benchmark.py --human sheets/ira.csv --human-scope 101:S07:1 --human-minutes 95
python scripts/benchmark.py --list
```

Latest rules-only run (Oct 24, traps 1-22, seeds 101-110, bagel chain 56 stores x 2 weeks): **3,676/3,676 traps,
0 false alarms, 173 sent to a human, ~53s**.

Metrics per contestant: traps caught, false alarms, correct dollars, sent to a human, p50 / p95 seconds per finding,
findings per minute, estimated $ per 1,000 findings (tokens ~ chars / 4 at list prices, `--price model=in,out`),
works offline, and for the human contestant minutes per store-week. Training seeds for a tuned judge must stay out of
`--seeds`.

Output for the dashboard's Benchmark tab: `data/benchmarks/results.json` with `current[<contestant>]` (latest aggregate
per contestant: `label, status, note, caught, total, accuracy, false_alarms, correct_dollars, to_human, p50_s, p95_s,
per_minute, cost_per_1000, offline, fallbacks, human_minutes_per_store_week, seeds, run_id, at, config`) and `runs[]`
(every run ever, with `per_seed[<contestant>]` rows that carry each trap's `got` vs `expected_outcome`).
`data/benchmarks/results.csv` has the same rows flat, one per contestant and seed plus a `seed=all` aggregate.

## Tuned judge: the training set (step 1)

`scripts/judge_dataset.py` turns generated seeds into the exact conversations the panel has at inference: each judge's
persona plus the panel system prompt, the finding payload the panel sends (no ids, so nothing about the seed leaks), and
the `{outcome, confidence, reason}` JSON the panel parses, labelled from that seed's answer key. One example per finding
per judge, split by seed into `data/training/judge_train.jsonl` and `judge_val.jsonl`, with a `manifest.json`.

```bash
export FLOCK_OFFLINE=1
python scripts/judge_dataset.py                       # seeds 1-50 (val 46-50), bagel chain, 2 weeks, ~5 min
python scripts/judge_dataset.py --seeds 101-105       # refused: the benchmark's held-out seeds are locked
```

The benchmark's held-out seeds (101-110) are refused before anything is generated and recorded in the manifest, so the
proof table stays honest. Step 2 happens on the GX10: LoRA an 8B instruct model on `judge_train.jsonl`, watch
`judge_val.jsonl`, serve it with vLLM, then `python scripts/benchmark.py --contestants rules,local,local-tuned
--tuned-model <id the server reports>`.

## Live seed demo (a judge picks the number)

```bash
export FLOCK_OFFLINE=1
python -m flock.demo --seed 4821                                    # fresh data from that seed, full run, scoreboard
python -m flock.demo --seed 4821 --profile ca_bagel_chain --weeks 2  # the (simulated) bagel chain
python -m flock.demo --seed 4821 --confirm                          # accept the unseen layout without asking
python -m flock.demo --seed 4821 --keep                             # leave the run on the dashboard afterwards
```

Generates into a temp folder, collects, runs the whole pipeline with the progress bar (also on the dashboard's live
board, so run `python -m dashboard.app` next to it), and prints the scoreboard against the **freshly generated**
answer key. It never writes `data/answer_key.csv` or `mappings.json` (it uses temp copies); the unseen-layout trap
still asks y/N unless `--confirm`. Afterwards `data/state` and the sandbox portal are restored to the fixtures demo,
unless `--keep` (then the generated key sits at `data/state/answer_key.csv` for the scoreboard until the next
`pipeline --reset`). Other knobs: `--stores`, `--days`, `--trap-rate`, `--forget` (no learned rules), `--no-approve`
(leave decisions to a human on the dashboard), `--pace`, `--workdir` (keep the generated files).

## Clicky demo (agent files disputes in a sandbox portal)

`flock/portal.py` is a FAKE merchant dispute portal on `127.0.0.1:8765` only (login `sandbox` / `sandbox`).
By default Clicky writes to `data/sandbox_portal/disputes.jsonl` (no setup). Browser mode is opt-in:

```bash
python -m playwright install chromium        # once; if it fails, Clicky uses your installed Google Chrome
python -m flock.portal                       # terminal 1: open http://127.0.0.1:8765 (sandbox / sandbox)

# terminal 2: Clicky logs in, fills the form for each APPROVED finding, submits, reads back the SBX- reference
export FLOCK_OFFLINE=1
FLOCK_CLICKY_MODE=browser FLOCK_CLICKY_HEADED=1 python -m flock.pipeline --reset --demo-approve
```

Refresh "My disputes" in the portal to see the filings. Each finding's `file` entry in `data/state/audit_log.jsonl`
carries the portal reference (written by the gatekeeper, not Clicky). If the portal isn't running, Clicky starts it;
if Playwright or a browser is missing, it falls back to the jsonl file. Other knobs: `FLOCK_CLICKY_SLOWMO` (ms,
headed only), `FLOCK_CLICKY_CHANNEL=chrome`, `FLOCK_PORTAL_USER` / `FLOCK_PORTAL_PASS` / `FLOCK_PORTAL_PORT`.
Real-browser test: `FLOCK_TEST_BROWSER=1 pytest tests/test_clicky.py`.

### Clicky learns the form from one demonstration

Instead of hard-coded selectors, Clicky can learn the dispute form by watching a person file one dispute
(`flock/recorder.py`, `flock/workflow.py`). Agents propose, humans approve: the learned workflow is a draft until a
human approves it.

```bash
python -m playwright install chromium ffmpeg      # once (ffmpeg = the recording's video)
export FLOCK_OFFLINE=1
python -m flock.pipeline --reset --forget         # gives some drafted disputes to show

# 1. Record: a real browser opens the localhost portal with a panel showing the finding to file. File it as you
#    normally would. Recording stops when the SBX- reference appears.
python -m flock.agents.clicky record              # -> data/demos/<id>/ demo.json, step-NN.png, video.webm, trace.zip
# 2. Learn happens right after recording (or: python -m flock.agents.clicky learn [--demo <id>]).
python -m flock.agents.clicky show                # steps in plain English, which finding field feeds each
# 3. A human approves it once (or the dashboard's "Clicky" tab). Logged by the gatekeeper with the workflow's hash.
python -m flock.agents.clicky --approve --actor preet
# 4. Replay: Clicky fills the form for every approved finding from the learned steps
FLOCK_CLICKY_MODE=browser FLOCK_CLICKY_HEADED=1 python -m flock.pipeline --reset --demo-approve
# 5. The platform redesigns its form: renamed and reordered fields. Clicky checks every field before typing,
#    can't find "Sales channel", files nothing, marks the workflow STALE; the dashboard asks for one new demonstration
FLOCK_PORTAL_LAYOUT=v2 FLOCK_CLICKY_MODE=browser python -m flock.pipeline --reset --demo-approve
FLOCK_PORTAL_LAYOUT=v2 python -m flock.agents.clicky record     # show it once, the new way -> WF-file_dispute-v2
python -m flock.agents.clicky --approve --actor preet
FLOCK_PORTAL_LAYOUT=v2 FLOCK_CLICKY_HEADED=1 python -m flock.agents.clicky file   # files the approved ones left, v2 steps
```

- **What is learned:** every fill, select and click, with a locator built from what a person sees (label, then role +
  text, then the name attribute; id last), and each typed value matched back to the finding field it equals:
  channel, order IDs, amount (always typed to the cent by code), category, finding ID, the approved dispute message.
  A value that matches nothing stays a constant and is flagged. The password is never stored (replay reads
  `FLOCK_PORTAL_PASS`).
- **Rules:** a workflow that doesn't take the amount and order IDs from the finding can't be approved; steps edited
  after learning can't be approved (hash recomputed), and steps edited after approval are refused at replay.
  A learned workflow that is DRAFT or STALE stops browser filing (no fallback, no guessing); with none learned,
  the hard-coded selectors still work. Filing is still `gatekeeper.transition("file")`, and each filing's audit
  entry names the workflow and hash. The demonstration itself is a real sandbox filing: if its finding was
  approved, the gatekeeper marks it FILED so Clicky never files it twice.
- If Clicky isn't the one starting the portal (you ran `python -m flock.portal` yourself), the layout is whatever
  that terminal's `FLOCK_PORTAL_LAYOUT` says; restart it with `FLOCK_PORTAL_LAYOUT=v2` for step 5.
- Files: `data/demos/` and `data/workflows/` (gitignored; the workflow log is `data/workflows/audit_log.jsonl`,
  which survives `--reset`). Committed examples: `fixtures/clicky_demo_v1.json`, `fixtures/file_dispute_workflow.example.json`.
- Tests: `pytest tests/test_clicky_learn.py` (mapping, approval, replay and v2 stale detection against the real portal
  HTML without a browser); `FLOCK_TEST_BROWSER=1` adds the full record -> learn -> approve -> replay -> v2 stale ->
  record again -> replay run in a real browser, with a scripted person at the keyboard.

## Deploy (Railway)

The `Procfile` serves the JSON API (`python -m flock.api --host 0.0.0.0`); the dashboard can run as a second service
(`python -m dashboard.app --host 0.0.0.0`). Set `FLOCK_API_TOKEN` on any public deploy (POSTs need
`Authorization: Bearer <token>`), `FLOCK_OFFLINE=1` until model keys are set, and mount a volume at `/app/data/state`.
Step by step: [`docs/DEPLOY.md`](docs/DEPLOY.md). Simulated data only.

## Getting started (teammates)

```bash
git clone https://github.com/Preet37/FLOCK.git && cd FLOCK
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then run the Quickstart above, open your files from the "Who owns what" table, and read the docstring at the top of each file.
Work on a branch (`git checkout -b maja/generator`), push it, and open a PR into `main`. Keep `main` runnable: the Quickstart and `pytest` should pass before you merge.

## Pipeline

```
Collect -> Read -> Connect -> Deal terms -> Check + Anomaly Detector -> [Investigate] -> Judge (3-judge panel + market)
        -> [Investigate] -> Rank -> Draft -> [human approves] -> File (Clicky, sandbox portal)
        -> Journal (draft GL entries, human approves, gatekeeper posts) -> Learn (proposes rules, human approves)
Audit log underneath every step, via flock/gatekeeper.py
```

Shapes, state machines and who may do each move: [`CONTRACT.md`](CONTRACT.md).

**Journal entries (real data only).** `flock/agents/journal.py` drafts one balanced journal entry per store, channel
and payout: Dr Cash (the store's share of the bank deposit), Dr commission / promotions / fees / error charges,
Dr Due from Platform (Disputed) for every open finding (with its `finding_id`, instead of expense), Cr Delivery Sales,
and sales tax by the deal's `tax_rule`. Accounts come from `chart_of_accounts.json`. Entries are drafts until a human
approves them (dashboard "Journal" tab); then the gatekeeper posts them by writing a NetSuite / QuickBooks-style
import CSV to `data/exports/`. Approve and post are gatekeeper transitions in the same audit log as findings.

Until real data exists, the pipeline runs on `fixtures/fake_findings.json` (6 findings) scored against `data/answer_key.csv`.
As soon as `data/std/` has real tables and the matcher/checkers return findings, the pipeline switches to real data automatically.

## Who owns what (updated Saturday Oct 24; details in `docs/PLAN.md`)

| Who | Now | Owns |
| --- | --- | --- |
| **Preet** | Hosting (Railway, `docs/DEPLOY.md`) and all ML + benchmarking: GX10 models, local and tuned judges, benchmark runs; merging into `main` | `flock/llm.py`, `panel.py`, `market.py`, `anomaly.py`, `scripts/benchmark.py`, `scripts/judge_*.py`, `scripts/gx10.sh`, deploy config |
| **Thabhelo** | CREAO: every AI role as a hosted CREAO agent, the Flock Judge on agent.creao.ai, CREAO connectors for Slack/Gmail | `flock/llm.py` (creao providers), `scripts/creao_setup.py`, `flock/api.py`, `matcher.py`, `checkers.py`, `deal_keeper.py` |
| **Himaja** | UI, in her own frontend folder (not merged yet; `dashboard/` is the UI that runs today) | the frontend; still `generator/`, `collector.py`, `formatter.py`, `profiles/` |
| **Ira** | Pitch, demo video, Devpost [confirm] | |

## Rules for everyone
1. Build to `CONTRACT.md`. Changing it means telling the team.
2. Every change to a finding, journal entry or workflow goes through `flock.gatekeeper.transition()`. Never edit one directly.
3. Every row keeps `source_file` and `source_row`.
4. No LLM does money math. Use `flock.models.money()`.
5. Agents propose. Only humans approve. Only the gatekeeper files disputes or posts journal entries.
6. Never commit generated data, `rules.json`, `.env` or API keys.

## Repo map

```
CONTRACT.md            rules, data shapes, state machines, traps 1-22 (build to this)
FLOCK_CONTEXT.md       background: problem, research, pitch, plan
flock/models.py        constants, Finding, Actor, state machines (findings, journal entries)
flock/gatekeeper.py    transition(): the one door, logs every move incl. denied ones
flock/store.py         Store protocol + get_store(): JsonStore, JournalStore, SpacetimeStore stub
flock/pipeline.py      runs everything end to end; flock/scan.py = year-scale parallel match + check
flock/market.py        judge market: Brier-scored bets, balance weights, attention budget
flock/llm.py           model providers per role (local, groq, nvidia, anthropic, creao, creaoapp), offline + air gap
flock/agents_live.py   live agent status feed, tracker, pooling, /api/agents snapshot
flock/connectors.py    drop-folder hand-off for Slack / Gmail / call-note connectors (docs/connectors.md)
flock/workflow.py      Clicky's learned workflow: learn, human approval, stale detection (flock/recorder.py records)
flock/portal.py        FAKE sandbox dispute portal (localhost) that Clicky files into
flock/demo.py          live seed demo: generate -> collect -> pipeline -> score, then restore
flock/progress.py      terminal progress bar + data/state/progress.json
flock/api.py           stdlib JSON API (port 8787, Procfile, bearer token on writes)
flock/agents/          one file per agent, owner at the top: collector, formatter, matcher, deal_keeper, checkers,
                       anomaly, investigator, panel, ranker, dispute_writer, clicky, journal, learner
dashboard/             Flask + one page (python -m dashboard.app, port 5050)
generator/             seeded data generator, traps 1-22, answer key (output in generator/out/)
profiles/              company profiles (default, ca_bagel_chain: simulated)
fixtures/              fake findings for the Quickstart, adversarial findings, Clicky examples
scripts/               score.py, benchmark.py, judge_dataset.py, judge_load.py, llm_check.py, creao_setup.py, gx10.sh
mappings.json          Formatter column maps per layout (human-confirmed)
chart_of_accounts.json GL accounts for the Journal Entry Drafter
creao_agents.json      CREAO agent ids per role (ids, not secrets)
Procfile               web: python -m flock.api --host 0.0.0.0 (Railway)
tests/                 offline test suite (models mocked)
docs/                  STATUS, PLAN, connectors, DEPLOY, benchmark schema, track notes
```
