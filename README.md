# Flock

Flock does the boring job a restaurant pays an accountant to do by hand: it takes the messy money reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, sorts them into one place, and checks them, faster and cheaper than a person can.

**The problem (Dave's Hot Chicken, dozens of franchise stores):**
- **What:** Dave's gets paid by four food apps (DoorDash, Uber Eats, Grubhub, its own app), and each one sends money and reports in a different, messy way.
- **Why:** Each app has its own deal with Dave's (commission tiers, promos, refund rules), and none of them talk to each other or to Dave's register, bank or contracts.
- **When:** Every week, when the payouts land as lump sums that never match what the stores actually sold.
- **Where:** On a staff accountant's desk, stuck in four app portals, the register, the bank and a pile of spreadsheets.
- **Why it matters:** Dave's pays someone to sort it all by hand. It's slow, and mistakes mean Dave's loses money it already earned.

## Read first

| File | What's in it |
| --- | --- |
| `FLOCK_CONTEXT.md` | Everything: problem, money flow, research, numbers, pitch, agents, 17 edge cases, team prompts, demo plan, timeline. Paste it into Claude at the start of a session. |
| `CONTRACT.md` | Shared data shapes (order row, payout line, bank deposit, deal term, finding) and the state machine. Build to this. |
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

## Data (generator -> collector -> formatter)

```bash
export FLOCK_OFFLINE=1
python generator/generate.py --mini                   # first milestone: 1 store x 1 day, traps 3, 7, 16
python generator/generate.py --seed 42 --stores 12 --weeks 2     # full run, all 17 traps -> generator/out/
python -m flock.agents.collector                      # copy generator/out/* untouched into data/raw/<source>/ + manifest.csv
python -m flock.agents.formatter                      # data/raw -> data/std/*.csv; asks y/N for the unseen layout (trap 2)
python -m flock.agents.formatter --confirm            # same, accepting proposed mappings without asking
```

- Same `--seed` and args = byte-identical files. `--traps 3,7,16` picks traps, `--days N` overrides `--weeks`. Traps 2 and 5 need `--weeks 2`.
- The answer key goes to `generator/out/answer_key.csv`. `data/answer_key.csv` (the demo scoreboard) is only overwritten with `--write-answer-key`; restore it with `git checkout data/answer_key.csv`. The collector never copies the answer key.
- `data/std/` gets `orders.csv`, `payout_lines.csv`, `bank_deposits.csv` (CONTRACT.md columns; payout lines add `promo_total, promo_code, placed_at`), plus `error_charges.csv`, `refunds.csv`, `payout_summaries.csv`, `duplicates_removed.csv`.
- Column maps live in `mappings.json`. Unknown layouts get a proposed mapping (Claude, or a header-name heuristic offline) that a human confirms once; then it's saved and automatic. Run with `--mappings /tmp/m.json` if you want to rehearse the demo moment without touching the committed file.
- `data/raw/` and `data/std/` are gitignored. To get back to a fixtures-only Quickstart: `rm -rf data/raw/*/ data/raw/manifest.csv data/std/*.csv`.

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
        -> [human approves] -> File (Clicky, sandbox portal) -> Learn (proposes rules)
Audit log underneath every step, via flock/gatekeeper.py
```

Until real data exists, the pipeline runs on `fixtures/fake_findings.json` (6 findings) scored against `data/answer_key.csv`.
As soon as `data/std/` has real tables and the matcher/checkers return findings, the pipeline switches to real data automatically.

## Who owns what, and where to start

| Who | Files | Your first step |
| --- | --- | --- |
| **Maja** | `generator/generate.py`, `flock/agents/collector.py`, `flock/agents/formatter.py` | Generator for 1 store x 1 day with 3 traps. Write answer key rows to `data/answer_key.csv`. Then the Formatter writes `data/std/*.csv` |
| **Thabhelo** | `flock/agents/matcher.py`, `deal_keeper.py`, `checkers.py` | `check_commission` is a working example; copy its pattern for promo, tax, error charge, deposit. `deal_keeper.resolve()` already does the authority ranking |
| **Preet** | `flock/agents/panel.py`, `ranker.py`, `learner.py`, `dispute_writer.py`, `clicky.py` | All working on fixtures. Next: real fake-portal web page + browser automation for Clicky; tune judges with a real key |
| **Ira** | `flock/gatekeeper.py`, `flock/store.py`, `dashboard/`, `scripts/score.py` | Gatekeeper + JSON store work. Next: dashboard (see `dashboard/README.md`), then swap `JsonStore` for SpacetimeDB with the same methods |

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
flock/llm.py           model providers (local/vLLM/Ollama, Groq, NVIDIA, Claude) per role, offline fallback
flock/pipeline.py      runs everything end to end
flock/portal.py        FAKE sandbox dispute portal (localhost) that Clicky files into
flock/agents/          one file per agent, owner named at the top
fixtures/              fake findings so everyone can work in parallel (+ adversarial_findings.json for tests)
generator/             Maja's data generator (output in generator/out/, gitignored)
mappings.json          Formatter column maps per source layout (human-confirmed)
scripts/score.py       scoreboard vs answer key (scripts/llm_check.py pings the configured models)
tests/                 gatekeeper, store, dashboard, detection, data, panel/ranker/learner/writer, llm, providers, end-to-end
dashboard/             Ira's UI: Flask + one page (python -m dashboard.app)
docs/                  Hyphenate track notes, sponsor rules, pipeline sketch
```
