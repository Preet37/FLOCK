"""Connector hand-off (owner: Preet). Where an outside connector drops what it saw in Slack or Gmail, so Flock reads it.

Flock never calls Slack, Gmail or any other service. A connector (the intended one is a CREAO agent, set up next to
the others by scripts/creao_setup.py, watching a Slack workspace and a Gmail inbox) writes one JSON payload per batch
into the drop folder; this module validates it and turns it into the same files the Deal Keeper already reads:
  email  -> raw/emails/<date>_<slug>.eml          (RFC 822, plain text)
  slack  -> raw/slack/users.json, channels.json, <channel>/<YYYY-MM-DD>.json   (Slack export layout)
  note   -> raw/notes/<date>_<slug>.md            (meeting notes; always non-binding)
The files go in through the Collector (untouched copies, sha256 manifest, idempotent), exactly like a portal download.
Contract and an example payload: docs/connectors.md.

  python -m flock.connectors ingest                       # every data/inbox/connectors/*.json -> data/raw/
  python -m flock.connectors ingest path/to/drop --raw path/to/raw
  python -m flock.connectors check payload.json           # validate only, print problems

Processed payloads move to <drop>/processed/, rejected ones to <drop>/rejected/ with a <name>.errors.txt beside them.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from email.utils import format_datetime, parseaddr
from pathlib import Path
from typing import Any

from flock.agents import collector
from flock.store import ROOT

DROP = ROOT / "data" / "inbox" / "connectors"
VERSION = "1"
KINDS = {"email", "slack", "note"}
MAX_ITEMS = 5000
TS = re.compile(r"^\d{9,11}\.\d{1,6}$")             # Slack message ts: epoch seconds . sequence
CHANNEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")  # Slack channel names


class PayloadError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _is_addr(s: Any) -> bool:
    return isinstance(s, str) and "@" in parseaddr(s)[1]


def _iso(s: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate(payload: Any) -> list[str]:
    """Problems with a payload ([] = fine). Checks shape only; whether a claim binds is the Deal Keeper's call."""
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    p = []
    if str(payload.get("flock_connector")) != VERSION:
        p.append(f'"flock_connector" must be "{VERSION}"')
    kind = payload.get("source")
    if kind not in KINDS:
        p.append(f'"source" must be one of {sorted(KINDS)}')
    if not isinstance(payload.get("connector"), str) or not payload["connector"].strip():
        p.append('"connector" must name who wrote this (e.g. "creao:slack-watcher")')
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return p + ['"items" must be a non-empty list']
    if len(items) > MAX_ITEMS:
        p.append(f'"items" has {len(items)} entries; split batches at {MAX_ITEMS}')
    for i, it in enumerate(items[:MAX_ITEMS]):
        where = f"items[{i}]"
        if not isinstance(it, dict):
            p.append(f"{where} must be an object")
            continue
        if kind == "email":
            for k in ("from", "to"):
                if not _is_addr(it.get(k)):
                    p.append(f"{where}.{k} must be an email address")
            if _iso(it.get("date")) is None:
                p.append(f"{where}.date must be an ISO datetime")
            if not isinstance(it.get("body"), str) or not it["body"].strip():
                p.append(f"{where}.body must be the plain-text body")
            if not isinstance(it.get("subject", ""), str):
                p.append(f"{where}.subject must be text")
        elif kind == "slack":
            if not isinstance(it.get("channel"), str) or not CHANNEL.match(it["channel"].lstrip("#")):
                p.append(f"{where}.channel must be a Slack channel name")
            u = it.get("user")
            if not isinstance(u, dict) or not u.get("id") or not u.get("name"):
                p.append(f"{where}.user must be an object with id and name (email strongly recommended: "
                         "authority is matched against the contract's authorized contacts)")
            if not TS.match(str(it.get("ts", ""))):
                p.append(f"{where}.ts must be a Slack ts like 1768821000.000004")
            if it.get("thread_ts") is not None and not TS.match(str(it["thread_ts"])):
                p.append(f"{where}.thread_ts must be a Slack ts")
            if not isinstance(it.get("text"), str) or not it["text"].strip():
                p.append(f"{where}.text must be the message text (after edits)")
        elif kind == "note":
            try:
                date.fromisoformat(str(it.get("date")))
            except ValueError:
                p.append(f"{where}.date must be YYYY-MM-DD")
            for k in ("title", "body"):
                if not isinstance(it.get(k), str) or not it[k].strip():
                    p.append(f"{where}.{k} must be text")
    return p


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "item"


def _crlf_free(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s).strip()


def _render(payload: dict, stage: Path) -> list[Path]:
    """Write the payload's items as Deal Keeper files under stage/<source folder>/."""
    kind, who = payload["source"], _crlf_free(payload["connector"])
    out: list[Path] = []
    if kind == "email":
        for i, it in enumerate(payload["items"]):
            at = _iso(it["date"])
            mid = it.get("message_id") or f"{at:%Y%m%d%H%M%S}.{i}.{_slug(who)}@connector.flock"
            head = [f"From: {_crlf_free(it['from'])}", f"To: {_crlf_free(it['to'])}", f"Date: {format_datetime(at)}",
                    f"Subject: {_crlf_free(it.get('subject', ''))}", f"Message-ID: <{_crlf_free(mid).strip('<>')}>",
                    "MIME-Version: 1.0", 'Content-Type: text/plain; charset="utf-8"',
                    "Content-Transfer-Encoding: 8bit", f"X-Flock-Connector: {who}"]
            if it.get("in_reply_to"):
                head.append(f"In-Reply-To: <{_crlf_free(it['in_reply_to']).strip('<>')}>")
            path = stage / "emails" / f"{at:%Y-%m-%d}_{_slug(it.get('subject') or 'email')}_{i:03d}.eml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(head) + "\n\n" + it["body"].replace("\r\n", "\n").rstrip("\n") + "\n")
            out.append(path)
    elif kind == "slack":
        users, channels, days = {}, {}, {}
        for it in payload["items"]:
            u, name = it["user"], it["channel"].lstrip("#")
            users[u["id"]] = {"id": u["id"], "team_id": u.get("team_id", ""), "name": u["name"],
                              "real_name": u.get("real_name", u["name"]), "deleted": False, "is_bot": False,
                              "profile": {"real_name": u.get("real_name", u["name"]), "display_name": u["name"],
                                          "email": u.get("email", ""), "title": u.get("title", "")}}
            channels.setdefault(name, {"name": name, "is_ext_shared": bool(it.get("shared")), "members": set()})
            channels[name]["members"].add(u["id"])
            msg = {"type": "message", "user": u["id"], "text": it["text"], "ts": str(it["ts"])}
            if it.get("thread_ts"):
                msg["thread_ts"] = str(it["thread_ts"])
            if it.get("edited"):
                msg["edited"] = {"user": u["id"], "ts": str(it["edited"]) if TS.match(str(it["edited"])) else msg["ts"]}
            if it.get("reactions"):
                msg["reactions"] = it["reactions"]
            day = datetime.fromtimestamp(float(it["ts"]), tz=timezone.utc).date()
            days.setdefault((name, day), []).append(msg)
        d = stage / "slack"
        d.mkdir(parents=True, exist_ok=True)
        (d / "users.json").write_text(json.dumps(sorted(users.values(), key=lambda u: u["id"]), indent=2) + "\n")
        chans = [{"id": f"C{_slug(n).upper()[:10]}", "name": n, "is_ext_shared": c["is_ext_shared"],
                  "members": sorted(c["members"]), "purpose": {"value": f"via {who}"}}
                 for n, c in sorted(channels.items())]
        (d / "channels.json").write_text(json.dumps(chans, indent=2) + "\n")
        out += [d / "users.json", d / "channels.json"]
        for (name, day), msgs in sorted(days.items()):
            path = d / name / f"{day.isoformat()}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sorted(msgs, key=lambda m: float(m["ts"])), indent=2) + "\n")
            out.append(path)
    else:
        for i, it in enumerate(payload["items"]):
            lines = [f"# {_crlf_free(it['title'])}", "", f"Date: {it['date']}"]
            if it.get("channel"):
                lines.append(f"Channel: {_crlf_free(it['channel'])}")
            if it.get("attendees"):
                lines.append(f"Attendees: {_crlf_free(it['attendees'])}")
            lines.append(f"Source: {who}")
            path = stage / "notes" / f"{it['date']}_{_slug(it['title'])}_{i:03d}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n\n" + it["body"].rstrip("\n") + "\n")
            out.append(path)
    return out


def write_payload(payload: dict, raw: Path | None = None) -> list[Path]:
    """Validate one payload and write it into raw/ through the Collector. Returns the newly copied paths
    (none when this exact content was already ingested). Raises PayloadError listing every problem."""
    problems = validate(payload)
    if problems:
        raise PayloadError(problems)
    with tempfile.TemporaryDirectory(prefix="flock_connector_") as tmp:
        _render(payload, Path(tmp))
        return collector.collect(Path(tmp), Path(raw or collector.RAW))


def ingest(drop: Path | None = None, raw: Path | None = None) -> dict[str, Any]:
    """Every *.json payload in the drop folder -> raw/. Connectors should write <name>.json.tmp and rename when done,
    so a half-written file is never read."""
    drop = Path(drop or DROP)
    done, rejected, copied = [], [], []
    for f in sorted(drop.glob("*.json")) if drop.exists() else []:
        try:
            copied += write_payload(json.loads(f.read_text()), raw)
            dest = drop / "processed"
            done.append(f.name)
        except (json.JSONDecodeError, PayloadError) as e:
            dest = drop / "rejected"
            rejected.append(f.name)
            dest.mkdir(parents=True, exist_ok=True)
            probs = e.problems if isinstance(e, PayloadError) else [f"not JSON: {e}"]
            (dest / f"{f.stem}.errors.txt").write_text("\n".join(probs) + "\n")
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest / f.name)
    return {"processed": done, "rejected": rejected, "copied": copied}


RECEIVED = "connectors_received.jsonl"   # in the store's state dir (a Railway volume when hosted)


def contracts_dir() -> Path | None:
    """Where the current contracts live, so a received message's sender is judged against the authorized contacts:
    FLOCK_CONTRACTS_DIR, else the newest showtime month's raw/contracts, else data/raw/contracts."""
    import os
    env = os.environ.get("FLOCK_CONTRACTS_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    months = sorted((ROOT / "data" / "showtime").glob("*/raw/contracts"), key=lambda d: d.stat().st_mtime)
    if months:
        return months[-1]
    default = Path(collector.RAW) / "contracts"
    return default if default.is_dir() else None


def receive(payload: Any, state_dir: Path, contracts: Path | None = None, raw: Path | None = None) -> dict[str, Any]:
    """One connector batch over HTTP (POST /api/connectors): validate, keep it (append-only, in the state dir), read it
    with the Deal Keeper next to the current contracts, and return what Flock understood: every deal claim with its
    exact quote, who sent it and whether that sender may change the deal. Raises PayloadError listing every problem.
    The messages also land in data/raw/ through the Collector, so the next pipeline run uses them."""
    from flock.agents import deal_keeper
    problems = validate(payload)
    if problems:
        raise PayloadError(problems)
    contracts = contracts if contracts is not None else contracts_dir()
    with tempfile.TemporaryDirectory(prefix="flock_receive_") as tmp:
        stage = Path(tmp)
        _render(payload, stage)
        if contracts and Path(contracts).is_dir():
            shutil.copytree(contracts, stage / "contracts")
        got = deal_keeper.read_sources(stage / "contracts", stage / "emails", stage / "slack", stage / "notes")
        mine = [c for c in got["claims"] if not str(c.get("source_file", "")).startswith(str(stage / "contracts"))
                and "/contracts/" not in str(c.get("source_file", ""))]
        keep = ("term_type", "channel", "store_id", "value", "effective_from", "effective_to", "authority",
                "authority_basis", "sender", "quote", "medium", "locator")
        claims = [{k: c.get(k) for k in keep if c.get(k) is not None} for c in mine]
        for c in claims:
            c["binding"] = c.get("authority") in deal_keeper.BINDING
        copied = write_payload(payload, raw)
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "connector": payload["connector"],
           "source": payload["source"], "items": len(payload["items"]), "simulated": bool(payload.get("simulated")),
           "claims": claims, "superseded": len(got.get("superseded") or []), "ignored": got.get("ignored"),
           "unread": [str(u) for u in got.get("unread") or []], "contracts_from": str(contracts) if contracts else None,
           "copied": [str(c) for c in copied]}
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / RECEIVED).open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def received(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    """The last connector batches received over HTTP, newest first (skips a half-written last line)."""
    path = Path(state_dir) / RECEIVED
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[::-1][:limit]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Validate connector payloads and write them into data/raw/.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="ingest every payload in the drop folder")
    i.add_argument("drop", nargs="?", default=str(DROP))
    i.add_argument("--raw", default=None)
    c = sub.add_parser("check", help="validate one payload file")
    c.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        problems = validate(json.loads(Path(a.file).read_text()))
        print("ok" if not problems else "\n".join(problems))
        sys.exit(1 if problems else 0)
    r = ingest(Path(a.drop), Path(a.raw) if a.raw else None)
    print(f"processed {len(r['processed'])}, rejected {len(r['rejected'])}, copied {len(r['copied'])} new files")
    for name in r["rejected"]:
        print(f"  rejected {name}: see {Path(a.drop) / 'rejected' / (Path(name).stem + '.errors.txt')}")


if __name__ == "__main__":
    main()
