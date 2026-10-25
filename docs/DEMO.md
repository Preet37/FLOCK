# The 3-minute demo (run sheet)

**One story:** 100K transactions a month collapse into a few lump-sum deposits. Flock breaks a lump sum down, finds
the few mistakes, and shows what's still owed.

Everything on stage is prepared the night before by `python -m flock.showtime prepare`, runs on localhost, and needs
no network. All data is **simulated** (Ringtide Bagels, the fictional 56-store `ca_bagel_chain` profile).

Every dollar figure you say comes from `data/state/showtime.md`, which `prepare` computes from that run's findings
and generated answer key; the lump-sum figures are exactly what the dashboard's **Lump sums** tab shows
(`flock/payouts.py`). The numbers quoted below are what our run printed (seed 2025, Oct 25); if you
change the seed or the code, read the new cheat sheet, not this page.

## The night before

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
export FLOCK_OFFLINE=1                       # or set up live judges first (see "Live providers" below)
python -m flock.showtime prepare             # ~50 s: 4 prior months + September 2026, prints the cheat sheet
python -m flock.showtime status              # "Headline F-...: JUDGED (ready for the presenter)"
cat data/state/showtime.md                   # print it; this is what you read from on stage
python -m dashboard.app                      # http://127.0.0.1:5050 (leave it running)
python -m flock.api                          # optional: http://127.0.0.1:8787 for the frontend / curl fallback
```

Rehearse the clicks as often as you like, then put the prepared state back:

```bash
python -m flock.showtime restore             # 0.2 s: findings, audit log, market, runs, portal, rules as prepared
```

`python -m flock.showtime reset` goes back to the plain Quickstart (fixtures, week 1, 2 sent to a human);
`restore` brings the show back afterwards. Generated files live in `data/showtime/` (gitignored). `prepare` never
writes `data/answer_key.csv` or `mappings.json` (it uses a temp copy).

### What `prepare` builds

| Month | Scenario | What happens |
| --- | --- | --- |
| May to Aug 2026 | `prior_month`, plans 0-3, a different seed each | Full pipeline (findings rolled up per lump sum). The demo human approves recoverables and rejects what was sent to a human; Clicky files; the platform pays every claim except one in August; the Learner proposes rules and the demo human approves them before the next month. |
| Sep 2026 | `demo_month` | Same company, plus an amendment that cut DoorDash from 25% to 20% on Sep 1 that DoorDash ignores at 3 stores all month. The demo human makes the routine calls, **except the headline finding and anything sent to a human**: those wait for you. |
| (carried) | | August's unpaid claim is re-opened in the demo state (replayed through the gatekeeper with its original SBX- reference) and the follow-up agent drafts a reminder, waiting for a human. |

The scenario on its own: `python generator/generate.py --scenario demo_month --seed 2025` prints the month's
volumes, each planted incident with its answer-key rows and dollars, and the big error per weekly payout, all computed
from the generated data and saved in `scenario.json` next to the answer key (the collector never picks it up).

Planted on the demo month (8 incidents): DoorDash's old commission at 3 stores (big), an Uber Eats deposit short of
its statement, an Uber Eats BOGO charged 100% to the store, a DoorDash error charge after the 72-hour window, a DoorDash
order sold but never paid, a legitimate DoorDash error charge (must NOT be disputed), a Grubhub rep with no authority
promising a fee waiver (a rule learned in May settles it), and an Uber Eats rep with no authority promising one (new:
it goes to a human).

## On stage (3:00)

Open the dashboard on the **Live board** with the cheat sheet next to you. "Acting as" = your name.

| Time | Click | Say |
| --- | --- | --- |
| 0:00 | Live board | "Ringtide Bagels, 56 stores, four delivery channels. In September: 102,730 orders. The bank saw 20 deposits. Nobody can check 100,000 lines by hand, so nobody does." |
| 0:25 | **Lump sums** tab, open DoorDash DD-PO-20260901 | "Take one deposit: DoorDash payout DD-PO-20260901, 8,114 orders from 56 stores, one wire of $131,085.91. By the contracts it should have been $131,635.24. Flock broke it down line by line (the waterfall) and found why: three stores are still being charged 25% after the amendment cut it to 20%. $549.33 in this one deposit, $2,019.17 this month." |
| 0:45 | **Human queue** tab: the headline (`F-...`, the one drafted dispute). Type "amendment says 20%", click **Approve**, then **File 1 approved via Clicky** | "Agents propose, people approve, and only the gatekeeper files. Clicky files it in the platform's portal; here's the reference." (toast: SBX-...) |
| 1:20 | Same tab: the Uber Eats conflict, click **Reject** with "not binding: rep not on the contract" | "This one Flock won't decide. An Uber Eats rep promised a fee waiver, but he's not on the contract's authorized contacts. A person decides. In May a Grubhub rep did the same thing twice; a person said no twice; now that's a rule, and this month it was settled without anyone." |
| 1:50 | **Scoreboard** tab | "Hand-offs to a human by month: 5, 4, 3, 1, 1. Every planted mistake caught: 1,945 of 1,945 answer-key rows, zero false alarms." Then **Judge market** tab: "the judges bet on their calls; the honest ones gain weight." |
| 2:20 | Terminal: `python -m flock.agents.follow_up` then `... approve F-... --actor <you> --send` | "And what's still owed: in August we filed a claim for a short Uber Eats deposit, $55.38, and it was never paid. Flock drafted the follow-up with the claim number and the evidence; I approve it and Clicky sends it." |
| 2:45 | Live board | "5.9 seconds of agent time for 280 store-weeks a person would spend an hour each on. That's Flock." |

The hours figure is an ASSUMPTION (60 minutes per store-week, cited in the metrics card), not a measurement: say
"we assume an hour per store a week" if asked.

## Fallbacks

| If | Do |
| --- | --- |
| Wi-Fi dies | Nothing on stage needs it: the dashboard, API and sandbox portal are localhost, the state is on disk, drafts and votes were written during prep. Keep `export FLOCK_OFFLINE=1` in the stage terminal so nothing even tries. |
| The dashboard won't load | `python -m flock.showtime approve --actor <you>` (same gatekeeper move, then Clicky files it) and read the numbers from `showtime.md`. Or the API: `curl -X POST http://127.0.0.1:8787/api/findings/<id>/approve -H 'Content-Type: application/json' -d '{"actor": "<you>"}'` (approve also files). |
| A rehearsal left the headline approved | `python -m flock.showtime restore` (0.2 s). `status` says "ready for the presenter" again. |
| The state is gone or broken | `python -m flock.showtime prepare` (~50 s offline). Worst case `python -m flock.showtime reset` and tell the Quickstart story: week 1 sends 2 to a human, a rule is approved, week 2 sends 0. |
| The laptop dies | The hosted site (Railway) shows an older 56-store run; have a screenshot of the cheat sheet and the queue. |
| Clicky in a real browser | Leave `FLOCK_CLICKY_MODE` unset on stage (file mode). Browser mode is a separate, rehearsed demo. |

## Live providers

`prepare` works with whatever `flock/llm.py` is configured for (CREAO, GX10, Claude). The pipeline applies
`FLOCK_LIVE_JUDGE_BUDGET` as usual: the riskiest findings get live judges, the rest use offline rules and are marked
`live_budget: outside`; each month's line in the prepare output says "live judges on N of M". With the rollup a month
has about 10 to 12 findings, so a budget of 25 covers every finding. We did not time a live prepare on this branch
(no keys in the worktree): run it at least an hour early, then `status` and rehearse. Judges' votes, drafts and memos
are saved in the state, so the stage itself needs no model call.

## Timings (measured, MacBook, offline, Oct 25)

| Step | Time |
| --- | --- |
| `generator/generate.py --scenario demo_month` (56 stores, 102-113K orders) | ~5 s |
| One month through the pipeline in `prepare` (generate + collect + format + match/check + judge + file) | 9 to 11 s |
| `showtime prepare` (4 prior months + the demo month, 56 stores) | 49 to 51 s |
| `showtime status` / `restore` / `reset` | 0.26 s / 0.24 s / 0.26 s |
| Dashboard approve / File via Clicky (file mode) | 12 ms / 5 ms |
| API `POST /api/findings/<id>/approve` (approves and files) | 20 ms |
| `prepare --stores 5 --months 2` (the test) | ~3 s |

## Our run's cheat sheet (seed 2025)

```
## The story
- 102,730 orders in September 2026 across 56 stores and 4 channels collapsed into 20 lump-sum bank deposits ($1,604,396.10).
- By the deals in force they should have been $1,606,518.87: $2,122.77 short (answer key).
- The big one: DoorDash kept charging 25% at S28, S38, S41 after the amendment cut it to 20% on 2026-09-01: 1,912 orders, $2,019.17 this month.

## Headline finding (approve it live)
- F-e7127388: DoorDash commission, gap $549.33 (457 orders at S28, S38, S41)
- Sits in DoorDash payout DD-PO-20260901, deposit BNK-2063106 on 2026-09-10: one lump sum for 8,114 orders from 56 stores (dashboard: Lump sums tab)
- Lump sum: expected $131,635.24 | arrived $131,085.91 | short $549.33 (findings inside: F-e7127388 commission $549.33)
- Answer key for the same deposit: expected $131,652.92, deposited $131,085.91, short $567.01 ($17.68 of it is an order sold but never paid, which belongs to no lump sum on the dashboard: $549.33 on the paid orders)

## Metrics cards
1. Agent time: 5.929s of agent wall time vs 280.0 human hours (280 store-weeks x 60 min, ASSUMPTION)
2. Accuracy: 1,945/1,945 answer-key rows (100.0%), 0 false alarms
3. Money found: $2,182.57 (filed $1,633.24, recovered $0.00, pending $549.33)

## Human hand-offs, month over month
May 2026: 5 | June 2026: 4 | July 2026: 3 | August 2026: 1 | September 2026: 1
```

Finding ids and SBX- references change on every `prepare` (they're random); the dollar figures and counts are the
same for the same seed.
