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
| `offline` | never call anything | |

| Role | Env | Default |
| --- | --- | --- |
| Judges | `FLOCK_JUDGE_PROVIDERS` comma list in panel order (strict, operator, skeptic), e.g. `local,local,groq`; one entry = all three. `FLOCK_JUDGE_MODELS` optionally gives each judge its own model | `FLOCK_PROVIDER` |
| Reviewer | `FLOCK_REVIEWER_PROVIDER`, `FLOCK_REVIEWER_MODEL` | `anthropic` |
| Dispute writer | `FLOCK_WRITER_PROVIDER`, `FLOCK_WRITER_MODEL` | `FLOCK_PROVIDER` |
| Column mapper | `FLOCK_MAPPER_PROVIDER`, `FLOCK_MAPPER_MODEL` | `FLOCK_PROVIDER` |

`FLOCK_PROVIDER` defaults to `anthropic`, which is offline without a key. So setting only `ANTHROPIC_API_KEY`
still means "Claude everywhere", as before. A provider without its key falls back to offline rules.
`FLOCK_LLM_TIMEOUT` is the per-call timeout in seconds (default 30). If a call fails or times out, that judge
falls back to the offline rules and the finding's reason says so. Each live vote records the `model` that cast it.

How a live panel decides:
1. Round 1: three judges vote independently, in parallel.
2. If they disagree: one debate round. Each judge sees the other two votes and reasons and may revise. Both rounds stay in `votes` (`round: 1`, `round: 2`).
3. Still split: the finding is a `conflict` and goes to a human. The reviewer does **not** break the tie; it writes `review_memo` on the finding (each side, and what evidence would settle it).
4. Audit sample: `FLOCK_AUDIT_RATE` (default `0.1`, picked by a hash of `finding_id`, so the same finding is always in or out) of the findings the panel decided on its own get a reviewer second opinion. If the reviewer disagrees, the finding becomes a `conflict`, the reason starts with "Audit sample: ...", and a human decides.

Offline the debate and the reviewer do nothing. Approved rules, the tolerance and weak-evidence guardrails, and
"agents propose, humans approve" all still apply on top of any model.

Pointing it at the GX10 (over Tailscale):

```bash
# on the GX10: serve an OpenAI-compatible endpoint, e.g.
#   vllm serve <model> --host 0.0.0.0 --port 8000      or      OLLAMA_HOST=0.0.0.0 ollama serve
export FLOCK_LOCAL_URL=http://<gx10-tailscale-name-or-100.x.y.z>:8000/v1   # Ollama: :11434/v1
export FLOCK_LOCAL_MODEL=<model id the server reports>
export FLOCK_JUDGE_PROVIDERS=local,local,local FLOCK_WRITER_PROVIDER=local FLOCK_MAPPER_PROVIDER=local
export ANTHROPIC_API_KEY=...                                               # reviewer only
unset FLOCK_OFFLINE
python scripts/llm_check.py        # pings every configured route: latency, which model answered, models served
```

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
Collect -> Read -> Connect -> Deal terms -> Check -> Judge (3-judge panel) -> Rank -> Draft
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
| **Thabhelo** | ML: anomaly detector, benchmark harness (`scripts/benchmark.py`), tuned local judge with held-out eval | `matcher.py`, `checkers.py`, `deal_keeper.py`, `flock/api.py`, new ML code |
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
flock/demo.py          live seed demo: generate -> collect -> pipeline -> score, then restore
profiles/              company profiles for the generator (default, ca_bagel_chain: simulated)
flock/portal.py        FAKE sandbox dispute portal (localhost) that Clicky files into
flock/agents/          one file per agent, owner named at the top
fixtures/              fake findings so everyone can work in parallel (+ adversarial_findings.json for tests)
generator/             Maja's data generator (output in generator/out/, gitignored)
mappings.json          Formatter column maps per source layout (human-confirmed)
chart_of_accounts.json GL accounts + which account each kind of amount posts to (Journal Entry Drafter)
scripts/score.py       scoreboard vs answer key (scripts/llm_check.py pings the configured models)
tests/                 gatekeeper, store, dashboard, detection, data, panel/ranker/learner/writer, llm, providers, market, journal, end-to-end
dashboard/             Ira's UI: Flask + one page (python -m dashboard.app)
docs/                  Hyphenate track notes, sponsor rules, pipeline sketch
```
