"""Merge the LoRA adapter into its base model and save plain safetensors (for `ollama create` or vLLM).

    python scripts/train/merge_export.py                         # ~/flock-train-out/judge-lora/adapter -> .../merged
    python scripts/train/merge_export.py --no-adapter --out ~/flock-train-out/base   # the untouched base, same format

--no-adapter exports the base model exactly as downloaded, so base and tuned go through the same import and
quantization and the benchmark compares the fine-tune, not the packaging.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

OUT = Path.home() / "flock-train-out"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adapter", default=str(OUT / "judge-lora/adapter"))
    ap.add_argument("--base", default=None, help="default: the base recorded in the adapter's config")
    ap.add_argument("--out", default=str(OUT / "judge-lora/merged"))
    ap.add_argument("--no-adapter", action="store_true", help="export the base model only")
    a = ap.parse_args()
    base = a.base or json.loads((Path(a.adapter) / "adapter_config.json").read_text())["base_model_name_or_path"]
    model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16, device_map={"": 0})
    tok = AutoTokenizer.from_pretrained(base)
    if not a.no_adapter:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, a.adapter).merge_and_unload()
    model.save_pretrained(a.out, safe_serialization=True, max_shard_size="5GB")
    tok.save_pretrained(a.out)
    print(f"{'base' if a.no_adapter else 'merged'} model ({base}) -> {a.out}")


if __name__ == "__main__":
    main()
