#!/usr/bin/env bash
# run_all.sh: dataset -> LoRA -> merge -> Ollama import, on the GX10, in one nohup job.
#
#   cd ~/flock && nohup bash scripts/train/run_all.sh > ~/flock-train-run.log 2>&1 &
#   tail -f ~/flock-train-run.log
#
# Knobs: BASE [Qwen/Qwen2.5-7B-Instruct]  SEEDS [1-50]  VAL_SEEDS [46-50]  TRAIN_ARGS [extra train_lora.py args]
#        SKIP_DATASET=1 / SKIP_TRAIN=1 / SKIP_BASE=1 to resume part-way.
# Benchmark seeds (101-110) are locked out by scripts/judge_dataset.py itself.
set -eu
cd "$(dirname "$0")/../.."
. "${FLOCK_TRAIN_VENV:-$HOME/flock-train}/bin/activate"
BASE="${BASE:-Qwen/Qwen2.5-7B-Instruct}"
OUT="$HOME/flock-train-out"
stamp() { echo "== $(date -u +%FT%TZ) $*"; }

if [ -z "${SKIP_DATASET:-}" ]; then
  stamp "dataset: seeds ${SEEDS:-1-50}, val ${VAL_SEEDS:-46-50}"
  python scripts/judge_dataset.py --seeds "${SEEDS:-1-50}" --val-seeds "${VAL_SEEDS:-46-50}"
fi
if [ -z "${SKIP_TRAIN:-}" ]; then
  stamp "train: LoRA on $BASE"
  python scripts/train/train_lora.py --base "$BASE" --out "$OUT/judge-lora" ${TRAIN_ARGS:-}
fi
stamp "merge adapter"
python scripts/train/merge_export.py --adapter "$OUT/judge-lora/adapter" --out "$OUT/judge-lora/merged"
stamp "import tuned into ollama"
bash scripts/train/ollama_import.sh "$OUT/judge-lora/merged" flock-judge-v1
if [ -z "${SKIP_BASE:-}" ]; then
  stamp "export + import the untouched base the same way (fair comparison)"
  python scripts/train/merge_export.py --no-adapter --base "$BASE" --out "$OUT/base"
  bash scripts/train/ollama_import.sh "$OUT/base" flock-judge-base
fi
stamp "all done"
