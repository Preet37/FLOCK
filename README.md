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
python scripts/score.py
pytest                                                      # gatekeeper rules
```

That week 1 vs week 2 drop is the "self-improving" demo moment.
Drop `FLOCK_OFFLINE=1` and set `ANTHROPIC_API_KEY` (see `.env.example`) to use Claude for the judges and dispute drafts.

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
flock/store.py         findings + append-only audit log (JSON now, SpacetimeDB later)
flock/gatekeeper.py    transition(): the one door, logs every move incl. denied ones
flock/llm.py           Claude wrapper with offline fallback
flock/pipeline.py      runs everything end to end
flock/portal.py        FAKE sandbox dispute portal (localhost) that Clicky files into
flock/agents/          one file per agent, owner named at the top
fixtures/              fake findings so everyone can work in parallel
generator/             Maja's data generator
scripts/score.py       scoreboard vs answer key
tests/                 gatekeeper tests
dashboard/             Ira's UI
docs/                  Hyphenate track notes, sponsor rules, pipeline sketch
```
