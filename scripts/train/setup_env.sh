#!/usr/bin/env bash
# setup_env.sh: user-space training venv on the GX10 (GB10, aarch64, CUDA 13 driver). No sudo, no Docker.
#
#   nohup bash scripts/train/setup_env.sh > ~/flock-train-setup.log 2>&1 &
#
# Tries the official PyTorch CUDA wheels for aarch64 (cu130, then cu129, cu128), checks the GPU with a bf16
# matmul, then installs the Hugging Face stack. Venv: ~/flock-train (FLOCK_TRAIN_VENV to change).
set -x
VENV="${FLOCK_TRAIN_VENV:-$HOME/flock-train}"
python3 -m venv "$VENV"
. "$VENV/bin/activate"
pip install -U pip wheel setuptools
for cu in cu130 cu129 cu128; do
  echo "== trying $cu"
  if pip install torch --index-url "https://download.pytorch.org/whl/$cu"; then
    python -c "import torch; assert torch.cuda.is_available()" && break
  fi
done
python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda, "available", torch.cuda.is_available())
print("device", torch.cuda.get_device_name(0), "capability", torch.cuda.get_device_capability(0))
a = torch.randn(4096, 4096, device="cuda", dtype=torch.bfloat16)
b = a @ a
torch.cuda.synchronize()
print("bf16 matmul ok", b.float().abs().mean().item())
PY
pip install transformers peft trl datasets accelerate safetensors sentencepiece
python -c "import transformers, peft, trl, datasets, accelerate; print('transformers', transformers.__version__, 'peft', peft.__version__, 'trl', trl.__version__, 'datasets', datasets.__version__, 'accelerate', accelerate.__version__)"
echo "== setup done"
