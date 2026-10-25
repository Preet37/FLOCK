"""Thin wrapper around the models Flock can talk to. Local-model-first: the judges, writer and mapper can run on
a local OpenAI-compatible server (vLLM or Ollama on an NVIDIA GX10), Groq or NVIDIA's hosted API, with Claude
kept as the reviewer. If a role's provider isn't configured, callers fall back to offline rules, so the whole
pipeline still runs on a laptop with no key (useful for tests and the demo backup).

Rule: never ask the model to do money math. Give it numbers already computed in Python.

Providers:
  local      OpenAI-compatible server. FLOCK_LOCAL_URL (default http://127.0.0.1:11434/v1, Ollama),
             FLOCK_LOCAL_MODEL (default llama3.1:8b), FLOCK_LOCAL_API_KEY (optional, e.g. vLLM --api-key)
  groq       GROQ_API_KEY, FLOCK_GROQ_MODEL (default llama-3.3-70b-versatile)
  nvidia     NVIDIA_API_KEY, FLOCK_NVIDIA_MODEL (default meta/llama-3.3-70b-instruct),
             FLOCK_NVIDIA_URL (default https://integrate.api.nvidia.com/v1, build.nvidia.com)
  anthropic  ANTHROPIC_API_KEY, FLOCK_MODEL (default claude-sonnet-5-5)
  creao      CREAO's Developer Platform /v2 (developer.creao.ai): every Flock AI role runs as a hosted CREAO agent.
             CREAO_API_KEY is a PROJECT key from the Developer Console (cr_sk_..., billed from a USD wallet);
             FLOCK_CREAO_URL (default https://developer.creao.ai), FLOCK_CREAO_API (v2; v1 for a CLI account key).
             The "model" slot of a creao route is the CREAO agent id: by role from creao_agents.json
             (scripts/creao_setup.py --create writes it; FLOCK_CREAO_AGENTS points elsewhere), or FLOCK_CREAO_AGENT
             for every role, or FLOCK_*_MODEL per role. FLOCK_CREAO_LLM picks the catalog model CREAO runs the agent
             on (default anthropic/claude-sonnet-5-5; anthropic/claude-haiku-4-5 is the cheap one).
             FLOCK_CREAO_MODE=async polls POST /runs instead of the realtime NDJSON stream. Run creates are paced to
             FLOCK_CREAO_RPM per minute (default 28; CREAO allows 30 per account) and 429s wait for Retry-After.
  offline    never call anything (same as leaving a key out)

Which provider each role uses:
  FLOCK_JUDGE_PROVIDERS    comma list, one per judge in panel order, e.g. "local,local,groq" (one entry = all judges)
  FLOCK_JUDGE_MODELS       optional comma list of model ids per judge, overriding that provider's model
  FLOCK_REVIEWER_PROVIDER  second opinions + escalation memos (default anthropic)
  FLOCK_WRITER_PROVIDER    dispute drafts
  FLOCK_MAPPER_PROVIDER    column mappings for unseen report layouts
  FLOCK_READER_PROVIDER    the Deal Keeper's document reader (PDFs, free-text emails -> deal terms with quotes)
  FLOCK_REVIEWER_MODEL / FLOCK_WRITER_MODEL / FLOCK_MAPPER_MODEL / FLOCK_READER_MODEL   optional model id for that role
  FLOCK_PROVIDER           default for judge/writer/mapper when their own variable is unset (default anthropic,
                           which is offline without ANTHROPIC_API_KEY: nothing set = offline rules)

Other env:
  FLOCK_LLM_TIMEOUT  seconds per request before we give up and fall back (default 30)
  FLOCK_OFFLINE=1    never call any provider, even with keys
  FLOCK_AIRGAP=1     only call providers whose URL host is on this network: loopback, private (10/8, 172.16/12,
                     192.168/16), link-local, Tailscale (100.64.0.0/10, *.ts.net, or a one-word MagicDNS name such
                     as "gx10" that resolves there). Anything else (anthropic, groq, nvidia by default) is offline
                     with a reason saying why, and its role uses offline rules.

Every model call is counted by host (airgap_report()), air-gapped or not, so a run can prove where its calls went:
"air-gapped: 412 model calls, 0 left this network". The pipeline prints that line and writes it to
data/state/airgap.json for the dashboard (GET /api/airgap).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = "claude-sonnet-5-5"
MODEL = os.environ.get("FLOCK_MODEL", DEFAULT_MODEL)  # kept for anyone importing it; model() is read per call

# provider -> (base url env, default base url, key env, model env, default model). anthropic has its own client.
OPENAI_COMPATIBLE = {
    "local": ("FLOCK_LOCAL_URL", "http://127.0.0.1:11434/v1", "FLOCK_LOCAL_API_KEY", "FLOCK_LOCAL_MODEL", "llama3.1:8b"),
    "groq": ("FLOCK_GROQ_URL", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "FLOCK_GROQ_MODEL", "llama-3.3-70b-versatile"),
    "nvidia": ("FLOCK_NVIDIA_URL", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "FLOCK_NVIDIA_MODEL",
               "meta/llama-3.3-70b-instruct"),
}
PROVIDERS = ("local", "groq", "nvidia", "anthropic", "creao", "offline")
ROLE_ENV = {"reviewer": "FLOCK_REVIEWER_PROVIDER", "writer": "FLOCK_WRITER_PROVIDER", "mapper": "FLOCK_MAPPER_PROVIDER",
            "reader": "FLOCK_READER_PROVIDER"}

CREAO_URL = "https://developer.creao.ai"
CREAO_AGENTS_FILE = Path(__file__).resolve().parent.parent / "creao_agents.json"   # role -> CREAO agent id
CREAO_ROLE_KEYS = {("judge", 0): "judge.strict", ("judge", 1): "judge.operator", ("judge", 2): "judge.skeptic"}
CREAO_POLL_SECONDS = 1.0
CREAO_RPM_DEFAULT = 28           # CREAO /v2 allows 30 run creates per account per minute; stay under it
CREAO_RETRIES = 3                # on 429 RATE_LIMITED: wait Retry-After (or 60s) and try again, this many times
# USD per 1M tokens (input, output) as the Developer Console dashboard lists them (2026-10-24). Unknown ids cost 0 here
# and are flagged in creao_usage()["unpriced"]; the wallet is the truth.
CREAO_USD_PER_M = {
    "anthropic/claude-sonnet-5-5": (2.60, 13.00), "anthropic/claude-sonnet-5": (2.60, 13.00),
    "anthropic/claude-opus-5-5": (5.20, 26.00), "anthropic/claude-opus-4-8": (6.50, 32.50),
    "anthropic/claude-fable-5": (13.00, 65.00), "anthropic/claude-fable-5-1": (13.00, 65.00),
}
_credits: Counter = Counter()    # CREAO agent id -> credits (v1 only; /v2 bills USD, see _usage)
_usage: dict[str, Counter] = {}  # CREAO agent id -> Counter(input_tokens, output_tokens, usd, calls, unpriced)
_run_times: list[float] = []     # monotonic times of recent run creates, for pacing
_sleep = __import__("time").sleep

ANTHROPIC_URL = "https://api.anthropic.com"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # CGNAT range Tailscale hands out (100.x.y.z)

_client_lock = threading.Lock()
_client_obj = None
_stats_lock = threading.Lock()
_calls: Counter = Counter()      # host -> model calls made (attempted) by this process
_refused: dict[str, str] = {}    # route label -> why FLOCK_AIRGAP kept it offline


def model() -> str:
    return os.environ.get("FLOCK_MODEL") or DEFAULT_MODEL


def timeout() -> float:
    try:
        return float(os.environ.get("FLOCK_LLM_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",")]


def _pick(items: list[str], index: int, repeat_last: bool = True) -> str:
    """Entry `index` of a comma list. One entry applies to everyone ("local" = every judge local); past the end
    of a longer list, providers repeat the last entry, models fall back to the provider's default."""
    if index < len(items):
        return items[index]
    return items[-1] if items and (repeat_last or len(items) == 1) else ""


def creao_agents() -> dict[str, str]:
    """role -> CREAO agent id, from creao_agents.json (or FLOCK_CREAO_AGENTS). Empty when nobody has run the setup."""
    path = Path(os.environ.get("FLOCK_CREAO_AGENTS") or CREAO_AGENTS_FILE)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    agents = data.get("agents", data) if isinstance(data, dict) else {}
    return {k: str(v["agent_id"] if isinstance(v, dict) else v) for k, v in agents.items() if v}


def creao_role_key(role: str, index: int = 0) -> str:
    return CREAO_ROLE_KEYS.get((role, index), role)


def default_model(provider: str, role: str = "default", index: int = 0) -> str:
    if provider == "anthropic":
        return model()
    if provider in OPENAI_COMPATIBLE:
        _, _, _, model_env, fallback = OPENAI_COMPATIBLE[provider]
        return os.environ.get(model_env) or fallback
    if provider == "creao":  # the "model" of a creao route is the agent that plays this role
        return creao_agents().get(creao_role_key(role, index)) or os.environ.get("FLOCK_CREAO_AGENT") or ""
    return ""


def route(role: str = "default", index: int = 0) -> tuple[str, str]:
    """(provider, model) for a role: 'judge' (index = which judge), 'reviewer', 'writer', 'mapper'."""
    fallback = (os.environ.get("FLOCK_PROVIDER") or "anthropic").strip().lower()
    override = ""
    if role == "judge":
        provider = _pick(_env_list("FLOCK_JUDGE_PROVIDERS"), index).lower() or fallback
        override = _pick(_env_list("FLOCK_JUDGE_MODELS"), index, repeat_last=False)
    elif role in ROLE_ENV:
        provider = (os.environ.get(ROLE_ENV[role]) or "").strip().lower() or ("anthropic" if role == "reviewer" else fallback)
        override = (os.environ.get(f"FLOCK_{role.upper()}_MODEL") or "").strip()
    else:
        provider = fallback
    return provider, override or default_model(provider, role, index)


def label(r: tuple[str, str]) -> str:
    return f"{r[0]}/{r[1]}" if r[1] else r[0]


def available(r: tuple[str, str] | None = None) -> bool:
    """Would a call on this route reach a model? False offline, when the provider's key is missing, or when
    FLOCK_AIRGAP=1 and its host is off this network (offline_reason() says which)."""
    r = r or route()
    provider = r[0]
    if os.environ.get("FLOCK_OFFLINE") == "1" or provider == "offline":
        return False
    blocked = airgap_block(r)
    if blocked:
        with _stats_lock:
            _refused[label(r)] = blocked
        return False
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "local":
        return True  # chosen on purpose; no key needed. If it's unreachable, the call fails and says so.
    if provider in OPENAI_COMPATIBLE:
        return bool(os.environ.get(OPENAI_COMPATIBLE[provider][2]))
    if provider == "creao":
        return bool(os.environ.get("CREAO_API_KEY")) and bool(r[1])  # a key and an agent to run
    return True  # unknown name: let the call fail loudly so the audit trail records the typo


def _client():
    """One shared Anthropic client (it is thread-safe), so parallel judges reuse the connection pool."""
    global _client_obj
    with _client_lock:
        if _client_obj is None:
            import anthropic

            _client_obj = anthropic.Anthropic(timeout=timeout(), max_retries=1)
        return _client_obj


def base_url(provider: str) -> str:
    url_env, url_default, _, _, _ = OPENAI_COMPATIBLE[provider]
    return (os.environ.get(url_env) or url_default).rstrip("/")


# --- air gap ---------------------------------------------------------------------------------------------------

def airgap() -> bool:
    return os.environ.get("FLOCK_AIRGAP") == "1"


def host(provider: str) -> str:
    """The host a provider's calls go to ('' for offline / unknown providers)."""
    if provider in OPENAI_COMPATIBLE:
        url = base_url(provider)
    elif provider == "anthropic":
        url = os.environ.get("ANTHROPIC_BASE_URL") or ANTHROPIC_URL
    elif provider == "creao":
        url = creao_url()
    else:
        return ""
    return (urlparse(url).hostname or "").lower()


def creao_url() -> str:
    return (os.environ.get("FLOCK_CREAO_URL") or CREAO_URL).rstrip("/")


def creao_api() -> str:
    """Route prefix: 'v2' (Developer Platform project keys, USD) or 'v1' (CLI account keys, credits)."""
    return (os.environ.get("FLOCK_CREAO_API") or "v2").strip().strip("/").lower()


def creao_rpm() -> int:
    try:
        return max(1, int(os.environ.get("FLOCK_CREAO_RPM") or CREAO_RPM_DEFAULT))
    except ValueError:
        return CREAO_RPM_DEFAULT


def _creao_pace() -> None:
    """Keep run creates under the per-minute budget across threads (the three judges run in parallel)."""
    import time

    while True:
        with _stats_lock:
            now = time.monotonic()
            while _run_times and now - _run_times[0] > 60:
                _run_times.pop(0)
            if len(_run_times) < creao_rpm():
                _run_times.append(now)
                return
            wait = 60 - (now - _run_times[0]) + 0.05
        _sleep(max(wait, 0.05))


def _private_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip in TAILSCALE_NET


def on_this_network(h: str) -> bool:
    """True when calls to host h stay on this network: loopback, private, link-local or Tailscale.

    IP literals are checked directly. localhost and *.ts.net (Tailscale MagicDNS) pass by name. A one-word name
    ("gx10") is resolved (Tailscale or /etc/hosts answers it locally) and every address must be private. Any other
    DNS name counts as the internet and is refused without being looked up."""
    h = (h or "").strip("[]").lower().rstrip(".")
    if not h:
        return False
    if _private_ip(h):
        return True
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".ts.net"):
        return True
    if "." in h or ":" in h:
        return False
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(h, None)}
    except OSError:
        return False
    return bool(addrs) and all(_private_ip(a) for a in addrs)


def airgap_block(r: tuple[str, str]) -> str | None:
    """Why FLOCK_AIRGAP=1 keeps this route offline, or None when it may be called."""
    if not airgap() or r[0] == "offline":
        return None
    h = host(r[0])
    if not h:
        return f"FLOCK_AIRGAP=1: unknown provider {r[0]!r} has no URL to check"
    if not on_this_network(h):
        return f"FLOCK_AIRGAP=1: {r[0]} would call {h}, which is off this network"
    return None


def offline_reason(r: tuple[str, str]) -> str | None:
    """Why a route won't reach a model (None when it will). For health checks and the dashboard."""
    provider = r[0]
    if os.environ.get("FLOCK_OFFLINE") == "1":
        return "FLOCK_OFFLINE=1"
    if provider == "offline":
        return "provider is 'offline'"
    blocked = airgap_block(r)
    if blocked:
        return blocked
    if provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "no ANTHROPIC_API_KEY"
    if provider in OPENAI_COMPATIBLE and provider != "local" and not os.environ.get(OPENAI_COMPATIBLE[provider][2]):
        return f"no {OPENAI_COMPATIBLE[provider][2]}"
    if provider == "creao":
        if not os.environ.get("CREAO_API_KEY"):
            return "no CREAO_API_KEY"
        if not r[1]:
            return "no CREAO agent for this role (python scripts/creao_setup.py --create, or FLOCK_CREAO_AGENT)"
    return None


def _count(provider: str) -> None:
    with _stats_lock:
        _calls[host(provider) or provider] += 1


def reset_calls() -> None:
    """Start a fresh count (the pipeline does this at the start of each run)."""
    with _stats_lock:
        _calls.clear()
        _refused.clear()
        _credits.clear()
        _usage.clear()


def creao_usage() -> dict[str, Any]:
    """What this process has spent on CREAO: tokens and estimated USD per agent (/v2), credits (/v1)."""
    with _stats_lock:
        by_agent = {a: dict(c) for a, c in _usage.items()}
        credits = dict(_credits)
    tot = Counter()
    for c in by_agent.values():
        tot.update(c)
    return {"usd": round(tot.get("usd", 0.0), 6), "input_tokens": int(tot.get("input_tokens", 0)),
            "output_tokens": int(tot.get("output_tokens", 0)), "calls": int(tot.get("calls", 0)),
            "unpriced": int(tot.get("unpriced", 0)), "credits": round(sum(credits.values()), 4),
            "by_agent": by_agent}


def creao_credits() -> dict[str, Any]:
    """Kept for callers of the v1 name: {"credits", "usd", "by_agent"}."""
    u = creao_usage()
    return {"credits": u["credits"], "usd": u["usd"], "by_agent": u["by_agent"]}


def airgap_report() -> dict[str, Any]:
    """Where this process's model calls went. 'external' = hosts off this network (empty when air-gapped)."""
    with _stats_lock:
        calls, refused = dict(_calls), dict(_refused)
    external = {h: n for h, n in calls.items() if not on_this_network(h)}
    total, left = sum(calls.values()), sum(external.values())
    if airgap():
        line = f"air-gapped: {total} model calls, {left} left this network"
    else:
        line = f"not air-gapped (FLOCK_AIRGAP=1 enforces it): {total} model calls, {left} left this network"
    if external:
        line += " (" + ", ".join(f"{h}: {n}" for h, n in sorted(external.items())) + ")"
    if refused:
        line += f"; kept offline: {', '.join(sorted(refused))}"
    return {"airgap": airgap(), "calls": total, "by_host": calls, "external_calls": left,
            "external_hosts": sorted(external), "refused": refused, "line": line,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def write_airgap_report(path: Path | str) -> dict[str, Any]:
    """Save airgap_report() as JSON for the dashboard. Never breaks a run."""
    rep = airgap_report()
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep, indent=2))
    except OSError:
        pass
    return rep


def read_airgap_report(path: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _openai_chat(provider: str, model_id: str, system: str, user: str, max_tokens: int) -> tuple[str, str]:
    """POST {base}/chat/completions with plain urllib. Works for vLLM, Ollama, Groq and build.nvidia.com."""
    key_env = OPENAI_COMPATIBLE[provider][2]
    body = json.dumps({
        "model": model_id, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.environ.get(key_env):
        headers["Authorization"] = f"Bearer {os.environ[key_env]}"
    req = urllib.request.Request(base_url(provider) + "/chat/completions", data=body, headers=headers, method="POST")
    _count(provider)
    try:
        with urllib.request.urlopen(req, timeout=timeout()) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:120] if e.fp else ""
        raise RuntimeError(f"HTTP {e.code} from {provider}: {detail}") from None
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    return (msg.get("content") or "").strip(), str(data.get("model") or model_id)



# --- CREAO Developer Platform --------------------------------------------------------------------------------------

class CreaoHTTPError(RuntimeError):
    def __init__(self, code: int, error: str, retry_after: float | None = None):
        super().__init__(f"HTTP {code} from creao: {error}")
        self.code, self.error, self.retry_after = code, error, retry_after


def _creao_request(method: str, path: str, body: dict[str, Any] | None = None) -> bytes:
    """One call to developer.creao.ai with the account key. Returns the raw body; raises CreaoHTTPError on 4xx/5xx."""
    headers = {"Authorization": f"Bearer {os.environ.get('CREAO_API_KEY', '')}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(creao_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout()) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace") if e.fp else ""
        try:
            code = json.loads(raw).get("error") or raw[:120]
        except ValueError:
            code = raw[:120]
        retry_after = None
        try:
            retry_after = float((e.headers or {}).get("Retry-After")) if e.headers and e.headers.get("Retry-After") else None
        except (TypeError, ValueError):
            retry_after = None
        raise CreaoHTTPError(e.code, str(code), retry_after) from None


def creao_path(route: str) -> str:
    """'/runs' -> '/v2/runs' (or /v1/...)."""
    return f"/{creao_api()}/{route.lstrip('/')}"


def _creao_text(result: Any) -> str:
    text = result.get("text") if isinstance(result, dict) else result
    return (text if isinstance(text, str) else json.dumps(text)).strip() if text is not None else ""


def _creao_realtime(body: dict[str, Any]) -> tuple[str, str]:
    """POST /v1/realtime/runs: NDJSON, one event per line; the answer is the output_text deltas (or result.text)."""
    raw = _creao_request("POST", creao_path("realtime/runs"), body)
    deltas, final, served, failed = [], None, "", None
    for line in raw.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "run.created":
            served = str(ev.get("model") or "")
        elif kind == "output_text.delta":
            deltas.append(str(ev.get("delta") or ""))
        elif kind == "run.completed":
            final = ev.get("result")
            _creao_note_usage(body.get("agent_id", "general"), ev.get("usage"), served)
        elif kind in ("run.failed", "error"):
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            failed = str(err.get("code") or ev.get("code") or kind)
            _creao_note_usage(body.get("agent_id", "general"), ev.get("usage"), served)
    if failed and final is None:
        raise RuntimeError(f"creao run failed: {failed}")
    text = "".join(deltas).strip() or _creao_text(final)
    return text, f"creao:{served}" if served else "creao"


def _creao_async(body: dict[str, Any]) -> tuple[str, str]:
    """POST /v1/runs then poll GET /v1/runs/{run_id} until completed/failed/cancelled (bounded by FLOCK_LLM_TIMEOUT)."""
    import time

    run = json.loads(_creao_request("POST", creao_path("runs"), body) or b"{}")
    run_id = run.get("run_id")
    if not run_id:
        raise RuntimeError("creao: run was not created")
    deadline = time.monotonic() + timeout()
    while True:
        status = json.loads(_creao_request("GET", creao_path(f"runs/{run_id}")) or b"{}")
        state = status.get("status")
        if state == "completed":
            _creao_note_usage(body.get("agent_id", "general"), status.get("usage"), str(status.get("model") or ""))
            return _creao_text(status.get("result")), f"creao:{status.get('model') or ''}".rstrip(":")
        if state in ("failed", "cancelled"):
            err = status.get("error") if isinstance(status.get("error"), dict) else {}
            raise RuntimeError(f"creao run {state}: {err.get('code') or ''}".strip())
        if time.monotonic() >= deadline:
            raise TimeoutError(f"creao run {run_id} still {state} after {timeout():.0f}s")
        time.sleep(CREAO_POLL_SECONDS)


def _creao_note_usage(agent_id: str, usage: Any, served_model: str = "") -> None:
    """v2: usage.models = [{model|kind|id, input_tokens, output_tokens}], billed in USD. v1: usage.credits."""
    if not isinstance(usage, dict):
        with _stats_lock:
            _usage.setdefault(agent_id, Counter())["calls"] += 1
        return
    try:
        credits = float(usage.get("credits") or 0)
    except (TypeError, ValueError):
        credits = 0.0
    with _stats_lock:
        c = _usage.setdefault(agent_id, Counter())
        c["calls"] += 1
        if credits:
            _credits[agent_id] += credits
        for m in usage.get("models") or []:
            if not isinstance(m, dict):
                continue
            model_id = str(m.get("model") or m.get("kind") or m.get("id") or served_model or "")
            try:
                i, o = int(m.get("input_tokens") or 0), int(m.get("output_tokens") or 0)
            except (TypeError, ValueError):
                continue
            c["input_tokens"] += i
            c["output_tokens"] += o
            price = next((p for key, p in CREAO_USD_PER_M.items() if key == model_id or key in model_id), None)
            if price:
                c["usd"] += (i * price[0] + o * price[1]) / 1_000_000
            else:
                c["unpriced"] += 1


def _creao_run(agent_id: str, system: str, user: str, max_tokens: int) -> tuple[str, str]:
    """Run a Flock role as a hosted CREAO agent. The agent was created to treat `instructions` as its system prompt
    and answer `request` with only the JSON the instructions ask for (scripts/creao_setup.py). max_tokens is
    advisory: CREAO sizes its own replies."""
    body: dict[str, Any] = {"agent_id": agent_id, "input": {"instructions": system, "request": user,
                                                             "max_tokens": max_tokens}}
    if os.environ.get("FLOCK_CREAO_LLM"):
        body["model"] = os.environ["FLOCK_CREAO_LLM"]
    _count("creao")
    realtime = (os.environ.get("FLOCK_CREAO_MODE") or "realtime").lower() != "async"
    for attempt in range(CREAO_RETRIES + 1):
        _creao_pace()
        try:
            if realtime:
                try:
                    return _creao_realtime(body)
                except CreaoHTTPError as e:
                    if e.code not in (403, 404, 405):  # realtime not enabled for this account or route: poll instead
                        raise
                    realtime = False
            return _creao_async(body)
        except CreaoHTTPError as e:
            if e.code != 429 or attempt == CREAO_RETRIES:
                raise
            _sleep(e.retry_after if e.retry_after else 60.0)  # RATE_LIMITED: nothing was created; wait and retry
    raise RuntimeError("creao: unreachable")  # pragma: no cover


def complete(system: str, user: str, max_tokens: int, r: tuple[str, str] | None = None) -> tuple[str, str]:
    """One raw call: (reply text, the model id the server says answered). Raises on any failure."""
    provider, model_id = r or route()
    blocked = airgap_block((provider, model_id))
    if blocked:  # callers check available() first; this is the last line of defence
        raise PermissionError(blocked)
    if provider in OPENAI_COMPATIBLE:
        return _openai_chat(provider, model_id, system, user, max_tokens)
    if provider == "creao":
        if not model_id:
            raise ValueError("creao: no agent id for this role (scripts/creao_setup.py --create)")
        return _creao_run(model_id, system, user, max_tokens)
    if provider != "anthropic":
        raise ValueError(f"unknown provider {provider!r} (use one of {', '.join(PROVIDERS)})")
    _count(provider)
    msg = _client().messages.create(
        model=model_id, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return text, str(getattr(msg, "model", None) or model_id)


def _create(system: str, user: str, max_tokens: int, r: tuple[str, str] | None = None) -> str:
    return complete(system, user, max_tokens, r)[0]


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model reply. Tolerates code fences, chatter and <think> blocks."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models (DeepSeek-R1, Qwen3) think out loud
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)] + [text]
    decoder = json.JSONDecoder()
    for chunk in candidates:
        chunk = chunk.strip()
        for start in [i for i, ch in enumerate(chunk) if ch == "{"]:
            try:
                obj, _ = decoder.raw_decode(chunk, start)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def call_json(system: str, user: str, max_tokens: int = 600,
              r: tuple[str, str] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """Like ask_json, but also says WHY it failed, so callers can record it in the audit trail.

    r is a (provider, model) route from route(); None means the default provider.
    Returns (obj, None) on success, (None, None) when offline on purpose, (None, "error") on a real failure.
    """
    r = r or route()
    if not available(r):
        return None, None
    try:
        text = _create(system + "\nRespond with a single JSON object and nothing else.", user, max_tokens, r)
    except Exception as e:  # network, auth, timeout
        err = f"{type(e).__name__}: {e}"[:200]
        print(f"[llm:{label(r)}] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    obj = parse_json_object(text)
    if obj is None:
        err = f"unparseable reply: {text[:80]!r}"
        print(f"[llm:{label(r)}] falling back to offline rules: {err}", file=sys.stderr)
        return None, err
    return obj, None


def ask_json(system: str, user: str, max_tokens: int = 600, r: tuple[str, str] | None = None) -> dict[str, Any] | None:
    """Ask for a JSON object back. Returns None on any failure so callers can fall back."""
    return call_json(system, user, max_tokens, r)[0]


def ask_text(system: str, user: str, max_tokens: int = 800, r: tuple[str, str] | None = None) -> str | None:
    r = r or route()
    if not available(r):
        return None
    try:
        text = _create(system, user, max_tokens, r)
    except Exception as e:
        print(f"[llm:{label(r)}] falling back to template: {type(e).__name__}: {e}"[:200], file=sys.stderr)
        return None
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip() or None
