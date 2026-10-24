"""Where findings and the audit log live.

Everything else depends on the `Store` protocol below, never on a concrete class.
Pick the backend with env FLOCK_STORE:
  json       (default) JSON files in data/state/ so everyone can run locally with zero setup
  spacetime  SpacetimeDB (stub for now, see SpacetimeStore)

  from flock.store import get_store
  store = get_store()
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"


@runtime_checkable
class Store(Protocol):
    """The six methods every backend implements. Nothing else in Flock touches storage."""

    def get(self, finding_id: str) -> dict[str, Any]:
        """One finding by id. Raises KeyError if it doesn't exist."""

    def all(self) -> list[dict[str, Any]]:
        """Every finding."""

    def put_new(self, finding: dict[str, Any]) -> None:
        """Insert a brand-new finding in state NEW. Everything after goes through the gatekeeper."""

    def commit(self, finding: dict[str, Any] | None, log_entry: dict[str, Any]) -> None:
        """Save a finding change (or None for a denied attempt) and its log entry atomically."""

    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        """Append-only audit log, oldest first, optionally for one finding."""

    def reset(self) -> None:
        """Wipe findings and the log (fresh demo run only)."""


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
        lines = self.log_path.read_text().split("\n")
        # The last piece is "" when the file ends cleanly. Anything else is a line another process
        # (the pipeline, while the dashboard reads) is still writing, so leave it until it's complete.
        rows = [json.loads(line) for line in lines[:-1] if line.strip()]
        return [r for r in rows if subject_id is None or r["subject_id"] == subject_id]

    def reset(self) -> None:
        for p in (self.findings_path, self.log_path):
            if p.exists():
                p.unlink()


class SpacetimeStore:
    """SpacetimeDB backend (owner: Ira). NOT built yet: selecting it raises NotImplementedError.

    Plan (same six methods as Store, so nothing else changes):
      Tables
        findings      finding_id (pk), state, channel, check_type, gap, outcome, ... plus the full row as JSON
        audit_log     id (auto-inc pk), at, actor, role, action, subject_id, allowed, before, after, diff,
                      reason, evidence. Insert-only: no reducer updates or deletes rows here.
        agent_status  agent_id (pk), state (idle|running|done), current_finding, updated_at.
                      Written by the pipeline so the live board can show which agent is working.
      Reducers
        put_new(finding)               insert into findings; state must be NEW
        commit(finding | null, entry)  upsert the finding (if allowed) and insert the log entry in ONE reducer
                                       call, so the change and its audit row land in the same transaction
        reset()                        demo only; clears findings + audit_log
        Later: move the role/state check from flock.gatekeeper into a `transition` reducer so the database
        itself refuses an agent approving, not just the Python client.
      Python side
        get/all/log read from subscribed tables (findings, audit_log WHERE subject_id = ?);
        put_new/commit/reset call the reducers. Connection from env SPACETIME_URI + SPACETIME_DB.
      Dashboard
        subscribes to findings + audit_log + agent_status and re-renders on row events instead of polling
        /api/summary. Approve / reject still go through the Flask endpoint -> gatekeeper.transition().
    """

    def __init__(self, *args: Any, **kwargs: Any):
        raise NotImplementedError(
            "FLOCK_STORE=spacetime is not implemented yet; use FLOCK_STORE=json (the default). "
            "Plan (see SpacetimeStore in flock/store.py): tables findings, audit_log, agent_status; "
            "reducers put_new / commit / reset, where commit writes the finding and its log entry in one "
            "transaction; the dashboard subscribes to the tables for live updates."
        )


class JournalStore:
    """Journal entries (flock/agents/journal.py), with the same six methods as Store so the gatekeeper can move them.

    Entries live in their own file (journal_entries.json next to the findings), keyed by je_id, so nothing that
    reads store.all() ever sees one. Their log entries go to the base store's audit log: one append-only trail for
    findings and journal entries (each entry's subject_type says which). The entry is saved first, then its log
    line is appended through base.commit(None, entry), which only appends.
    """

    def __init__(self, base: Store, path: Path | None = None):
        self.base = base
        self.path = Path(path) if path else Path(getattr(base, "dir", STATE_DIR)) / "journal_entries.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text() or "{}")

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def get(self, je_id: str) -> dict[str, Any]:
        return self._load()[je_id]

    def all(self) -> list[dict[str, Any]]:
        return list(self._load().values())

    def put_new(self, je: dict[str, Any]) -> None:
        """Only for brand-new entries in state NEW. The draft and everything after go through the gatekeeper."""
        assert je["state"] == "NEW", "put_new only accepts NEW journal entries"
        with self._lock:
            data = self._load()
            data[je["je_id"]] = je
            self._save(data)

    def commit(self, je: dict[str, Any] | None, log_entry: dict[str, Any]) -> None:
        with self._lock:
            if je is not None:
                data = self._load()
                data[je["je_id"]] = je
                self._save(data)
            self.base.commit(None, log_entry)

    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        if subject_id is not None:
            return self.base.log(subject_id)
        return [e for e in self.base.log() if e.get("subject_type") == "journal_entry"]

    def reset(self) -> None:
        """Wipe journal entries only; the shared audit log is reset by the base store."""
        self.path.unlink(missing_ok=True)


def get_journal_store(base: Store | None = None) -> JournalStore:
    """Journal entries next to the findings of `base` (default: get_store()), logging into the same audit log."""
    return JournalStore(base if base is not None else get_store())


BACKENDS: dict[str, type] = {"json": JsonStore, "spacetime": SpacetimeStore}


def get_store(state_dir: Path | None = None) -> Store:
    """The store picked by env FLOCK_STORE (json | spacetime). Defaults to json."""
    name = os.environ.get("FLOCK_STORE", "json").strip().lower() or "json"
    if name not in BACKENDS:
        raise ValueError(f"FLOCK_STORE={name!r} is not a known store. Pick one of: {', '.join(BACKENDS)}")
    if name == "json" and state_dir is not None:
        return JsonStore(state_dir)
    return BACKENDS[name]()
