"""Learned workflows (owner: Preet). Clicky watches a human file one dispute and learns the steps.

  demo.json  (flock/recorder.py: every navigation, click, fill and select the human made, with a robust locator
             for each element, the value typed and a screenshot per step; plus the finding the human was shown)
    -> learn()  each typed value is matched back to the finding field it came from (channel, order ids, amount,
                category, finding id, dispute draft). A value that matches nothing stays a constant. The password is
                never stored: it becomes "read FLOCK_PORTAL_PASS at replay time".
    -> a DRAFT workflow in data/workflows/file_dispute.json (gitignored)
    -> a human approves it once (python -m flock.agents.clicky --approve, or the dashboard's Clicky view)
    -> Clicky replays it for every approved finding (flock/agents/clicky.py, browser mode)
    -> a step whose element is gone (the portal changed) stops Clicky before it types anything and marks the
       workflow STALE; one new demonstration learns the next version.

Workflows move through the gatekeeper like findings and journal entries (machine WORKFLOW below):
agents learn and mark stale, only a human approves, and every move is in the audit log with the workflow's hash.
The approval guard recomputes the hash, so steps edited after learning can't be approved, and Clicky refuses an
approved workflow whose steps no longer hash to what the human approved.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flock import llm
from flock.models import Actor, Machine, money
from flock.store import ROOT, Store

WORKFLOWS_DIR = Path(os.environ.get("FLOCK_WORKFLOWS_DIR") or ROOT / "data" / "workflows")
DEMOS_DIR = Path(os.environ.get("FLOCK_DEMOS_DIR") or ROOT / "data" / "demos")
NAME = "file_dispute"
LEARNER = Actor(id="clicky.learner", role="agent")
CLI_HUMAN = Actor(id="cli-human", role="human")

# finding field -> how it reads in a step description. Order = matching priority when a value fits two fields.
FIELDS = {
    "finding_id": "finding ID",
    "order_ids": "order IDs",
    "amount": "dispute amount",
    "channel": "sales channel",
    "category": "dispute category",
    "dispute_draft": "approved dispute message",
}
# A workflow that doesn't take these from the finding would file the same order / dollars every time.
REQUIRED = ("order_ids", "amount")
SECRET_ENV, USER_ENV = "FLOCK_PORTAL_PASS", "FLOCK_PORTAL_USER"

WF_TRANSITIONS: dict[tuple[str, str], tuple[str, set[str]]] = {
    ("NEW", "learn"): ("DRAFT", {"agent"}),
    ("DRAFT", "approve"): ("APPROVED", {"human"}),
    ("DRAFT", "reject"): ("REJECTED", {"human"}),
    ("APPROVED", "stale"): ("STALE", {"agent", "gatekeeper"}),   # Clicky could not find a step's element
    ("DRAFT", "retire"): ("RETIRED", {"agent"}),                  # a newer demonstration replaced it
    ("APPROVED", "retire"): ("RETIRED", {"agent"}),
    ("STALE", "retire"): ("RETIRED", {"agent"}),
    ("REJECTED", "retire"): ("RETIRED", {"agent"}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# hashing: what the workflow DOES (targets, sources, order), not how it is described -----------------------------
BEHAVIOR_KEYS = ("phase", "action", "path", "target", "source", "navigates", "pattern")


def workflow_hash(wf: dict[str, Any]) -> str:
    body = {"name": wf.get("name"), "version": wf.get("version"),
            "steps": [{k: s.get(k) for k in BEHAVIOR_KEYS} for s in wf.get("steps", [])]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def missing_required(wf: dict[str, Any]) -> list[str]:
    fed = {s["source"].get("field") for s in wf.get("steps", []) if (s.get("source") or {}).get("kind") == "finding"}
    return [f for f in REQUIRED if f not in fed]


def _guard(wf: dict[str, Any], action: str) -> str | None:
    if action != "approve":
        return None
    if workflow_hash(wf) != wf.get("hash"):
        return "steps changed after Clicky learned them (hash mismatch); record a new demonstration"
    missing = missing_required(wf)
    if missing:
        return f"no step takes {' or '.join(FIELDS[m] for m in missing)} from the finding; it would file the same " \
               f"values every time"
    return None


WORKFLOW = Machine("workflow", "workflow_id", WF_TRANSITIONS, guard=_guard)


def usable(wf: dict[str, Any] | None) -> bool:
    """Approved by a human, and the steps still hash to exactly what the human approved."""
    return bool(wf) and wf["state"] == "APPROVED" and workflow_hash(wf) == wf.get("approved_hash") == wf.get("hash")


# storage ---------------------------------------------------------------------------------------------------------
class WorkflowStore:
    """Workflows, with the six Store methods so the gatekeeper can move them.

    <dir>/<name>.json is the current version (what Clicky reads); older versions move to <dir>/archive/.
    Log entries go to <dir>/audit_log.jsonl (kept across pipeline --reset, like the workflow itself) and, when a base
    store is given, to its audit log too, so approvals show up next to the filings they allow.
    """

    def __init__(self, base: Store | None = None, path: Path | None = None):
        self.base = base
        self.dir = Path(path or WORKFLOWS_DIR)
        self.log_path = self.dir / "audit_log.jsonl"
        self._lock = threading.Lock()

    def _current_path(self, name: str = NAME) -> Path:
        return self.dir / f"{name}.json"

    def _files(self) -> list[Path]:
        return sorted(self.dir.glob("*.json")) + sorted((self.dir / "archive").glob("*.json"))

    def _where(self, wid: str) -> Path | None:
        for p in self._files():
            try:
                if json.loads(p.read_text()).get("workflow_id") == wid:
                    return p
            except ValueError:
                continue
        return None

    @staticmethod
    def _write(path: Path, wf: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(wf, indent=2))
        tmp.replace(path)

    def current(self, name: str = NAME) -> dict[str, Any] | None:
        p = self._current_path(name)
        return json.loads(p.read_text()) if p.exists() else None

    def get(self, wid: str) -> dict[str, Any]:
        p = self._where(wid)
        if p is None:
            raise KeyError(wid)
        return json.loads(p.read_text())

    def all(self) -> list[dict[str, Any]]:
        out = []
        for p in self._files():
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                continue
        return out

    def put_new(self, wf: dict[str, Any]) -> None:
        """A brand-new workflow in state NEW becomes the current one; the previous current moves to archive/."""
        assert wf["state"] == "NEW", "put_new only accepts NEW workflows"
        with self._lock:
            cur = self._current_path(wf["name"])
            if cur.exists():
                old = json.loads(cur.read_text())
                (self.dir / "archive").mkdir(parents=True, exist_ok=True)
                shutil.move(str(cur), self.dir / "archive" / f"{old['workflow_id']}.json")
            self._write(cur, wf)

    def commit(self, wf: dict[str, Any] | None, log_entry: dict[str, Any]) -> None:
        with self._lock:
            if wf is not None:
                self._write(self._where(wf["workflow_id"]) or self._current_path(wf["name"]), wf)
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(log_entry) + "\n")
        if self.base is not None:
            self.base.commit(None, log_entry)

    def log(self, subject_id: str | None = None) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(x) for x in self.log_path.read_text().splitlines() if x.strip()]
        return [r for r in rows if subject_id is None or r["subject_id"] == subject_id]

    def reset(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def get_workflow_store(base: Store | None = None, path: Path | None = None) -> WorkflowStore:
    return WorkflowStore(base, path)


# demos -----------------------------------------------------------------------------------------------------------
def demo_dir(demo_id: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", demo_id or ""):
        raise ValueError(f"bad demo id {demo_id!r}")
    return Path(root or DEMOS_DIR) / demo_id


def load_demo(demo_id: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """demo.json by id, or the most recent one."""
    root = Path(root or DEMOS_DIR)
    if demo_id is None:
        demos = sorted(root.glob("*/demo.json"), key=lambda p: p.stat().st_mtime)
        if not demos:
            raise FileNotFoundError(f"no demonstrations in {root}; record one: python -m flock.agents.clicky record")
        path = demos[-1]
    else:
        path = demo_dir(demo_id, root) / "demo.json"
    demo = json.loads(path.read_text())
    demo["demo_id"] = path.parent.name  # the folder its screenshots and video are in
    return demo


# learning: typed values -> finding fields --------------------------------------------------------------------------
def finding_values(f: dict[str, Any]) -> dict[str, str]:
    """What the finding offers a form, in the portal's vocabulary (the same values the hard-coded path types)."""
    from flock import portal

    return {
        "finding_id": f["finding_id"],
        "order_ids": ", ".join(f["order_ids"]),
        "amount": f"{money(abs(f['gap'])):.2f}",
        "channel": f["channel"],
        "category": f["check_type"] if f["check_type"] in portal.CATEGORIES else "commission",
        "dispute_draft": f.get("dispute_draft") or "",
    }


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _amount(text: str) -> float | None:
    t = str(text).strip().replace(",", "")
    if not re.fullmatch(r"\$?\s*\d+(\.\d+)?", t):
        return None
    return money(t.lstrip("$").strip())


def match_field(value: str, f: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(field, format) when the typed value is exactly what this finding says for one field, else None."""
    if value is None or not str(value).strip():
        return None
    v = str(value)
    vals = finding_values(f)
    for field in FIELDS:
        want = vals[field]
        if not want:
            continue
        if field == "order_ids":
            parts = [p for p in re.split(r"[,;\s]+", v.strip()) if p]
            if sorted(parts) == sorted(f["order_ids"]):
                sep = ", " if ", " in v else "," if "," in v else ";" if ";" in v else " " if len(parts) > 1 else ", "
                return field, {"join": sep}
        elif field == "amount":
            if _amount(v) is not None and _amount(v) == money(abs(f["gap"])):
                return field, {"prefix": "$" if v.strip().startswith("$") else ""}
        elif field == "dispute_draft":
            if _norm(v) == _norm(want):
                return field, {}
        elif v.strip() == want:
            return field, {}
    return None


def value_for(source: dict[str, Any], f: dict[str, Any]) -> str:
    """The text one step types for this finding. Money is formatted by code, always to the cent."""
    kind = source["kind"]
    if kind == "constant":
        return source["value"]
    if kind == "env":  # the sandbox portal's own defaults, same as the hard-coded path; never read from the demo
        return os.environ.get(source["name"]) or {USER_ENV: "sandbox", SECRET_ENV: "sandbox"}.get(source["name"], "")
    field, fmt = source["field"], source.get("format") or {}
    if field == "order_ids":
        return fmt.get("join", ", ").join(f["order_ids"])
    if field == "amount":
        return f"{fmt.get('prefix', '')}{money(abs(f['gap'])):.2f}"
    return finding_values(f)[field]


def _target_name(t: dict[str, Any]) -> str:
    return t.get("label") or t.get("text") or t.get("placeholder") or t.get("name") or t.get("id") or t.get("css") or "?"


def _key(t: dict[str, Any]) -> str:
    return json.dumps({k: t.get(k) for k in ("label", "name", "id", "role", "text", "css")}, sort_keys=True)


def describe(step: dict[str, Any]) -> str:
    """Plain-English template for one step (the offline label)."""
    a, src, where = step["action"], step.get("source") or {}, _target_name(step.get("target") or {})
    if a == "goto":
        return f"Open {step['path']}"
    if a == "click":
        return f"Click “{where}”" + (" and wait for the next page" if step.get("navigates") else "")
    if a == "read_ref":
        return "Read back the portal's reference number from the confirmation page"
    verb = "Pick" if a == "select" else "Type"
    into = "in" if a == "select" else "into"
    if src.get("kind") == "finding":
        return f"{verb} the finding's {FIELDS[src['field']]} {into} “{where}”"
    if src.get("kind") == "env":
        what = "portal password" if src.get("secret") else "portal username"
        tail = ", never stored" if src.get("secret") else ""
        return f"{verb} the {what} (from {src['name']}{tail}) {into} “{where}”"
    shown = src.get("value", "")
    shown = shown if len(shown) <= 40 else shown[:37] + "..."
    return f"{verb} the same value every time, “{shown}”, {into} “{where}”"


def label_steps(steps: list[dict[str, Any]]) -> str:
    """Plain-English labels. The writer model route may reword them when one is configured; offline = template.
    Labels are display only: they are not part of the hash, and a bad reply keeps the templates."""
    for s in steps:
        s["describe"] = describe(s)
    r = llm.route("writer")
    if not llm.available(r):
        return "template"
    brief = [{"n": s["n"], "action": s["action"], "element": _target_name(s.get("target") or {}),
              "source": {k: v for k, v in (s.get("source") or {}).items() if k != "value"}} for s in steps]
    obj = llm.ask_json("You label the steps of a recorded web-form workflow for an accountant. One short imperative "
                       "sentence per step, plain English, no jargon. Reply {\"labels\": [\"...\", ...]} in order.",
                       json.dumps(brief), max_tokens=800, r=r)
    labels = (obj or {}).get("labels")
    if isinstance(labels, list) and len(labels) == len(steps) and all(isinstance(x, str) and x.strip() for x in labels):
        for s, text in zip(steps, labels):
            s["describe"] = text.strip()[:160]
        return llm.label(r)
    return "template"


def _ref_pattern(ref: str) -> str:
    m = re.fullmatch(r"([A-Z]+)-([A-Z0-9]+)", ref.strip())
    return rf"^{m.group(1)}-[A-Z0-9]{{{len(m.group(2))}}}$" if m else r"^\S+$"


def learn_steps(demo: dict[str, Any]) -> list[dict[str, Any]]:
    """demo events -> parameterized steps. Pure function (no browser, no files), so it is easy to test."""
    shown = demo["shown"]
    events = [e for e in demo["events"] if e.get("kind") in ("goto", "fill", "select", "click", "reference")]
    # A field the human typed in twice on the same page counts once, with the last value.
    keep: list[dict[str, Any]] = []
    for i, e in enumerate(events):
        if e["kind"] in ("fill", "select"):
            later = events[i + 1:]
            stop = next((j for j, x in enumerate(later) if x["kind"] == "goto" or x.get("navigated_to")), len(later))
            if any(x["kind"] in ("fill", "select") and _key(x["target"]) == _key(e["target"]) for x in later[:stop + 1]):
                continue
        keep.append(e)

    user = os.environ.get(USER_ENV, "sandbox")
    has_secret = any(e.get("secret") for e in keep)
    steps: list[dict[str, Any]] = []
    phase = "login" if has_secret else "file"
    seen_secret = False
    for e in keep:
        step: dict[str, Any] = {"n": len(steps) + 1, "phase": phase, "action": e["kind"], "path": e.get("path") or "/"}
        if e["kind"] == "goto":
            step["target"] = None
        else:
            step["target"] = e["target"]
        if e["kind"] in ("fill", "select"):
            if e.get("secret"):
                step["source"] = {"kind": "env", "name": SECRET_ENV, "secret": True}
                seen_secret = True
            elif has_secret and not seen_secret and phase == "login" and e.get("value") == user:
                step["source"] = {"kind": "env", "name": USER_ENV, "default": "sandbox"}
            else:
                hit = match_field(e.get("value", ""), shown)
                if hit:
                    step["source"] = {"kind": "finding", "field": hit[0], "format": hit[1]}
                else:
                    step["source"] = {"kind": "constant", "value": e.get("value", "")}
                step["demo_value"] = e.get("value", "")
                if e["kind"] == "select" and e.get("option_text"):
                    step["demo_option"] = e["option_text"]
        if e["kind"] == "click":
            step["navigates"] = bool(e.get("navigated_to"))
            if e.get("navigated_to"):
                step["next_path"] = e["navigated_to"]
        if e["kind"] == "reference":
            step["action"] = "read_ref"
            step["pattern"] = _ref_pattern(e.get("value", ""))
            step["demo_value"] = e.get("value", "")
        if e.get("screenshot"):
            step["screenshot"] = e["screenshot"]
        steps.append(step)
        if phase == "login" and seen_secret and e["kind"] == "click" and e.get("navigated_to"):
            phase = "file"
    # Navigations the human didn't make (redirects after a click) are not steps; the click carries them.
    return steps


def learn(demo: dict[str, Any], wstore: WorkflowStore, name: str = NAME,
          actor: Actor = LEARNER) -> dict[str, Any]:
    """Turn a demonstration into a DRAFT workflow (through the gatekeeper). Replaces the current version."""
    from flock.gatekeeper import transition

    steps = learn_steps(demo)
    if not any(s["phase"] == "file" for s in steps):
        raise ValueError("the demonstration has no steps after signing in; file one dispute while recording")
    labeled_by = label_steps(steps)
    prev = wstore.current(name)
    version = (prev or {}).get("version", 0) + 1
    fed = {s["source"]["field"]: s["n"] for s in steps if (s.get("source") or {}).get("kind") == "finding"}
    constants = [s["n"] for s in steps if (s.get("source") or {}).get("kind") == "constant"]
    wf = {
        "workflow_id": f"WF-{name}-v{version}", "name": name, "version": version, "state": "NEW",
        "learned_from": demo["demo_id"], "learned_at": _now(), "portal_layout": demo.get("portal_layout"),
        "portal_url": demo.get("portal_url"), "shown_finding": demo["shown"].get("finding_id"),
        "labeled_by": labeled_by, "steps": steps, "fields": fed, "constants": constants,
        "unused_fields": [f for f in FIELDS if f not in fed and finding_values(demo["shown"]).get(f)],
        "video": demo.get("video"), "trace": demo.get("trace"),
    }
    wf["hash"] = workflow_hash(wf)
    warnings = [f"no step takes the {FIELDS[m]} from the finding" for m in missing_required(wf)]
    warnings += [f"step {s['n']} types the same text every time ({describe(s)})" for s in steps
                 if s["n"] in constants and (s.get("target") or {}).get("tag") in ("input", "textarea")]
    wf["warnings"] = warnings
    if prev and prev["state"] not in ("RETIRED",):
        transition(wstore, prev["workflow_id"], "retire", actor, patch={"retired_by": wf["workflow_id"]},
                   reason=f"Replaced by {wf['workflow_id']}, learned from demonstration {demo['demo_id']}",
                   evidence=[{"demo_id": demo["demo_id"]}], machine=WORKFLOW)
    wstore.put_new(wf)
    return transition(wstore, wf["workflow_id"], "learn", actor,
                      reason=f"Learned {len(steps)} steps from demonstration {demo['demo_id']}: "
                             f"{len(fed)} filled from the finding, {len(constants)} constant. Hash {wf['hash'][:12]}. "
                             f"Waiting for a human to approve.",
                      evidence=[{"demo_id": demo["demo_id"], "workflow_hash": wf["hash"]}], machine=WORKFLOW)


def approve(wstore: WorkflowStore, approver: Actor = CLI_HUMAN, workflow_id: str | None = None,
            reason: str = "") -> dict[str, Any]:
    """Human-only (the gatekeeper enforces it). Records who approved and the exact hash they approved."""
    from flock.gatekeeper import transition

    wf = wstore.get(workflow_id) if workflow_id else wstore.current()
    if wf is None:
        raise KeyError("no learned workflow yet; record one: python -m flock.agents.clicky record")
    return transition(wstore, wf["workflow_id"], "approve", approver,
                      patch={"approved_by": approver.id, "approved_at": _now(), "approved_hash": wf.get("hash")},
                      reason=(reason or f"Approved {wf['workflow_id']} for Clicky") + f". Hash {wf.get('hash')}",
                      evidence=[{"workflow_hash": wf.get("hash"), "demo_id": wf.get("learned_from")}],
                      machine=WORKFLOW)


def mark_stale(wstore: WorkflowStore, wf: dict[str, Any], step: dict[str, Any] | None, why: str,
               actor: Actor, submitted: bool = False) -> dict[str, Any]:
    """Clicky could not follow the workflow. It stops, and the next filing needs one new demonstration."""
    from flock.gatekeeper import transition

    n = step["n"] if step else None
    sent = "A dispute may have been submitted; check the portal" if submitted else "Nothing was submitted"
    return transition(wstore, wf["workflow_id"], "stale", actor,
                      patch={"stale_reason": why, "stale_step": n, "stale_at": _now(), "stale_submitted": submitted},
                      reason=f"Stopped: {why}. {sent}. Record one new demonstration.",
                      evidence=[{"workflow_hash": wf.get("hash"), "step": n}], machine=WORKFLOW)
