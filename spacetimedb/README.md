# Flock on SpacetimeDB

The module in `src/lib.rs` holds findings, the append-only audit log and the live agent feed as three public tables,
written only through four reducers (`put_new`, `commit`, `agent_event`, `reset`). `flock/store.py`'s `SpacetimeStore`
talks to it over SpacetimeDB's HTTP API (reducer calls + SQL), so nothing else in Flock changes.

```bash
curl -sSf https://install.spacetimedb.com | sh          # once; adds ~/.local/bin/spacetime
spacetime start                                         # local server on http://127.0.0.1:3000 (leave it running)
spacetime publish flock --server local --module-path spacetimedb --yes
export FLOCK_STORE=spacetime                            # SPACETIME_URL / SPACETIME_DB / SPACETIME_TOKEN to point elsewhere
export FLOCK_OFFLINE=1
python -m flock.pipeline --reset --forget --demo-approve && python scripts/score.py   # the Quickstart, on SpacetimeDB
spacetime sql flock "SELECT finding_id, state FROM finding" --server local
```

Maincloud: `spacetime login`, then `spacetime publish flock --yes` and `SPACETIME_URL=https://maincloud.spacetimedb.com`
with `SPACETIME_TOKEN` from `spacetime login show --token`.

Sidecar files the pipeline keeps next to the store (market ledger, run history, progress, journal entries) live in
`data/state/spacetime/` for this backend; they are not in the database yet.
