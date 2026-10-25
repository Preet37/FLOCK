"""Deal Keeper (owner: Thabhelo). The contract manager: what deal was actually in effect, and says so with a quote.

IN:  data/raw/contracts/*, data/raw/emails/* (.json, .txt, .eml), data/raw/slack/ (a Slack export), data/raw/notes/*.md
     (PDF text, free-text emails and messages the rules can't parse go through the 'reader' model role)
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

Messy sources (read_sources(); every claim keeps an exact quote, asserted verbatim, plus source_file + locator):
  data/raw/emails/<name>.eml   RFC 822 email, plain-text body. Quoted reply lines ('>') are history, not claims.
                               locator "line N" in the file
  data/raw/slack/              Slack export: users.json (user id -> email / handle), channels.json,
                               <channel>/<YYYY-MM-DD>.json message arrays (ts, user, text, thread_ts, edited,
                               reactions; message_changed events applied). locator "ts=... [thread_ts=...] [(edited)]".
                               In a thread, the latest binding message about the same term supersedes earlier ones.
  data/raw/notes/<name>.md     meeting notes ('Date:', 'Channel:' lines, bullets). Always call_note. locator "line N"
  Extraction: rule-based patterns (RULES) for one-day fee waivers and commission changes; a line with a deal word and
  a number that the rules can't parse goes to DOCUMENT_READER, whose claims are kept only if their quote is verbatim
  in the message; offline it is listed as unread ("<file>#<locator>"). Lines with no deal content are ignored.
  The channel comes from the Slack channel name, the sender's domain, the 'Channel:' line or the text.

Authority of a message (sender_authority()): contracts list `authorized_contacts` (emails and "@slack_handles";
`schedule_b_authorized_contacts` emails count too). Sender on the list -> auth_email; same company but not on it ->
unauth_email; anyone else -> unknown (None, a conflict). Only when the channel's contract has no list (old files)
does the `sender_authorized` flag decide. Call notes are always call_note.

Value meaning per term_type (what the checkers expect):
  commission   fraction of gross (0.20)
  promo_split  merchant's share of the promo's full cost (0.5); promo_code + effective dates = the promo window
  fee          flat dollars per order, compared to other_fees
  tax_rule     "platform_remits" (platform collected the tax; merchant payout must not be cut for it) | "merchant_remits"
  error_window hours after delivery within which an error charge may be billed (proposed term_type; default 72)

Files without structured fields (PDFs, free-text emails) go to DOCUMENT_READER: the 'reader' model role
(FLOCK_READER_PROVIDER: local on the GX10, creao, anthropic). Offline they are listed as unread, never guessed.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from email import policy as email_policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
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
READER_SYSTEM = (
    "You read one restaurant delivery document (a platform contract, an amendment, a rep email or a call note) and "
    "return the deal terms it states, as JSON. Never do arithmetic; copy numbers as written. Return "
    '{"claims": [{"channel": "doordash|ubereats|grubhub|direct_app|all", "term_type": "commission|promo_split|fee|'
    'tax_rule|error_window", "store_id": "S01 or all", "value": <number, fraction for percentages, or a string>, '
    '"promo_code": "optional", "effective_from": "YYYY-MM-DD or null", "effective_to": "YYYY-MM-DD or null", '
    '"quote": "the exact sentence from the document", "authority": "contract|amendment|auth_email|unauth_email|'
    'call_note|unknown"}]}. authority: a signed contract is contract; a signed amendment is amendment; an email from '
    "someone whose title gives them authority to change terms is auth_email; any other rep email is unauth_email; "
    "a call note is call_note; if you cannot tell, unknown. If the document states no deal terms, return "
    '{"claims": []}.'
)
VALID_AUTHORITY = set(AUTHORITY_RANK) | {"unknown"}
TERM_TYPES = {"commission", "promo_split", "fee", "tax_rule", "error_window"}


def read_with_local_model(path: Path, text: str) -> list[dict[str, Any]]:
    """Read a contract/email the simple parser can't (PDF text, free-text email) with whichever model the "reader"
    role points at (FLOCK_READER_PROVIDER: local on the GX10, creao, anthropic...). Returns claims in the term shape
    (channel, term_type, store_id, value, effective_from, effective_to, quote, authority); the quote is the model's
    exact excerpt. Offline, or when the model fails, returns nothing and the file is listed by unread_files()."""
    from flock import llm  # here, not at the top: the deal keeper is otherwise model-free

    r = llm.route("reader")
    if not text.strip() or not llm.available(r):
        return []
    user = json.dumps({"file": path.name, "text": text[:60000]})
    got, _err = llm.call_json(READER_SYSTEM, user, max_tokens=1500, r=r)
    claims = (got or {}).get("claims") if isinstance(got, dict) else None
    out = []
    for c in claims or []:
        if not isinstance(c, dict) or c.get("term_type") not in TERM_TYPES or "value" not in c:
            continue
        auth = c.get("authority")
        out.append({**c, "authority": auth if auth in VALID_AUTHORITY and auth != "unknown" else None})
    return out


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


# who may change the deal ----------------------------------------------------------
PLATFORM_HINTS = {"doordash": re.compile(r"door\s*dash", re.I), "ubereats": re.compile(r"uber\s*eats", re.I),
                  "grubhub": re.compile(r"grub\s*hub", re.I)}


def _identities(sender: Any) -> set[str]:
    """Lowercased ways to recognize a sender: 'Name <a@b.com>' -> {'a@b.com'}; '@handle' stays '@handle'."""
    out = set()
    for s in sender if isinstance(sender, (list, tuple, set)) else [sender]:
        if not s:
            continue
        s = str(s).strip()
        if s.startswith("@"):
            out.add(s.lower())
            continue
        email = parseaddr(s)[1].lower()
        if "@" in email:
            out.add(email)
    return out


def contacts_from(doc: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(identities, domains) a contract says may change its terms: `authorized_contacts` (emails or Slack
    handles, strings or {"email", "slack"}) plus the emails in `schedule_b_authorized_contacts`."""
    ids: set[str] = set()
    for c in list(doc.get("authorized_contacts") or []) + list(doc.get("schedule_b_authorized_contacts") or []):
        if isinstance(c, dict):
            ids |= _identities([c.get("email"), "@" + str(c["slack"]).lstrip("@") if c.get("slack") else None])
        else:
            ids |= _identities(c)
    return ids, {i.split("@", 1)[1] for i in ids if not i.startswith("@")}


def sender_authority(sender: Any, channel: str | None, contacts: dict[str, tuple[set, set]],
                     flag: Any = None) -> tuple[str | None, str]:
    """(authority, why) for a message about `channel`. The contract's authorized contacts decide:
    on the list -> auth_email; same company but not on the list -> unauth_email; anyone else -> unknown (None,
    a conflict). No list for that channel, or no identifiable sender: the old `sender_authorized` flag, else unknown."""
    ids = _identities(sender)
    listed = contacts.get(channel or "")
    if listed and listed[0] and ids:
        if ids & listed[0]:
            return "auth_email", "sender is on the contract's authorized contacts"
        if any(i.split("@", 1)[-1] in listed[1] for i in ids if not i.startswith("@")):
            return "unauth_email", "sender works for the platform but is not on the contract's authorized contacts"
        return None, "sender is not on the contract's authorized contacts (unknown authority)"
    if isinstance(flag, str):
        flag = {"yes": True, "true": True, "no": False, "false": False}.get(flag.strip().lower())
    if flag is None:
        return None, "unknown sender"
    return ("auth_email" if flag else "unauth_email"), "sender_authorized flag (no authorized contacts list)"


def _authority_for_sender(doc: dict[str, Any], contacts: dict[str, tuple[set, set]] | None = None,
                          ) -> tuple[str | None, str]:
    if str(doc.get("kind", "email")).lower() == "call_note":
        return "call_note", "call note"
    return sender_authority(doc.get("from"), str(doc.get("channel") or "").lower(), contacts or {},
                            doc.get("sender_authorized"))


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
    for k in ("locator", "medium", "authority_basis", "edited", "slack_channel", "subject", "read_by"):
        if raw.get(k) or doc.get(k):
            c[k] = raw.get(k) or doc.get(k)
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


# messages: rule-based extraction first, the reader model only for what the rules can't parse --------------------
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = rf"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?(?:{MONTHS})\s+\d{{1,2}}(?:,\s*\d{{4}})?"
RULES = [  # (term_type, pattern); the planted phrasing, written the way reps actually write it
    ("fee", re.compile(rf"waive[sd]?\s+the\s+(?P<fee>[A-Za-z][\w ]*?)\s*(?:\(\$[\d.,]+(?:/order)?\)\s*)?for\s+"
                       rf"[^()\n]*?\((?P<store>S\d{{2,3}})\)\s+on\s+(?P<date>{DATE})", re.I)),
    ("commission", re.compile(rf"(?:starting|effective)\s+(?P<date>{DATE})[^.\n]*?\b(?P<pct>\d+(?:\.\d+)?)%\s+"
                              r"commission", re.I)),
    ("commission", re.compile(rf"\b(?P<pct>\d+(?:\.\d+)?)%\s+commission[^.\n]*?(?:starting|effective(?:\s+from)?)\s+"
                              rf"(?P<date>{DATE})", re.I)),
]
DEAL_WORDS = re.compile(r"\b(commission|fees?|waive[sd]?|waiver|promo(?:tion)?s?|bogo|split|rates?|tax|"
                        r"amendment|plan)\b", re.I)


def relevant(text: str) -> bool:
    """Could this line change the deal? A deal word AND a number. Chit-chat ('Who can cover Saturday?') and vague
    talk ('they might revisit the commission next quarter') are ignored, not sent to a model."""
    return bool(DEAL_WORDS.search(text) and re.search(r"\d", text))


def _date_in(text: str, ref: date | None) -> date | None:
    """'Monday, January 26' (year from the message date: the next such day on or after ~6 months before it)."""
    m = re.search(rf"(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>\d{{4}}))?", text)
    if not m:
        return None
    month = MONTHS.split("|").index(m["month"]) + 1
    if m["year"]:
        return date(int(m["year"]), month, int(m["day"]))
    ref = ref or date.today()
    d = date(ref.year, month, int(m["day"]))
    return date(ref.year + 1, month, int(m["day"])) if (ref - d).days > 180 else d


def rule_claims(line: str, ref: date | None) -> list[dict[str, Any]]:
    """Claims the patterns can read from one line. The quote is the whole line, verbatim."""
    out = []
    quote = line.strip().lstrip("-*").strip()
    for term_type, pat in RULES:
        for m in pat.finditer(line):
            day = _date_in(m["date"], ref)
            if day is None:
                continue
            if term_type == "fee":   # a one-day waiver for one store
                out.append({"term_type": "fee", "store_id": m["store"].upper(), "value": 0.0,
                            "effective_from": day.isoformat(), "effective_to": day.isoformat(), "quote": quote})
            else:                    # a new rate from that day on
                out.append({"term_type": "commission", "store_id": "all", "value": float(m["pct"]) / 100,
                            "effective_from": day.isoformat(), "effective_to": None, "quote": quote})
    for c in out:
        assert c["quote"] and c["quote"] in line, "a claim's quote must be verbatim source text"
    return out


def _platform(*hints: Any) -> str | None:
    for h in hints:
        for ch, pat in PLATFORM_HINTS.items():
            if h and (ch in str(h).lower() or pat.search(str(h))):
                return ch
    return None


class _Reading:
    """Accumulates what one read of the deal sources found."""

    def __init__(self) -> None:
        self.claims: list[dict[str, Any]] = []
        self.unread: list[str] = []
        self.superseded: list[dict[str, Any]] = []
        self.ignored = 0   # messages / lines with no deal content

    def message(self, path: Path, text: str, lines: list[tuple[str, str]], ref: date | None, channel: str | None,
                authority: str | None, basis: str, sender: Any, medium: str, extra: dict | None = None,
                ) -> list[dict[str, Any]]:
        """One message (or note): lines = [(text, locator)]. Rules first; relevant lines the rules can't read go to
        DOCUMENT_READER (the 'reader' model role). Its claims are kept only if their quote is verbatim in the message;
        offline (or nothing verbatim) the message is listed as unread. Never invents a claim."""
        found, pending = [], []
        for line, loc in lines:
            if not relevant(line):
                self.ignored += 1
                continue
            got = rule_claims(line, ref)
            for c in got:
                c["locator"] = loc
            found += got
            if not got:
                pending.append((line, loc))
        if pending:
            got = [c for c in DOCUMENT_READER(path, "\n".join(ln for ln, _ in pending)) or []
                   if isinstance(c, dict) and c.get("quote") and str(c["quote"]) in text]
            for c in got:
                c["locator"] = next((loc for ln, loc in pending if str(c["quote"]) in ln), pending[0][1])
                c["read_by"] = "reader model"
            found += got
            if not got:
                self.unread += [f"{_rel(path)}#{loc}" for _, loc in pending]
        doc = {"channel": channel, "from": sender, "medium": medium, "authority_basis": basis, **(extra or {})}
        out = []
        for raw in found:
            if not (raw.get("read_by") and raw.get("channel")):
                raw["channel"] = channel or _platform(raw["quote"]) or "all"
            raw["authority"] = authority      # who sent it decides, never the text (or the model)
            out.append(_claim(raw, doc, authority, path))
        self.claims += out
        return out


def _line_no(text: str, quote: str) -> str:
    i = text.find(quote)
    return f"line {text.count(chr(10), 0, i) + 1}" if i >= 0 else "line ?"


def read_eml(path: Path, contacts: dict, reading: _Reading) -> None:
    """RFC 822 email: From/To/Date/Subject, plain-text body. Quoted reply lines ('>') and the 'On ... wrote:' line
    are history, not new claims."""
    raw = path.read_text(errors="replace")
    msg = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
    part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    body = part.get_content() if part is not None else ""
    try:
        ref = parsedate_to_datetime(msg["Date"]).date() if msg["Date"] else None
    except (TypeError, ValueError):
        ref = None
    sender = str(msg["From"] or "")
    channel = _platform(parseaddr(sender)[1].split("@")[-1], msg["Subject"], body)
    authority, basis = sender_authority(sender, channel, contacts)
    lines = [(ln, _line_no(raw, ln.strip()) if ln.strip() in raw else "body") for ln in body.splitlines()
             if ln.strip() and not ln.lstrip().startswith(">") and not re.match(r"^On .+wrote:\s*$", ln.strip())]
    reading.message(path, body, lines, ref, channel, authority, basis, sender, "email",
                    {"subject": str(msg["Subject"] or "")})


def read_note(path: Path, reading: _Reading) -> None:
    """Meeting notes (.md): 'Date:' / 'Channel:' header lines, then bullets. Always a call note: never binding."""
    text = path.read_text(errors="replace")
    hdr = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip().lower() in ("date", "channel", "attendees"):
            hdr.setdefault(k.strip().lower(), v.strip())
    m = re.search(r"\d{4}-\d{2}-\d{2}", hdr.get("date", "") or path.name)
    ref = date.fromisoformat(m.group(0)) if m else None
    channel = (hdr.get("channel") or "").lower() or _platform(path.name, text.splitlines()[0] if text else "")
    lines = [(ln, f"line {i}") for i, ln in enumerate(text.splitlines(), 1)
             if ln.strip() and not ln.startswith("#") and ln.split(":")[0].strip().lower() not in hdr]
    reading.message(path, text, lines, ref, channel, "call_note", "call note", hdr.get("attendees"), "call_note")


def _ts_day(ts: Any) -> date | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except (TypeError, ValueError):
        return None


def read_slack(slack_dir: Path, contacts: dict, reading: _Reading) -> None:
    """A Slack export: users.json (ids -> email/handle), channels.json, <channel>/<YYYY-MM-DD>.json. Edits are
    applied (the export's text is the edited text; message_changed events update the original). Within a thread,
    the latest binding message about a term supersedes the earlier ones (a correction wins)."""
    slack_dir = Path(slack_dir)
    users = {}
    for f in sorted(slack_dir.glob("users*.json")):   # users.<sha8>.json: a later connector batch (collector naming)
        users.update({u["id"]: u for u in json.loads(f.read_text()) if isinstance(u, dict) and u.get("id")})
    for ch_dir in sorted(p for p in slack_dir.iterdir() if p.is_dir()):
        by_ts: dict[str, dict] = {}
        for f in sorted(ch_dir.glob("*.json")):
            try:
                rows = json.loads(f.read_text())
            except json.JSONDecodeError:
                reading.unread.append(_rel(f))
                continue
            for m in rows if isinstance(rows, list) else []:
                if m.get("subtype") == "message_changed" and isinstance(m.get("message"), dict):
                    new = m["message"]
                    if new.get("ts") in by_ts:
                        by_ts[new["ts"]].update(text=new.get("text", ""), edited=new.get("edited") or {"ts": m["ts"]})
                    continue
                if m.get("subtype") in ("channel_join", "channel_leave", "message_deleted") or "ts" not in m:
                    continue
                by_ts[m["ts"]] = {**m, "_file": f}
        threads: dict[str, list[tuple[float, list[dict]]]] = {}
        for ts in sorted(by_ts, key=float):
            m = by_ts[ts]
            text = m.get("text") or ""
            u = users.get(m.get("user"), {})
            email = (u.get("profile") or {}).get("email")
            sender = [x for x in (email, "@" + u["name"] if u.get("name") else None) if x]
            channel = _platform(ch_dir.name) or _platform((email or "").split("@")[-1]) or _platform(text)
            authority, basis = sender_authority(sender, channel, contacts)
            loc = f"ts={ts}" + (f" thread_ts={m['thread_ts']}" if m.get("thread_ts") not in (None, ts) else "") + \
                  (" (edited)" if m.get("edited") else "")
            got = reading.message(m["_file"], text, [(text, loc)], _ts_day(ts), channel, authority, basis,
                                  email or (sender[0] if sender else None), "slack",
                                  {"edited": bool(m.get("edited")), "slack_channel": "#" + ch_dir.name})
            for c in got:
                if u.get("real_name") and email:
                    c["sender"] = f"{u['real_name']} <{email}>"
            threads.setdefault(m.get("thread_ts") or ts, []).append((float(ts), got))
        for msgs in threads.values():    # a later binding message in the thread corrects earlier ones
            for i, (_, later) in enumerate(msgs):
                for c in later:
                    if c.get("authority") not in BINDING:
                        continue
                    k = (c["channel"], c["store_id"], c["term_type"], c.get("promo_code"))
                    for _, earlier in msgs[:i]:
                        for e in [e for e in earlier if (e["channel"], e["store_id"], e["term_type"],
                                                        e.get("promo_code")) == k]:
                            earlier.remove(e)
                            reading.claims.remove(e)
                            reading.superseded.append({**e, "superseded_by": c["locator"]})


def read_sources(contracts_dir: Path | None = None, emails_dir: Path | None = None, slack_dir: Path | None = None,
                 notes_dir: Path | None = None) -> dict[str, Any]:
    """Read every deal source. slack/ and notes/ default to siblings of the emails folder (data/raw/slack, notes).
    Returns {"claims", "unread", "superseded", "ignored"}; claims carry term_id, quote, source_file, locator."""
    contracts_dir = Path(contracts_dir or RAW / "contracts")
    emails_dir = Path(emails_dir or RAW / "emails")
    slack_dir = Path(slack_dir or emails_dir.parent / "slack")
    notes_dir = Path(notes_dir or emails_dir.parent / "notes")
    reading = _Reading()
    claims, unread = reading.claims, reading.unread
    contacts: dict[str, tuple[set, set]] = {}
    for folder, is_contract in ((contracts_dir, True), (emails_dir, False), (notes_dir, False)):
        if not folder.exists():
            continue
        for path in sorted(p for p in folder.iterdir() if p.is_file()):
            suffix = path.suffix.lower()
            if not is_contract and suffix == ".eml":
                read_eml(path, contacts, reading)
                continue
            if not is_contract and suffix == ".md":
                read_note(path, reading)
                continue
            text = path.read_text(errors="replace") if suffix in (".json", ".txt", ".md") else ""
            doc = None
            if suffix == ".json":
                try:
                    doc = json.loads(text)
                except json.JSONDecodeError:
                    doc = None
            elif suffix == ".txt" and not is_contract:
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
                ids, domains = contacts_from(doc)
                if ids and doc.get("channel"):
                    have = contacts.setdefault(str(doc["channel"]).lower(), (set(), set()))
                    have[0].update(ids)
                    have[1].update(domains)
                claims += [_claim(r, doc, authority, path) for r in raw_terms]
            else:
                authority, basis = _authority_for_sender(doc, contacts)
                claims += [_claim(r, {**doc, "authority_basis": basis}, authority, path)
                           for r in doc.get("claims", [])]
    if slack_dir.is_dir():
        read_slack(slack_dir, contacts, reading)
    for i, c in enumerate(claims, 1):
        c["term_id"] = f"T-{i:03d}"
    return {"claims": claims, "unread": unread, "superseded": reading.superseded, "ignored": reading.ignored}


def load_claims(contracts_dir: Path | None = None, emails_dir: Path | None = None, slack_dir: Path | None = None,
                notes_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """Read every contract/amendment/email/Slack message/call note. Returns (claims, unread_files)."""
    got = read_sources(contracts_dir, emails_dir, slack_dir, notes_dir)
    return got["claims"], got["unread"]


def extract_terms(contracts_dir: Path | None = None, emails_dir: Path | None = None, slack_dir: Path | None = None,
                  notes_dir: Path | None = None) -> list[dict[str, Any]]:
    """All claims as deal terms (CONTRACT.md shape, plus promo_code/sender/locator when present). Pick one with
    effective()."""
    return load_claims(contracts_dir, emails_dir, slack_dir, notes_dir)[0]


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
