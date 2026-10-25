"""SpacetimeStore: the Store protocol over SpacetimeDB's HTTP API. A tiny in-process server stands in for SpacetimeDB
(reducer calls + SQL in the SATS JSON shape the real server returns), so these run anywhere. Set
FLOCK_SPACETIME_LIVE=1 with `spacetime start` + the module published to also run the same checks against the real thing."""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flock.store import SpacetimeStore, Store, get_store


class FakeSpacetime(BaseHTTPRequestHandler):
    """Three tables, four reducers, the handful of SQL shapes SpacetimeStore sends. Shared state per server."""

    state = {"finding": {}, "audit_log": [], "agent_status": []}
    calls: list = []

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        st = type(self).state
        type(self).calls.append((self.path, self.headers.get("Authorization")))
        m = re.match(r"^/v1/database/(?P<db>[^/]+)/(?P<what>call/(?P<reducer>\w+)|sql)$", self.path)
        if not m:
            return self._send(404, {"error": "no such route"})
        if m.group("what") == "sql":
            q = body.decode().strip()
            t = re.search(r"FROM (\w+)", q).group(1)
            rows = list(st[t].values()) if isinstance(st[t], dict) else list(st[t])
            w = re.search(r"WHERE (\w+) = '((?:[^']|'')*)'", q)
            if w:
                col, val = w.group(1), w.group(2).replace("''", "'")
                rows = [r for r in rows if r[col] == val]
            cols = {"finding": ["finding_id", "state", "row"], "audit_log": ["id", "subject_id", "entry"],
                    "agent_status": ["seq", "event"]}[t]
            kinds = {"id": {"U64": []}, "seq": {"U64": []}}
            schema = {"elements": [{"name": {"some": c}, "algebraic_type": kinds.get(c, {"String": []})} for c in cols]}
            return self._send(200, [{"schema": schema, "rows": [[r[c] for c in cols] for r in rows],
                                     "total_duration_micros": 1, "stats": {}}])
        reducer, args = m.group("reducer"), json.loads(body or b"[]")
        if reducer == "put_new":
            fid, state, row = args
            if state != "NEW":
                return self._send(400, "put_new only accepts NEW findings")
            st["finding"][fid] = {"finding_id": fid, "state": state, "row": row}
        elif reducer == "commit":
            fid, state, row, subject, entry = args
            if fid:
                st["finding"][fid] = {"finding_id": fid, "state": state, "row": row}
            st["audit_log"].append({"id": len(st["audit_log"]) + 1, "subject_id": subject, "entry": entry})
        elif reducer == "agent_event":
            st["agent_status"].append({"seq": len(st["agent_status"]) + 1, "event": args[0]})
        elif reducer == "reset":
            st["finding"].clear(); st["audit_log"].clear(); st["agent_status"].clear()
        else:
            return self._send(404, {"error": "no such reducer"})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture(params=["fake"] + (["live"] if os.environ.get("FLOCK_SPACETIME_LIVE") == "1" else []))
def store(request, tmp_path):
    if request.param == "live":
        s = SpacetimeStore(state_dir=tmp_path)
        s.reset()
        yield s
        s.reset()
        return
    FakeSpacetime.state = {"finding": {}, "audit_log": [], "agent_status": []}
    FakeSpacetime.calls = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSpacetime)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield SpacetimeStore(url=f"http://127.0.0.1:{srv.server_port}", database="flock", token="tok", state_dir=tmp_path)
    finally:
        srv.shutdown()


def finding(fid="F-0001", state="NEW"):
    return {"finding_id": fid, "state": state, "order_ids": ["ORD-1"], "channel": "doordash", "check_type": "commission",
            "expected": 4.0, "actual": 6.5, "gap": 2.5, "evidence": [{"source_file": "a.csv", "row": 1, "what": "it's 'quoted'"}],
            "outcome": None, "confidence": None, "reason": "", "votes": [], "rank_score": None, "dispute_draft": None}


def entry(subject, action, allowed=True):
    return {"at": "2026-10-25T00:00:00+00:00", "actor": "checkers", "role": "agent", "action": action, "subject_id": subject,
            "allowed": allowed, "before": "NEW", "after": "CHECKED" if allowed else "NEW", "diff": {}, "reason": "r", "evidence": []}


def test_conforms_to_the_protocol(store):
    assert isinstance(store, Store)


def test_put_get_all_and_quotes_survive(store):
    store.put_new(finding("F-0001"))
    store.put_new(finding("F-0002"))
    got = store.get("F-0001")
    assert got["finding_id"] == "F-0001" and got["evidence"][0]["what"] == "it's 'quoted'"
    assert {f["finding_id"] for f in store.all()} == {"F-0001", "F-0002"}
    with pytest.raises(KeyError):
        store.get("F-NOPE")


def test_put_new_refuses_anything_but_new(store):
    with pytest.raises((AssertionError, RuntimeError)):
        store.put_new(finding("F-0003", "JUDGED"))


def test_commit_writes_finding_and_log_together_and_denied_attempts_log_only(store):
    store.put_new(finding("F-0001"))
    store.commit(finding("F-0001", "CHECKED"), entry("F-0001", "check"))
    store.commit(None, entry("F-0001", "approve", allowed=False))
    assert store.get("F-0001")["state"] == "CHECKED"
    log = store.log()
    assert [(e["action"], e["allowed"]) for e in log] == [("check", True), ("approve", False)], "oldest first, denied kept"
    assert len(store.log("F-0001")) == 2 and store.log("F-OTHER") == []


def test_agent_feed_has_a_cursor(store):
    store.agent_event({"at": "t", "event": "spawn", "agent_id": "a1"})
    store.agent_event({"at": "t", "event": "done", "agent_id": "a1"})
    ev = store.agents()
    assert [e["seq"] for e in ev] == [0, 1] and ev[1]["event"] == "done"
    assert store.agents(since=1) == ev[1:]


def test_reset_wipes_everything(store):
    store.put_new(finding("F-0001"))
    store.commit(finding("F-0001", "CHECKED"), entry("F-0001", "check"))
    store.agent_event({"at": "t", "event": "spawn", "agent_id": "a1"})
    store.reset()
    assert store.all() == [] and store.log() == [] and store.agents() == []


def test_token_is_sent_as_bearer(store):
    if not isinstance(FakeSpacetime.calls, list) or not FakeSpacetime.calls or store.url.startswith("http://127.0.0.1:3000"):
        pytest.skip("fake server only")
    store.reset()
    assert FakeSpacetime.calls[-1][1] == "Bearer tok"


def test_get_store_picks_spacetime(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOCK_STORE", "spacetime")
    monkeypatch.setenv("SPACETIME_STATE_DIR", str(tmp_path))
    s = get_store()
    assert isinstance(s, SpacetimeStore) and s.database == "flock" and s.dir == tmp_path


def test_unreachable_server_says_how_to_fix_it(tmp_path):
    s = SpacetimeStore(url="http://127.0.0.1:9", state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="spacetime start"):
        s.all()


def test_gatekeeper_runs_on_spacetime(store):
    """The one door still works: a legal move lands with its log entry, an illegal one is logged and refused."""
    from flock.gatekeeper import Denied, transition
    from flock.models import Actor

    store.put_new(finding("F-0001"))
    after = transition(store, "F-0001", "check", Actor("checkers", "agent"), reason="computed in code")
    assert after["state"] == "CHECKED" and store.get("F-0001")["state"] == "CHECKED"
    with pytest.raises(Denied):
        transition(store, "F-0001", "approve", Actor("panel", "agent"), reason="an agent may not approve")
    assert [e["allowed"] for e in store.log("F-0001")] == [True, False]
