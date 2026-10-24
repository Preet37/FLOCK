"""Progress reporter. One small object the generator, formatter and pipeline call as they work.

  from flock.progress import Progress
  p = Progress(state_path)                 # state_path=None: terminal only
  p.stage("format", total=1_200_000)       # starts a stage (and closes the previous one)
  p.advance(50_000)                        # or p.update(done)
  p.finish()

It draws a terminal progress bar (stage, done/total, rows/sec) on stderr and writes the same numbers to a small
JSON state file (default data/state/progress.json, next to the store's findings) so the dashboard's live board can
show a progress bar (GET /api/progress). Writes are throttled, so calling advance() per row is fine.
NullProgress does nothing, for code paths that don't care.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock.store import STATE_DIR

STATE_NAME = "progress.json"
BAR = 28


def progress_path(store: Any = None) -> Path:
    """Where the state file lives: the store's own folder when it has one (JsonStore), else data/state/."""
    return Path(getattr(store, "dir", None) or STATE_DIR) / STATE_NAME


def read_progress(path: Path | None = None) -> dict[str, Any]:
    """The last state written, or {"status": "idle"} when nothing has run (or the file is mid-write)."""
    path = Path(path or progress_path())
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "idle"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(n: float) -> str:
    n = float(n)
    for unit, size in (("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


class Progress:
    def __init__(self, state_path: Path | str | None = None, stream=None, run: str = "",
                 min_interval: float = 0.25, bar: bool | None = None):
        self.path = Path(state_path) if state_path else None
        self.stream = stream if stream is not None else sys.stderr
        self.bar = bar if bar is not None else bool(getattr(self.stream, "isatty", lambda: False)())
        self.run = run
        self.min_interval = min_interval
        self.started = time.monotonic()
        self.started_at = _now()
        self.stages: list[dict[str, Any]] = []
        self.name: str | None = None
        self.total: int | None = None
        self.done = 0
        self.unit = "rows"
        self.t0 = self.started
        self._last = 0.0
        self.status = "running"

    # the calls agents make ----------------------------------------------------------------------------------
    def stage(self, name: str, total: int | None = None, unit: str = "rows") -> "Progress":
        self._close()
        self.name, self.total, self.done, self.unit, self.t0 = name, total, 0, unit, time.monotonic()
        self._emit(force=True)
        return self

    def advance(self, n: int = 1) -> None:
        self.update(self.done + n)

    def update(self, done: int, total: int | None = None) -> None:
        self.done = done
        if total is not None:
            self.total = total
        self._emit()

    def finish(self, status: str = "done", **extra: Any) -> dict[str, Any]:
        self._close()
        self.status = status
        return self._emit(force=True, extra=extra)

    # numbers ---------------------------------------------------------------------------------------------------
    def rate(self) -> float:
        dt = time.monotonic() - self.t0
        return self.done / dt if dt > 0 else 0.0

    def state(self) -> dict[str, Any]:
        return {"run": self.run, "status": self.status, "stage": self.name, "done": self.done, "total": self.total,
                "unit": self.unit, "rate": round(self.rate(), 1),
                "elapsed": round(time.monotonic() - self.started, 2),
                "started_at": self.started_at, "updated_at": _now(), "pid": os.getpid(), "stages": self.stages}

    def _close(self) -> None:
        if self.name is None:
            return
        secs = time.monotonic() - self.t0
        if self.total is not None and self.done < self.total:
            self.done = self.total
        self.stages.append({"name": self.name, "done": self.done, "total": self.total, "unit": self.unit,
                            "seconds": round(secs, 2), "rate": round(self.done / secs, 1) if secs > 0 else 0.0})
        self._draw(final=True)
        self.name = None

    def _emit(self, force: bool = False, extra: dict | None = None) -> dict[str, Any]:
        now = time.monotonic()
        if not force and now - self._last < self.min_interval:
            return {}
        self._last = now
        st = self.state()
        if extra:
            st.update(extra)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(st))
                tmp.replace(self.path)
            except OSError:
                pass  # progress must never break a run
        if self.name and self.bar:
            self._draw()
        return st

    def _draw(self, final: bool = False) -> None:
        if not self.stream or self.name is None:
            return
        rate = f"{_num(self.rate())} {self.unit}/s"
        if self.total:
            frac = min(1.0, self.done / self.total)
            filled = int(BAR * frac)
            line = (f"[{self.name:<16}] |{'#' * filled}{'-' * (BAR - filled)}| {frac:>4.0%} "
                    f"{_num(self.done)}/{_num(self.total)} {rate}")
        else:
            line = f"[{self.name:<16}] {_num(self.done)} {self.unit} {rate}"
        if final:
            line += f" ({time.monotonic() - self.t0:.1f}s)"
        if self.bar:
            self.stream.write("\r\033[K" + line + ("\n" if final else ""))
        elif final:
            self.stream.write(line + "\n")
        self.stream.flush()


class NullProgress(Progress):
    """Same calls, does nothing."""

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(None, stream=None, bar=False)
        self.stream = None

    def _emit(self, force: bool = False, extra: dict | None = None) -> dict[str, Any]:
        return {}


def ensure(progress: Progress | None) -> Progress:
    return progress if progress is not None else NullProgress()
