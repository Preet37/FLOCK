"""Where findings and the audit log live.

Everything else depends on the `Store` protocol below, never on a concrete class.
Pick the backend with env FLOCK_STORE:
  json       (default) JSON files in data/state/ so everyone can run locally with zero setup
  spacetime  SpacetimeDB over its HTTP API (see SpacetimeStore and spacetimedb/README.md)

  from flock.store import get_store
  store = get_store()
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from flock import agents_live

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"


@runtime_checkable
class Store(Protocol):
    """The six methods every backend implements, plus the live agent feed (two methods with no-op defaults, so a
    backend without it still works: flock.agents_live also checks with getattr). Nothing else in Flock touches storage."""

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

    # live agent feed (flock/agents_live.py) -----------------------------------------------------------------
    def agent_event(self, event: dict[str, Any]) -> None:
        """Append one agent status event (spawn / working / done / failed). Default: dropped."""
        return None

    def agents(self, since: int | None = None) -> list[dict[str, Any]]:
        """Agent status events, oldest first, each with a `seq`; since=N skips the first N. Default: none."""
        return []


class JsonStore:
    def __init__(self, state_dir: Path = STATE_DIR):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.dir / "findings.json"
        self.log_path = self.dir / "audit_log.jsonl"
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] | None = None   # set inside batch()
        self._pending: list[str] = []
        self._flushed = 0.0
        self._every = 0.5
        self.feed = agents_live.Feed(self.dir / agents_live.STATE_NAME)

    # batching (not part of the Store protocol: a speed-up for big runs) -----------------------------
    @contextmanager
    def batch(self, flush_every: float = 0.5):
        """Keep findings in memory and write findings.json + the log at most every flush_every seconds
        (and at the end), instead of rewriting the whole file on every transition. Same results; a run with
        thousands of findings goes from minutes to seconds. Readers (the dashboard) see it at most that stale."""
        if self._cache is not None:  # already batching
            yield self
            return
        with self._lock:
            self._cache, self._pending, self._every, self._flushed = self._read(), [], flush_every, time.monotonic()
            self.feed.buffered, self.feed.every = True, flush_every
        try:
            yield self
        finally:
            with self._lock:
                self._flush()
                self._cache = None
            self.feed.buffered = False
            self.feed.flush()

    def _flush(self) -> None:
        if self._cache is not None:
            self._write(self._cache)
        if self._pending:
            with self.log_path.open("a") as f:
                f.write("".join(self._pending))
            self._pending = []
        self._flushed = time.monotonic()

    def _maybe_flush(self) -> None:
        if time.monotonic() - self._flushed >= self._every:
            self._flush()

    # findings ---------------------------------------------------------
    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.findings_path.exists():
            return {}
        return json.loads(self.findings_path.read_text() or "{}")

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.findings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.findings_path)

    def _load(self) -> dict[str, dict[str, Any]]:
        return self._cache if self._cache is not None else self._read()

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        if self._cache is not None:
            self._maybe_flush()
        else:
            self._write(data)

    def get(self, finding_id: str) -> dict[str, Any]:
        with self._lock:  # another thread (the API's background run) may be writing the batch cache
            return self._load()[finding_id]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
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
            if self._cache is not None:
                self._pending.append(json.dumps(log_entry) + "\n")
                self._maybe_flush()
            else:
                with self.log_path.open("a") as f:
                    f.write(json.dumps(log_entry) + "\n")

    # audit log --------------------------------------------------------
    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        if self._cache is not None and self._pending:
            with self._lock:
                self._flush()
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text().split("\n")
        # The last piece is "" when the file ends cleanly. Anything else is a line another process
        # (the pipeline, while the dashboard reads) is still writing, so leave it until it's complete.
        rows = []
        for line in lines[:-1]:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:  # a damaged line (e.g. a crash mid-write) is skipped, never fatal to a reader
                    continue
        return [r for r in rows if subject_id is None or r["subject_id"] == subject_id]

    def reset(self) -> None:
        if self._cache is not None:
            self._cache, self._pending = {}, []
        for p in (self.findings_path, self.log_path):
            if p.exists():
                p.unlink()
        self.feed.reset()

    # live agent feed: agent_status.jsonl, buffered like the log inside batch() --------------------------------
    def agent_event(self, event: dict[str, Any]) -> None:
        self.feed.add(event)

    def agents(self, since: int | None = None) -> list[dict[str, Any]]:
        return self.feed.read(since)

    def flush_agents(self) -> None:
        """Write buffered agent events now (not part of the protocol: the tracker calls it after a burst)."""
        self.feed.flush()


class SpacetimeStore:
    """SpacetimeDB backend: the same six methods plus the live agent feed, over SpacetimeDB's HTTP API.

    The module (spacetimedb/src/lib.rs) keeps three public tables: finding (finding_id pk, state, row JSON),
    audit_log (id auto-inc, subject_id, entry JSON; insert-only) and agent_status (seq auto-inc, event JSON).
    Writes are reducer calls, so a finding change and its audit entry land in ONE transaction (`commit`); reads are
    SQL. Env: SPACETIME_URL (default http://127.0.0.1:3000), SPACETIME_DB (default flock), SPACETIME_TOKEN (optional,
    needed on Maincloud). Sidecar files other parts of the pipeline keep next to the store (market ledger, run history,
    progress, journal entries) live in `self.dir`, data/state/spacetime by default.

      spacetime start && spacetime publish flock --server local --module-path spacetimedb --yes
      FLOCK_STORE=spacetime python -m flock.pipeline --reset --forget --demo-approve
    """

    def __init__(self, url: str | None = None, database: str | None = None, token: str | None = None,
                 state_dir: Path | None = None):
        import urllib.request  # noqa: F401  (kept local: the JSON backend must not need the network stack)

        self.url = (url or os.environ.get("SPACETIME_URL") or "http://127.0.0.1:3000").rstrip("/")
        self.database = database or os.environ.get("SPACETIME_DB") or "flock"
        self.token = token if token is not None else os.environ.get("SPACETIME_TOKEN", "")
        self.dir = Path(state_dir or os.environ.get("SPACETIME_STATE_DIR") or STATE_DIR / "spacetime")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # --- HTTP -------------------------------------------------------------------------------------------------
    def _request(self, method: str, path: str, body: bytes | None, content_type: str) -> bytes:
        import urllib.error
        import urllib.request

        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.url}/v1/database/{self.database}{path}", data=body, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300] if e.fp else ""
            raise RuntimeError(f"SpacetimeDB {method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"SpacetimeDB unreachable at {self.url} ({e.reason}). "
                               "Run `spacetime start` and publish the module (spacetimedb/README.md).") from None

    def call(self, reducer: str, *args: Any) -> None:
        """One reducer call = one transaction."""
        self._request("POST", f"/call/{reducer}", json.dumps(list(args)).encode(), "application/json")

    def sql(self, query: str) -> list[dict[str, Any]]:
        """Rows of the first statement as dicts, column names taken from the returned schema."""
        raw = self._request("POST", "/sql", query.encode(), "text/plain")
        results = json.loads(raw or b"[]")
        if not results:
            return []
        first = results[0]
        names = [_column_name(el, i) for i, el in enumerate((first.get("schema") or {}).get("elements", []))]
        out = []
        for row in first.get("rows", []):
            values = row if isinstance(row, list) else [row]
            out.append({names[i] if i < len(names) else f"c{i}": _plain(v) for i, v in enumerate(values)})
        return out

    # --- findings ---------------------------------------------------------------------------------------------
    @staticmethod
    def _q(text: str) -> str:
        return "'" + str(text).replace("'", "''") + "'"

    def get(self, finding_id: str) -> dict[str, Any]:
        rows = self.sql(f"SELECT * FROM finding WHERE finding_id = {self._q(finding_id)}")
        if not rows:
            raise KeyError(finding_id)
        return json.loads(rows[0]["row"])

    def all(self) -> list[dict[str, Any]]:
        return [json.loads(r["row"]) for r in self.sql("SELECT * FROM finding")]

    def put_new(self, finding: dict[str, Any]) -> None:
        assert finding["state"] == "NEW", "put_new only accepts NEW findings"
        with self._lock:
            self.call("put_new", finding["finding_id"], finding["state"], json.dumps(finding))

    def commit(self, finding: dict[str, Any] | None, log_entry: dict[str, Any]) -> None:
        """The finding change (or none, for a denied attempt) and its log entry in one reducer call."""
        with self._lock:
            if finding is not None:
                self.call("commit", finding["finding_id"], finding["state"], json.dumps(finding),
                          str(log_entry.get("subject_id", "")), json.dumps(log_entry))
            else:
                self.call("commit", "", "", "", str(log_entry.get("subject_id", "")), json.dumps(log_entry))

    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        where = f" WHERE subject_id = {self._q(subject_id)}" if subject_id is not None else ""
        rows = self.sql(f"SELECT * FROM audit_log{where}")
        rows.sort(key=lambda r: int(r.get("id", 0)))
        return [json.loads(r["entry"]) for r in rows]

    def reset(self) -> None:
        with self._lock:
            self.call("reset")

    # --- live agent feed ---------------------------------------------------------------------------------------
    def agent_event(self, event: dict[str, Any]) -> None:
        self.call("agent_event", json.dumps(event))

    def agents(self, since: int | None = None) -> list[dict[str, Any]]:
        rows = self.sql("SELECT * FROM agent_status")
        rows.sort(key=lambda r: int(r.get("seq", 0)))
        events = []
        for i, r in enumerate(rows):
            ev = json.loads(r["event"])
            events.append({"seq": i, **{k: v for k, v in ev.items() if k != "seq"}})
        return events[since:] if since else events

    def flush_agents(self) -> None:
        return None  # every event is already in the database


def _column_name(element: Any, index: int) -> str:
    """SpacetimeDB encodes an optional name as {"some": "x"} (or a plain string in older servers)."""
    name = element.get("name") if isinstance(element, dict) else None
    if isinstance(name, dict):
        name = name.get("some")
    return str(name) if name else f"c{index}"


def _plain(value: Any) -> Any:
    """SATS JSON wraps some scalars (e.g. u64 as a string, options as {"some": x} / {"none": []})."""
    if isinstance(value, dict) and len(value) == 1:
        if "some" in value:
            return _plain(value["some"])
        if "none" in value:
            return None
    return value


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


def batched(store: Store):
    """store.batch() when the backend has one, else a no-op context (every write lands immediately)."""
    return store.batch() if hasattr(store, "batch") else nullcontext(store)


def get_store(state_dir: Path | None = None) -> Store:
    """The store picked by env FLOCK_STORE (json | spacetime). Defaults to json."""
    name = os.environ.get("FLOCK_STORE", "json").strip().lower() or "json"
    if name not in BACKENDS:
        raise ValueError(f"FLOCK_STORE={name!r} is not a known store. Pick one of: {', '.join(BACKENDS)}")
    if name == "json" and state_dir is not None:
        return JsonStore(state_dir)
    return BACKENDS[name]()
