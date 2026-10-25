"""Run Flock's AI roles on CREAO (owner: Thabhelo). One hosted CREAO agent per role, created from a plain-language
description through the Developer Platform, then wired in as the `creao` provider in flock/llm.py.

  export CREAO_API_KEY=cr_sk_...            # PROJECT key from the Developer Console (developer.creao.ai), USD wallet
  python scripts/creao_setup.py --create    # creates the agents (async, polled), writes creao_agents.json
  python scripts/creao_setup.py --check     # runs each agent once with a tiny case; prints reply + credits
  python scripts/creao_setup.py --env       # prints the export lines that put every role on CREAO
  python scripts/creao_setup.py --list      # agents on the account

Roles -> agents (creao_agents.json):
  judge.strict, judge.operator, judge.skeptic   the panel's three judges
  reviewer                                     second opinions and escalation memos
  writer                                       dispute drafts
  mapper                                       column mappings for unseen report layouts
  reader                                       the Deal Keeper's document reader (PDF text, free-text emails)

The contract every agent is created with: inputs `instructions` and `request`; treat `instructions` as the complete
system prompt, answer `request`, reply with only the JSON object the instructions ask for. Flock keeps the prompts in
its own code (panel.py, dispute_writer.py, formatter.py, deal_keeper.py), so the agents stay thin and the money math
stays in Python. CREAO's write limiter allows 30 agent creates per hour; --create makes 7. Runs are paced to 28 per
minute (CREAO's limit is 30 per account), so a 42-finding week takes about five minutes of judging on CREAO.

/v2 facts this script relies on: POST /v2/agents returns 202 with a creation run_id and agent_id null; the agent id
appears in GET /v2/agents once created (we diff the list) and the run is polled at GET /v2/agents/{agent_id}/edits/{run_id}
(status queued|running|succeeded|failed|cancelled). Set FLOCK_CREAO_API=v1 for a CLI account key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flock import llm  # noqa: E402
from flock.agents import panel  # noqa: E402

CONTRACT = (
    "Inputs: `instructions` (text) and `request` (text). Treat `instructions` as your complete system prompt and follow "
    "it exactly. `request` is the case to handle, usually JSON. Reply with only the single JSON object the instructions "
    "ask for: no markdown, no prose before or after, no tool use, no web search, no files. Never compute or recompute "
    "money; every number in the request was already computed in code. If the request is unusable, reply with the JSON "
    "object {\"error\": \"<one sentence>\"}."
)

ROLES: dict[str, dict[str, str]] = {
    "judge.strict": {"name": "Flock Judge: Strict Auditor",
                     "about": "one of three judges on Flock's panel. " + panel.JUDGES["judge.strict"]},
    "judge.operator": {"name": "Flock Judge: Operator",
                       "about": "one of three judges on Flock's panel. " + panel.JUDGES["judge.operator"]},
    "judge.skeptic": {"name": "Flock Judge: Skeptic",
                      "about": "one of three judges on Flock's panel. " + panel.JUDGES["judge.skeptic"]},
    "reviewer": {"name": "Flock Reviewer",
                 "about": "Flock's reviewer: a second opinion on panel decisions and a short memo explaining a split "
                          "to the human who decides. It never breaks ties."},
    "writer": {"name": "Flock Dispute Writer",
               "about": "Flock's dispute writer: drafts the short, factual message a restaurant sends a delivery "
                        "platform to recover money, citing the evidence given. A human approves before anything is sent."},
    "mapper": {"name": "Flock Column Mapper",
               "about": "Flock's column mapper: given the header row and sample rows of an unfamiliar payout report, "
                        "proposes how its columns map onto Flock's standard table. A human confirms once."},
    "reader": {"name": "Flock Document Reader",
               "about": "Flock's document reader: reads a platform contract, amendment, rep email or call note and "
                        "returns the deal terms it states with exact quotes and the sender's authority."},
}

AGENTS_FILE = llm.CREAO_AGENTS_FILE


def describe(role: str) -> str:
    r = ROLES[role]
    return (f"Create an agent named \"{r['name']}\". Flock is a system that checks restaurant delivery payouts "
            f"(DoorDash, Uber Eats, Grubhub, the restaurant's own app) against the deal in effect and recovers money "
            f"the platform short-paid. This agent is {r['about']} {CONTRACT}")


def api(method: str, route: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = llm._creao_request(method, llm.creao_path(route), body)
    return json.loads(raw or b"{}")


def list_agent_ids() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        page = api("GET", f"agents?limit=100&offset={offset}")
        for ag in page.get("agents", []):
            out[ag["agent_id"]] = ag
        if not page.get("has_more") or not page.get("agents"):
            return out
        offset += len(page["agents"])


DONE = {"completed", "succeeded", "failed", "cancelled"}


def wait_creation(started: dict[str, Any], before: set[str], every: float = 3.0, limit: float = 600.0) -> dict[str, Any]:
    """Wait for an NL create. The 202 has agent_id null; the id shows up in GET /agents, then the run is polled at
    GET /agents/{agent_id}/edits/{run_id}. Returns the run with agent_id filled in (or a failed run)."""
    run_id = started["run_id"]
    agent_id = started.get("agent_id")
    deadline = time.monotonic() + limit
    while True:
        if not agent_id:
            new = [a for a in list_agent_ids() if a not in before]
            if len(new) == 1:
                agent_id = new[0]
            elif len(new) > 1:  # several creates in flight: match by name later, take the newest for now
                agent_id = sorted(new, key=lambda a: list_agent_ids()[a].get("created_at", ""))[-1]
        if agent_id:
            try:
                st = api("GET", f"agents/{agent_id}/edits/{run_id}")
            except llm.CreaoHTTPError as e:
                if e.code != 404:
                    raise
                st = {"status": "running"}
            if st.get("status") in DONE:
                return {**st, "agent_id": agent_id, "run_id": run_id}
        if time.monotonic() > deadline:
            raise TimeoutError(f"creation run {run_id} not finished after {limit:.0f}s")
        time.sleep(every)


def load() -> dict[str, Any]:
    try:
        data = json.loads(AGENTS_FILE.read_text())
    except (OSError, ValueError):
        return {"agents": {}}
    agents = data.get("agents", {}) if isinstance(data, dict) else {}
    data["agents"] = {k: (v if isinstance(v, dict) else {"agent_id": str(v)}) for k, v in agents.items() if v}
    return data


def save(data: dict[str, Any]) -> None:
    AGENTS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def create(roles: list[str], model: str | None, force: bool = False) -> dict[str, Any]:
    if not os.environ.get("CREAO_API_KEY"):
        raise SystemExit("set CREAO_API_KEY (a project key from the Developer Console at developer.creao.ai, cr_sk_...)")
    data = load()
    agents = data.setdefault("agents", {})
    for role in roles:
        if agents.get(role) and not force:
            print(f"{role}: already {agents[role]['agent_id']} (use --force to recreate)")
            continue
        body: dict[str, Any] = {"input": describe(role)}
        if model:
            body["model"] = model
        before = set(list_agent_ids())
        started = api("POST", "agents", body)
        print(f"{role}: creation run {started.get('run_id')} {started.get('status')}", flush=True)
        done = wait_creation(started, before)
        agent_id = done.get("agent_id") or (done.get("result") or {}).get("agent_id")
        if done.get("status") not in ("completed", "succeeded") or not agent_id:
            err = (done.get("error") or {}).get("code") if isinstance(done.get("error"), dict) else done.get("error")
            print(f"{role}: FAILED ({done.get('status')}, {err})", flush=True)
            continue
        agents[role] = {"agent_id": agent_id, "name": ROLES[role]["name"], "created_at": done.get("completed_at"),
                        "creation_run": started.get("run_id")}
        save(data)
        print(f"{role}: agent {agent_id} ({ROLES[role]['name']})", flush=True)
    data["url"] = llm.creao_url()
    save(data)
    return data


def check(roles: list[str]) -> int:
    """One tiny run per agent through the real provider path. Returns the number of agents that answered usable JSON."""
    agents = llm.creao_agents()
    ok = 0
    for role in roles:
        agent_id = agents.get(role)
        if not agent_id:
            print(f"{role}: no agent (run --create)")
            continue
        before = llm.creao_usage()
        try:
            text, served = llm.complete("Reply with exactly this JSON object: {\"ok\": true, \"role\": \"" + role + "\"}",
                                        "{}", 50, ("creao", agent_id))
            obj = llm.parse_json_object(text)
            after = llm.creao_usage()
            spent = (f"${after['usd'] - before['usd']:.5f}, {after['input_tokens'] - before['input_tokens']} in / "
                     f"{after['output_tokens'] - before['output_tokens']} out tokens")
            good = bool(obj) and obj.get("ok") is True
            ok += good
            print(f"{role}: {'ok' if good else 'unusable'} via {served} ({spent}): {text[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"{role}: FAILED {type(e).__name__}: {e}")
    return ok


def env_block() -> str:
    agents = llm.creao_agents()
    judges = ",".join(agents.get(k, "") for k in ("judge.strict", "judge.operator", "judge.skeptic"))
    lines = ["export CREAO_API_KEY=cr_sk_...   # project key, keep server-side", "unset FLOCK_OFFLINE",
             "export FLOCK_JUDGE_PROVIDERS=creao", f"export FLOCK_JUDGE_MODELS={judges}"]
    for role in ("reviewer", "writer", "mapper", "reader"):
        lines.append(f"export FLOCK_{role.upper()}_PROVIDER=creao")
        if agents.get(role):
            lines.append(f"export FLOCK_{role.upper()}_MODEL={agents[role]}")
    lines.append("# optional: export FLOCK_CREAO_LLM=anthropic/claude-haiku-4-5   # cheaper catalog model for the agents")
    lines.append("# then: python -m flock.pipeline --reset --forget   |   python scripts/benchmark.py --contestants rules,creao")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Create and check Flock's roles as hosted CREAO agents.")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--env", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--roles", default=",".join(ROLES), help="comma list (default: all seven)")
    ap.add_argument("--model", default=None, help="CREAO model id the agents run on, e.g. anthropic/claude-haiku-5-5")
    ap.add_argument("--force", action="store_true", help="recreate roles that already have an agent")
    ap.add_argument("--describe", action="store_true", help="print the creation prompts and exit")
    a = ap.parse_args(argv)
    roles = [r.strip() for r in a.roles.split(",") if r.strip() in ROLES]
    if a.describe:
        for r in roles:
            print(f"--- {r}\n{describe(r)}\n")
        return
    if a.list:
        for ag in list_agent_ids().values():
            print(f"{ag.get('agent_id')}  {ag.get('name')}  ({ag.get('status')}, {ag.get('default_model_id')})")
        return
    if a.create:
        create(roles, a.model, a.force)
    if a.check:
        n = check(roles)
        print(f"{n}/{len(roles)} agents answered usable JSON")
    if a.env or a.create:
        print("\n" + env_block())
    if not (a.create or a.check or a.env or a.list):
        ap.print_help()


if __name__ == "__main__":
    main()
