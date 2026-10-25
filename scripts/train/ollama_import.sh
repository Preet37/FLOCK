#!/usr/bin/env bash
# ollama_import.sh: put a merged / exported safetensors model into the GX10's Ollama, no sudo.
#
#   bash scripts/train/ollama_import.sh ~/flock-train-out/judge-lora/merged flock-judge-v1
#   bash scripts/train/ollama_import.sh ~/flock-train-out/base               flock-judge-base
#
# Ollama 0.40 imports safetensors only through its MLX engine, which refuses Qwen2ForCausalLM. So we convert to
# GGUF first with llama.cpp's converter (a git clone + `pip install -e llama.cpp/gguf-py` in the venv, user space)
# and `ollama create` FROM the .gguf; the CLI streams it to the running service over its API, so no sudo.
# Both models get the same ChatML template and the same GGUF type (GGUF_TYPE, default q8_0), so the
# base-vs-tuned comparison measures the fine-tune only. Then, through the SSH tunnel from the laptop:
#   python scripts/benchmark.py --contestants local-tuned --tuned-model flock-judge-v1
set -eu
src="$1"; name="$2"; gtype="${GGUF_TYPE:-q8_0}"
LLAMA_CPP="${LLAMA_CPP:-$HOME/llama.cpp}"
[ -f "$LLAMA_CPP/convert_hf_to_gguf.py" ] || git clone -q --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_CPP"
python -c "import gguf" 2>/dev/null || pip install -q -e "$LLAMA_CPP/gguf-py"
gguf="${src%/}.${gtype}.gguf"
echo "convert ${src} -> ${gguf} (${gtype})"
python "$LLAMA_CPP/convert_hf_to_gguf.py" "$src" --outtype "$gtype" --outfile "$gguf"
mf="$(mktemp)"
cat > "$mf" <<EOF
FROM ${gguf}
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
echo "ollama create ${name} from ${gguf}"
ollama create "$name" -f "$mf"
rm -f "$mf"
ollama list | grep -E "^${name}[: ]"
