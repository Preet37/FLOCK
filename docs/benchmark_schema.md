# Benchmark result schema (`flock.judge_load/1`)

`scripts/judge_load.py` writes one JSON file per run to `results/judge_load_<provider>_<UTC timestamp>.json`
(gitignored). `scripts/benchmark.py` reads it for the **Latency**, **Throughput**, **Cost** and **Works offline**
columns of the benchmark table (docs/PLAN.md). Accuracy columns (traps caught, false alarms, correct dollars, sent to
a human) come from the benchmark harness itself, not from this file.

## Core keys (stable: benchmark.py and the dashboard's Benchmark tab rely on these)

| Key | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `"flock.judge_load/1"`. Bump the number if a core key changes meaning. |
| `contestant` | string | Row name in the benchmark table. `--contestant`, else the judge routes joined by `+` (e.g. `local/gpt-oss:20b+local/qwen3:30b+local/gemma3:27b`), or `rules-only` when no judge called a model. |
| `models` | string[] | Distinct judge routes, `provider/model` (`offline-rules` for a judge on offline rules). |
| `n` | int | Findings judged. |
| `p50` | float, seconds | Median wall time for one finding through `panel.decide` (3 judges in parallel + a debate round on a split + reviewer memo / audit when configured). |
| `p95` | float, seconds | 95th percentile of the same (linear interpolation, like numpy). |
| `per_min` | float | Throughput: `n / wall_seconds * 60`, at the run's `concurrency`. |
| `cost_per_1k` | float or null | Dollars per 1,000 findings. `0` when every judge runs on this network (local) or on offline rules; `--cost-per-1k` for a hosted contestant; `null` when hosted and not given. |
| `offline` | bool | True when every judge's host is on this network (loopback, private, Tailscale; the same test as `FLOCK_AIRGAP=1`) or the judge uses offline rules. I.e. "works with Wi-Fi off". |
| `errors` | int | Judge calls that fell back to offline rules or failed the debate round, plus findings where `decide` raised. Non-zero = the latency numbers include fallbacks; the script exits 1. |

## Extras (informational, may grow)

| Key | Meaning |
| --- | --- |
| `provider` | Judge providers joined by `+` (also in the filename). |
| `concurrency` | Findings in flight at once (each runs its 3 judges in parallel, so up to 3x this many model requests). |
| `source` | `mixed` / `fake` / `adversarial` (fixtures, cycled with `-r2` ids past their count) or `seed:<n>` (generated). |
| `wall_seconds`, `mean`, `max` | Whole-run wall time; mean and max seconds per finding. |
| `judges[]` | Per judge: `judge`, `model` (route), `answered_by` (model labels seen), `calls`, `p50`, `p95`, `mean` (seconds per call, both rounds), `fallbacks`. |
| `fallbacks`, `debated`, `outcomes` | Votes that used offline rules; findings that went to a debate round; count per outcome. |
| `error_samples` | Up to 5 distinct error strings. |
| `model_calls` | `flock.llm.airgap_report()` for the run: `calls`, `by_host`, `external_calls`, `external_hosts`, `refused`, `line` ("air-gapped: N model calls, 0 left this network"). |
| `timeout_seconds`, `started_at`, `host` | Per-call timeout used, run start (UTC), machine the script ran on. |
| `findings[]` | Per finding: `finding_id`, `check_type`, `seconds`, `outcome`, `confidence`, `debated`, `fallbacks`, `error`. |

## Example (trimmed; a real run on a MacBook Air's Ollama)

```json
{
  "schema": "flock.judge_load/1",
  "contestant": "local/qwen2.5-coder:14b",
  "models": ["local/qwen2.5-coder:14b"],
  "n": 6, "p50": 88.413, "p95": 125.971, "per_min": 1.48,
  "cost_per_1k": 0.0, "offline": true, "errors": 0,
  "provider": "local", "concurrency": 3, "source": "mixed", "wall_seconds": 242.492,
  "model_calls": {"calls": 21, "by_host": {"127.0.0.1": 21}, "external_calls": 0,
                  "line": "air-gapped: 21 model calls, 0 left this network; kept offline: anthropic/claude-sonnet-5-5"}
}
```
