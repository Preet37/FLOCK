# Release QA report (Sat Oct 24, night before submission)

Branch `preet/release-qa`, off `main` at `25ab3b3`. Every run below used `FLOCK_OFFLINE=1` unless it says otherwise.
Machine: MacBook (darwin), Python 3.14, the worktree's own `.venv`.

## Summary

| | |
| --- | --- |
| Test suite (default, `pytest`) | **491 passed, 7 skipped, 16 s** (was 472 + 2 skipped before this branch) |
| Everything (`FLOCK_SLOW=1 FLOCK_TEST_BROWSER=1 pytest`) | **498 passed, 0 skipped, 57 s**, including both real-browser Clicky tests (Playwright Chromium 156) |
| Quickstart | Output unchanged: 6/6 caught, Week 1: 2 to a human, Week 2: 0 |
| Live seed demos | 15 runs over 3 profiles/source mixes: **every trap caught, 0 false alarms** |
| Full year, bagel chain 56 x 52 weeks | 1,257,013 orders, **9,669/9,669 traps, 0 false alarms, 118 s** |
| Hosted API (Railway) | All read routes up and returning valid JSON, but **the hosted state is empty** (no findings, no log). Run it before judging (checklist step 7) |
| Bugs fixed | 6 (below), each with a test |
| Bugs open | 7 in `flock/api.py` / `dashboard/app.py` (not mine to edit) + 2 design notes |

## What was tested

New file `tests/test_release.py`. Each scenario runs in a throwaway copy of the repo (code + committed data) under
pytest's tmp dir, with the real commands as subprocesses. A session fixture hashes `data/answer_key.csv`,
`mappings.json`, `chart_of_accounts.json`, `fixtures/fake_findings.json`, `rules.json` and `data/state/*` before and
after and fails if anything changed.

```bash
pytest tests/test_release.py                     # fast scenarios, ~10 s (part of plain `pytest`)
FLOCK_SLOW=1 pytest tests/test_release.py        # + slow ones (b: live seed demos, h: year-ish scale), ~40 s
FLOCK_SLOW=1 pytest -m slow                      # only the slow ones
FLOCK_TEST_BROWSER=1 pytest tests/test_clicky.py tests/test_clicky_learn.py   # real Chromium (python -m playwright install chromium)
```

| # | Scenario | How | Result |
| --- | --- | --- | --- |
| a | Quickstart: week 1, approve R-001, week 2 | The exact README commands as subprocesses; asserts the printed lines (6 findings, `correct: 1 \| recoverable: 3 ($17.90) \| sent to a human: 2`, `audit log: 28 entries (0 denied)`, no air-gap line offline, `Week 1: 2 \| Week 2: 0`) and that committed files are byte-identical afterwards | pass |
| b | `flock.demo` default profile; `ca_bagel_chain --stores 4 --sources email,slack,notes`; default + email/slack/notes (slow) | All traps caught, 0 false alarms, under 120 s, and `data/state` + the sandbox portal are byte-identical after the demo restores them | pass (about 2 s each) |
| c | Air gap | `FLOCK_AIRGAP=1` with groq / nvidia / anthropic / creao all configured with keys and `FLOCK_AUDIT_RATE=1`, on a generated week. A `sitecustomize.py` socket guard refuses and logs every non-local `connect` and DNS lookup in the process and its workers. A control test proves the guard catches a real call to api.groq.com. Second variant: local judges at `127.0.0.1` (nothing listening) and a cloud reviewer | `air-gapped: 0 model calls, 0 left this network`; all four cloud providers listed as kept offline; guard log empty. Local variant: calls counted, all to `127.0.0.1`, reviewer refused, 0 left |
| d | `--reset` twice, `--reset --forget` twice | Fixtures and a generated week. `--forget` twice gives identical findings, outcomes, states, portal filings and audit log; deletes `rules.json`; leaves one week of history. `--reset` twice keeps the same findings and dollars, never files twice | pass (see open note N1: real-data finding ids change every run) |
| e | Journal | Every drafted entry balances to the cent (recomputed from lines), totals match, stays DRAFT, every line has a source row or a finding id, every finding id exists, every source file exists and its row numbers are inside the file | pass |
| f | Humans approve, gatekeeper files | Whole-log scan: every allowed approve/reject is by a human, every file by the gatekeeper with a human approval earlier in the log, no state jumps, every denial has an empty diff. Then 224 probes (6 agent ids x approve/reject/file/recover on JUDGED and FILED findings, gatekeeper approving, human filing, an investigator patch touching `gap` or `state`): every one raises `Denied`, is logged, and nothing moves | pass |
| g | No money from a model | Offline: no vote carries a model or fallback, airgap count 0. Hostile model (mocked `urlopen`): every judge answers with made-up `gap`/`expected`/`actual`/`amount`/`state` and the writer drafts "refund $99,999.00". On fixtures and on a generated week: every finding's money, every portal amount and every journal entry's totals equal the offline run, no log diff touches a money field | pass after fix B3 (before it, the model's $99,999 went into the dispute text Clicky files) |
| h | Year-ish scale (slow) | `flock.demo --weeks 8` on the default company (12 x 8) and on the bagel chain (56 x 8, the parallel split path) | 87/87 in 9.1 s; 1,400/1,400 in 15.1 s; 0 false alarms |
| + | Strict JSON | State files and the exact `/api/findings` + `/api/log` bodies: no NaN/Infinity, no raw control characters | pass |

## Timings

| Run | Wall time | Result |
| --- | --- | --- |
| Quickstart week 1 / week 2 (`pipeline`) | 0.34 s / 0.26 s | 6/6, 2 then 0 to a human |
| `flock.demo --seed {1,42,4821,777,2026}` (default, 12 x 2 weeks) | 1.4 s each | 44/44, 55/55, 41/41, 48/48, 49/49 |
| `flock.demo --profile ca_bagel_chain --stores 8 --weeks 2`, same 5 seeds | 1.6-1.8 s | 213, 197, 187, 187, 160: all caught |
| same, `--sources json`, seeds 3, 99 | 1.6 s | 145/145, 154/154 |
| `flock.demo --sources email,slack,notes`, seeds 5, 4821 | 1.5 s | 68/68, 58/58 |
| `flock.demo --profile ca_bagel_chain --weeks 2` (56 stores), seed 11 | 4.5 s | 337/337 |
| `flock.demo --seed 4821 --weeks 52` (12 x 52) | 9.7 s | 477/477 |
| `flock.demo --seed 4821 --profile ca_bagel_chain --weeks 52` (56 x 52, slack/email/notes on) | **118 s** | 9,669/9,669, all 22 trap types |
| `scripts/benchmark.py` (rules, seeds 101-110, bagel 2 weeks) | 41 s | 3,676/3,676, 0 false alarms |
| Default `pytest` / everything | 16 s / 57 s | all pass |

Every demo run above: 0 false alarms. The README says 84 s for the full year; on this laptop it is 118 s with the
Slack/email/notes sources on. Budget two minutes for that on stage, or show the 2-week run live.

## Bugs fixed (each has a test in `tests/test_release.py`)

| # | File | Bug | Fix | Test |
| --- | --- | --- | --- | --- |
| B1 | `flock/pipeline.py` | `pipeline.run(store=<temp store>)` without `runs_path` appended to the repo's `data/state/runs.jsonl`. Plain `pytest` left a junk "Week" line there (`test_pipeline_e2e` did it every run), so `score.py`'s "human hand-offs by run" could show a week that never happened | Run history defaults to the store's folder (`runs_default()`); the repo store still uses `data/state/runs.jsonl` | `test_fix_temp_store_run_never_writes_the_repo_run_history` |
| B2 | `flock/pipeline.py` | `record_run` counted `learned_rules` from the repo's `rules.json`, not the rules the run used (`rules_path`), so `flock.demo` / tests reported the wrong count | Pass `rules_path` through | same test (`learned_rules == 1` from a temp rules file); scenario (a) checks `[0, 1]` |
| B3 | `flock/agents/dispute_writer.py` | A model-written dispute could state any dollar figure ("refund $99,999.00"), and Clicky files that text as the dispute message next to the code-computed amount. That breaks "no LLM does money math" in what the platform reads | A model draft whose `$` amounts aren't the finding's expected / actual / gap or a number in its evidence is thrown away and the template (numbers from code) is used, with a note on stderr | `test_fix_model_draft_with_a_made_up_amount_falls_back_to_the_template`, `..._with_only_known_amounts_is_kept`, scenario (g) |
| B4 | `scripts/score.py` | `score(store)` read the repo's run history whatever store it was given | Defaults to the history next to the store (`runs_for()`) | `test_fix_score_reads_the_history_next_to_the_store` |
| B5 | `flock/portal.py` | `read_filings` crashed with `JSONDecodeError` when Clicky (another process) was halfway through appending a line, which takes down the portal's "My disputes" page | A last line without its newline is left for the next read (same rule as `store.log()`) | `test_portal_page_survives_a_half_written_filing` |
| B6 | `flock/agents/learner.py`, `flock/market.py` | `learner --approve R-999` (a typo) and `market --recover F-xxxx` on a finding that isn't FILED printed a full traceback (on stage, in front of judges) | One-line message, exit 1; the gatekeeper still logs the denied recover | `test_fix_stage_clis_fail_with_a_sentence_not_a_traceback` |

Also: `tests/conftest.py` registers a `slow` marker; slow tests are skipped unless `FLOCK_SLOW=1` or `-m slow`.

## Bugs open: `flock/api.py` and `dashboard/app.py` (owned by another agent right now, not edited)

### The JSONDecodeError on `/api/findings`: the API sends valid JSON; the shell broke it

Checked byte by byte. Hosted (all 7 routes) and local (`flock.api` and `dashboard.app` after a real-data run, with
166 KB of findings and 363 KB of log): **0 raw control characters, 0 NaN/Infinity, every body parses with a strict
parser.** `json.dumps` escapes newlines inside strings as `\n` (75 in one findings body, from dispute drafts and
evidence blocks).

The error comes from zsh's `echo`, which turns the two characters `\n` back into a real newline:

```bash
x=$(curl -s $URL/api/findings); echo "$x" | python3 -c 'import json,sys; json.loads(sys.stdin.read())'
# json.decoder.JSONDecodeError: Invalid control character at: line 1 column 25092
print -r -- "$x" | python3 -c '...'     # ok
curl -s $URL/api/findings | python3 -c '...'   # ok
```

So: pipe curl straight into the parser (or use `print -r` / `printf '%s'`), never `echo "$json"` in zsh. Browsers'
`fetch().json()` are fine. Right now the hosted `/api/findings` is `[]`, so there's nothing to break anyway.

Two real API bugs that also end in a `JSONDecodeError` on the client are A1 and A2 below.

### A1 (high): any unexpected error on a GET drops the connection with an empty reply

- **File:** `flock/api.py`, `do_GET`, lines 167-189: only `KeyError` is caught.
- **Repro:** run `flock.api`, then (a) append half a line to `data/sandbox_portal/disputes.jsonl` (Clicky mid-append)
  and `curl /api/portal`, or (b) move `data/answer_key.csv` and `curl /api/score`. Result: `curl: (52) Empty reply
  from server`, HTTP 000, 0 bytes; the server prints a traceback. A frontend gets `JSONDecodeError: Expecting value:
  line 1 column 1 (char 0)` / `Unexpected end of JSON input`.
- **Fix:** mirror `do_POST`: add `except Exception as e: self._send(500, {"error": f"{type(e).__name__}: {e}"})`
  after the `KeyError` branch. Also make `_jsonl` (line 85-88) skip a partial last line, as `portal.read_filings` now
  does (B5): `lines = path.read_text().split("\n"); return [json.loads(l) for l in lines[:-1] if l.strip()]`, or
  just call `portal.read_filings(PORTAL)`.

### A2 (medium): `json.dumps` will emit `NaN` if a NaN ever reaches a payload

- **File:** `flock/api.py` line 149 (`data = json.dumps(payload).encode()`); `dashboard/app.py` uses Flask's
  `jsonify`, same default.
- **Status:** not seen in any run tonight (scenario + strict-JSON test pass). But Python's default writes `NaN`,
  which `JSON.parse` rejects, so one pandas NaN in a future source would blank the whole findings list.
- **Fix:** `json.dumps(payload, allow_nan=False)` inside a try; on `ValueError`, send 500 with the error, so it fails
  loudly at the source instead of in every browser.

### A3 (medium): concurrent requests share `sys.stdout` through `contextlib.redirect_stdout`

- **File:** `flock/api.py` lines 91-95 (`_captured`), used by `scoreboard()` (124) and `run()` (139) on a
  `ThreadingHTTPServer`.
- **Repro:** `POST /api/run` (seconds of pipeline output) while the UI polls `GET /api/score`. `redirect_stdout`
  swaps the process-wide `sys.stdout`; the two contexts exit out of order, so the score text can contain pipeline lines
  and `sys.stdout` can be left pointing at a dead `StringIO` (server logs vanish).
- **Fix:** a module-level `threading.Lock()` around `_captured`, or call `score(verbose=False)` (the scoreboard
  already returns everything as data) and build `text` from the result.

### A4 (medium): two `POST /api/run` at once run two pipelines on the same files

- **File:** `flock/api.py` line 139 (`Api.run`).
- **Repro:** double-click "Run" in a UI: both requests reset and write `data/state` concurrently (interleaved log,
  duplicate filings).
- **Fix:** `RUN_LOCK = threading.Lock()`; `if not RUN_LOCK.acquire(blocking=False): return 409 {"error": "a run is
  already going"}`; release in `finally`.

### A5 (low): `OPTIONS` answers 204 with a body

- **File:** `flock/api.py` line 165: `self._send(204, {})` writes `{}` and `Content-Length: 2`. A 204 must not have a
  body; strict proxies and some HTTP clients (undici) reject or warn on it.
- **Fix:** for 204 send the headers with `Content-Length: 0` and no body.

### A6 (low): docs and token check

- `flock/api.py` line 8 says `/api/findings` is "newest id first"; line 116 sorts ascending, and real-data ids are
  random (N1), so neither order means anything. Sort by `(check_type, channel, order_ids)` or document "by id".
- Line 194: compare the token with `hmac.compare_digest(...)` rather than `==` (constant time). Low risk tonight.

### A7 (low): `dashboard/app.py` line 183 has the same run-history default as B1

`create_app(store=<temp store>)` without `runs_path` reads the repo's `data/state/runs.jsonl`. Every test passes a
`runs_path` so nothing breaks today. Fix: `runs_path or scoreboard.runs_for(store)` (added to `scripts/score.py` on
this branch).

## Design notes (not fixed: they cross owners, and changing them the night before is riskier than leaving them)

- **N1: real-data finding ids are random per run.** `flock/models.py` line 73 (`"F-" + uuid4`). The same discrepancy
  gets a new id on every `--reset`, so a bookmarked `/api/findings/F-xxxx` or a finding page URL 404s after a re-run,
  and market bets voided by `--reset` can't be matched to the re-found finding. Fixture ids (`F-0001`...) are stable.
  Suggested later: a deterministic id from `(channel, check_type, sorted order_ids, store)`, with a suffix on the
  rare collision. Scenario (d) compares runs by content because of this.
- **N2: `flock.demo` without `--confirm` misses a trap if nobody answers "y".** The unseen Uber Eats layout is
  skipped when stdin isn't a terminal (or the answer is N), so its orders aren't read: seed 4821 scores 40/41 (one
  `bogo_wrong_split` inside that statement). That's by design, but on stage always pass `--confirm` or answer `y`.
  `flock.demo` also exits 0 even when traps are missed.

## Hosted check (read-only, Sun Oct 25 05:10 UTC)

`https://web-production-812b4.up.railway.app`, GET only, no token.

| Route | HTTP | Shape | Notes |
| --- | --- | --- | --- |
| `/`, `/api` | 200 | `{ok, routes[]}` (946 B) | fine |
| `/api/findings` | 200 | `[]` | **empty: no run since the last deploy / restart** |
| `/api/log` | 200 | `[]` | empty |
| `/api/agents` | 200 | `{agents[12], judges[3], humans[], transitions[6]}` | all `actions: 0` |
| `/api/score` | 200 | `{caught, total, missed, ..., traps[6], runs[], text}` | `caught 1/6` because there are no findings (the one "caught" is the `correct` trap nobody raised) |
| `/api/rules` | 200 | `{approved: [], proposed: []}` | fine |
| `/api/portal` | 200 | `[]` | fine |
| `/api/findings/F-0001` | 404 | `{"error": "not found: 'F-0001'"}` | expected while empty |
| `/api/meta`, `/api/airgap` | 404 | `{"error": "no such route"}` | those routes are on `dashboard.app`, not `flock.api` |

All bodies: strict-valid JSON, `Content-Type: application/json`, CORS `*` (set `FLOCK_CORS_ORIGIN` to narrow it).
The empty state means either no `POST /api/run` since the last redeploy or the volume isn't mounted at
`/app/data/state` (docs/DEPLOY.md). A judge opening it now sees an empty board and "1/6".

## Demo-day checklist (in order)

```bash
# 0. fresh terminal, repo root, on the commit you're submitting
source .venv/bin/activate && export FLOCK_OFFLINE=1
git status                                   # clean; no rules.json / data/state surprises

# 1. tests (16 s); then the release scenarios incl. slow + real browser (~1 min)
pytest -q
FLOCK_SLOW=1 FLOCK_TEST_BROWSER=1 pytest -q

# 2. the Quickstart, exactly as judges will read it
python -m flock.pipeline --reset --forget --demo-approve   # 6 findings, Clicky filed 3, sent to a human: 2
python scripts/score.py                                    # caught 6/6 | Week 1: 2
python -m flock.agents.learner --approve R-001
python -m flock.pipeline --reset --demo-approve            # sent to a human: 0
python scripts/score.py                                    # Week 1: 2 | Week 2: 0

# 3. a judge's seed (ALWAYS --confirm, or answer y to the layout question)
python -m flock.demo --seed <their number> --confirm                       # ~2 s, expect 0 missed / 0 false alarms
python -m flock.demo --seed <n> --profile ca_bagel_chain --weeks 2 --confirm   # ~5 s, the 56-store chain

# 4. Wi-Fi-off proof (models on the GX10 or nothing): expect "air-gapped: N model calls, 0 left this network"
unset FLOCK_OFFLINE; eval "$(scripts/gx10.sh env --airgap)"; python scripts/llm_check.py
python -m flock.pipeline --reset --forget --demo-approve | tail -3

# 5. the year (only if there's 2+ minutes): 1.26M orders, ~2 min on a laptop
python -m flock.demo --seed 4821 --profile ca_bagel_chain --weeks 52 --confirm

# 6. leave the board in the demo state, then start the UI
python -m flock.pipeline --reset --forget --demo-approve
python -m dashboard.app                                    # http://127.0.0.1:5050

# 7. hosted API: fill it (it is EMPTY right now), then check reads
curl -X POST -H "Authorization: Bearer $FLOCK_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"reset": true, "forget": true, "demo_approve": true}' https://web-production-812b4.up.railway.app/api/run
curl -s https://web-production-812b4.up.railway.app/api/score | python3 -c 'import json,sys; print(json.load(sys.stdin)["text"])'
# expect "caught 6/6 traps". Parse with a pipe like this, never `echo "$json"` in zsh (it breaks \n escapes).
```
