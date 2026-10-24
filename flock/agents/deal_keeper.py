"""Deal Keeper (owner: Thabhelo). The contract manager: what deal was actually in effect, and says so with a quote.

IN:  data/raw/contracts/*, data/raw/emails/*  (later: read by a local model on the GX10, see DOCUMENT_READER)
OUT: list of deal terms (CONTRACT.md "Deal term"), each with quote + authority

Authority, strongest first: contract > amendment > auth_email > unauth_email > call_note.
Newest binding document wins; see CONTRACT.md "Which term is in effect" and effective() below.

How a term is picked for one channel/store/date (effective()):
  1. Binding sources (contract, amendment, auth_email): the latest effective_from supersedes older ones
     (that's what an amendment or an authorized waiver is for). Same effective_from -> resolve() by authority.
  2. Non-binding sources (unauth_email, call_note, unknown) never override a binding term, but if they disagree
     they are returned as "conflicts" so the checkers put them in the evidence and a human sees them (edge case 15).
  3. Store-specific terms beat store "all" terms of the same strength.

INPUT FORMAT (what the generator should emit; one file per document)

data/raw/contracts/<name>.json   (contracts and amendments)
  {
    "doc_type": "contract" | "amendment",
    "channel": "doordash",
    "effective_from": "2026-01-01",          # default for every term below
    "effective_to": null,                    # null = open-ended
    "terms": [
      {"term_type": "commission",  "store_id": "all", "value": 0.20, "quote": "Marketplace commission: 20% of subtotal"},
      {"term_type": "promo_split", "store_id": "all", "value": 0.5,  "promo_code": "BOGO",
       "effective_from": "2026-10-01", "effective_to": "2026-10-14",
       "quote": "BOGO promotions funded 50/50 by Grubhub and Merchant"},
      {"term_type": "fee",         "store_id": "S01", "value": 2.00, "quote": "Tablet fee: $2.00 per order"},
      {"term_type": "tax_rule",    "store_id": "all", "value": "platform_remits", "quote": "..."},
      {"term_type": "error_window","store_id": "all", "value": 72, "quote": "Errors reported 72 hours or more ..."}
    ]
  }

data/raw/emails/<name>.json   (rep emails and call notes)
  {
    "kind": "email" | "call_note",
    "from": "jordan@ubereats.com",
    "sender_authorized": true | false,       # omit if unknown -> authority unknown -> conflict
    "channel": "ubereats",
    "sent_at": "2026-09-12T10:00:00",
    "body": "full text",
    "claims": [ {"term_type": "fee", "store_id": "all", "value": 0, "effective_from": "2026-10-01",
                 "effective_to": "2026-10-31", "quote": "We'll waive the tablet fee for October"} ]
  }

data/raw/emails/<name>.txt   (same thing, one claim, header lines then a blank line then the body)
  From: jordan@ubereats.com
  Authorized: no                  # yes | no | (omit = unknown)
  Kind: email                     # email | call_note
  Channel: ubereats
  Term-Type: fee
  Store: all
  Value: 0
  Promo-Code: BOGO                # optional
  Effective-From: 2026-10-01
  Effective-To: 2026-10-31        # optional
  Quote: We'll waive the tablet fee for October     # optional; defaults to the first line of the body

  <body>

Value meaning per term_type (what the checkers expect):
  commission   fraction of gross (0.20)
  promo_split  merchant's share of the promo's full cost (0.5); promo_code + effective dates = the promo window
  fee          flat dollars per order, compared to other_fees
  tax_rule     "platform_remits" (platform collected the tax; merchant payout must not be cut for it) | "merchant_remits"
  error_window hours after delivery within which an error charge may be billed (proposed term_type; default 72)

Files without structured fields (PDFs, free-text emails) go to DOCUMENT_READER (the GX10 local-model hook).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from flock.models import AUTHORITY_RANK
from flock.store import ROOT

RAW = ROOT / "data" / "raw"
BINDING = {"contract", "amendment", "auth_email"}
TERM_FIELDS = ["term_id", "channel", "store_id", "term_type", "value", "effective_from", "effective_to",
               "authority", "quote", "source_file"]


def resolve(claims: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick the winning claim among claims about the SAME term (same channel, store, term_type, date).

    Returns (winner, conflicting_claims). If conflicting_claims is non-empty, a human must decide.
    This part is plain code and already works; use it.
    """
    if not claims:
        return None, []
    rank = {a: i for i, a in enumerate(AUTHORITY_RANK)}
    ordered = sorted(claims, key=lambda c: rank.get(c.get("authority"), 99))
    best = ordered[0]
    if best.get("authority") not in rank:
        return None, claims
    ties = [c for c in ordered if rank.get(c.get("authority")) == rank[best["authority"]] and c["value"] != best["value"]]
    if ties:
        return None, [best] + ties
    return best, []


# reading documents -----------------------------------------------------------
def read_with_local_model(path: Path, text: str) -> list[dict[str, Any]]:
    """HOOK (GX10): read a contract/email the simple parser can't, return claims in the term shape
    (term_type, store_id, value, effective_from, effective_to, quote, authority). The quote must be exact text
    from the document. Not built yet: returns nothing, so unreadable files are listed by unread_files()."""
    return []


DOCUMENT_READER: Callable[[Path, str], list[dict[str, Any]]] = read_with_local_model


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _num(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip().replace("$", "").replace(",", "")
        try:
            return float(s[:-1]) / 100 if s.endswith("%") else float(s)
        except ValueError:
            return v.strip()
    return v


def _authority_for_sender(doc: dict[str, Any]) -> str | None:
    kind = str(doc.get("kind", "email")).lower()
    if kind == "call_note":
        return "call_note"
    auth = doc.get("sender_authorized")
    if isinstance(auth, str):
        auth = {"yes": True, "true": True, "no": False, "false": False}.get(auth.strip().lower())
    if auth is None:
        return None  # unknown authority -> conflict
    return "auth_email" if auth else "unauth_email"


def _claim(raw: dict[str, Any], doc: dict[str, Any], authority: str | None, path: Path) -> dict[str, Any]:
    c = {
        "channel": str(raw.get("channel") or doc.get("channel") or "all").lower(),
        "store_id": raw.get("store_id") or "all",
        "term_type": raw["term_type"],
        "value": _num(raw["value"]),
        "effective_from": raw.get("effective_from") or doc.get("effective_from"),
        "effective_to": raw.get("effective_to") or doc.get("effective_to"),
        "authority": raw.get("authority", authority),
        "quote": raw.get("quote") or "",
        "source_file": _rel(path),
    }
    if raw.get("promo_code") or doc.get("promo_code"):
        c["promo_code"] = raw.get("promo_code") or doc.get("promo_code")
    if doc.get("from"):
        c["sender"] = doc["from"]
    return c


def _parse_txt(text: str) -> dict[str, Any] | None:
    head, _, body = text.partition("\n\n")
    hdr = {}
    for line in head.splitlines():
        k, sep, v = line.partition(":")
        if sep:
            hdr[k.strip().lower()] = v.strip()
    if "term-type" not in hdr or "value" not in hdr:
        return None
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    claim = {"term_type": hdr["term-type"], "value": hdr["value"], "store_id": hdr.get("store", "all"),
             "effective_from": hdr.get("effective-from"), "effective_to": hdr.get("effective-to"),
             "quote": hdr.get("quote") or first}
    if hdr.get("promo-code"):
        claim["promo_code"] = hdr["promo-code"]
    return {"kind": hdr.get("kind", "email"), "from": hdr.get("from"), "sender_authorized": hdr.get("authorized"),
            "channel": hdr.get("channel"), "body": body, "claims": [claim]}


def load_claims(contracts_dir: Path | None = None, emails_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """Read every contract/amendment/email/call note. Returns (claims, unread_files)."""
    contracts_dir = Path(contracts_dir or RAW / "contracts")
    emails_dir = Path(emails_dir or RAW / "emails")
    claims, unread = [], []
    for folder, is_contract in ((contracts_dir, True), (emails_dir, False)):
        if not folder.exists():
            continue
        for path in sorted(p for p in folder.iterdir() if p.is_file()):
            text = path.read_text(errors="replace") if path.suffix.lower() in (".json", ".txt", ".md") else ""
            doc = None
            if path.suffix.lower() == ".json":
                try:
                    doc = json.loads(text)
                except json.JSONDecodeError:
                    doc = None
            elif path.suffix.lower() == ".txt" and not is_contract:
                doc = _parse_txt(text)
            if doc is None:
                got = DOCUMENT_READER(path, text)
                if got:
                    claims += [_claim(r, {}, r.get("authority"), path) for r in got]
                else:
                    unread.append(_rel(path))
                continue
            if is_contract:
                authority = doc.get("doc_type", "contract") if doc.get("doc_type") in ("contract", "amendment") else None
                raw_terms = doc.get("terms", [])
            else:
                authority = _authority_for_sender(doc)
                raw_terms = doc.get("claims", [])
            claims += [_claim(r, doc, authority, path) for r in raw_terms]
    for i, c in enumerate(claims, 1):
        c["term_id"] = f"T-{i:03d}"
    return claims, unread


def extract_terms(contracts_dir: Path | None = None, emails_dir: Path | None = None) -> list[dict[str, Any]]:
    """All claims as deal terms (CONTRACT.md shape, plus promo_code/sender when present). Pick one with effective()."""
    return load_claims(contracts_dir, emails_dir)[0]


# picking the term in effect ----------------------------------------------------
def _day(s: Any) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s if not isinstance(s, datetime) else s.date()
    return date.fromisoformat(str(s).strip()[:10])  # ISO date or datetime: the day is the first 10 chars


def _active(c: dict[str, Any], on: date | None) -> bool:
    if on is None:
        return True
    start, end = _day(c.get("effective_from")), _day(c.get("effective_to"))
    return (start is None or start <= on) and (end is None or on <= end)


def effective(terms: list[dict[str, Any]], channel: str, store_id: str, term_type: str, on: Any,
              promo_code: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(winner, conflicts) for one term on one day. winner None + conflicts = equal/unknown authority, a human decides."""
    day = _day(on)
    active = [c for c in terms if c["term_type"] == term_type and c["channel"] in (channel, "all")
              and c["store_id"] in (store_id, "all") and _active(c, day)
              and (promo_code is None or c.get("promo_code") in (promo_code, None))]
    if not active:
        return None, []
    binding = [c for c in active if c.get("authority") in BINDING]
    weak = [c for c in active if c.get("authority") not in BINDING]
    if not binding:
        return resolve(weak)
    if any(c["store_id"] == store_id for c in binding):
        binding = [c for c in binding if c["store_id"] == store_id]
    latest = max((_day(c.get("effective_from")) or date.min) for c in binding)
    winner, tied = resolve([c for c in binding if (_day(c.get("effective_from")) or date.min) == latest])
    if winner is None:
        return None, tied
    return winner, [c for c in weak if c["value"] != winner["value"]]


def _with_conflicts(winner: dict | None, conflicts: list[dict]) -> dict[str, Any] | None:
    if winner is not None:
        return {**winner, "conflicts": conflicts}
    if conflicts:  # nobody wins: compute with the first claim, but flag it so it can't be decided silently
        return {**conflicts[0], "conflicts": conflicts[1:], "unresolved": True}
    return None


def terms_for(terms: list[dict[str, Any]], story: dict[str, Any]) -> dict[str, Any]:
    """Effective terms for one story, keyed by term_type, each with "conflicts" (claims a human should see).
    Also "promo_windows": every promo_split claim for the story's promo code on any date (edge case 9)."""
    channel, store = story.get("channel"), story.get("store_id", "all")
    on = story.get("placed_at") or (story.get("payout_lines") or [{}])[0].get("payout_date")
    code = (story.get("pos") or {}).get("promo_code") or next(
        (ln.get("promo_code") for ln in story.get("payout_lines", []) if ln.get("promo_code")), None)
    out: dict[str, Any] = {}
    for tt in ("commission", "fee", "tax_rule", "error_window"):
        t = _with_conflicts(*effective(terms, channel, store, tt, on))
        if t:
            out[tt] = t
    if code:
        t = _with_conflicts(*effective(terms, channel, store, "promo_split", on, code))
        if t:
            out["promo_split"] = t
        out["promo_windows"] = [c for c in terms if c["term_type"] == "promo_split" and c["channel"] in (channel, "all")
                                and c["store_id"] in (store, "all") and c.get("promo_code") == code
                                and c.get("authority") in BINDING]
        out["promo_code"] = code
    return out


def find_conflicts(terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every term that needs a human, for the dashboard: [{"key", "on", "winner", "conflicts"}]."""
    out, seen = [], set()
    keys = {(c["channel"], c["store_id"], c["term_type"], c.get("promo_code")) for c in terms}
    for channel, store, tt, code in sorted(keys, key=str):
        days = sorted({_day(c.get("effective_from")) for c in terms if c["term_type"] == tt} - {None}) or [None]
        for d in days:
            winner, conflicts = effective(terms, channel, store, tt, d, code)
            sig = (channel, store, tt, code, tuple(sorted(c["term_id"] for c in conflicts)))
            if conflicts and sig not in seen:
                seen.add(sig)
                out.append({"key": {"channel": channel, "store_id": store, "term_type": tt, "promo_code": code},
                            "on": d.isoformat() if d else None, "winner": winner, "conflicts": conflicts})
    return out


class TermIndex:
    """terms_for() with a memory, for year-scale runs. Same answers: terms are pre-filtered to one channel + store
    and the result is cached per (channel, store, day, promo code), so a million orders cost a few thousand lookups.
    Callers must treat the returned dict as read-only (the checkers do)."""

    def __init__(self, terms: list[dict[str, Any]]):
        self.terms = terms
        self._subset: dict[tuple, list[dict[str, Any]]] = {}
        self._cache: dict[tuple, dict[str, Any]] = {}

    def subset(self, channel: Any, store: Any) -> list[dict[str, Any]]:
        k = (channel, store)
        if k not in self._subset:
            self._subset[k] = [c for c in self.terms if c["channel"] in (channel, "all")
                               and c["store_id"] in (store, "all")]
        return self._subset[k]

    def __call__(self, story: dict[str, Any]) -> dict[str, Any]:
        channel, store = story.get("channel"), story.get("store_id", "all")
        on = story.get("placed_at") or (story.get("payout_lines") or [{}])[0].get("payout_date")
        code = (story.get("pos") or {}).get("promo_code") or next(
            (ln.get("promo_code") for ln in story.get("payout_lines", []) if ln.get("promo_code")), None)
        k = (channel, store, str(on)[:10] if on else None, code)
        if k not in self._cache:
            self._cache[k] = terms_for(self.subset(channel, store), story)
        return self._cache[k]
