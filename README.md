# Flock

Flock does the boring job a restaurant pays an accountant to do by hand: it takes the messy money reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, sorts them into one place, and checks them, faster and cheaper than a person can.

> **Team: read [`docs/PLAN.md`](docs/PLAN.md) now.** It has the plan to submission (Sun Oct 25, 9 AM), the GX10 + benchmark plan, the 3-minute demo, and who's on what. Updated Saturday Oct 24.

**The company: Noah's NY Bagels**, about 56 locations across California, a mid-market chain. All data in this repo is **simulated** (`--profile ca_bagel_chain`); no real Noah's records or contract terms are used.
- **What:** the stores get paid by delivery apps (DoorDash, Uber Eats, Grubhub) and their own ordering channel, and each one sends money and reports in a different, messy way.
- **Why:** each app has its own deal (commission tiers, promos, refund rules), and none of them talk to each other or to the register, bank or contracts.
- **When:** every week, when the payouts land as lump sums that never match what the stores actually sold.
- **Where:** on a staff accountant's desk, stuck in app portals, the register, the bank and a pile of spreadsheets.
- **Why it matters:** someone gets paid to sort it all by hand. It's slow, and mistakes mean the company loses money it already earned.

## Read first

| File | What's in it |
| --- | --- |
| `FLOCK_CONTEXT.md` | Everything: problem, money flow, research, numbers, pitch, agents, 17 edge cases, team prompts, demo plan, timeline. Paste it into Claude at the start of a session. |
| `CONTRACT.md` | Shared data shapes (order row, payout line, bank deposit, deal term, finding) and the state machine. Build to this. |
| `docs/PLAN.md` | **The plan:** what's next, the benchmark (trained model vs human vs frontier), timeline, demo script, who's on what. |
| `docs/hyphenate-track.md` | What the Hyphenate judges want: criteria, workshop notes, the speaker's advice. |
| `docs/sponsor-tracks.md` | Sponsor track rules, submission checklist, schedule. **Submit by Sun Oct 25, 9:00 AM.** |
| `docs/pipeline-sketch.png` | The team's original hand-drawn pipeline. |

## Quickstart (works right now, no API key needed)

```bash
pip install -r requirements.txt
export FLOCK_OFFLINE=1                                      # offline rules: fast, free, deterministic

python -m flock.pipeline --reset --forget --demo-approve    # WEEK 1: day one, no learned rules
python scripts/score.py                                     # caught 6/6, 2 sent to a human
python -m flock.agents.learner --approve R-001              # human approves the rule the Learner proposed
python -m flock.pipeline --reset --demo-approve             # WEEK 2: same traps, 0 sent to a human
python scripts/score.py                                     # also prints Week 1: 2 | Week 2: 0
pytest                                                      # all tests (offline, models mocked)
python -m dashboard.app                                     # dashboard at http://127.0.0.1:5050
```

Dashboard details (screens, API, the live `--pace` demo) are in `dashboard/README.md`.
Storage is picked by `FLOCK_STORE=json|spacetime` (json default) through `flock.store.get_store()`.

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
| `creao` | CREAO Developer Platform: every AI role as a hosted CREAO agent (see below) | `CREAO_API_KEY` (`cr_sk_...`), `creao_agents.json` from `scripts/creao_setup.py --create`, optional `FLOCK_CREAO_LLM`, `FLOCK_CREAO_MODE=async` |
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

## Run every AI role on CREAO

[CREAO](https://creao.ai) hosts agents created from a plain-language description and runs them through its Developer
Platform. `scripts/creao_setup.py` creates one CREAO agent per Flock AI role (the three judges, reviewer, dispute writer,
column mapper, document reader) and the `creao` provider in `flock/llm.py` runs them. Flock keeps its prompts and all
money math in its own code: each agent is created with one contract, "treat `instructions` as your system prompt, answer
`request`, reply with only the JSON asked for", so the audit trail, the gatekeeper and the offline fallback are unchanged.

```bash
export CREAO_API_KEY=cr_sk_...                      # Account API key from developer.creao.ai, server-side only
python scripts/creao_setup.py --create              # 7 agents, async creation polled; writes creao_agents.json
python scripts/creao_setup.py --check               # one tiny run per agent: reply + credits spent
python scripts/creao_setup.py --env                 # the export lines below, filled in
export FLOCK_JUDGE_PROVIDERS=creao FLOCK_REVIEWER_PROVIDER=creao FLOCK_WRITER_PROVIDER=creao \
       FLOCK_MAPPER_PROVIDER=creao FLOCK_READER_PROVIDER=creao
unset FLOCK_OFFLINE
python -m flock.pipeline --reset --forget            # the whole flock judged, drafted and read on CREAO
python scripts/benchmark.py --contestants rules,creao   # CREAO on the proof table (credits reported per run)
```

How a call works: `POST /v1/realtime/runs` with `{agent_id, input: {instructions, request}}`, the reply is the NDJSON
stream's text (`run.completed.result.text`), and `usage.credits` is counted (`llm.creao_credits()`). If realtime is not
enabled for the account the provider polls `POST /v1/runs` + `GET /v1/runs/{id}` instead (`FLOCK_CREAO_MODE=async`
forces it). `FLOCK_CREAO_LLM` picks the model CREAO runs the agents on (default `anthropic/claude-sonnet-5-5`).
A role whose agent is missing stays on offline rules and says why (`offline_reason`). `FLOCK_AIRGAP=1` keeps CREAO
offline like any other internet host, so the "no internet" demo and the CREAO demo are two env blocks, not two builds.

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
python generator/generate.py --seed 42 --stores 12 --weeks 2     # full run, all 17 traps -> generator/out/
python -m flock.agents.collector                      # copy generator/out/* untouched into data/raw/<source>/ + manifest.csv
python -m flock.agents.formatter                      # data/raw -> data/std/*.csv; asks y/N for the unseen layout (trap 2)
python -m flock.agents.formatter --confirm            # same, accepting proposed mappings without asking
python generator/generate.py --profile ca_bagel_chain --weeks 52   # another (simulated) company, a full year
```

- Same `--seed` and args = byte-identical files. `--traps 3,7,16` picks traps, `--days N` overrides `--weeks`. Traps 2 and 5 need `--weeks 2`.
- **Company profiles** (`profiles/*.json`, `--profile NAME`): name, stores and cities, channels, orders per store per day by channel, menu / average ticket, weekday + monthly seasonality, dayparts, catering, and the contract terms per channel. `default` is the original 12-store company (same bytes as before profiles existed). `ca_bagel_chain` is a **fictional, simulated** 56-store California bagel chain (morning-heavy, weekend peaks, catering orders; ~1.2M orders a year); no real company, people or contract terms. `--stores` defaults to the profile's store count.
- **Traps across a year:** each trap is planted `max(1, round(trap_rate x weight x store_weeks / 100))` times, spread across the period (`--trap-rate`, default 2 per 100 store-weeks; a 2-week demo still gets exactly one of each, 12 stores x 52 weeks gets 12 of most, 56 x 52 gets 58). Waiver traps 14/15 are weighted lower, the unseen layout (2) happens once. The answer key scales with it.
- **Year scale** (offline, 10-core laptop, full chain `generate -> collect -> format -> pipeline -> score`, all traps caught, 0 false alarms): 12 stores x 2 weeks (3.2k orders) **2s**; 12 x 52 weeks (83k orders) **9s**; bagel chain 56 x 52 weeks (1.24M orders, 2.5M std rows, 1,126 answer-key rows) **84s** (generate 31s, format 33s, pipeline 19s). The formatter streams and converts column-wise, the pipeline splits the std tables by store and runs Matcher + Checkers in parallel processes (`flock/scan.py`, `FLOCK_WORKERS`), deal terms are cached per store/day, and the JSON store batches writes. The pipeline reuses `data/std/` when the raw files and mappings haven't changed.
- **Progress:** the formatter and pipeline draw a terminal progress bar (stage, done/total, rows/sec) and write `data/state/progress.json`; the dashboard's live board shows it (`GET /api/progress`).
- A statement whose layout nobody has confirmed yet is skipped, and "sold, never paid" for that channel is held back (not disputed) until a human says yes, because its orders were never read.
- The answer key goes to `generator/out/answer_key.csv`. `data/answer_key.csv` (the demo scoreboard) is only overwritten with `--write-answer-key`; restore it with `git checkout data/answer_key.csv`. The collector never copies the answer key.
- `data/std/` gets `orders.csv`, `payout_lines.csv`, `bank_deposits.csv` (CONTRACT.md columns; payout lines add `promo_total, promo_code, placed_at`), plus `error_charges.csv`, `refunds.csv`, `payout_summaries.csv`, `duplicates_removed.csv`.
- Column maps live in `mappings.json`. Unknown layouts get a proposed mapping (Claude, or a header-name heuristic offline) that a human confirms once; then it's saved and automatic. Run with `--mappings /tmp/m.json` if you want to rehearse the demo moment without touching the committed file.
- `data/raw/` and `data/std/` are gitignored. To get back to a fixtures-only Quickstart: `rm -rf data/raw/*/ data/raw/manifest.csv data/std/*.csv`.

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

## Getting started (teammates)

```bash
git clone https://github.com/Preet37/FLOCK.git && cd FLOCK
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Then run the Quickstart below, open your files from the "Who owns what" table, and start on the next step written at the top of each file.
Work on a branch (`git checkout -b maja/generator`), push it, and open a PR into `main`. Keep `main` runnable: the Quickstart and `pytest` should pass before you merge.

## Pipeline

```
Collect -> Read -> Connect -> Deal terms -> Check + Anomaly Detector -> Judge (3-judge panel) -> Rank -> Draft
        -> [human approves] -> File (Clicky, sandbox portal) -> Journal (draft GL entries) -> Learn (proposes rules)
Audit log underneath every step, via flock/gatekeeper.py
```

**Journal entries (real data only).** `flock/agents/journal.py` drafts one balanced journal entry per store, channel
and payout: Dr Cash (the store's share of the bank deposit), Dr commission / promotions / fees / error charges,
Dr Due from Platform (Disputed) for every open finding (with its `finding_id`, instead of expense), Cr Delivery Sales,
and sales tax by the deal's `tax_rule`. Accounts come from `chart_of_accounts.json`. Entries are drafts until a human
approves them (dashboard "Journal" tab); then the gatekeeper posts them by writing a NetSuite / QuickBooks-style
import CSV to `data/exports/`. Approve and post are gatekeeper transitions in the same audit log as findings.

Until real data exists, the pipeline runs on `fixtures/fake_findings.json` (6 findings) scored against `data/answer_key.csv`.
As soon as `data/std/` has real tables and the matcher/checkers return findings, the pipeline switches to real data automatically.

## Who owns what (updated Saturday Oct 24; details in `docs/PLAN.md`)

| Who | Now working on | Owns |
| --- | --- | --- |
| **Preet** | GX10 setup (vLLM, three local models), offline run, model providers, judge panel + market, merging into `main` | `flock/llm.py`, `panel.py`, `ranker.py`, `learner.py`, `dispute_writer.py`, `clicky.py`, `portal.py`, `market.py` |
| **Thabhelo** | ML: anomaly detector, benchmark harness (`scripts/benchmark.py`), tuned local judge with held-out eval | `matcher.py`, `checkers.py`, `deal_keeper.py`, `anomaly.py`, `flock/api.py`, `scripts/benchmark.py`, new ML code |
| **Himaja** | UI: the demo flow on the dashboard, including a benchmark tab | `dashboard/`; still owns `generator/`, `collector.py`, `formatter.py`, `profiles/` |
| **Ira** | [confirm] SpacetimeDB backend, pitch, video, Figma/Notability process tracks | `flock/store.py` (SpacetimeStore), `gatekeeper.py`, `scripts/score.py` |

## Rules for everyone
1. Build to `CONTRACT.md`. Changing it means telling the team.
2. Every finding change goes through `flock.gatekeeper.transition()`. Never edit a finding directly.
3. Every row keeps `source_file` and `source_row`.
4. No LLM does money math. Use `flock.models.money()`.
5. Agents propose. Only humans approve. Only the gatekeeper files.

## Repo map

```
CONTRACT.md            shared data shapes + state machine
FLOCK_CONTEXT.md       everything about the project (problem, research, pitch, plan)
flock/models.py        constants, Finding, Actor, the state machine table
flock/store.py         Store protocol + get_store(): JsonStore now, SpacetimeStore stub
flock/gatekeeper.py    transition(): the one door, logs every move incl. denied ones
flock/market.py        judge market: Brier-scored bets settled on human/RECOVERED truth, attention budget
flock/llm.py           model providers (local/vLLM/Ollama, Groq, NVIDIA, Claude) per role, offline fallback
flock/pipeline.py      runs everything end to end
flock/scan.py          year-scale detection: split std tables by store, match + check in parallel
flock/progress.py      progress reporter: terminal bar + data/state/progress.json (dashboard progress bar)
flock/agents_live.py   the live flock: agent status feed (spawn/working/done), tracker, pooling, /api/agents snapshot
flock/demo.py          live seed demo: generate -> collect -> pipeline -> score, then restore
profiles/              company profiles for the generator (default, ca_bagel_chain: simulated)
flock/portal.py        FAKE sandbox dispute portal (localhost) that Clicky files into
flock/agents/          one file per agent, owner named at the top (anomaly.py: learned baselines, no deal term needed)
fixtures/              fake findings so everyone can work in parallel (+ adversarial_findings.json for tests)
generator/             Maja's data generator (output in generator/out/, gitignored)
mappings.json          Formatter column maps per source layout (human-confirmed)
chart_of_accounts.json GL accounts + which account each kind of amount posts to (Journal Entry Drafter)
scripts/score.py       scoreboard vs answer key (scripts/llm_check.py pings the configured models)
scripts/gx10.sh        GX10 tunnel / env / status / model pulls; scripts/judge_load.py panel latency + throughput
tests/                 gatekeeper, store, dashboard, detection, data, panel/ranker/learner/writer, llm, providers, market, journal, end-to-end
dashboard/             Ira's UI: Flask + one page (python -m dashboard.app)
docs/                  Hyphenate track notes, sponsor rules, pipeline sketch
```
