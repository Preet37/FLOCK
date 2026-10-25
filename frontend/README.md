# Flock frontend

Static pages, no build step.

- `index.html`: landing page
- `dashboard.html` + `dashboard.js`: live console for Noah's NY Bagels (simulated data)

The console polls the Flock API: audit log and agents every 1.5s; findings, score, metrics and transactions every 4s.
It defaults to the hosted backend. Override it with `?api=http://localhost:8787` or in the key-icon settings.
`/api/metrics` and `/api/transactions` are picked up automatically once they ship. Until then the cards fall back to `/api/score` and the transactions table shows a placeholder.

Approve/Reject send `Authorization: Bearer <token>` plus your name as `actor`. Enter both in settings; they're kept in that browser's localStorage only.

```bash
python3 -m http.server 5173 --directory frontend
```
