#!/usr/bin/env bash
# ollama_import.sh: put a merged / exported safetensors model into the GX10's Ollama, no sudo.
#
#   bash scripts/train/ollama_import.sh ~/flock-train-out/judge-lora/merged flock-judge-v1
#   bash scripts/train/ollama_import.sh ~/flock-train-out/base               flock-judge-base
#
# `ollama create` streams the files to the running Ollama service over its API, so it works as a normal user.
# Both models get the same ChatML template and the same quantization (OLLAMA_QUANT, default int8; Ollama 0.40 accepts int4, int8, nvfp4, mxfp4, mxfp8 for safetensors), so the
# base-vs-tuned comparison measures the fine-tune only. Then, through the SSH tunnel from the laptop:
#   python scripts/benchmark.py --contestants local-tuned --tuned-model flock-judge-v1
set -eu
src="$1"; name="$2"; quant="${OLLAMA_QUANT:-int8}"
mf="$(mktemp)"
cat > "$mf" <<EOF
FROM ${src}
TEMPLATE """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ if .Prompt }}<|im_start|>user
{{ .Prompt }}<|im_end|>
{{ end }}<|im_start|>assistant
{{ .Response }}<|im_end|>
"""
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
PARAMETER num_ctx 4096
PARAMETER temperature 0
EOF
echo "ollama create ${name} --quantize ${quant} from ${src}"
ollama create "$name" --quantize "$quant" -f "$mf"
rm -f "$mf"
ollama list | grep -E "^${name}[: ]"
