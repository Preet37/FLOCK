import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE = json.loads((ROOT / "fixtures" / "fake_findings.json").read_text())
ADVERSARIAL = json.loads((ROOT / "fixtures" / "adversarial_findings.json").read_text())


@pytest.fixture(autouse=True)
def offline_and_isolated(tmp_path, monkeypatch):
    """Every test runs offline and never touches the repo's rules.json, state dir or sandbox portal."""
    from flock import llm
    from flock.agents import clicky, learner

    monkeypatch.setenv("FLOCK_OFFLINE", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for var in ("FLOCK_MODEL", "FLOCK_PROVIDER", "FLOCK_JUDGE_PROVIDERS", "FLOCK_JUDGE_MODELS", "FLOCK_REVIEWER_PROVIDER",
                "FLOCK_WRITER_PROVIDER", "FLOCK_MAPPER_PROVIDER", "FLOCK_REVIEWER_MODEL", "FLOCK_WRITER_MODEL",
                "FLOCK_MAPPER_MODEL", "FLOCK_AUDIT_RATE", "GROQ_API_KEY", "NVIDIA_API_KEY", "FLOCK_LOCAL_URL",
                "FLOCK_LOCAL_MODEL", "FLOCK_LOCAL_API_KEY", "FLOCK_GROQ_MODEL", "FLOCK_NVIDIA_MODEL", "FLOCK_NVIDIA_URL",
                "FLOCK_GROQ_URL", "FLOCK_AIRGAP", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm, "_client_obj", None)
    llm.reset_calls()
    monkeypatch.setattr(learner, "RULES", tmp_path / "iso_rules.json")
    monkeypatch.setattr(learner, "PROPOSED", tmp_path / "iso_proposed.json")
    monkeypatch.setattr(clicky, "PORTAL", tmp_path / "iso_portal.jsonl")


def fake(finding_id: str) -> dict:
    """A fresh copy of one fixture finding, in the shape the pipeline gives the panel."""
    from flock.models import Finding

    row = next(f for f in FAKE + ADVERSARIAL if f["finding_id"] == finding_id)
    return Finding(**copy.deepcopy(row)).to_dict()
