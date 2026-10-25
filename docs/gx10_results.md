# GX10 results (Sat Oct 24, night run)

What we ran on the team's NVIDIA GX10, what it measured, and what we did not do. Every number here comes from a run
logged on this branch (`preet/gx10-run`); raw files are gitignored (`results/judge_load_*.json`,
`data/benchmarks/results.json|csv`, GX10 `~/flock-train-out/judge-lora/{run,loss}.json`).

**Which code the numbers come from.** Benchmark and judge_load runs: this branch, based on `bca3166` (the
`ca_bagel_chain` profile and the 18-trap generator **before** main's deal-source change, which added traps 19-22 and
changed the profile). The training set was built on the GX10 from a clone of `c5c423c` (same generator and profile
as `bca3166`; only `deal_keeper.py` and `benchmark.py` differ). Numbers from main after `5128639` will differ.

## The machine

NVIDIA GB10 (compute capability 12.1), CUDA 13.0 driver 580.178, 121 GB unified memory, Ubuntu 24.04 aarch64, Python
3.12, Ollama 0.40.7 as the system service on `127.0.0.1:11434`. Reached from a MacBook over Tailscale with
`scripts/gx10.sh tunnel` (laptop `127.0.0.1:11435`). Nothing on the box was reconfigured: no sudo, no service
changes, no Docker. Everything ran as user `asus` (venv in `~/flock-train`, logs in `~/flock-*.log`).

## 1. Models on the GX10

| Model | Size | Notes |
| --- | --- | --- |
| `gpt-oss:20b` | 13 GB | judge 1 (strict). Can't switch reasoning off; `reasoning_effort: low` keeps it to a sentence |
| `qwen3:30b` | 18 GB | the preset's judge 2. **This tag is the thinking-only Qwen3-30B-A3B**: it ignores "no thinking" and spends 1,200-2,000+ tokens thinking per judge call, so under the panel's 600-token cap its JSON never arrives. Kept on disk, not used as a judge |
| `qwen3:30b-a3b-instruct-2507-q4_K_M` | 18 GB | judge 2 (operator) instead: same model family, instruct (non-thinking). 0.8-3 s per call |
| `gemma3:27b` | 17 GB | judge 3 (skeptic) |
| `qwen3:8b` | 5.2 GB | small model for quick checks |
| `gpt-oss-20b-8k`, `qwen3-30b-instruct-8k`, `gemma3-27b-8k` | 0 extra (same blobs) | `ollama create` aliases with `PARAMETER num_ctx 8192`. Ollama 0.40 loads Qwen3-30B at its full 262k context (45.6 GB resident); at 8k it is 19.4 GB, so the three judges take 50 GB instead of 77 GB and training fits next to them |
| `flock-judge-v1` | 8.1 GB | our LoRA-tuned Qwen2.5-7B-Instruct (section 5), GGUF q8_0 |
| `flock-judge-base` | 8.1 GB | the untouched Qwen2.5-7B-Instruct, same GGUF q8_0 conversion and template |

Pull speed was 60-70 MB/s (a 13-18 GB model in 4-5 minutes).

**All three judges stay loaded at once.** `/api/ps` showed `gpt-oss:20b` (13.6 GB), `qwen3:30b` (45.6 GB) and
`gemma3:27b` (17.6 GB) resident together, all 100% GPU. A fourth model evicts one (Ollama's default limit of 3 loaded
models), and Ollama also evicts when a training job holds memory.

**One request per model at a time.** Concurrency 3 and 6 give the same throughput (below): the service runs with
its default parallelism and we don't change the service. Different models do run in parallel, which is why three
different judges beat one model on all three seats.

## 2. Connect: `llm_check.py`

```
$ scripts/gx10.sh tunnel &          # [gx10] tunnel UP: http://127.0.0.1:11435 -> Ollama on gx10 {"version":"0.40.7"}
$ eval "$(scripts/gx10.sh env --airgap)"   # with GX10_JUDGE_MODELS=gpt-oss:20b,qwen3:30b-a3b-instruct-2507-q4_K_M,gemma3:27b
$ python scripts/llm_check.py
OK      local/gpt-oss:20b                               0.51s  roles: judge.strict, reviewer, writer, mapper
OK      local/qwen3:30b-a3b-instruct-2507-q4_K_M        6.91s  roles: judge.operator
OK      local/gemma3:27b                               15.87s  roles: judge.skeptic
air-gapped: 3 model calls, 0 left this network
```

Cold load (first call after the model is off the GPU): qwen3:30b 12.6 s, gemma3:27b 15.9 s, qwen3:8b ~40 s on its
very first load. Warm health-check calls: 0.5-2 s. Warm judge calls on real findings (one at a time): gpt-oss 1.7-4.7 s,
qwen3-30b-instruct 0.8-1 s, gemma3 4.4-5 s.

**Fix needed to get here:** Ollama counts reasoning tokens against `max_tokens`, so Qwen3 and gpt-oss returned empty
answers (`llm_check` printed `ODD` for qwen3:8b at 20 tokens). `flock/llm.py` now sends `reasoning_effort` to the
local server when `FLOCK_LOCAL_REASONING` is set; `scripts/gx10.sh env` sets it to `auto` (gpt-oss: `low`; others:
`none`, which turns Qwen3 thinking off and is ignored by Gemma). `llm_check` now allows a 200-token reply.

## 3. Speed: `judge_load.py`, Mac vs GX10

All with `FLOCK_AIRGAP=1`. Each finding goes through the full `panel.decide` (three judges in parallel, a debate
round on a split, plus in the fixture runs a reviewer memo on conflicts, which `--airgap` routes to gpt-oss).

| Where | Judges | Findings | Conc. | n | p50 s/finding | p95 | Findings/min | Errors | Air gap |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MacBook Air (baseline) | qwen2.5-coder:14b x3 | fixtures | 3 | 6 | 88.4 | 126.0 | 1.48 | 0 | 21 calls, 0 left |
| GX10 | qwen3:8b x3 | fixtures | 3 | 6 | 14.0 | 21.6 | 10.63 | 0 | 27 calls, 0 left |
| GX10 | gpt-oss:20b + qwen3:30b (thinking) + gemma3:27b | fixtures | 3 | 6 | 47.0 | 79.2 | 3.27 | **2** | 24 calls, 0 left |
| GX10 | gpt-oss:20b + qwen3-30b-instruct + gemma3:27b | fixtures | 3 | 24 | **26.9** | 81.0 | **4.11** | 0 | 110 calls, 0 left |
| GX10 | same | fixtures | 6 | 24 | 58.3 | 160.3 | 4.17 | 0 | 109 calls, 0 left |
| GX10 | same | seed 4821 | 3 | 24 | 24.8 | 30.5 | 7.03 | 0 | 76 calls, 0 left |
| GX10 | same | seed 4821 | 6 | 24 | 50.6 | 56.2 | 7.12 | 0 | 76 calls, 0 left |

The line the demo needs, verbatim from the runs: `air-gapped: 110 model calls, 0 left this network`.

Reading it:
- Three different 20-30B models on the GX10: **3.3x lower p50 and 2.8x the throughput** of one 14B model on the Mac,
  on the same fixtures. A small model (qwen3:8b on all three seats) is 6x lower p50 and 7x the throughput.
- Concurrency 6 doubles per-finding latency and adds no throughput: requests queue per model (one at a time). The
  bottleneck per run is the slowest judge (gemma3 on seeds; gpt-oss, which also writes reviewer memos, on fixtures).
  Raising Ollama's parallelism is a service setting, so we left it alone. A user-space second `ollama serve` on
  another port would be the no-sudo way to test it; not tried.
- The thinking `qwen3:30b` tag times out the operator judge (2 of 6 votes fell back to offline rules).
- Seed findings (generated deposits/fees) never split the panel (0 debated), so they're faster than the fixtures,
  which are built to be contested (10 of 24 debated).

## 4. Benchmark: `scripts/benchmark.py` on held-out seeds

Seeds **101-103** (of the harness's 101-110; three seeds to fit the night: 142 findings, 916 answer-key traps),
`ca_bagel_chain`, 56 stores x 14 days, `--workers 3`. Same findings, same prompts, no learned rules, equal weights for
every contestant; reviewer, writer and mapper offline for the local contestants (the harness's setup). Scored
against each seed's answer key by `scripts/score.py` logic.

| Contestant | Caught (traps) | False alarms | Correct $ | To a human (of 142) | p50 s/finding | p95 | Findings/min | $/1k | Offline | Run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Rules only | **916/916** | 0 | **765.60** | 28 | 0.000 | 0.001 | 284,000 | 0 | yes | BM-9ee343b1 |
| Claude, all three judges | skipped: no `ANTHROPIC_API_KEY` | | | | | | | | | BM-9ee343b1 |
| GX10 panel: gpt-oss-20b + qwen3-30b-instruct + gemma3-27b | 92/916 | 0 | 417.22 | 93 | 37.8 | 71.0 | 4.2 | 0 | yes | BM-9ee343b1 |
| GX10 single: qwen3-30b-instruct x3 | 895/916 | 0 | 692.82 | 47 | 6.9 | 13.8 | 21.4 | 0 | yes | BM-eef5c303 |
| GX10 single: gpt-oss-20b x3 | 862/916 | 0 | 492.00 | 74 | 17.4 | 38.6 | 8.3 | 0 | yes | BM-eef5c303 |
| GX10 single: gemma3-27b x3 | 145/916 | 0 | 545.17 | 34 | 47.8 | 107.7 | 2.6 | 0 | yes | BM-eef5c303 |
| Qwen2.5-7B-Instruct, base (`flock-judge-base`) | 910/916 | 0 | 751.69 | 30 | 11.7 | 23.5 | 12.9 | 0 | yes | BM-0ecb4209 |
| **Qwen2.5-7B, LoRA-tuned (`flock-judge-v1`)** | **916/916** | 0 | **765.60** | **28** | 15.9 | 23.6 | 10.4 | 0 | yes | BM-0ecb4209 |

0 votes fell back to offline rules in any model run (every vote was a real model answer). `$/1k` is 0 for local
models (GX10 power not metered).

Not run: **Claude** (no `ANTHROPIC_API_KEY`; the harness records it as skipped, it is not silently replaced by rules),
Groq, NVIDIA, the human contestant.

Rules-only on all ten held-out seeds (101-110): 3,176/3,176 caught, 0 false alarms, $2,452.55, 105 to a human.

How to read it:
- **"Caught" counts answer-key rows (orders), not findings, so it is dominated by one trap.** On seeds 101-103,
  `processing_fee_creep` is 765 of the 916 trap rows but only a handful of findings. A contestant that sends those
  few findings to a human (outcome `conflict` instead of `recoverable`) loses ~765 "caught" at once. That is what
  happened to the panel and to gemma3: gemma3 voted `conflict` on the fee-creep findings (765 rows), and the panel
  only files a dispute when its judges agree, so one skeptical judge turns the finding into a human question. The
  panel's 92/916 is cautious, not wrong: **no contestant filed a single false alarm.**
- Where single models lost rows: qwen3-30b-instruct sent `authorized_rep_waiver` (15) and `error_charge_after_72h`
  (4) to a human and called 2 error charges correct; gpt-oss sent `authorized_rep_waiver` (43) and
  `payout_deposit_mismatch` (6) to a human and called 5 rep-promise rows correct; the base Qwen2.5-7B missed
  `tax_withheld_platform_collected` (4 called correct, 2 sent to a human).
- The fastest model contestant is qwen3-30b-instruct (a 3B-active MoE): 6.9 s p50 per finding with three judge calls
  queued on one model. Three different models are slower per finding than the best single one here because the
  slowest judge (gemma3, ~48 s p50 when it holds all three seats) sets the pace.

## 5. Training: a tuned local judge (LoRA on the GX10)

**Environment (user space).** `~/flock-train` venv; `pip install torch --index-url
https://download.pytorch.org/whl/cu130` gave **torch 2.14.1+cu130** (aarch64), which sees the GB10:
`torch.cuda.is_available() == True`, device `NVIDIA GB10`, capability (12, 1), a 4096x4096 bf16 matmul ran. The cu130
index worked on the first try (cu129 / cu128 not needed). Then transformers 5.20.2, peft 0.21.2, trl 1.16.0,
datasets 5.1.0, accelerate 1.15.0.

**Data.** `scripts/judge_dataset.py --seeds 1-50 --val-seeds 46-50` on the GX10: 2,294 findings, 6,156 train / 726
val examples (one per finding x judge persona). Train labels: recoverable 1,437, conflict 480, correct 135 (findings).
Held-out benchmark seeds 101-110 locked out (the script refuses them; the manifest records the lock).

**Config.** `Qwen/Qwen2.5-7B-Instruct` (ungated), LoRA r 16 / alpha 32 / dropout 0.05 on q, k, v, o, gate, up, down
(40.4M trainable params, 0.53%), bf16, lr 2e-4 cosine, 5% warmup, batch 8 x grad-accum 2 = 16, max length 1,536
(examples are 410 tokens median, 543 max), loss on the assistant turn only. **1 epoch over a random 2,400 of the
6,156 training examples** (150 steps). Why the subsample: the full set ran at 31 s/step while sharing the GPU with
the benchmark (3 h 20 min projected); alone, without gradient checkpointing, it ran at ~9.5 s/step, and the three
persona copies of each finding carry the same label, so 2,400 examples still cover ~all training findings. Eval on
200 random val examples (seeds 46-50, never trained on). **29.7 minutes** on the GB10.

| Step | 0 | 5 | 10 | 15 | 20 | 25 | 50 | 75 | 100 | 125 | 150 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Train loss | | 1.831 | 0.731 | 0.131 | 0.032 | 0.0050 | 3.4e-5 | 1.2e-5 | 9e-6 | 8e-6 | 8e-6 |
| Val loss | 1.983 | | | | | 0.0040 | 2.2e-5 | 1.1e-5 | 9.0e-6 | 8.7e-6 | 8.5e-6 |

Val loss falls with train loss: the target is learnable and it generalises to unseen seeds. It goes to ~0 because
the labels are synthetic and the reason text is templated from the finding (see caveats): the model learns the
answer-key's mapping, not open-ended judgment.

**Serving.** Merged the adapter (`merge_export.py`), converted to GGUF q8_0 with llama.cpp's
`convert_hf_to_gguf.py` (user-space clone), `ollama create flock-judge-v1`. The untouched base went through the same
steps as `flock-judge-base`. Ollama 0.40's own safetensors import refused both (`unsupported MLX architecture:
Qwen2ForCausalLM`; its `--quantize` only offers int4/int8/nvfp4/mxfp4/mxfp8 and goes through the same MLX path), hence
the GGUF step. Both are served by the same Ollama through the same tunnel, so they enter the benchmark like any
other local model.

**Result** (seeds 101-103, same table as section 4):

| | Caught | False alarms | Correct $ | To a human | p50 | Findings/min |
| --- | --- | --- | --- | --- | --- | --- |
| Rules only | 916/916 | 0 | 765.60 | 28 | 0.000 s | 284,000 |
| **Tuned Qwen2.5-7B (`flock-judge-v1`)** | **916/916** | 0 | **765.60** | **28** | 15.9 s | 10.4 |
| Base Qwen2.5-7B (`flock-judge-base`, same packaging) | 910/916 | 0 | 751.69 | 30 | 11.7 s | 12.9 |
| Best off-the-shelf GX10 model (qwen3-30b-instruct x3) | 895/916 | 0 | 692.82 | 47 | 6.9 s | 21.4 |
| GX10 three-model panel | 92/916 | 0 | 417.22 | 93 | 37.8 s | 4.2 |
| Claude | not run (no API key) | | | | | |

The tuned 7B matches the rules exactly on three held-out seeds (same rows caught, same dollars, same 28 sent to a
human) and fixes the base model's 6 `tax_withheld_platform_collected` misses. It beats every off-the-shelf local
model we ran, including 4x larger ones. It is slower per finding than its base (15.9 s vs 11.7 s p50) because it
writes the full reason sentence it was trained on. What this does **not** show: that it beats the rules (it can
only tie on these labels), or anything about real data or Claude.

## Caveats (read before quoting any number)

- **Synthetic labels.** Truth is the generator's answer key, not people. The training targets are built from the same
  answer key and the reason sentences are templated from each finding's own text.
- **The rules already score 100%** on these seeds (they were built alongside the generator), so a model can at best tie
  them on accuracy here. The benchmark shows how often a model agrees with the key and how often it hands the case
  to a person, not that models beat rules.
- **Held-out seeds are honest but similar.** Seeds 101-103 were never trained or validated on; they come from the
  same generator and profile, so this is in-distribution generalisation, not new companies or real data.
- **"Caught" is order-weighted**: one trap (`processing_fee_creep`, 765 of 916 rows, a few findings) dominates it.
  Look at false alarms and "to a human" alongside it.
- **Three of ten held-out seeds** for the model contestants (time). Rules-only ran on all ten.
- **Code version.** Numbers are from the pre-deal-sources generator (`bca3166` / `c5c423c`). main has since added traps
  19-22 and changed the bagel profile; rerun before putting these next to new numbers.
- **Latency under contention.** The panel benchmark row ran partly while the tuned model was being converted and
  imported on the same box (CPU/disk, plus one 4th-model load that evicted a judge); judge_load numbers were taken
  with nothing else running. Model latency is the GX10 through an SSH tunnel over Tailscale from a laptop.
- **Training subsample.** 1 epoch over 2,400 of 6,156 examples (see above), not the full set.
- **Judge 2 swapped.** The preset's `qwen3:30b` is a thinking-only model and fails the panel's token budget; the
  results use `qwen3:30b-a3b-instruct-2507-q4_K_M` (8k-context alias).
- **Not done:** Claude contestant (no key), human contestant, vLLM serving, raising Ollama parallelism, full ten-seed
  model runs, a second training run / hyper-parameter search, any real (non-simulated) data.

## Reproduce

```bash
# GX10 models (on the laptop; pulls run on the GX10 under nohup, log ~/flock-pulls.log)
scripts/gx10.sh pull large
ssh gx10 'ollama pull qwen3:8b; ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M'
ssh gx10 'for m in "gpt-oss:20b gpt-oss-20b-8k" "qwen3:30b-a3b-instruct-2507-q4_K_M qwen3-30b-instruct-8k" "gemma3:27b gemma3-27b-8k"; do
  set -- $m; printf "FROM %s\nPARAMETER num_ctx 8192\n" $1 > /tmp/mf && ollama create $2 -f /tmp/mf; done'

# connect + check
scripts/gx10.sh tunnel &
export GX10_JUDGE_MODELS=gpt-oss:20b,qwen3:30b-a3b-instruct-2507-q4_K_M,gemma3:27b
eval "$(scripts/gx10.sh env --airgap)"
python scripts/llm_check.py

# speed
python scripts/judge_load.py --n 24 --concurrency 3        # and --concurrency 6
python scripts/judge_load.py --source seed --seed 4821 --n 24 --concurrency 3

# benchmark (FLOCK_LLM_TIMEOUT=300 so slow calls don't fall back)
FLOCK_LLM_TIMEOUT=300 python scripts/benchmark.py --seeds 101-103 --workers 3 \
  --contestants "rules,claude,local:gpt-oss-20b-8k+qwen3-30b-instruct-8k+gemma3-27b-8k,local:gpt-oss-20b-8k,local:qwen3-30b-instruct-8k,local:gemma3-27b-8k"

# training (on the GX10, see scripts/train/README.md)
git clone https://github.com/Preet37/FLOCK.git ~/flock && cd ~/flock
nohup bash scripts/train/setup_env.sh > ~/flock-train-setup.log 2>&1 &
TRAIN_ARGS="--train-limit 2400 --val-limit 200 --eval-steps 25 --no-grad-ckpt" nohup bash scripts/train/run_all.sh > ~/flock-train-run.log 2>&1 &

# tuned vs base (laptop, tunnel up)
FLOCK_LLM_TIMEOUT=300 python scripts/benchmark.py --seeds 101-103 --workers 3 \
  --contestants "local:flock-judge-base,local-tuned" --tuned-model flock-judge-v1
```
