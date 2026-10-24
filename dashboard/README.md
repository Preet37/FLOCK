# Dashboard (owner: Ira)

Reads from the store (`flock/store.py`, today JSON files in `data/state/`, later SpacetimeDB).

Screens, in demo order:
1. **Live board:** counters ticking as agents run (checked, judged, recoverable, sent to human), dollars found.
2. **Human queue:** conflicts with approve / reject buttons. Clicking calls `transition(..., "approve" | "reject", Actor(name, "human"))`.
3. **Finding detail (the PR view):** sources, claims, rule applied, the 3 judges' votes, who approved, what was filed. Data: `store.get(id)` + `store.log(id)`.
4. **Scoreboard:** output of `scripts/score.py`, plus week 1 vs week 2 human workload.

Swap to SpacetimeDB: replace `JsonStore` with a class that has the same methods
(`get`, `all`, `put_new`, `commit`, `log`, `reset`). Nothing else changes.
