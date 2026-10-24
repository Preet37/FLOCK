# Flock

Flock does the boring job a restaurant pays an accountant to do by hand: it takes the messy money reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, sorts them into one place, and checks them, faster and cheaper than a person can.

Full context: `FLOCK_CONTEXT.md`. Shared rules and data shapes: `CONTRACT.md`. Read both first.

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
flock/agents/          one file per agent, owner named at the top
fixtures/              fake findings so everyone can work in parallel
generator/             Maja's data generator
scripts/score.py       scoreboard vs answer key
tests/                 gatekeeper tests
dashboard/             Ira's UI
```
