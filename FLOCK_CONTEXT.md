# Flock: Full Project Context (Cal Hacks 13.0, Hyphenate Track)

Last updated: Saturday Oct 24, 2026 (company and owners synced late Saturday; first written ~5:20 AM PT)
Submission deadline: Sunday Oct 25, 9:00 AM local. Judging 10:30 AM to 12:30 PM.
Team: Himaja (Maja), Thabhelo, Preet, Ira

This file is the background for the team: problem, research, pitch, plan. Paste it into Claude (or any agent) at the start of a session so it has full context.
For what the code does today, `CONTRACT.md` (rules, data shapes, state machines, traps 1-22) and `docs/STATUS.md` (what's built, measured, simulated) win over anything here.

**The company: Noah's NY Bagels**, about 56 locations across California, a mid-market chain. All data is **simulated** with `--profile ca_bagel_chain` (a fictional 56-store California bagel chain; the generated files use the made-up brand "Ringtide Bagels"). No real Noah's records, people or contract terms are used. Dave's Hot Chicken was the first target; its sourced facts are kept below as earlier research.

---

## 1. The one-liner

Flock does the boring job a restaurant pays an accountant to do by hand: it takes the messy money reports from DoorDash, Uber Eats, Grubhub and the restaurant's own app, sorts them all into one place, and checks them, faster and cheaper than a person can.

Name: Flock = a flock of agents (the name came from the first target, a hot chicken chain: chicken / wings).

---

## 2. The problem (5 Ws, using Noah's NY Bagels)

**Short version (ELI5)**
- **What:** Noah's gets paid by four food apps, and each one sends money and reports in a different, messy way.
- **Why:** Each app has its own deal with Noah's, and none of them talk to each other.
- **When:** Every week, when the money shows up and never matches what the stores actually sold.
- **Where:** On an accountant's desk, stuck in four websites and a pile of spreadsheets.
- **Why it matters:** Noah's pays someone to sort it all by hand, it's slow, and mistakes mean Noah's loses money it already earned.

**Longer version**
- **What is it?** Noah's NY Bagels, a chain of about 56 stores across California, gets paid by DoorDash, Uber Eats, Grubhub and its own app, and every one of them sends money and reports in a different, messy format, under different deals.
- **Why does it exist?** Each app is a separate company with its own contract, commission tiers, promos and refund rules, and none of their systems talk to Noah's register, bank or contracts.
- **When does it come into play?** Every week, when the payouts land in Noah's bank as lump sums that never match what the register says the stores sold.
- **Where does it live?** In Noah's accounting team, on a staff accountant's desk, spread across four app portals, the register, the bank account, the contracts and a pile of spreadsheets.
- **Why does it matter?** Someone gets paid to sort and check all of it by hand, which is slow and expensive, and because it's slow and manual, mistakes slip through and Noah's quietly loses money it already earned.

**Two halves of the cost (use both in the pitch)**
1. Cost of doing the work: paying people to manually sort messy data from four apps.
2. Cost of doing it badly: because it's slow and manual, mistakes slip through and money is lost.

Flock fixes both.

---

## 3. How the money actually flows (so everyone can explain it)

Money only flows one way: the app pays Noah's. Everything Noah's "owes" the app is subtracted before the money arrives. Accountants call this netting.

Example:
1. Customer pays DoorDash $10 for a Noah's bagel order.
2. DoorDash subtracts its commission (example: 30% = $3).
3. DoorDash subtracts Noah's share of any promo.
4. DoorDash subtracts any error charge (the "my schmear was missing" refund).
5. Whatever is left lands in Noah's bank, bundled with every other order that week.

The weekly payout is one number with a pile of hidden subtractions. The question is: was each subtraction correct according to the deal?

Gym analogy (Ira's framing):
| Gym | Noah's |
| --- | --- |
| Contract: gym membership | Noah's contract with DoorDash, Uber Eats, Grubhub |
| Payment structure: $16 biweekly | Commission %, promo split, fees, refund rules |
| Invoice with a deadline | The weekly payout statement each app sends |
| Billed $16, paid $14, follow up | App owed X, paid less, is the gap right? If not, dispute |
| Double charged, give it back | Same error charge taken twice, get it back |

---

## 4. Company, role, and what we automate (Hyphenate slide 4 framing)

- **Company:** Noah's NY Bagels, about 56 locations across California: a mid-market chain (Hyphenate's smallest customers are around $75M revenue). Data is simulated with `--profile ca_bagel_chain` (56 stores, morning-heavy, weekend peaks, catering; ~1.2M orders a year). [need: a sourced revenue figure before quoting one on stage]
- **Roles we replace:** (1) the delivery reconciliation staff accountant, (2) the disputes clerk. The controller stays human and approves disputes and new rules.
- **Hyphenate teams this maps to:** Receivables ("payments in, short or late; who to chase") + Cash ("every bank line matched to what it pays").
- **Pitch line:** "We replace the delivery reconciliation accountant and the disputes clerk at a multi-unit restaurant chain like Noah's NY Bagels."
- **What we automate:** checking every delivery order's payout against the deal actually in effect, then recovering what's short, with proof for every dollar.
- **Why "receivables" even with no invoice:** a receivable is just money someone owes you. The moment a customer orders on DoorDash, DoorDash owes Noah's. The weekly payout pays that debt.
- **Q&A answer if asked about close or rev rec:** "We deliberately stayed in receivables and cash, because one corner done deep beats ten done shallow." (Hyphenate's own line.)

**Hyphenate's four questions (slide 8), answered**
| Question | Our answer |
| --- | --- |
| Where does it act? | Delivery payouts: the register, 4 platform statements, the bank, contracts, emails |
| What does it remember next quarter? | The deals, past dispute outcomes, rules learned from human decisions |
| Who does it work with? | The controller (approves disputes and rules) and the platforms' dispute portals |
| What may it do, and can it prove why? | Propose and draft disputes only; a human approves; every step is in the audit log |

---

## 5. What Hyphenate wants (from their slides, workshop, and conversations)

**About Hyphenate**
- Formerly Maximor, renamed Oct 1, 2026. Same company whose track Preet's team placed 4th in at HackMIT with Money Maxer (short-wire matching / cash application). Don't repeat that.
- "Autonomous finance platform" on top of existing ERPs (NetSuite, SAP, Intacct). Five areas: order-to-cash, treasury, GL and close, procure-to-pay, reporting.
- Customers mentioned: Mini Melts, Rently, Dura, Sierra, Decagon, Zuma (restaurant group), Sephora-type retail, hospitality.

**Judging criteria**
1. Is it real enough?
2. Would you hire it? (most important, per their answer)
3. Did we see something new?
4. Did it have a "wow" moment?

Prizes: $8K / $5K / $3K. Top 5 teams: expedited interviews for every member.

**Strong vs weak submissions (slide 19)**
- Strong: a real company with a messy corner and an answer key; a few roles done deep, named in one line; asks at the right moment and handles a no; limits enforced in code; a record of every call; a moment on stage and a score to back it.
- Weak: a chatbot over a spreadsheet; ten workflows none deep; clean happy-path data; an admin key and "the prompt tells it not to"; memory that ends with the context window; "trust me."

**A good system of work (slides 11 to 18)**
- Self-learning: learns the job at onboarding from company history.
- Self-improving: every human correction becomes precedent, then a proposed policy.
- Audit trail: append-only, "why?" is one query months later.
- Build pieces: sync records statefully (raw, standardized, golden, each row tracing to raw), map actors/actions/resources (who may do what), tools are CRUD on the work via a state machine, log every transition as a diff including denied attempts.
- Example state machine: NEW -> PROPOSED -> (ask/answer NEEDS_INFO) -> APPROVED -> POSTED. Only the gateway posts. Separation of duties: you can't approve what you proposed.

**Advice from the Hyphenate people we talked to (Rudraksh, Najib, Pavan's team)**
- Don't try to solve finance end to end. Pick one narrow module, go deep.
- Pass the "$3 for $1" test: mid-market pays $1 only if you save them $3. Must be quantifiable.
- Start from a company you know as a buyer and reverse engineer how the money flows.
- Mid-market (~$75M to $400M revenue, usually on NetSuite) is the right target. Enterprise needs consulting-style transformation; not a weekend build.
- Make it audit-ready. "Audit trail is the most important thing. If you guys can build something around that, you guys will win." An audit trail works like a pull request.
- Hyphenate is ~98% autonomous, 2% human: the human approves and posts to the ERP, because auditors won't sign off on all-AI.
- The product is the decision, not the notice. Sending the email is "five seconds on top."
- Plant the depth in the first 30 seconds or judges hear the shallow version.
- For receivables, the hard part is ingesting payments and statements that arrive in different forms, maintaining context across too much data, and applying each customer's terms.
- "Many truths" problem: sources contradict (contract says one deadline, an email says another). Must keep context and stay auditable.
- Don't worry about overlapping with Hyphenate or asking for their internal pain.
- Judging is fast (70 to 80 submissions, about an hour). Optimize the demo. Show a flow/mock and they'll give better feedback.
- Lunch invite: Saturday 11:45 AM. Bring a rough flow. Ask Najib why the Toronto discount example made him pause.

**Decagon example (the story that made it click)**
- Decagon (AI customer service) bills airlines per conversation and per successful resolution. American: 10 cents / 70 cents. Delta: 5 cents / 40 cents. Tiers drop after volume thresholds and in year two. "Success" is disputed (user leaves without saying thanks). Someone has to decide what counted and apply each customer's negotiated tier. That's revenue recognition.
- Lesson: negotiated, per-customer terms + messy evidence + volume = deep problem. Flock is the restaurant-side version: per-platform negotiated deals + messy statements + volume.

---

## 6. Research and evidence (with sources)

**The role exists and is manual today**
- Multi-unit restaurant staff accountant postings list: audit and reconcile cash deposits and third-party delivery platforms (DoorDash, Uber Eats). https://app.qureos.com/jobs/staff-accountant-1785713286262
- Another restaurant group posting: process refund disputes and chargebacks across Toast, Uber Eats, DoorDash; reconcile delivery charges and post payment. Reports to Director of Finance. https://recruiting.paylocity.com/Recruiting/Jobs/Details/3853452
- Another posting: tie platform payouts to gross sales, commissions, marketing fees, chargebacks and adjustments across POS, third-party delivery, and bank. Also asks the accountant to document workflows "clearly enough to be automated" (KoreFi, building AI coworkers for restaurants). https://apply.workable.com/ai-accountant/jobs/view/CBDDB00959.md
- A $5M LA hospitality group posted a staff accountant role at $70,000 including POS, cash, tips and delivery platform reconciliation. https://www.appleone.com/CareerSearch/JobDetail/Staff-Accountant-1776310WR

**Killer proof point**
- A California bakery found DoorDash charged ~30% commission for nearly a decade when the contract said 13%, costing over $100,000. https://www.aol.com/california-bakery-paid-extra-100k-121500910.html

**How much leaks**
- Industry panel (Freddy's exec): 2.5 to 3% of an operator's total revenue gets caught up in delivery disputes leading to chargebacks; 1.5 to 2% of orders produce refund requests in some markets; a dispute firm on the panel wins 80 to 90%. https://foodondemand.com/05172024/its-really-just-theft-freddys-exec-says-of-chargebacks-at-fodc/
- Lunchbox (vendor claim): up to 5% of third-party orders end in disputes; reconciling one location can take an hour. https://lunchbox.io/third-party-disputes-the-silent-restaurant-revenue-killer
- Voosh (vendor claim): disputes and downtime can erase 3 to 5% of revenue. https://chargebacks911.com/podcast-episodes/third-party-delivery-first-party-fraud/

**DoorDash rules we can enforce in code**
- Error charges are deducted directly from payouts; restaurant may be charged 25% to 100% of item price plus tax depending on severity; wrong order handed to Dasher = 100% of subtotal plus tax.
- If customers report errors 72+ hours after delivery, DoorDash absorbs the cost (no merchant error charge).
- Merchants can only dispute a certain percentage of error charges in a period before contacting support (so you must pick which to fight).
- Financial reports with error charges are downloadable from the Merchant Portal.
- Sources: https://help.doordash.com/merchants/s/article/How-Do-I-Dispute-Error-Charges , https://merchants.doordash.com/en-us/learning-center/doordash-merchant-refund

**Dave's Hot Chicken facts (earlier research, from when Dave's was the target; not about Noah's)**
- Opened its 500th restaurant Oct 9, 2026, ~100 locations in six months. https://www.fastcasual.com/news/daves-hot-chicken-hits-500-restaurants-adds-100-units-in-6-months/
- Roark Capital bought a majority stake in 2025 at ~$1B valuation. (This is a valuation, NOT sales. Don't say "$1B in sales.") https://lexpress-franchise.com/en-us/news/daves-hot-chicken-500-restaurants-franchise-growth/
- 95% of Dave's franchisees are multi-unit operators. https://www.franchising.com/articles/20260829_the_2026_multiunit_50_a_snapshot_of_scale.html

**Market sizing inputs**
- Multi-unit operators with 26 to 50 units: 615; with 50+ units: 504 (2025, all franchise sectors, not restaurant-only). https://www.franchise.org/wp-content/uploads/2026/06/FRANdata-MU-Ownership-Trends-2026-Ambika.pdf
- [need: restaurant-only count of large operators]
- [need: third-party delivery share of sales for fast casual]

**Competitors (be ready for "isn't this Loop?")**
- Loop ($6M seed): POS vs third-party vs payout vs bank reconciliation, journal entries. https://foodondemand.com/?p=19747
- Voosh: chargeback recovery, reconciliation, analytics. https://www.olo.com/partners/voosh
- Restaurant365: automated dispute filing across platforms. https://www.restaurant365.com/accounting/automated-dispute-resolution/
- Lunchbox: dispute manager, claims 82% win-back.
- Checkmate: refund dispute program, takes 8% of recovered revenue (pricing benchmark). https://itsacheckmate.com/blog/when-small-refunds-become-big-losses
- KoreFi: AI coworkers for restaurant accounting (still uses human accountants).

**Our differentiation (what they mostly advertise vs what we do)**
- They: dashboards + error-charge disputes, app vs register.
- We: check every payout against the actual signed deal (contract, amendments, rep emails), handle conflicting sources with an authority ranking, decide when to bring a human in, learn rules from human decisions, and prove every dollar with a PR-style audit trail.

---

## 7. The numbers (worked example, assumptions labeled)

| | Example: 60-store group |
| --- | --- |
| Reconciliation time per location (vendor claim ~1 hour, [need: per week? verify]) | ~60 hours/week |
| That's roughly | 1.5 full-time staff accountants |
| Salary reference | $70,000/yr |
| Labor cost (before benefits) | ~$105K/yr |
| Delivery sales (assumption: 15% of ~$150M) | ~$22M/yr |
| Lost to disputes and deductions (2.5% panel figure) | ~$560K/yr |
| Total problem per customer | ~$665K/yr |

Range for $150M to $400M chains (delivery share 10% to 20%, 2.5% leakage, 80% recovery): ~$300K to $1.6M/yr recovered, plus labor saved.

**Pricing:** base fee per location + share of what we recover (outcome-based pricing, Hyphenate slide 10). Checkmate takes 8% of recovered, so don't charge "a third." Something like 8 to 15% of recovered + small base, justified by catching contract errors others miss.

**$3 for $1:** customer keeps 85 to 92 cents of every recovered dollar plus labor savings.

---

## 8. Pitch material

**30-second hook (problem + market)**
> "A family bakery in California used DoorDash for years. Their contract said 13% commission. DoorDash charged them 30%, for almost a decade, and nobody noticed until they'd lost over $100,000.
> That's not rare. Industry operators say 2 to 3% of restaurant revenue gets lost in delivery app disputes and deductions. And today the only thing checking is a staff accountant with four spreadsheets, a week late.
> Take Noah's NY Bagels: about 56 stores across California, four apps, four different deals, every week. Every chain that size has this problem, and pays someone to miss it."

**Alternate hook (relatable)**
> "You've ordered bagels on DoorDash. Here's what you never see: the store got paid less than the order total, and nobody can tell you if that's right. Commission, a buy-one-get-one DoorDash was supposed to help pay for, a refund for a schmear someone claimed never came. Now do that across four apps, dozens of stores, every week. Tools today catch the missing schmear. We catch the broken deals, and prove every call."

**One-line problem:** "Mid-size restaurant groups pay accountants to manually piece together four delivery apps' messy reports, and still lose 2 to 3% of revenue to mistakes they never catch. Flock does that job automatically, faster and more accurately, for less than the people cost."

**Q&A prep**
- "Isn't this Loop/Voosh?" -> "They catch error charges. We check every payout against the actual deal, handle conflicting sources, learn from each human decision, and prove every call."
- "Which team?" -> "Receivables and cash: the staff accountant who reconciles delivery payouts and the person who files disputes. Here's a real job posting."
- "What do they do today?" -> "Download four statements, match them to the register and bank in spreadsheets, file disputes by hand on each portal, usually a week late."
- "Why can't they just ChatGPT it?" -> "A one-time answer goes stale. Deals change, formats change, and someone has to maintain and learn. Flock remembers decisions and keeps the audit trail."
- "Is the data real?" -> "Simulated, with planted traps and an answer key. In production the collector pulls reports from each portal; DoorDash already offers downloadable financial reports."
- "Where's the AI vs code?" -> "AI only for messy formats, reading contracts, and judgment. All math is plain code."

---

## 9. The pipeline (plain words, following one order)

Example: Sam orders $20 of bagels from Noah's on DoorDash Saturday morning. (Numbers illustrative.)

1. **Collect:** grab register, 4 app reports, bank deposits, contracts, rep emails. Save untouched.
2. **Read:** translate every file into one standard format. No deleting, no deciding. Every row remembers its source file and row.
3. **Connect:** link every record about the same order (register #881 = DoorDash line A92 = part of Tuesday's deposit). Unlinked records are findings ("sold, never paid").
4. **Decide:** work out the deal in effect, compute what Noah's should have been paid (plain code), compare to actual, and put it in a bucket:
   - Correct: close it.
   - Recoverable: clear mistake with proof.
   - Conflict: sources disagree or no precedent, ask a human.
5. **Rank:** sort recoverable mistakes by dollars x chance of winning, within each app's dispute limit.
6. **Human approves:** nothing is filed without a click. Dispute drafted with evidence.
7. **Learn:** each human decision becomes a precedent; repeated ones become proposed rules.
8. **Audit log underneath everything:** who/what did it, what changed, why, evidence.

Collect -> Read -> Connect -> Decide -> Rank -> Human approves -> Learn, all logged.

**Cleaning vs standardizing vs connecting vs deciding (settled)**
| Word | Meaning | Example |
| --- | --- | --- |
| Standardizing | Same shape and units | "$12.50" text and 12.5 become 12.50 |
| Cleaning | Remove only definite junk | Exact duplicate row in one file |
| Connecting | Link records about the same order | Register #881 = DoorDash A92 = deposit |
| Deciding | When connected records disagree, which is true and who's accountable | Refund log says "initiated" but no money moved |

Rule: cleaning never deletes a disagreement.

**Authority ranking (for "many truths")**
1. Signed contract
2. Signed amendment
3. Email from someone authorized to change terms
4. Email from someone not authorized
5. Note from a phone call

Stronger wins automatically. Equal strength or unknown authority -> conflict -> human.

**When a human is brought in**
1. Equal-authority sources conflict, or authority unknown.
2. Dollar amount above a threshold.
3. Never-seen-before situation.
4. Anything that moves money or gets filed (always).
5. The judge panel disagrees (see Panel below).

---

## 10. Where real data comes from (production) vs hackathon

| Source | What they get | Real life | Hackathon |
| --- | --- | --- | --- |
| DoorDash | Payout report + error charges/adjustments report | Merchant Portal download | Simulated CSV |
| Uber Eats, Grubhub | Payout/statement reports | Merchant portals [need: confirm report names] | Simulated CSV / xlsx |
| Direct app | Orders, refunds, delivery partner bill | Ordering system export | Simulated CSV |
| Register (POS) | Every order | Toast/Square exports or APIs | Simulated CSV |
| Bank | Deposits | Statement download or Plaid | Simulated CSV |
| Contracts, emails | Deals, amendments, authorized contacts, rep promises | PDFs, inbox | Simulated JSON, .txt and .eml |
| Slack, call notes | Rep messages in shared channels, meeting notes | Slack export / a CREAO connector watching Slack and Gmail (`docs/connectors.md`) | Simulated Slack export + .md notes |

Say on stage: "In production the collector downloads these from each portal; here it reads simulated exports."

---

## 11. The agents

Rule: AI only where things are messy or need judgment. All math in plain code.

| # | Name | AI or code | Job |
| --- | --- | --- | --- |
| 1 | Collector | Code | Picks up all files (incl. Slack exports, .eml, .md notes, connector drops), saves raw untouched |
| 2 | Formatter / Format Mapper | AI (mapper role) + code | Maps unknown columns to the standard format, asks a human once, remembers |
| 3 | Document Reader | AI (reader role; local on the GX10 or CREAO) | Reads free-text deal messages the rules can't parse; keeps a claim only if its quote is verbatim |
| 4 | Matcher | Code | Links each order's records together; unlinked records become missing_order findings |
| 5 | Deal Keeper | Rules (+ reader) | Effective deal per store/app/date: newest binding source wins, authority from the contract's authorized contacts |
| 6 | Checkers | Code | Commission, promo, tax, fee, error charges, refunds, deposit; one worker per store in parallel |
| 7 | Anomaly Detector | Code (learned baselines) | Learns each store x app's normal fees/commission/timing; flags jumps no deal term covers |
| 8 | Investigator | Code (+ writer model for the summary) | Spawned only for an unexplained finding; attaches a case file, never changes an outcome or a dollar |
| 9 | Panel (judges) | AI | 3 judges vote and bet; one debate round on a split; agree = decide, disagree = human (reviewer writes a memo, never breaks the tie) |
| 10 | Judge market | Code | Brier-scored bets settle on human decisions; balances weight the votes; attention budget orders the human queue |
| 11 | Ranker | Code | Ranks disputes by value x win chance within dispute limits |
| 12 | Dispute Writer | AI (writer role) | Drafts dispute with evidence |
| 13 | Clicky | Browser automation | Files approved disputes into a FAKE sandbox portal (never real); learns the form from one human demonstration |
| 14 | Journal Entry Drafter | Code | Balanced GL entries per store x channel x payout; a human approves, the gatekeeper posts the import CSV |
| 15 | Learner | Code | Proposes rules from human decisions with evidence counts; a human approves |
| 16 | Gatekeeper / Logging | Code | Permissions, transition(), append-only log |

**State machine:** NEW -> CHECKED -> JUDGED -> APPROVED (human only) -> FILED (gatekeeper only) -> RECOVERED, plus JUDGED -> REJECTED (human). Journal entries and Clicky's learned workflows have their own machines through the same gatekeeper. Full tables: `CONTRACT.md`.

**Betting, done right:** the Panel's confidence bets decide when to bring a human in. Disagreement between judges = escalate. That's the "betting" idea with a real job.

**Learning, done right (no model training needed):**
- Onboarding: replay history weeks, propose rules with evidence counts ("41 of 41 late error charges won on dispute").
- Every day: each human decision is a precedent; similar ones become proposed rules; human approves.
- Rules stored as readable entries (rules.json), not hidden in model weights.
- Demo it: week 1 sends N conflicts to a human, week 2 sends fewer with the same accuracy.

**GX10 use:** local models (Ollama over an SSH tunnel, `scripts/gx10.sh`) as judges, writer, mapper and reader; `FLOCK_AIRGAP=1` proves no call left the network ("restaurant financials never leave the building"). Preet's HackMIT paper (small-model document extraction for the financial close, Zenodo DOI 10.5281/zenodo.22927774) is directly relevant if reused.

---

## 12. Shared contract (CONTRACT.md)

The contract started here as a short sketch on Saturday morning. It has grown with the code, so the living version is
`CONTRACT.md`: the rules, folders, every table and row shape (std tables, deal terms with authorized contacts and
locators, findings with votes / case files / attention, journal entries, workflows, the market ledger, agent status
events, log entries with `subject_type` and `diff`), all three state machines with who may do each move, the
"newest binding document wins" rule, check types (incl. `fee` and `anomaly`) and traps 1-22.

Rule (unchanged): any change to a finding goes through one function, `transition()`, which writes the log entry. No agent edits a finding directly.

---

## 13. Edge cases (traps for the data generator and answer key)

Each goes in data/answer_key.csv with order_id, trap_type, correct outcome, correct dollar amount.

**Read**
1. Same platform file uploaded twice (exact duplicate rows)
2. A statement in a new, unseen column layout

**Connect**
3. Order in POS that never appears in any payout (sold, never paid)
4. Platform order with no matching POS order
5. One bank deposit covering two weeks of payouts
6. Payout statement total doesn't match the bank deposit

**Decide**
7. Commission charged at the wrong tier
8. Buy-one-get-one charged 100% to the store when the contract says 50/50
9. Promo charged outside its date range
10. Error charge for a complaint made 72+ hours after delivery
11. Same error charge applied twice
12. Sales tax deducted even though the platform collected it
13. Refund log says "initiated" but no money moved
14. Rep email waives a fee, sender authorized -> recoverable
15. Rep email waives a fee, sender NOT authorized -> conflict
16. Legit error charge -> correct, must NOT be disputed (tests false alarms)
17. Penny rounding differences -> ignore within tolerance
18. The direct app's payment processor quietly raises its processing rate for one store-week; no contract term states a flat fee, so only history can catch it -> recoverable (Anomaly Detector)

**Messy deal sources (Slack only; planted with `--sources ...slack...`, the default for `ca_bagel_chain`)**
19. An authorized rep waives a fee in the shared Slack channel -> recoverable
20. A rep NOT on the contract's authorized contacts promises a waiver in Slack -> conflict
21. A thread reply corrects the waiver to a different day: the new day -> recoverable
22. ... and the day the first message named is billed as normal -> correct

**Data generation approach**
- Seeded Python generator. Build the TRUE world first, then render each source with its own quirks, then plant traps and write the answer key.
- Use Claude only for text parts (rep emails, complaint text, contract wording).
- Different seeds = different weeks. History weeks for learning; one fresh week generated live for judges.

---

## 14. Team split and prompts

**Current owners (late Saturday Oct 24; `docs/PLAN.md` has the live task list)**

| Who | Owns now |
| --- | --- |
| **Preet** | Hosting (Railway deploy of the API, `docs/DEPLOY.md`) and all ML + benchmarking: GX10 models, local and tuned judges, `scripts/benchmark.py`, `scripts/judge_*.py`, `scripts/gx10.sh`, the anomaly detector, `flock/llm.py`, `panel.py`, `market.py`; merging into `main` |
| **Thabhelo** | CREAO: every AI role as a hosted CREAO agent (`scripts/creao_setup.py`), the Flock Judge built on agent.creao.ai (track requirement), CREAO connectors for Slack/Gmail (`docs/connectors.md`); also `flock/api.py`, `matcher.py`, `checkers.py`, `deal_keeper.py` |
| **Himaja (Maja)** | UI, in her own frontend folder (not merged into this repo yet; the Flask dashboard in `dashboard/` is what runs today); still owns `generator/`, `collector.py`, `formatter.py`, `profiles/` |
| **Ira** | Pitch, demo video, Devpost [confirm with Ira]. SpacetimeDB (`SpacetimeStore` is still a stub) and the Figma/Notability process tracks are unassigned unless Ira confirms them |

**Original split and prompts (Saturday morning, kept for reference; owners above win)**

**Step 0 (everyone, 30 min):** lock CONTRACT.md (section 12).

### Maja: Data
Owns: data generator with edge cases and answer key, Collector, Formatter.
Done when: a new seed makes a fresh week, all formats load into data/std/, every trap is in the answer key.
Ship 1 store x 1 day within the first hour so others are unblocked.

Prompt:
> I'm building a hackathon project that automates delivery-payout reconciliation for a fictional multi-unit hot chicken franchise group. Read CONTRACT.md. Write a seeded Python generator that first builds the TRUE world (12 stores, 4 channels: DoorDash, Uber Eats, Grubhub, direct app; 2 weeks of orders; contracts per channel with commission tier, promo funding split, fees, effective dates; one amendment; 3 rep emails), then renders each source in its own messy format: different column names, date formats, currency as text, DoorDash error charges as separate lines, Grubhub as an xlsx with summary and detail sheets, bank deposits as weekly lumps. Then plant these traps and record each in data/answer_key.csv with order_id, trap_type, correct outcome (correct/recoverable/conflict) and correct dollar amount: the 17 traps listed in section 13 of FLOCK_CONTEXT.md. Also write a Collector that copies files into data/raw/<source>/ untouched, and a Formatter that maps each source to the standard order row and standard payout line, using an LLM to propose column mappings for unknown formats, saving confirmed mappings to mappings.json. Every standardized row must keep source_file and source_row.

### Thabhelo: The Detective (core)
Owns: Matcher, Deal Keeper, Checkers (rule book), guardrail.
Done when: on Maja's data, every trap produces the right finding with evidence, and the legit error charge produces no dispute.

Prompt:
> Read CONTRACT.md. Build the detection layer for delivery-payout reconciliation. (1) Matcher: link each POS order to its platform line and its bank deposit, using exact IDs first, then time-window plus amount matching with a confidence score; unmatched records become missing_order findings. (2) Deal Keeper: read contracts, amendments and emails with an LLM and output effective terms per channel, store and date, each with the exact quoted source text and an authority level (contract > amendment > authorized email > unauthorized email > call note); if two equal-authority claims conflict, mark it a conflict. (3) Checkers in plain Python, no LLM math: commission rate vs contract, promo funding split, sales tax collected by the platform, error charges (flag ones reported 72+ hours after delivery, and duplicates), and payout total vs bank deposit, with a small rounding tolerance. Each checker emits findings with expected, actual, gap and evidence. All state changes go through transition(). Write tests that run against data/answer_key.csv and print caught, missed and false alarms.

### Preet: The Brain
Owns: Ranker, Panel (betting), Learner, Dispute Writer, Clicky (sandbox portal).
Done when: findings come out ranked, disagreements route to a human, approvals produce a filed dispute in the sandbox portal, week 2 sends fewer items to a human than week 1.
Until Thabhelo and Ira's pieces exist, build against a hand-written fake_findings.json (5 findings in contract format).

Prompt:
> Read CONTRACT.md. Build the decision layer. (1) Panel: three independent LLM judges each read a finding plus its evidence and return outcome, confidence 0 to 1 and a one-sentence reason; if all agree with average confidence above a threshold, set outcome; otherwise set conflict for human review. Log every vote. (2) Ranker in plain Python: score = gap x win_probability minus effort cost; respect a per-channel dispute limit. (3) Learner: after humans approve or reject, group similar decisions and propose a rule like "DoorDash commission above contract rate: always dispute", with an evidence count; rules require human approval and are stored in rules.json and applied on the next run. (4) Dispute Writer: draft a dispute message per finding citing evidence. (5) Clicky: a small fake merchant portal (simple web page) plus a browser-automation agent that logs in and files approved disputes there. Agents may only propose; only a human action moves a finding to APPROVED, via transition().

### Ira: Platform, face and pitch
Owns: Logging agent + Gatekeeper (transition(), permissions, append-only log), SpacetimeDB backend, dashboard, scoreboard, pitch, process-track material (Figma, Notability).
Done when: clicking any dollar shows its full history, scoreboard runs live on a freshly generated week.

Prompt:
> Read CONTRACT.md. Build the platform. (1) transition(item, action, actor, patch, reason): look up allowed (state, action) moves and role permissions (agents can propose, only humans approve, only the gatekeeper files), write an append-only log entry in the same transaction including denied attempts, raise on denial. (2) Use SpacetimeDB as the real-time backend for findings, agent status and the log so the dashboard updates live. (3) Dashboard: live counters while agents run; a queue of conflicts with approve and reject buttons; a finding detail page showing the full audit trail like a GitHub pull request (sources, claims, rule applied, votes, who approved, what was filed); a scoreboard comparing results to data/answer_key.csv (caught, missed, false alarms, dollars recovered, items sent to humans in week 1 vs week 2). Keep it clean and demo-ready.

---

## 15. Sponsor tracks

| Track | Fit | Owner | Notes |
| --- | --- | --- | --- |
| Hyphenate | Main target | Everyone | $8K/$5K/$3K + interviews for top 5 |
| SpacetimeDB | Strong | Ira [confirm] | Must be the core real-time backend (shared state, live sync). $2,048/$1,024/$512. `SpacetimeStore` in `flock/store.py` is still a stub |
| CREAO | Strong | Thabhelo | At least one agent built on agent.creao.ai doing core work, its agent ID and a run share link in the README, shown in the demo. Ours: the Flock Judge (README "CREAO track") |
| Microsoft | Strong | Ira (pitch) | Production-minded agent, customer understanding, measurable usefulness, responsible design |
| Figma | Good | Ira | Requires stage log (<=200 words per stage), Figma Slides with embedded prototype, video <=3 min, public Figma access. Log as you go. Prize details conflict between brief and Devpost |
| Notability | Good | Anyone | Keep real evolving notes and diagrams in Notability all weekend. Code CALHACKS for a month of Pro |
| Fetch.ai | Stretch | Preet, only if ahead | Must register on Agentverse, implement Agent Chat Protocol, main workflow usable in ASI:One, public repo, submit via Devpost and ASI:One |
| CRS | Weak | Skip | Needs CRS credit/identity data at the center; don't bend the idea |

---

## 16. The demo (2 to 3 min pitch + 2 min Q&A)

**Setup:** generator creates a fresh week with traps and an answer key, run live so it's clearly not hard-coded.

- **0:00 to 0:25 Hook.** Bakery story, then "Noah's NY Bagels, 56 California stores" (simulated data). Say example numbers are examples.
- **0:25 to 0:45 Inputs.** Four statements in four formats, register export, contract PDF, rep email. "This is what the bookkeeper gets every Monday."
- **0:45 to 1:20 The run.** Live board, checkers running in parallel, counters ticking. Zoom into three catches:
  1. Broken deal (differentiator): BOGO charged 100% to the store, contract says 50/50.
  2. Rule break (deterministic): error charge on a complaint 72+ hours after delivery.
  3. Conflict (human moment): contract has a marketing fee, rep email says waived. System stops and asks. Teammate decides; system proposes a rule.
- **1:20 to 1:40 Proof.** Click a finding, show the PR-style trail: sources, claims, rule, votes, who approved, dispute filed (Clicky in the sandbox portal).
- **1:40 to 2:00 Score.** On unseen data: traps caught vs answer key, false alarms, dollars recovered, hours saved, week 1 vs week 2 human workload chart. Close with pricing line.

**Differentiators to emphasize**
1. Check the deal, not just the errors.
2. Measurable learning (human workload drops week over week).
3. Live score on fresh data against an answer key.
4. Live "never-seen format" moment: drop in a new layout, it maps it, asks one question.
5. PR-style audit trail for every dollar.

**Backup:** record a demo video by Sunday 7 AM in case live breaks. Keep one clean architecture diagram + README in the repo; every teammate should be able to explain any box.

---

## 17. Architecture diagram notes (fixes to the team's v1 diagram)

1. Add a "Deal Terms" box fed by contracts, amendments, promo agreements and rep emails; Payout Analysis checks against it.
2. Unified Data Layer keeps raw, standardized and claims; never overwrite contradictions.
3. Ingestion happens before the data layer, not after.
4. Merge Anomaly Detection into Payout Analysis as parallel rule checkers; keep Priority Ranker as the dispute-budget picker.
5. Split "No Pass" into correct / recoverable / conflict.
6. Add learning loop arrow from approvals back to a Rules box.
7. Add an audit log bar under the whole diagram; agents propose, only approved items go out through one gate.

---

## 18. Timeline

- By 6:30 AM Sat: contract locked; Maja ships tiny sample (1 store, 1 day).
- By 11:30 AM Sat: thin end-to-end on the tiny sample (collect, standardize, one checker, one finding, approve, log). Bring to the 11:45 Hyphenate lunch.
- Saturday evening: full data, all checkers, panel, dashboard.
- Overnight: learning loop, Clicky, scoreboard, pitch rehearsal.
- Sunday 7 AM: code freeze, record backup video.
- Sunday 9 AM: submit (repo, project image, working demo).
- Sunday 10:30 AM to 12:30 PM: judging, full team at table.
- Sunday 6 to 9 PM: YC afterparty, Dogpatch.

---

## 19. Open items ([need])

- A sourced Noah's NY Bagels revenue / sales-per-location figure (to say "mid-market" with a number). Until then, say "about 56 California locations" and that the data is simulated.
- Third-party delivery share of sales for fast casual.
- Restaurant-only count of large multi-unit operators.
- Whether "reconciling one location takes an hour" is per week, per day, or per month.
- Exact Uber Eats and Grubhub report names.
- Ask at lunch: do Hyphenate's restaurant customers (Zuma) do this by hand? Why did Najib pause on the Toronto discount example?

---

## 20. Ideas considered and parked (for reference)

- Agent at the Door (KYA, "know your agent" identity checks on inbound vendor agents in payables). Parked: too generic a role.
- Escrow officer / title company wire fraud with KYA. Parked.
- CPG deductions analyst (retailers short-paying brands). Parked: not relatable enough.
- AI customer-service outcome billing disputes (Decagon-style). Parked: too close to Hyphenate's own example.
- University restricted funds / tuition receivables. Parked.
- Early-payment discount capture in payables (City of Toronto auditor general found ~$450K lost in vendor discounts from processing delays; TTC could gain ~$100K/yr by raising discount capture from 75% to 90%). Najib reacted strongly to this. Strong backup idea if the restaurant idea falls apart. Sources: https://www.toronto.ca/legdocs/mmis/2013/au/bgrd/backgroundfile-55721.pdf , https://www.torontoauditor.ca/?p=516
