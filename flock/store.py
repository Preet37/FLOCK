"""Where findings and the audit log live.

Today: JSON files in data/state/ so everyone can run locally with zero setup.
Ira: swap this for SpacetimeDB later. Keep the same four methods and nothing else has to change.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"


class JsonStore:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.dir / "findings.json"
        self.log_path = self.dir / "audit_log.jsonl"
        self._lock = threading.Lock()

    # findings ---------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.findings_path.exists():
            return {}
        return json.loads(self.findings_path.read_text() or "{}")

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.findings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.findings_path)

    def get(self, finding_id: str) -> dict[str, Any]:
        return self._load()[finding_id]

    def all(self) -> list[dict[str, Any]]:
        return list(self._load().values())

    def put_new(self, finding: dict[str, Any]) -> None:
        """Only for brand-new findings in state NEW. Everything after goes through the gatekeeper."""
        assert finding["state"] == "NEW", "put_new only accepts NEW findings"
        with self._lock:
            data = self._load()
            data[finding["finding_id"]] = finding
            self._save(data)

    def commit(self, finding: dict[str, Any] | None, log_entry: dict[str, Any]) -> None:
        """Save the finding change and its log entry together (one 'transaction')."""
        with self._lock:
            if finding is not None:
                data = self._load()
                data[finding["finding_id"]] = finding
                self._save(data)
            with self.log_path.open("a") as f:
                f.write(json.dumps(log_entry) + "\n")

    # audit log --------------------------------------------------------
    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]
        return [r for r in rows if subject_id is None or r["subject_id"] == subject_id]

    def reset(self) -> None:
        for p in (self.findings_path, self.log_path):
            if p.exists():
                p.unlink()
