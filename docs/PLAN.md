# Plan to submission

**Company: Noah's NY Bagels** (~56 California locations, mid-market). Data is simulated with `--profile ca_bagel_chain`.

**Everyone: read this, then check your row in "Who's on what".** Submission: **Sunday Oct 25, 9:00 AM**. Judging 10:30 AM to 12:30 PM, full team at the table. Demo + pitch: 3 minutes.

## Where we are (on `main`)

Everything below runs today with `FLOCK_OFFLINE=1` and no API key.

| Piece | Status |
| --- | --- |
| Pipeline: collect, format, match, deal terms, check, judge, rank, draft, human, file, learn | Works end to end |
| Generator + answer key | 17 trap types; `--profile ca_bagel_chain` = fictional 56-store chain |
| Scale | 56 stores x 52 weeks = 1.24M orders, 1,126/1,126 traps caught, 0 false alarms, 84s on a laptop |
| Live seed demo | `python -m flock.demo --seed <judge's number> --confirm` |
| Judge panel | 3 judges, one debate round on splits, Claude writes a memo for the human (never breaks the tie), 10% random audit |
| Judge market | Brier-scored bets settle on human decisions and RECOVERED; votes weighted by balance; attention budget |
| Journal Entry Drafter | Balanced entries per store x channel x payout, disputed money to "Due from Platform", human approves, NetSuite/QuickBooks CSV |
| Clicky | Files approved disputes in a localhost sandbox portal with a real browser |
| Dashboard | Live board, human queue, PR-style finding page, journal, judge market, scoreboard, progress bar |
| Models | Provider switch per role: local (vLLM/Ollama), Groq, NVIDIA, Claude; `python scripts/llm_check.py` |
| Tests | 337 passing |

## What's next: show what the GX10 can do

About 19 teams have a GX10, so owning one isn't the edge. Using it for real work is. Must-haves first.

| # | Piece | What the audience sees | Owner |
| --- | --- | --- | --- |
| 1 | **Offline run** | Wi-Fi off; the whole pipeline still runs on a year of data with live local models | Preet |
| 2 | **Benchmark board** | Human vs Claude vs local models vs our tuned model on the same held-out data (below) | Thabhelo (harness), Himaja (UI) |
| 3 | **Three different models as judges** | Three open models loaded side by side on the GX10, one per judge | Preet |
| 4 | **The flock at scale** | Hundreds of judge/investigator calls batched in parallel; agents appear on the dashboard | Preet, Ira (SpacetimeDB agent_status) |
| 5 | **Anomaly detector (trained)** | Learns each store x channel's normal fees/commission/timing from history; flags a jump no rule covers | Thabhelo |
| 6 | Local contract reader | A vision model reads PDF contracts into deal terms with exact quotes | stretch |
| 7 | Tuned local judge | Trained on some seeds, tested on held-out seeds, result goes on the benchmark board | Thabhelo, stretch |

## The benchmark (trained model vs human vs frontier)

We don't claim our model is better. We measure it and show the table, including where we lose.

**Test set.** Held-out seeds (e.g. 101 to 110) that no model is ever trained or tuned on. Labels come from each seed's answer key.

**Contestants**

| Contestant | What it is |
| --- | --- |
| Rules only | `FLOCK_OFFLINE=1`, the baseline |
| Frontier | Claude as all three judges |
| Local, off the shelf | Open models on the GX10 as judges |
| Local, tuned | Our model tuned on training seeds (e.g. 1 to 50), never on the test seeds |
| Human | A teammate who did not build the generator works one store-week from the raw files, with a spreadsheet and a timer |

**Same inputs for every model:** same findings, same evidence, same prompts. The money math is code for all of them, so we compare judgment, not arithmetic.

**Metrics**

| Metric | Meaning |
| --- | --- |
| Traps caught | Recall vs the answer key |
| False alarms | Disputes that shouldn't be filed |
| Correct dollars | Recoverable dollars found that the answer key agrees with |
| Sent to a human | How often it needed a person |
| Latency | Seconds per finding, p50 and p95 |
| Throughput | Findings judged per minute |
| Cost | Dollars per 1,000 findings |
| Works offline | Yes or no |
| Human time | Minutes per store-week, vs Flock's seconds |

Output: `scripts/benchmark.py` writes one results table (CSV + JSON) that the dashboard shows as a "Benchmark" tab. Report every run, not just the good ones.

## Who's on what (updated Saturday Oct 24)

| Who | Now working on | Owns |
| --- | --- | --- |
| **Preet** | GX10 setup over Tailscale (vLLM, three models), offline run, model providers, judge panel + market, merging branches into `main` | `flock/llm.py`, `panel.py`, `ranker.py`, `learner.py`, `dispute_writer.py`, `clicky.py`, `portal.py`, `market.py` |
| **Thabhelo** | ML: anomaly detector, benchmark harness (`scripts/benchmark.py`), tuned local judge with held-out eval | `matcher.py`, `checkers.py`, `deal_keeper.py`, `flock/api.py`, new ML code |
| **Himaja** | UI: the demo flow on the dashboard (live board, progress bar, finding page, market, journal, benchmark tab) | `dashboard/`; still owns `generator/`, `collector.py`, `formatter.py`, `profiles/` |
| **Ira** | [confirm] SpacetimeDB backend (findings, audit log, agent_status, live dashboard), pitch, video, Figma/Notability process tracks | `flock/store.py` (SpacetimeStore), `gatekeeper.py`, `scripts/score.py` |

One UI: the dashboard in this repo (`dashboard/`). If anyone has UI work in another repo, move it here so judges see one codebase.

## How we work tonight

1. Branch per task (`thabhelo/anomaly`, `himaja/benchmark-tab`), PR into `main`.
2. Before merging: the Quickstart prints 6/6 caught, 2 then 0 to a human, and `pytest` passes.
3. Never commit generated data, `rules.json`, `.env` or keys.
4. Rules in `CONTRACT.md` still apply: money math is code, agents propose, humans approve, only the gatekeeper files or posts.

## Timeline (Pacific)

| When | What |
| --- | --- |
| Sat evening | GX10 reachable; benchmark harness + anomaly detector on branches |
| Sat night | Three local models judging; offline run works; benchmark tab |
| **Sun 3:00 AM** | **Feature freeze.** Only fixes after this |
| Sun 3 to 7 AM | Rehearse the 3-minute demo, record the video (it must open with the key catch/recovery), write Devpost, say what's real vs simulated |
| **Sun 9:00 AM** | **Submit**: repo, project image, working demo, video |

## The 3-minute demo (draft)

1. **0:00 to 0:30, plant the depth.** "Noah's NY Bagels: 56 California stores, paid by four apps under four different deals. The same short payout can be right, wrong, or a judgment call depending on which contract or amendment was in effect." Show one finding where that's true.
2. **0:30 to 1:15, the year.** Hit run with Wi-Fi off. The progress bar goes through 1.24M orders; three local models judge; the board fills.
3. **1:15 to 2:00, accountable.** Open one finding like a pull request: sources, the deal term in effect, judge votes and bets, the reviewer memo, the human decision, the journal entry. Show "Try as an agent" being refused.
4. **2:00 to 2:30, it learns.** Week 1 sends N to a human; approve one rule; week 2 sends fewer. The operator judge lost credits for backing an unauthorized rep.
5. **2:30 to 3:00, the proof.** The benchmark table: human vs Claude vs our models (accuracy, seconds, cost, offline). Close on the $3 for $1 line.
