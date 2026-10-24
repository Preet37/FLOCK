# Hyphenate Track: What the Judges Want

Notes from the Hyphenate workshop (Fri Oct 23, 7 PM) and our follow-up conversation with the Hyphenate team.
`FLOCK_CONTEXT.md` section 5 has the short version. This file has the detail, so anyone can check a design choice against what they actually said.

Workshop recording: https://fathom.video/share/UQKfEaP_BUs_ACNWzcPNP_XhmV843z-Y

## Judging criteria

1. Is it real enough?
2. **Would you actually hire it?** (They told us to focus on this one, dig into it and defend it with evidence and metrics.)
3. Did we see something new?
4. Did it have a "wow" moment?

What they need to see in 30 seconds to believe a real finance team would adopt it:
- Accountability
- Technical depth
- Understanding of the problem
- Edge cases, not the happy path

Data: simulate our own, tailored to the point we're making, or use the public datasets they attached.

## System of record vs system of work

| System of record | System of work |
| --- | --- |
| Where the official data lives (Salesforce, QuickBooks, GitHub, SAP) | Where the work gets done: workflows, decisions, approvals, actions across people and systems (Cursor, Claude Code, n8n) |
| Traditional | Where the money is moving |

Flock is a system of work. It sits on top of the apps, the register, the bank and the contracts.

## Good system design

| Trait | What it looks like | Where it lives in Flock |
| --- | --- | --- |
| Self-learning | Learns the job at onboarding | Deal Keeper reads contracts, amendments and rep emails |
| Self-improving | Gets better with every correction | `learner.py`: human decisions become proposed rules; week 2 sends fewer items to a human |
| Audit trail | Proves every step, indefinitely | `gatekeeper.transition()` + append-only log, including denied attempts |

## What strong submissions look like

1. **Realistic company.** A real use case with numbers showing the bottleneck. Why would this company hire it?
2. **Messy data and an answer key.** Edge cases, complex data, no happy path. Try to break the system. Show expected vs produced side by side (`data/answer_key.csv` + `scripts/score.py`).
3. **A few roles done deep.** End-to-end. Fewer things done deep beat many done shallow.
4. **Escalate to humans intelligently.** Know when confidence is low (the 3-judge panel). Log every decision and its reasoning. High automation and few human hand-offs, without the agents doing a poor job.

**Accountability:** a persistent, long-term audit trail of ALL decisions, easy to query weeks, months or years later.
- Can the system defend its assumptions, every number, every log entry, every decision?
- Everything posted MUST go through human approval, like Cursor not replacing code until you accept the change.

**Technical depth:** don't cover every finance function shallowly. Pick one or a few roles and automate them end-to-end with edge cases.

## Questions every agent should answer

1. Where does it act? (role, workflow, what it automates)
2. What does it remember next quarter? (long-term memory, updated when things change)
3. Who does it work with? (other agents, human teams)
4. What may it do, and can it prove why? (guardrails, auditable, accurate)

## Advice from the Hyphenate speaker

- **Don't solve finance end to end.** Pick one narrow module and go deep. Ten things at 1% each fails.
- **Boring is good.** Accruals and receivables sound solved. They aren't, and the judgement work is what sticks.
- **The $3 / $1 test.** Mid-market pays $1 if you save them $3. If we can't say who saves what, by how much, it's the wrong build.
- **Start from a company you know as a buyer** and reverse engineer how the money flows into its finance stack.
- **ERP ladder:** Xero (small shops) → QuickBooks (startups) → Sage Intacct → NetSuite (~$25–40M up to ~$250–400M revenue, ~$200K) → Oracle Fusion / SAP (~$500M+). Aim at mid-market.
- **Audit trail is what wins.** "If you guys can build something around that, you guys will win." It works like a pull request: who touched what data, what the inputs were, where they fed into the ERP.
- **Keep a human in the loop.** Hyphenate is ~98% autonomous, 2% human: the human approves and posts. The system flags; the human rejects, flags it to the vendor, accepts a one-time change, or confirms a new rule; the system learns. His examples: $10 billed when $12 was agreed; an AWS bill that sits at $1,000 for two years then jumps to $6,500; December invoices that are always bigger, which is expected once a human confirms it.
- **Per-customer terms are the depth.** Decagon bills American Airlines 10¢ per conversation / 70¢ per resolution and Delta 5¢ / 40¢, with volume and year-two tiers on top. Applying the wrong customer's terms is a serious error, not rounding. Flock's version: each delivery app has its own negotiated deal.
- **"Why not just ChatGPT it?"** One answer goes stale. Contracts change; the system has to keep up and learn from new decisions.
- **For receivables, the hard part is messy input and context:** statements arrive in different forms, there's too much data, and each customer's terms have to be applied with evidence attached to every flag.
- **Plant the depth in the first 30 seconds.** Otherwise judges hear "someone paid late, send a reminder." Show that the same-looking gap can have completely different correct answers depending on the terms and the evidence.
- **The notice is not the product.** Sending the email is "five seconds on top." The product is the decision underneath it and the trail that proves it.

His one-line scope, mapped to Flock: *messy statements in → each deal's terms applied → short amounts flagged with evidence → a human approves → only then the agent files the dispute.*
