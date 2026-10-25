"""LoRA fine-tune of an instruct model as a Flock judge, on data/training/judge_train.jsonl (scripts/judge_dataset.py).

    python scripts/train/train_lora.py                                  # Qwen/Qwen2.5-7B-Instruct, 1 epoch, bf16
    python scripts/train/train_lora.py --base Qwen/Qwen3-8B --max-steps 50   # a quick smoke run

Plain transformers Trainer + peft (no TRL API surface to drift). Each example is {"messages": [system, user,
assistant]}, exactly what flock/llm.py sends at inference; the loss is on the assistant turn only (prompt tokens are
masked to -100). Validation loss on judge_val.jsonl (seeds never trained on) every --eval-steps.

Writes <out>/adapter (LoRA weights + tokenizer), <out>/loss.json (every logged train / eval point) and
<out>/run.json (config, counts, timings). Merge and export with scripts/train/merge_export.py.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model
from transformers import (AutoModelForCausalLM, AutoTokenizer, Trainer, TrainerCallback, TrainingArguments,
                          set_seed)

ROOT = Path(__file__).resolve().parents[2]


def load_jsonl(path: Path, limit: int | None, seed: int) -> list[dict]:
    rows = [json.loads(line) for line in path.open() if line.strip()]
    if limit and len(rows) > limit:
        rows = random.Random(seed).sample(rows, limit)
    return rows


def encode(tok, messages: list[dict], max_len: int) -> dict | None:
    """input_ids for the whole chat; labels = -100 on everything before the assistant's reply."""
    prompt = tok.apply_chat_template(messages[:-1], tokenize=True, add_generation_prompt=True)
    full = tok.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
    if hasattr(prompt, "input_ids"):  # some tokenizers return a BatchEncoding
        prompt, full = prompt["input_ids"], full["input_ids"]
    if full[:len(prompt)] != prompt:
        raise ValueError("chat template: the prompt is not a prefix of the full chat; cannot mask the prompt")
    # drop a trailing newline after the end-of-turn token so the model learns to stop at <|im_end|> (ChatML)
    if len(full) > len(prompt) + 1 and tok.decode(full[-1:]).strip() == "":
        full = full[:-1]
    if len(full) > max_len:
        return None
    return {"input_ids": full, "labels": [-100] * len(prompt) + full[len(prompt):]}


def collate(tok):
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id

    def fn(batch):
        n = max(len(b["input_ids"]) for b in batch)
        ids = torch.full((len(batch), n), pad, dtype=torch.long)
        labels = torch.full((len(batch), n), -100, dtype=torch.long)
        mask = torch.zeros((len(batch), n), dtype=torch.long)
        for i, b in enumerate(batch):
            k = len(b["input_ids"])
            ids[i, :k] = torch.tensor(b["input_ids"])
            labels[i, :k] = torch.tensor(b["labels"])
            mask[i, :k] = 1
        return {"input_ids": ids, "labels": labels, "attention_mask": mask}

    return fn


class Log(TrainerCallback):
    """Print every log line as one JSON object, flushed, so `tail -f` on the nohup log shows progress."""

    def __init__(self):
        self.t0 = time.time()

    def on_log(self, args, state, control, logs=None, **kw):
        if logs:
            print(json.dumps({"step": state.global_step, "of": state.max_steps,
                              "min": round((time.time() - self.t0) / 60, 1), **logs}), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="Qwen/Qwen2.5-7B-Instruct", help="ungated HF instruct model")
    ap.add_argument("--train", default=str(ROOT / "data/training/judge_train.jsonl"))
    ap.add_argument("--val", default=str(ROOT / "data/training/judge_val.jsonl"))
    ap.add_argument("--out", default=str(Path.home() / "flock-train-out/judge-lora"))
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=-1, help="cap steps (smoke test); -1 = full epochs")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--r", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--batch", type=int, default=8, help="per-device batch")
    ap.add_argument("--grad-accum", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=1536)
    ap.add_argument("--train-limit", type=int, default=None, help="subsample training examples")
    ap.add_argument("--val-limit", type=int, default=300, help="subsample validation examples (eval speed)")
    ap.add_argument("--eval-steps", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-grad-ckpt", action="store_true")
    a = ap.parse_args()
    set_seed(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    tok = AutoTokenizer.from_pretrained(a.base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    train_rows = load_jsonl(Path(a.train), a.train_limit, a.seed)
    val_rows = load_jsonl(Path(a.val), a.val_limit, a.seed)

    def enc(rows):
        got = [encode(tok, r["messages"], a.max_len) for r in rows]
        kept = [g for g in got if g]
        return Dataset.from_list(kept), len(got) - len(kept)

    train_ds, train_drop = enc(train_rows)
    val_ds, val_drop = enc(val_rows)
    lens = sorted(len(x) for x in train_ds["input_ids"])
    print(f"train {len(train_ds)} (dropped {train_drop} > {a.max_len} tokens), val {len(val_ds)} (dropped {val_drop}); "
          f"tokens/example median {lens[len(lens) // 2]} max {lens[-1]}", flush=True)

    model = AutoModelForCausalLM.from_pretrained(a.base, dtype=torch.bfloat16, device_map={"": 0},
                                                 attn_implementation="sdpa")
    if not a.no_grad_ckpt:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(
        r=a.r, lora_alpha=a.alpha, lora_dropout=a.dropout, bias="none", task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    steps_per_epoch = math.ceil(len(train_ds) / (a.batch * a.grad_accum))
    total_steps = a.max_steps if a.max_steps > 0 else math.ceil(steps_per_epoch * a.epochs)
    args = TrainingArguments(
        output_dir=str(out / "checkpoints"), num_train_epochs=a.epochs, max_steps=a.max_steps,
        per_device_train_batch_size=a.batch, per_device_eval_batch_size=a.batch, gradient_accumulation_steps=a.grad_accum,
        learning_rate=a.lr, lr_scheduler_type="cosine", warmup_steps=max(1, int(0.05 * total_steps)), weight_decay=0.0,
        bf16=True, logging_steps=5, eval_strategy="steps", eval_steps=a.eval_steps, save_strategy="no",
        report_to="none", dataloader_num_workers=2, remove_unused_columns=False, seed=a.seed)
    trainer = Trainer(model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds, data_collator=collate(tok),
                      callbacks=[Log()])
    first_eval = trainer.evaluate()
    print(json.dumps({"step": 0, "eval_loss_before_training": first_eval["eval_loss"]}), flush=True)
    trainer.train()
    final_eval = trainer.evaluate()
    print(json.dumps({"final_eval": final_eval}), flush=True)

    model.save_pretrained(out / "adapter")
    tok.save_pretrained(out / "adapter")
    hist = [{"step": 0, "eval_loss": first_eval["eval_loss"]}] + trainer.state.log_history
    (out / "loss.json").write_text(json.dumps(hist, indent=1))
    (out / "run.json").write_text(json.dumps({
        "base": a.base, "train_file": a.train, "val_file": a.val, "train_examples": len(train_ds),
        "val_examples": len(val_ds), "dropped": {"train": train_drop, "val": val_drop}, "steps_per_epoch": steps_per_epoch,
        "global_steps": trainer.state.global_step, "lora": {"r": a.r, "alpha": a.alpha, "dropout": a.dropout},
        "lr": a.lr, "epochs": a.epochs, "batch": a.batch, "grad_accum": a.grad_accum, "max_len": a.max_len,
        "eval_loss_before": first_eval["eval_loss"], "eval_loss_after": final_eval["eval_loss"],
        "minutes": round((time.time() - t0) / 60, 1), "torch": torch.__version__,
        "device": torch.cuda.get_device_name(0)}, indent=1))
    print(f"done in {(time.time() - t0) / 60:.1f} min -> {out / 'adapter'}", flush=True)


if __name__ == "__main__":
    main()
