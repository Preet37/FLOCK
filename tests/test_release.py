"""Release QA: end-to-end scenarios the judges will hit, run the way a person runs them.

Every scenario works in a throwaway copy of the repo (code + committed data) under pytest's tmp dir and runs the real
commands there as subprocesses, so nothing here can touch the committed data/answer_key.csv, mappings.json, rules.json
or data/state. The in-process scenarios use temp stores and temp copies of mappings.json the same way.

  pytest tests/test_release.py                      # fast scenarios (a, c, d, e, f, g + regression tests)
  FLOCK_SLOW=1 pytest tests/test_release.py         # also the slow ones (b: live seed demos, h: year-ish scale)
  FLOCK_SLOW=1 pytest -m slow                       # only the slow ones

Scenarios:
  a  Quickstart: week 1 -> human approves R-001 -> week 2 (exact Quickstart output lines)
  b  flock.demo on the default profile and on ca_bagel_chain with --sources email,slack,notes; state restored after
  c  FLOCK_AIRGAP=1 with cloud providers configured: 0 calls left this network, proven by a socket guard
  d  pipeline --reset twice and --forget twice are idempotent
  e  journal entries balance to the cent and every line traces to a source row or a finding
  f  every FILED finding was approved by a human first; every agent attempt to approve / file is denied and logged
  g  no money field comes from a model: offline run has no model votes; a hostile mocked model can't move a dollar
  h  year-ish scale (12 stores x 8 weeks, and 56 x 8 through the parallel split path) within a time budget, all traps
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT
from flock import llm, pipeline
from flock.agents import dispute_writer
from flock.gatekeeper import GATEKEEPER, Denied, transition
from flock.models import Actor
from flock.store import STATE_DIR, JsonStore

PY = sys.executable
COMMITTED = ("data/answer_key.csv", "mappings.json", "chart_of_accounts.json", "fixtures/fake_findings.json")
SCRUB = ("FLOCK_MODEL", "FLOCK_PROVIDER", "FLOCK_JUDGE_PROVIDERS", "FLOCK_JUDGE_MODELS", "FLOCK_REVIEWER_PROVIDER",
         "FLOCK_WRITER_PROVIDER", "FLOCK_MAPPER_PROVIDER", "FLOCK_READER_PROVIDER", "FLOCK_REVIEWER_MODEL",
         "FLOCK_WRITER_MODEL", "FLOCK_MAPPER_MODEL", "FLOCK_READER_MODEL", "FLOCK_AUDIT_RATE", "GROQ_API_KEY",
         "NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "FLOCK_LOCAL_URL", "FLOCK_LOCAL_MODEL",
         "FLOCK_LOCAL_API_KEY", "FLOCK_AIRGAP", "CREAO_API_KEY", "CREAO_APP_KEY", "FLOCK_CLICKY_MODE",
         "FLOCK_PORTAL_LAYOUT", "FLOCK_STORE", "FLOCK_ATTENTION_BUDGET", "FLOCK_OFFLINE")
SCORE_LINE = re.compile(r"caught (\d+)/(\d+) traps \| missed (\d+) \| false alarms (\d+)")


# --- helpers ---------------------------------------------------------------------------------------------------

def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


@pytest.fixture(scope="session", autouse=True)
def committed_files_untouched():
    """The whole module must leave the repo's committed data (and its state / rules) exactly as it found them."""
    watched = [ROOT / p for p in COMMITTED] + [ROOT / "rules.json", STATE_DIR / "runs.jsonl",
                                               STATE_DIR / "findings.json", STATE_DIR / "audit_log.jsonl"]
    before = {p: _sha(p) for p in watched}
    yield
    after = {p: _sha(p) for p in watched}
    changed = [str(p.relative_to(ROOT)) for p in watched if before[p] != after[p]]
    assert not changed, f"release tests touched repo files: {changed}"


def make_sandbox(dst: Path) -> Path:
    """A copy of the repo's code and committed data, without generated data, state, the venv or git."""
    skip = {".venv", ".git", ".claude", "__pycache__", ".pytest_cache", "results"}
    skip_data = {"state", "sandbox_portal", "benchmarks", "training", "exports", "demos", "workflows", "inbox"}
    emptied = {ROOT / "data" / "raw", ROOT / "data" / "std", ROOT / "generator" / "out"}

    def ignore(d, names):
        d = Path(d)
        out = {n for n in names if n in skip}
        if d == ROOT / "data":
            out |= {n for n in names if n in skip_data}
        if d in emptied:
            out |= {n for n in names if n != ".gitkeep"}
        return out

    shutil.copytree(ROOT, dst, ignore=ignore)
    return dst


def clean_env(**extra: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB and not k.startswith(("FLOCK_CREAO", "PYTEST_"))}
    env.update(FLOCK_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    env.pop("PYTHONPATH", None)
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def sh(sb: Path, *args: str, env: dict | None = None, timeout: float = 300, ok: bool = True,
       stdin: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run([PY, *args], cwd=sb, env=env or clean_env(), capture_output=True, text=True,
                       timeout=timeout, input=stdin)
    if ok:
        assert r.returncode == 0, f"{' '.join(args)} exited {r.returncode}\nstdout:\n{r.stdout[-3000:]}\nstderr:\n{r.stderr[-3000:]}"
    return r


def scoreline(text: str) -> tuple[int, int, int, int]:
    m = SCORE_LINE.findall(text)
    assert m, f"no scoreboard line in:\n{text[-2000:]}"
    return tuple(int(x) for x in m[-1])


def generate_week(sb: Path, seed: int = 42, stores: int = 3, extra: tuple[str, ...] = ()) -> None:
    sh(sb, "generator/generate.py", "--seed", str(seed), "--stores", str(stores), "--weeks", "2", *extra)
    sh(sb, "-m", "flock.agents.collector")


def state(sb: Path) -> JsonStore:
    return JsonStore(sb / "data" / "state")


def jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


@pytest.fixture
def sandbox(tmp_path) -> Path:
    return make_sandbox(tmp_path / "flock")


@pytest.fixture(scope="module")
def real_week(tmp_path_factory) -> Path:
    """A sandbox with a generated 3-store x 2-week dataset, run once like the demo: reset, forget, demo human."""
    sb = make_sandbox(tmp_path_factory.mktemp("real") / "flock")
    generate_week(sb)
    r = sh(sb, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve", "--confirm")
    (sb / "pipeline_out.txt").write_text(r.stdout)
    return sb


# --- a: Quickstart ---------------------------------------------------------------------------------------------

def test_a_quickstart_week1_approve_week2(sandbox):
    w1 = sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve").stdout
    assert "== Flock run on fixtures/fake_findings.json: 6 findings ==" in w1
    assert "Clicky filed 3 disputes in the sandbox portal" in w1
    assert "correct: 1 | recoverable: 3 ($17.90) | sent to a human: 2" in w1
    assert "audit log: 28 entries (0 denied)" in w1
    assert "R-001: ubereats promo backed by contract, unauth_email: accept, do not dispute (evidence: 2)" in w1
    assert "air-gapped" not in w1 and "model calls" not in w1  # offline Quickstart output has no air-gap line

    s1 = sh(sandbox, "scripts/score.py").stdout
    assert "caught 6/6 traps | missed 0 | false alarms 0" in s1
    assert "recoverable dollars found: $17.90 | sent to a human: 2 | settled by learned rules: 0" in s1
    assert "human hand-offs by run: Week 1: 2" in s1

    assert not (sandbox / "rules.json").exists()
    ap = sh(sandbox, "-m", "flock.agents.learner", "--approve", "R-001").stdout
    assert '"rule_id": "R-001"' in ap and (sandbox / "rules.json").exists()

    w2 = sh(sandbox, "-m", "flock.pipeline", "--reset", "--demo-approve").stdout
    assert "correct: 3 | recoverable: 3 ($17.90) | sent to a human: 0" in w2
    s2 = sh(sandbox, "scripts/score.py").stdout
    assert "caught 6/6 traps | missed 0 | false alarms 0" in s2
    assert "sent to a human: 0 | settled by learned rules: 2" in s2
    assert "human hand-offs by run: Week 1: 2 | Week 2: 0" in s2
    runs = jsonl(sandbox / "data" / "state" / "runs.jsonl")
    assert [r["learned_rules"] for r in runs] == [0, 1]
    for p in COMMITTED:
        assert _sha(sandbox / p) == _sha(ROOT / p), f"Quickstart changed {p}"


# --- b: live seed demo ------------------------------------------------------------------------------------------

def _state_hashes(sb: Path) -> dict[str, str]:
    d = sb / "data" / "state"
    return {p.name: _sha(p) for p in sorted(d.iterdir()) if p.is_file() and p.name != "progress.json"}


@pytest.mark.slow
@pytest.mark.parametrize("args", [
    ("--seed", "4821"),
    ("--seed", "4821", "--profile", "ca_bagel_chain", "--stores", "4", "--weeks", "2",
     "--sources", "email,slack,notes"),
    ("--seed", "77", "--sources", "email,slack,notes"),
], ids=["default", "bagel-email-slack-notes", "default-email-slack-notes"])
def test_b_live_seed_demo_catches_everything_and_restores_state(sandbox, args):
    sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve")  # the fixtures demo is on screen
    before = _state_hashes(sandbox)
    portal = sandbox / "data" / "sandbox_portal" / "disputes.jsonl"
    portal_before = _sha(portal)
    t0 = time.monotonic()
    out = sh(sandbox, "-m", "flock.demo", "--confirm", *args, timeout=600).stdout
    took = time.monotonic() - t0
    caught, total, missed, alarms = scoreline(out)
    assert (caught, missed, alarms) == (total, 0, 0) and total > 0, out[-3000:]
    assert took < 120, f"demo took {took:.0f}s"
    assert "restored the previous demo state" in out
    assert _state_hashes(sandbox) == before and _sha(portal) == portal_before
    for p in COMMITTED:
        assert _sha(sandbox / p) == _sha(ROOT / p), f"demo changed {p}"


# --- c: air gap -------------------------------------------------------------------------------------------------

GUARD = r'''
import os, socket, ipaddress
_LOG = os.environ["FLOCK_NET_GUARD_LOG"]
def _local(host):
    host = str(host or "").strip("[]")
    if host in ("localhost", ""):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local
def _deny(host, what):
    if _local(host):
        return
    with open(_LOG, "a") as fh:
        fh.write(f"{what} {host}\n")
    raise OSError(f"network guard: {what} {host} refused (test)")
_connect, _connect_ex, _gai = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
def connect(self, addr):
    if isinstance(addr, tuple):
        _deny(addr[0], "connect")
    return _connect(self, addr)
def connect_ex(self, addr):
    if isinstance(addr, tuple):
        _deny(addr[0], "connect")
    return _connect_ex(self, addr)
def getaddrinfo(host, *a, **k):
    _deny(host if isinstance(host, str) else (host or b"").decode(), "resolve")
    return _gai(host, *a, **k)
socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def guarded(tmp_path):
    """Env pieces for a subprocess whose every non-local connect / DNS lookup is refused and written to a log."""
    d = tmp_path / "guard"
    d.mkdir()
    (d / "sitecustomize.py").write_text(GUARD)
    log = tmp_path / "net_guard.log"
    log.write_text("")
    return {"PYTHONPATH": str(d), "FLOCK_NET_GUARD_LOG": str(log)}, log


CLOUD = {"FLOCK_OFFLINE": None, "FLOCK_AIRGAP": "1", "FLOCK_PROVIDER": "groq",
         "FLOCK_JUDGE_PROVIDERS": "groq,nvidia,anthropic", "FLOCK_REVIEWER_PROVIDER": "anthropic",
         "FLOCK_WRITER_PROVIDER": "creao", "FLOCK_MAPPER_PROVIDER": "groq", "FLOCK_READER_PROVIDER": "nvidia",
         "FLOCK_AUDIT_RATE": "1", "GROQ_API_KEY": "gsk-release-test", "NVIDIA_API_KEY": "nv-release-test",
         "ANTHROPIC_API_KEY": "sk-ant-release-test", "CREAO_API_KEY": "cr-release-test",
         "FLOCK_CREAO_AGENT": "agent-release-test"}


def test_c_guard_catches_a_real_outbound_call(sandbox, guarded):
    """Control: the guard really does see (and stop) a call off this network."""
    genv, log = guarded
    r = sh(sandbox, "-c", "import urllib.request; urllib.request.urlopen('https://api.groq.com/openai/v1', timeout=3)",
           env=clean_env(**genv), ok=False)
    assert r.returncode != 0 and "resolve api.groq.com" in log.read_text()


def test_c_airgap_with_cloud_providers_configured_sends_nothing_out(sandbox, guarded):
    genv, log = guarded
    generate_week(sandbox, seed=5, stores=2)
    out = sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve", "--confirm",
             env=clean_env(**CLOUD, **genv)).stdout
    assert "air-gapped: 0 model calls, 0 left this network; kept offline:" in out
    rep = json.loads((sandbox / "data" / "state" / "airgap.json").read_text())
    assert rep["airgap"] is True and rep["calls"] == 0 and rep["external_calls"] == 0
    assert {"anthropic", "groq", "nvidia", "creao"} <= {k.split("/")[0] for k in rep["refused"]}
    assert all("off this network" in why for why in rep["refused"].values())
    assert log.read_text() == "", f"calls left the machine: {log.read_text()}"
    for f in state(sandbox).all():  # every vote came from offline rules, and says nothing about a model
        assert all("model" not in v and "fallback" not in v for v in f["votes"])


def test_c_airgap_local_judges_are_called_and_counted_cloud_reviewer_is_not(sandbox, guarded):
    genv, log = guarded
    port = _free_port()  # nothing listens: local calls are refused instantly, judges fall back and say so
    env = clean_env(**{**CLOUD, **genv, "FLOCK_PROVIDER": "local", "FLOCK_JUDGE_PROVIDERS": "local",
                       "FLOCK_WRITER_PROVIDER": "local", "FLOCK_LOCAL_URL": f"http://127.0.0.1:{port}/v1",
                       "FLOCK_LLM_TIMEOUT": "3"})
    out = sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve", env=env).stdout
    rep = json.loads((sandbox / "data" / "state" / "airgap.json").read_text())
    assert rep["calls"] > 0 and rep["by_host"] == {"127.0.0.1": rep["calls"]} and rep["external_calls"] == 0
    assert f"air-gapped: {rep['calls']} model calls, 0 left this network" in out
    assert any(k.startswith("anthropic") for k in rep["refused"])
    assert log.read_text() == ""


# --- d: idempotency ---------------------------------------------------------------------------------------------

def _picture(sb: Path) -> dict:
    """What a run produced, keyed by content: real-data finding ids are random per run (see docs/QA_REPORT.md)."""
    st = state(sb)
    key = {f["finding_id"]: f"{f['channel']}|{f['check_type']}|{','.join(f['order_ids'])}" for f in st.all()}
    fs = {key[f["finding_id"]]: (f["expected"], f["actual"], f["gap"], f["outcome"], f["state"]) for f in st.all()}
    assert len(fs) == len(key), "two findings share channel + check + orders"
    portal = sorted(({**{k: v for k, v in d.items() if k not in ("portal_ref", "at", "merchant_ref", "message")},
                      "finding": key[d["merchant_ref"]]}
                     for d in jsonl(sb / "data" / "sandbox_portal" / "disputes.jsonl")), key=lambda d: d["finding"])
    log = sorted((e["actor"], e["action"], key.get(e["subject_id"], e["subject_id"]), e["allowed"], e["before"],
                  e["after"]) for e in st.log())
    return {"findings": fs, "portal": portal, "log": log}


@pytest.mark.parametrize("data", ["fixtures", "real"])
def test_d_reset_and_forget_are_idempotent(sandbox, data):
    extra = ()
    if data == "real":
        generate_week(sandbox, seed=11, stores=2)
        extra = ("--confirm",)
    run = lambda *a: sh(sandbox, "-m", "flock.pipeline", "--reset", "--demo-approve", *a, *extra)
    sd = sandbox / "data" / "state"
    (sandbox / "rules.json").write_text(json.dumps([{"rule_id": "R-999", "approved_by": "x", "channel": "none",
                                                     "check_type": "none", "authority": [], "outcome": "correct"}]))

    run("--forget")
    first = _picture(sandbox)
    run("--forget")
    assert _picture(sandbox) == first, "--reset --forget twice gave different results"
    assert not (sandbox / "rules.json").exists()
    assert len(jsonl(sd / "runs.jsonl")) == 1  # --forget starts the week history over

    run()
    second = _picture(sandbox)
    run()
    third = _picture(sandbox)
    assert set(second["findings"]) == set(third["findings"]) == set(first["findings"])
    gaps = lambda p: {k: v[:3] for k, v in p["findings"].items()}
    assert gaps(second) == gaps(third) == gaps(first), "money moved between identical runs"
    assert len(third["portal"]) == len(second["portal"])  # the portal is wiped by --reset, never filed twice
    assert len(jsonl(sd / "runs.jsonl")) == 3


# --- e: journal -------------------------------------------------------------------------------------------------

def _rows(path: Path) -> int | None:
    if path.suffix != ".csv":
        return None
    with path.open(newline="") as fh:
        return sum(1 for _ in csv.reader(fh))


def test_e_journal_entries_balance_and_every_line_traces_back(real_week):
    entries = json.loads((real_week / "data" / "state" / "journal_entries.json").read_text())
    assert entries, "a real-data week should draft journal entries"
    fids = {f["finding_id"] for f in state(real_week).all()}
    sizes: dict[str, int | None] = {}
    for je in entries.values():
        dr = sum(round(ln["debit"] * 100) for ln in je["lines"])
        cr = sum(round(ln["credit"] * 100) for ln in je["lines"])
        assert dr == cr and dr > 0 and je["balanced"], je["je_id"]
        assert round(je["total_debit"] * 100) == dr and round(je["total_credit"] * 100) == cr
        assert je["state"] == "DRAFT"  # waiting for a human, never auto-approved
        for ln in je["lines"]:
            assert (ln["debit"] == 0) != (ln["credit"] == 0) or ln["debit"] == ln["credit"] == 0
            assert ln["sources"] or ln["finding_id"], f"{je['je_id']} line {ln['line_no']} traces to nothing"
            if ln["finding_id"]:
                assert ln["finding_id"] in fids and ln["finding_id"] in je["finding_ids"]
            for src in ln["sources"]:
                p = real_week / src["source_file"].split("#")[0]  # "book.xlsx#Detail" names a sheet
                assert p.exists(), src["source_file"]
                if p not in sizes:
                    sizes[p] = _rows(p)
                if sizes[p] is not None and src["rows"]:
                    assert 1 <= min(src["rows"]) and max(src["rows"]) <= sizes[p], src["source_file"]
    log = [e for e in state(real_week).log() if e.get("subject_type") == "journal_entry"]
    assert {e["action"] for e in log} == {"draft"} and all(e["role"] == "agent" for e in log)


# --- f: humans approve, the gatekeeper files, agents are refused --------------------------------------------------

AGENT_IDS = ("panel", "clicky", "dispute_writer", "learner", "checkers", "investigator")


def _audit_invariants(st: JsonStore) -> None:
    log = st.log()
    last_state: dict[str, str] = {}
    for i, e in enumerate(log):
        if e.get("subject_type", "finding") != "finding":
            continue
        if e["allowed"]:
            if e["action"] in ("approve", "reject"):
                assert e["role"] == "human", e
            if e["action"] == "file":
                assert e["role"] == "gatekeeper", e
                approvals = [x for x in log[:i] if x["subject_id"] == e["subject_id"] and x["allowed"]
                             and x["action"] == "approve" and x["role"] == "human"]
                assert approvals, f"{e['subject_id']} filed with no human approval before it"
            if e["subject_id"] in last_state and e["before"] != e["after"]:
                assert e["before"] == last_state[e["subject_id"]], f"state jumped: {e}"
            last_state[e["subject_id"]] = e["after"]
        else:
            assert e["reason"].startswith("DENIED"), e
            assert e["diff"] == {} and e["before"] == e["after"], e
        if e["role"] == "agent" and e["action"] in ("approve", "reject", "file", "recover"):
            assert not e["allowed"], f"an agent got through: {e}"
    for f in st.all():
        if f["state"] in ("FILED", "RECOVERED"):
            moves = [(e["action"], e["role"]) for e in st.log(f["finding_id"]) if e["allowed"]]
            assert moves.index(("approve", "human")) < moves.index(("file", "gatekeeper"))


def test_f_agents_cannot_approve_or_file_and_every_attempt_is_logged(real_week):
    st = state(real_week)
    _audit_invariants(st)
    filed = [f for f in st.all() if f["state"] == "FILED"]
    judged = [f for f in st.all() if f["state"] == "JUDGED"]
    assert filed and judged, "the demo week should leave filed disputes and items waiting"
    denied_before = sum(1 for e in st.log() if not e["allowed"])

    attempts = 0
    for f in judged[:5] + filed[:5]:
        for aid in AGENT_IDS:
            for action in ("approve", "reject", "file", "recover"):
                if action == "recover" and f["state"] == "FILED":
                    continue  # recover from FILED is legal for a human or the gatekeeper, not an agent: tried below
                with pytest.raises(Denied):
                    transition(st, f["finding_id"], action, Actor(aid, "agent"), reason="release QA probe")
                attempts += 1
        with pytest.raises(Denied):
            transition(st, f["finding_id"], "recover", Actor("clicky", "agent"), reason="release QA probe")
        attempts += 1
    # a human can't file (only the gatekeeper), and the gatekeeper can't approve (only a human)
    with pytest.raises(Denied):
        transition(st, judged[0]["finding_id"], "approve", GATEKEEPER, reason="release QA probe")
    with pytest.raises(Denied):
        transition(st, judged[0]["finding_id"], "file", Actor("preet", "human"), reason="release QA probe")
    attempts += 2
    # nor can anyone smuggle a state change or a money change through an annotation patch
    with pytest.raises(Denied):
        transition(st, judged[0]["finding_id"], "investigate", Actor("investigator", "agent"),
                   patch={"case_file": {}, "gap": 0.0}, reason="release QA probe")
    attempts += 1
    with pytest.raises(Denied):
        transition(st, judged[0]["finding_id"], "investigate", Actor("investigator", "agent"),
                   patch={"case_file": {"summary": "probe"}, "state": "APPROVED"}, reason="release QA probe")
    attempts += 1

    log = st.log()
    assert sum(1 for e in log if not e["allowed"]) == denied_before + attempts
    probes = [e for e in log if e["reason"].endswith("release QA probe") and not e["allowed"]]
    assert len(probes) == attempts
    for f in judged[:5] + filed[:5]:
        assert st.get(f["finding_id"])["state"] == f["state"]  # nothing moved
    _audit_invariants(st)


def test_f_quickstart_log_has_no_agent_approvals(sandbox):
    sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve")
    _audit_invariants(state(sandbox))


# --- g: no money from a model -------------------------------------------------------------------------------------

def test_g_offline_run_has_no_model_votes_or_fallbacks(real_week):
    rep = json.loads((real_week / "data" / "state" / "airgap.json").read_text())
    assert rep["calls"] == 0
    for f in state(real_week).all():
        for v in f["votes"]:
            assert "model" not in v and "fallback" not in v
        assert "offline fallback" not in f["reason"] and "API error" not in f["reason"]
        assert f["expected"] == round(f["expected"], 2) and f["gap"] == round(f["gap"], 2)
        if f["check_type"] != "deposit":
            assert round((f["actual"] - f["expected"]) * 100) in (round(f["gap"] * 100), -round(f["gap"] * 100))


HOSTILE_VOTE = {"outcome": "recoverable", "confidence": 0.99, "reason": "Refund $99,999.00 now.",
                "gap": 99999.0, "expected": 0.0, "actual": 99999.0, "amount": 99999.0, "state": "FILED",
                "outcome_override": "recoverable"}


class HostileModel:
    """A local model that answers every judge with made-up money fields and drafts disputes with a fake amount."""

    def __init__(self):
        self.calls = 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        body = json.loads(req.data)
        system = body["messages"][0]["content"]
        if "JSON" in system:
            content = json.dumps(HOSTILE_VOTE)
        else:
            content = "Dear support, please refund $99,999.00 for this order. Thanks."
        return io.BytesIO(json.dumps({"model": "hostile-1", "choices": [{"message": {"content": content}}]}).encode())


def _hostile_env(monkeypatch):
    monkeypatch.delenv("FLOCK_OFFLINE", raising=False)
    monkeypatch.setenv("FLOCK_PROVIDER", "local")
    monkeypatch.setenv("FLOCK_REVIEWER_PROVIDER", "local")
    monkeypatch.setenv("FLOCK_AUDIT_RATE", "1")
    model = HostileModel()
    monkeypatch.setattr(urllib.request, "urlopen", model)
    return model


def test_g_hostile_model_cannot_move_a_dollar_on_the_fixtures(tmp_path, monkeypatch):
    model = _hostile_env(monkeypatch)
    portal = tmp_path / "portal.jsonl"
    store = pipeline.run(reset=True, forget=True, demo_approve=True, store=JsonStore(tmp_path / "s"),
                         rules_path=tmp_path / "rules.json", portal=portal)
    assert model.calls > 0
    fixtures = {f["finding_id"]: f for f in json.loads((ROOT / "fixtures" / "fake_findings.json").read_text())}
    for f in store.all():
        src = fixtures[f["finding_id"]]
        assert (f["expected"], f["actual"], f["gap"]) == (src["expected"], src["actual"], src["gap"])
        assert any(v.get("model") for v in f["votes"])  # the model really voted
        for v in f["votes"]:
            assert not {"gap", "expected", "actual", "amount", "state"} & set(v), v
        assert "99,999" not in (f.get("dispute_draft") or "")
    filings = jsonl(portal)
    assert filings
    for d in filings:
        assert d["amount"] == abs(fixtures[d["merchant_ref"]]["gap"]) and "99,999" not in d["message"]
    # money fields only ever change through code: no allowed log entry diff touches them after 'check'
    for e in store.log():
        assert not {"expected", "actual", "gap"} & set(e.get("diff", {})), e


def test_g_hostile_model_cannot_move_a_dollar_on_real_data(real_week, tmp_path, monkeypatch):
    """Same generated week as the offline run; a hostile model judges and drafts. Every finding's money and every
    journal entry's totals must match the offline run exactly."""
    mappings = tmp_path / "mappings.json"
    shutil.copy(real_week / "mappings.json", mappings)  # the offline run confirmed the unseen layout into this copy
    std = tmp_path / "std"
    model = _hostile_env(monkeypatch)
    store = pipeline.run(reset=True, forget=True, demo_approve=True, store=JsonStore(tmp_path / "s"),
                         rules_path=tmp_path / "rules.json", portal=tmp_path / "portal.jsonl",
                         raw=real_week / "data" / "raw", std=std, mappings=mappings, confirm=True)
    assert model.calls > 0
    key = lambda f: (tuple(f["order_ids"]), f["check_type"], f["channel"])
    offline = {key(f): (f["expected"], f["actual"], f["gap"]) for f in state(real_week).all()}
    hostile = {key(f): (f["expected"], f["actual"], f["gap"]) for f in store.all()}
    assert hostile == offline
    for d in jsonl(tmp_path / "portal.jsonl"):
        assert "99,999" not in d["message"]
    off_je = json.loads((real_week / "data" / "state" / "journal_entries.json").read_text())
    hos_je = json.loads((tmp_path / "s" / "journal_entries.json").read_text())
    assert set(off_je) == set(hos_je)
    for je_id, je in hos_je.items():
        assert je["balanced"]
        assert (je["total_debit"], je["total_credit"]) == (off_je[je_id]["total_debit"], off_je[je_id]["total_credit"])


# --- h: year-ish scale --------------------------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("args,budget", [
    (("--seed", "8", "--weeks", "8"), 90),                                                 # 12 stores x 8 weeks
    (("--seed", "8", "--profile", "ca_bagel_chain", "--weeks", "8", "--sources", "json"), 240),  # 56 x 8, split path
], ids=["default-12x8", "bagel-56x8-split"])
def test_h_year_ish_scale_within_budget_catches_all_traps(sandbox, args, budget):
    t0 = time.monotonic()
    out = sh(sandbox, "-m", "flock.demo", "--confirm", *args, timeout=budget * 3).stdout
    took = time.monotonic() - t0
    caught, total, missed, alarms = scoreline(out)
    assert (caught, missed, alarms) == (total, 0, 0) and total > 50, out[-3000:]
    assert took < budget, f"{' '.join(args)} took {took:.0f}s (budget {budget}s)"
    print(f"\n[release timing] flock.demo {' '.join(args)}: {took:.1f}s, {total} traps")


# --- what the API / dashboard serve must be strict JSON -------------------------------------------------------------

def _strict(text: str, where: str):
    def no_nan(c):
        raise AssertionError(f"{where}: non-standard JSON constant {c} (JSON.parse and strict parsers reject it)")
    return json.loads(text, parse_constant=no_nan)


def test_state_files_and_api_payloads_are_strict_json(real_week):
    """No NaN / Infinity anywhere (Python writes them by default; browsers' JSON.parse rejects them) and no raw
    control characters inside strings (the API escapes them; json.dumps does)."""
    sd = real_week / "data" / "state"
    for name in ("findings.json", "journal_entries.json", "airgap.json", "proposed_rules.json"):
        _strict((sd / name).read_text(), name)
    for name in ("audit_log.jsonl", "runs.jsonl", "market_ledger.jsonl", "agent_status.jsonl"):
        for i, line in enumerate((sd / name).read_text().splitlines()):
            _strict(line, f"{name}:{i + 1}")
    for line in (real_week / "data" / "sandbox_portal" / "disputes.jsonl").read_text().splitlines():
        _strict(line, "disputes.jsonl")
    # the exact bytes flock.api sends for the read routes (json.dumps of the same objects)
    st = state(real_week)
    for where, payload in (("/api/findings", sorted(st.all(), key=lambda f: f["finding_id"])), ("/api/log", st.log())):
        body = json.dumps(payload)
        assert not [c for c in body if ord(c) < 0x20], f"{where}: raw control character in the body"
        _strict(body, where)
        assert json.loads(body, strict=True) == payload


def test_portal_page_survives_a_half_written_filing(tmp_path):
    """Clicky appends to disputes.jsonl from another process; the portal's 'My disputes' page read the file while a
    line was half written and crashed with JSONDecodeError. A partial last line is now left for the next read."""
    from flock import portal

    p = tmp_path / "disputes.jsonl"
    portal.append_filing(p, {"portal_ref": "SBX-1", "at": "2026-10-25T00:00:00", "amount": 2.5})
    with p.open("a") as fh:
        fh.write('{"portal_ref": "SBX-2", "amou')
    assert [d["portal_ref"] for d in portal.read_filings(p)] == ["SBX-1"]
    with p.open("a") as fh:
        fh.write('nt": 3.0}\n')
    assert [d["portal_ref"] for d in portal.read_filings(p)] == ["SBX-1", "SBX-2"]


# --- regression tests for bugs fixed during release QA -------------------------------------------------------------

def test_fix_temp_store_run_never_writes_the_repo_run_history(tmp_path):
    """pipeline.run(store=<temp>) used to append to data/state/runs.jsonl (the repo's week history) when no
    runs_path was given; it also counted the repo's rules.json in learned_rules instead of the run's rules."""
    repo_runs = STATE_DIR / "runs.jsonl"
    before = _sha(repo_runs)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"rule_id": "R-1", "approved_by": "t", "channel": "x", "check_type": "y",
                                  "authority": [], "outcome": "correct"}]))
    store = pipeline.run(reset=True, store=JsonStore(tmp_path / "s"), rules_path=rules,
                         portal=tmp_path / "p.jsonl")
    assert _sha(repo_runs) == before
    [run] = jsonl(tmp_path / "s" / "runs.jsonl")
    assert run["learned_rules"] == 1
    assert pipeline.runs_default(store) == tmp_path / "s" / "runs.jsonl"
    assert pipeline.runs_default(JsonStore(STATE_DIR)) == pipeline.RUNS


def test_fix_score_reads_the_history_next_to_the_store(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("score_rel", ROOT / "scripts" / "score.py")
    score_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(score_mod)
    store = pipeline.run(reset=True, forget=True, store=JsonStore(tmp_path / "s"), rules_path=tmp_path / "r.json",
                         portal=tmp_path / "p.jsonl")
    s = score_mod.score(store, verbose=False)
    assert [r["label"] for r in s["runs"]] == ["Week 1"]


def test_fix_stage_clis_fail_with_a_sentence_not_a_traceback(sandbox):
    """`learner --approve R-999` (typo) and `market --recover` on a finding that isn't FILED printed tracebacks."""
    sh(sandbox, "-m", "flock.pipeline", "--reset", "--forget", "--demo-approve")
    r = sh(sandbox, "-m", "flock.agents.learner", "--approve", "R-999", ok=False)
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "no proposed rule R-999; proposed: ['R-001']" in r.stderr
    assert "F-0001 RECOVERED" in sh(sandbox, "-m", "flock.market", "--recover", "F-0001").stdout
    for fid, why in (("F-0001", "in state RECOVERED"), ("F-0005", "in state JUDGED"), ("F-9999", "no finding F-9999")):
        r = sh(sandbox, "-m", "flock.market", "--recover", fid, ok=False)
        assert r.returncode == 1 and "Traceback" not in r.stderr and why in r.stderr, r.stderr
    denied = [e for e in state(sandbox).log() if not e["allowed"] and e["action"] == "recover"]
    assert [e["subject_id"] for e in denied] == ["F-0001", "F-0005"]  # still logged by the gatekeeper


def test_fix_model_draft_with_a_made_up_amount_falls_back_to_the_template(monkeypatch):
    from conftest import fake

    f = fake("F-0001")
    monkeypatch.setattr(llm, "ask_text", lambda *a, **k: "Please refund $250.00 for order ORD-1001 "
                        "(data/raw/doordash/payouts_wk1.csv, data/raw/contracts/doordash_msa.pdf).")
    text = dispute_writer.draft(f)
    assert "$250.00" not in text and "$2.50" in text and text.startswith("Hello DoorDash")
    assert dispute_writer.foreign_amounts("refund $250.00", f) == ["250.00"]


def test_fix_model_draft_with_only_known_amounts_is_kept(monkeypatch):
    from conftest import fake

    f = fake("F-0001")
    reply = ("We were charged $6.50 commission on a $20.00 order; the contract says 20%, so $4.00. "
             "Please credit $2.50. Sources: data/raw/doordash/payouts_wk1.csv, data/raw/contracts/doordash_msa.pdf")
    monkeypatch.setattr(llm, "ask_text", lambda *a, **k: reply)
    assert dispute_writer.draft(f) == reply
    assert dispute_writer.foreign_amounts(reply, f) == []


# --- fixes from the docs agent's review -----------------------------------------------------------------------------

def _proposal(tmp_path, rid="R-001", channel="ubereats"):
    store = JsonStore(tmp_path / "s")
    (tmp_path / "s" / "proposed_rules.json").write_text(json.dumps([{
        "rule_id": rid, "channel": channel, "check_type": "promo", "authority": ["contract", "unauth_email"],
        "outcome": "correct", "evidence_count": 2, "precedents": ["F-1", "F-2"], "description": "d"}]))
    return store


def test_fix_approved_rule_keeps_its_proposed_id_and_logs_as_a_rule(tmp_path):
    from flock.agents import learner

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"rule_id": "R-001", "channel": "x", "check_type": "y", "authority": [],
                                  "outcome": "correct", "approved_by": "a"}]))
    store = _proposal(tmp_path, rid="R-005")
    got = learner.approve("R-005", Actor("preet", "human"), store=store, rules_path=rules)
    assert got["rule_id"] == "R-005" and "proposed_as" not in got  # used to become R-002 (count of rules.json + 1)
    [entry] = store.log()
    assert entry["subject_type"] == "rule" and entry["subject_id"] == "R-005" and entry["action"] == "approve_rule"
    # a later week proposes R-001 again for a different rule: R-001 is taken, so it is renumbered and says so
    store = _proposal(tmp_path, rid="R-001", channel="doordash")
    got = learner.approve("R-001", Actor("preet", "human"), store=store, rules_path=rules)
    assert got["rule_id"] == "R-003" and got["proposed_as"] == "R-001"
    assert [r["rule_id"] for r in json.loads(rules.read_text())] == ["R-001", "R-005", "R-003"]


def test_fix_reader_model_claims_keep_their_read_by_tag(tmp_path):
    from flock.agents import deal_keeper

    raw = {"term_type": "commission", "value": 0.2, "quote": "20%", "read_by": "reader model", "channel": "doordash"}
    c = deal_keeper._claim(raw, {"channel": "doordash"}, "unauth_email", tmp_path / "note.md")
    assert c["read_by"] == "reader model"
    assert "read_by" not in deal_keeper._claim({**raw, "read_by": None}, {}, "contract", tmp_path / "c.json")


def test_fix_refund_money_moved_is_derived_not_always_zero():
    from flock.agents import checkers

    assert checkers.money_moved({"amount": 4.25, "status": "initiated", "settled_at": None}) == 0.0
    assert checkers.money_moved({"amount": 4.25, "status": "completed", "settled_at": "2026-03-01T10:00:00"}) == 4.25
    assert checkers.money_moved({"amount": 4.25, "money_moved": 1.5}) == 1.5
    story = {"order_id": "O-1", "channel": "direct_app", "payout_lines": [], "error_charges": [],
             "refunds": [{"refund_id": "RF-1", "amount": 4.25, "status": "initiated", "settled_at": None,
                          "source_file": "r.csv", "source_row": 2},
                         {"refund_id": "RF-2", "amount": 3.0, "status": "initiated", "settled_at": "2026-03-01",
                          "source_file": "r.csv", "source_row": 3}]}
    assert [r["refund_id"] for r in checkers._stuck_refunds(story)] == ["RF-1"]  # RF-2 settled: money moved


def test_fix_ranker_has_win_odds_for_every_check_type():
    from flock.agents import ranker
    from flock.models import CHECK_TYPES

    assert set(CHECK_TYPES) <= set(ranker.WIN_PROBABILITY)
    assert ranker.WIN_PROBABILITY["anomaly"] < ranker.WIN_PROBABILITY["fee"]


def test_fix_fee_backed_only_by_an_unauthorized_rep_is_weak_evidence():
    from flock.agents import panel

    line = {"source_file": "data/raw/doordash/doordash_payouts.csv", "row": 9, "what": "fees 1.00 charged"}
    unauth = {"check_type": "fee", "evidence": [line, {"source_file": "e.eml", "quote": "we'll waive it",
                                                       "authority": "unauth_email"}]}
    auth = {"check_type": "fee", "evidence": [line, {"source_file": "e.eml", "quote": "fee waived",
                                                     "authority": "auth_email"}]}
    assert panel.weak_evidence(unauth) and not panel.weak_evidence(auth)
