# Tuned local judge: train on the GX10, serve through Ollama, score on held-out seeds

Everything here runs as a normal user on the GX10 (no sudo, no Docker, no service changes). Results from the
first run: [`docs/gx10_results.md`](../../docs/gx10_results.md).

| File | What it does |
| --- | --- |
| `setup_env.sh` | `~/flock-train` venv: PyTorch CUDA wheel for aarch64 (tries cu130, cu129, cu128), checks the GB10 with a bf16 matmul, installs transformers, peft, trl, datasets, accelerate |
| `train_lora.py` | LoRA (r 16, alpha 32, all attention + MLP projections) on `data/training/judge_train.jsonl`, bf16, 1 epoch. Loss on the assistant turn only. Eval loss on `judge_val.jsonl` before training and every `--eval-steps`. Writes `adapter/`, `loss.json`, `run.json` |
| `merge_export.py` | Merges the adapter into the base and saves safetensors. `--no-adapter` exports the untouched base the same way |
| `ollama_import.sh` | `ollama create <name> --quantize int8` from a safetensors folder, ChatML template. Works without sudo (the CLI streams the files to the service) |
| `run_all.sh` | dataset, train, merge, import tuned as `flock-judge-v1`, export and import the base as `flock-judge-base` |

## Rerun it

```bash
# on the GX10 (ssh gx10)
git clone https://github.com/Preet37/FLOCK.git ~/flock && cd ~/flock
nohup bash scripts/train/setup_env.sh > ~/flock-train-setup.log 2>&1 &      # ~10 min, mostly the torch download
nohup bash scripts/train/run_all.sh  > ~/flock-train-run.log 2>&1 &         # dataset + train + merge + import
tail -f ~/flock-train-run.log                                               # JSON line per logged step

# on the laptop
scripts/gx10.sh tunnel &                                                    # 127.0.0.1:11435 -> GX10 Ollama
eval "$(scripts/gx10.sh env --airgap)"
python scripts/benchmark.py --seeds 101-103 --workers 3 \
    --contestants "rules,local:flock-judge-base,local-tuned" --tuned-model flock-judge-v1
```

Knobs for `run_all.sh`: `BASE` (default `Qwen/Qwen2.5-7B-Instruct`, ungated), `SEEDS` / `VAL_SEEDS` (default 1-50 /
46-50), `TRAIN_ARGS` (passed to `train_lora.py`, e.g. `--max-steps 50` for a smoke run), `SKIP_DATASET=1`,
`SKIP_TRAIN=1`, `SKIP_BASE=1` to resume part-way.

## Honesty rules (do not break these)

- Benchmark seeds 101-110 are refused by `scripts/judge_dataset.py`. Validation seeds (46-50) are never trained on.
- Labels are synthetic: they come from the generator's answer key, not from people. The offline rules already score
  100% on these seeds, so the tuned model can at best tie the rules here. What it can show: a small local model
  learning the panel's judgment format and labels, measured on seeds it never saw.
- Base and tuned go through the same export, the same int8 quantization and the same template, so the comparison
  measures the fine-tune, not the packaging.

## Gotchas we hit

- transformers 5.x removed `TrainingArguments(warmup_ratio=...)` and `group_by_length`; the script computes
  `warmup_steps` instead.
- Ollama on the GX10 serves one request per model at a time (service default; we don't change the service), so
  model calls queue. Training and serving share the 121 GB of unified memory; keep the served models at 8k context
  (`ollama create <alias> -f` with `PARAMETER num_ctx 8192`) or Ollama reserves up to 45 GB for a 262k context.
