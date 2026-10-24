<!-- Discord: paste one "MESSAGE" block per message (each is under 2,000 characters), or upload this file. -->

<!-- ===== MESSAGE 1 / 7 ===== -->
# 🐔 FLOCK: team brief (Sat Oct 24)

**Submit: Sun Oct 25, 9:00 AM.** Judging 10:30 AM to 12:30 PM, all four of us at the table. **Feature freeze: 3:00 AM.**
Repo: https://github.com/Preet37/FLOCK
Full plan: `docs/PLAN.md`. Rules and data shapes: `CONTRACT.md`. Background: `FLOCK_CONTEXT.md`.

**Company: Noah's NY Bagels**, ~56 locations across California (mid-market). Our data is simulated (`--profile ca_bagel_chain`), modeled on a chain that size; no real Noah's records or contracts.

## What we're building, in one line
Flock does the job a restaurant group pays a staff accountant to do by hand: it takes the messy payout reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, matches them to what the stores actually sold and what the bank received, checks every charge against the deal that was in effect, and turns the mistakes into disputes and journal entries that a human approves.

## Why it wins the Hyphenate track
Their judges score: Is it real? **Would you hire it?** (they told us this one matters most) Is it new? Is there a wow moment?
They told us to pick one boring job and go deep, use messy data with an answer key, escalate to humans intelligently, and keep an audit trail of every decision. **"Audit trail is the most important thing. If you build something around that, you win."** That's what Flock is.

<!-- ===== MESSAGE 2 / 7 ===== -->
## How it works (the pipeline)
1. **Collect + Format**: copy every file untouched, then map each app's columns into one standard shape. A new layout gets a proposed mapping that a human confirms once.
2. **Match**: pin every record about one order together: register, payout line, bank deposit.
3. **Deal Keeper**: which commission, promo split, fee and tax rule applied *that day*. Newest binding document wins (contract, amendment, or a rep with authority). A rep without authority never changes the deal; it becomes a conflict for a human.
4. **Checkers**: plain Python calculators. **No AI ever does money math.**
5. **Judge panel**: 3 judges (strict, operator, skeptic) vote and bet. If they split: one debate round, then a human decides, with a reviewer memo.
6. **Ranker + Dispute Writer**: spend the limited dispute budget on the best cases and draft them.
7. **Human approves** → **Clicky** files in a sandbox portal → **Journal entries** for NetSuite/QuickBooks.
8. **Learner + Judge market**: repeated human calls become rules; judges that are wrong lose influence.
Under all of it: **the gatekeeper**. Every change goes through one door, and every move (refused ones too) lands in an append-only audit log.

<!-- ===== MESSAGE 3 / 7 ===== -->
## What already works (on `main`, 337 tests passing)
- Full pipeline, end to end, no API key needed (`FLOCK_OFFLINE=1`)
- Generator: 17 kinds of planted mistakes + an answer key. 56-store bagel chain profile, modeled on Noah's
- **Scale: 1.24M orders (56 stores × 52 weeks), all 1,126 traps caught, 0 false alarms, 84 seconds on a laptop**
- Live seed: `python -m flock.demo --seed <judge's number> --confirm`. A judge picks the number, it runs fresh
- Judge debate + Claude as reviewer (memo + 10% audit), never as tiebreaker
- Judge market: Brier-scored bets, balance-weighted votes, human attention budget
- Journal Entry Drafter: balanced entries, disputed money booked as "Due from Platform"
- Clicky: a real browser files disputes in a localhost sandbox portal
- Dashboard: live board, human queue, PR-style finding page, journal, market, scoreboard, progress bar
- Model switch per role: local (GX10), Groq, NVIDIA, Claude

**Quickstart (everyone, 5 minutes):**
```
git pull && pip install -r requirements.txt
export FLOCK_OFFLINE=1
python -m flock.pipeline --reset --forget --demo-approve && python scripts/score.py
python -m dashboard.app    # http://127.0.0.1:5050
```

<!-- ===== MESSAGE 4 / 7 ===== -->
## 👤 Preet: GX10, models, the brain, integration
**Mission:** make the GX10 do real work everyone can see, and keep `main` green.
**Next, in order:**
1. GX10 over Tailscale: install vLLM, load **three different open models**, point Flock at them, run `scripts/llm_check.py`
2. **Offline run**: Wi-Fi off, the whole pipeline on a year of data with live local models
3. Throughput run: hundreds of judge calls in parallel; numbers for the benchmark
4. Merge everyone's PRs into `main`
**Owns:** `flock/llm.py`, `panel.py`, `ranker.py`, `learner.py`, `dispute_writer.py`, `clicky.py`, `portal.py`, `market.py`
**Done when:** `llm_check` shows three local models answering, and the demo runs with the network off.
**Demo moment:** "This is running on our own box. No internet."

## 👤 Thabhelo: ML
**Mission:** the one genuinely *trained* piece, and the proof table.
**Next, in order:**
1. **Benchmark harness** `scripts/benchmark.py`: held-out seeds (e.g. 101 to 110), the same findings for every contestant, results to CSV + JSON
2. **Anomaly detector**: learn each store × app's normal fees, commission and timing from history; flag jumps no rule covers; emit findings through the normal panel. Plant one new trap in the generator so the scoreboard proves it catches something new
3. Stretch: tune a local judge on training seeds only, then score it on the held-out seeds
**Owns:** `matcher.py`, `checkers.py`, `deal_keeper.py`, `flock/api.py`, new ML code
**Done when:** one command prints the benchmark table, and the anomaly trap is caught.
**Demo moment:** "It learned what normal looks like and caught this fee jump no rule covered."

<!-- ===== MESSAGE 5 / 7 ===== -->
## 👤 Himaja: UI (and still the data)
**Mission:** the 3-minute demo, on screen, smooth.
**Next, in order:**
1. Walk the demo flow on the dashboard (live board → progress bar → finding page → market → journal → scoreboard) and fix anything clunky
2. **Benchmark tab**: reads Thabhelo's results file. Human vs Claude vs local vs tuned: accuracy, seconds per finding, cost per 1,000, offline yes/no
3. Make the progress bar look great on the year-scale run
4. Keep the generator healthy (`profiles/`, traps, answer key)
**Owns:** `dashboard/`, `generator/`, `collector.py`, `formatter.py`, `profiles/`
**One UI rule:** all UI lives in `dashboard/` in this repo. Anything in another repo moves here.
**Done when:** someone who's never seen Flock can follow the demo just by watching the screen.
**Demo moment:** the year flying by on the progress bar, then one finding opened like a pull request.

## 👤 Ira: platform, pitch, process tracks *(confirm with Preet)*
**Mission:** SpacetimeDB as the live backend, and the story we tell.
**Next, in order:**
1. `SpacetimeStore` in `flock/store.py`: tables for findings, audit_log (insert-only), agent_status; the dashboard subscribes live
2. Pitch + video: the video must **open with the key catch/recovery** (Hyphenate requirement); say clearly what's real vs simulated
3. Process tracks: Figma stage log + Notability notes (log as we go; you can't rebuild these at the end)
**Owns:** `flock/store.py`, `gatekeeper.py`, `scripts/score.py`, pitch, video, Devpost
**Done when:** `FLOCK_STORE=spacetime` runs the Quickstart, and the video is recorded.
**Demo moment:** agents appearing live on the dashboard as the flock works.

<!-- ===== MESSAGE 6 / 7 ===== -->
## 📊 The benchmark (how we prove it)
We **don't claim** our model beats Claude. We measure it and show the table, including where we lose.
- **Test set:** held-out seeds no model is trained or tuned on. Labels = the answer key.
- **Contestants:** rules only · Claude · local off-the-shelf (GX10) · local tuned · **a human** (a teammate who didn't build the generator works one store-week from the raw files, with a spreadsheet and a timer)
- **Same inputs for every model.** Money math is code for all of them, so we compare judgment, not arithmetic.
- **Metrics:** traps caught · false alarms · correct dollars · sent to a human · seconds per finding (p50/p95) · findings per minute · $ per 1,000 findings · works offline · human minutes per store-week

## 🎤 The 3-minute demo (draft)
- **0:00** "Noah's NY Bagels, 56 California stores." Plant the depth: same short payout, three different right answers depending on which deal was in effect
- **0:30** Wi-Fi off, run a full year: 1.24M orders, three local models judging
- **1:15** One finding like a pull request: sources, deal term, votes + bets, reviewer memo, human decision, journal entry. "Try as an agent" gets refused
- **2:00** It learns: week 1 vs week 2 hand-offs; the judge that backed an unauthorized rep lost credits
- **2:30** The benchmark table. Close: "If we save you $3, you pay us $1."

<!-- ===== MESSAGE 7 / 7 ===== -->
## ⚙️ How we work tonight
1. **Branch per task** (`thabhelo/anomaly`, `himaja/benchmark-tab`, `ira/spacetime`), PR into `main`
2. **Before merging:** the Quickstart prints *caught 6/6, 2 then 0 sent to a human*, and `pytest` passes
3. **Never commit** generated data, `rules.json`, `.env` or API keys
4. **The rules don't bend:** money math is code · agents propose, humans approve · only the gatekeeper files or posts · every row keeps its source file and row
5. Stuck for 20+ minutes? Say so in the channel. Don't sink an hour

## ⏰ Timeline
- **Sat evening:** GX10 reachable · benchmark harness + anomaly detector on branches
- **Sat night:** three local models judging · offline run · benchmark tab · SpacetimeDB
- **Sun 3:00 AM: FEATURE FREEZE.** Fixes only
- **Sun 3 to 7 AM:** rehearse, record the video, write the Devpost
- **Sun 9:00 AM: SUBMIT** (repo, project image, working demo, video)

Questions → ask in the channel or read `docs/PLAN.md`. Let's go 🐔
