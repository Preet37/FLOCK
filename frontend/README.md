# Flock frontend (the console)

Himaja's UI: static HTML + JS + CSS, no build step. This is the product UI; `dashboard/` (Flask) is only a backup.

- `dashboard.html` + `dashboard.js`: the console for Ringtide Bagels / Noah's NY Bagels (simulated data)
- `index.html` + `styles.css` + `script.js`: the landing page ("Get Started" opens the console)
- `vendor/`: Tailwind (play build 3.4.16) and Lucide 0.453.0, vendored so the console works with no network on stage
  (only the Plus Jakarta Sans web font comes from Google Fonts, and falls back to the system font offline)

## Run it

```bash
python -m flock.api                        # API + console: http://127.0.0.1:8787/
```

The API serves this folder: `/` is `dashboard.html`, `/frontend/<file>` and `/<file>` are the other files. The hosted
API does the same, so https://web-production-812b4.up.railway.app/ is the console.

Which API the console talks to:

1. `?api=http://127.0.0.1:8787` in the address, if given
2. the page's own origin, when the page is served by a Flock API (it checks `/api/health`)
3. the API saved in settings (key icon)
4. the hosted default (`DEFAULT_API` in `dashboard.js`, the Railway URL)

So opening the file directly, or `python3 -m http.server 5173 --directory frontend`, still works against the hosted API.

## Screens (hash routes)

| Route | Shows | Endpoints |
| --- | --- | --- |
| `#/` Overview | showtime banner (this month's lump sum, headline finding), 4 metric cards + "How we got this" (formula, value, inputs, sources with MEASURED / ASSUMPTION / SIMULATED / PROJECTION labels), hand-offs to a human by month, discrepancies (filters: all, needs review, recoverable, conflict, filed), audit log (refusals in red), agent chain | `/api/showtime`, `/api/metrics`, `/api/score`, `/api/findings`, `/api/log`, `/api/agents` |
| `#/lumpsums` | every deposit: expected net per contract, received, shortfall, status; the showtime lump sum highlighted | `/api/payouts` |
| `#/payout/<id>` | the waterfall (gross -> each deduction, statement vs contract -> statement net -> expected net -> deposit -> shortfall), the gaps zoomed, line-by-line table with the deal-term basis, findings inside (click -> finding), per-store contribution | `/api/payouts/<id>` |
| `#/finding/<id>` (drawer) | expected vs actual, rollup label ("457 orders at S28, S38, S41"), lump sum link, evidence with authority badges, votes per round, reviewer memo, investigator case file, dispute draft, audit trail; **Approve** / **Reject** (approve also files via Clicky: the SBX- ref is shown); **Try to approve as an agent** (the gatekeeper refuses: 409 + the logged refusal); **Vendor follow-up** card on FILED claims (draft, approve with edited text, reject, send via Clicky, sent ref) | `/api/findings/<id>`, `POST .../approve`, `.../reject`, `.../follow_up[/approve|/reject|/send]` |
| `#/theater` | Live or **Replay last run** at 1x / 2x / 4x / 8x: judge balances, votes with confidence, debate revisions, bets staked / settled, latest verdict, the human lane (escalations, decisions), the flock feed (agents, Clicky filings, denials), rule banner | `/api/stream`, `/api/stream/replay` |
| `#/sources` | connector batches: each claim's quote, sender, authority, Binding / Not binding | `/api/connectors` |
| `#/scoreboard` | answer-key score, learning curve by month, traps by type, judge market (balances, weights, won / lost, sparkline, recent bets) | `/api/score`, `/api/market` |

Polling: audit log and agents every 1.5 s; everything else (and an open drawer) every 4 s; the theater on its own timer.
Panels re-render only when their payload changed, and a half-typed reason or follow-up text survives a refresh.

## Approve / Reject

They send `Authorization: Bearer <token>` and your name as `actor`. Enter both in settings (key icon); they're kept in
that browser's localStorage only. Errors are shown, not swallowed: 401 opens settings ("add the token"), 409 says
"a run is in progress" or what the gatekeeper refused, 404 / 400 / 500 show the API's message.

## Test

```bash
FLOCK_TEST_BROWSER=1 pytest -q tests/test_frontend_e2e.py   # the whole demo click path in Chromium; results/ui/*.png
```
