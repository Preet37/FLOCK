# Connectors: getting Slack, Gmail and call notes into Flock

Deal changes don't arrive as contracts. They arrive as a rep's email, a message in a shared Slack channel, or a line
in someone's call notes. Flock reads all of them and applies one rule (CONTRACT.md "Which term is in effect"): only a
contract, an amendment, or a message from someone the contract lists as an **authorized contact** can change the
deal. Everything else is shown to a human as a conflict.

Flock itself never calls Slack, Gmail or any other service. An outside connector writes what it saw into a drop
folder; `flock/connectors.py` validates it and hands it to the Collector. **The intended connector is a CREAO agent**
(created next to Flock's other CREAO agents by `scripts/creao_setup.py`) that watches the Slack workspace and the
accounting Gmail inbox and writes one payload per batch. Anything else that can write a JSON file works the same way.

## The drop folder

```
data/inbox/connectors/<anything>.json      one payload per file (gitignored)
data/inbox/connectors/processed/           moved here after a successful ingest
data/inbox/connectors/rejected/            moved here if invalid, with <name>.errors.txt listing every problem
```

Write `<name>.json.tmp` first and rename it to `<name>.json` when complete, so a half-written file is never read.

```bash
python -m flock.connectors check payload.json        # validate only
python -m flock.connectors ingest                    # every payload in the drop folder -> data/raw/
python -m flock.pipeline --reset --demo-approve      # the Deal Keeper now reads the new messages
```

From Python: `flock.connectors.write_payload(payload, raw=Path("data/raw"))`.

## What each payload becomes

| `source` | Written to | Read by the Deal Keeper as |
| --- | --- | --- |
| `email` | `data/raw/emails/<date>_<subject>_<n>.eml` (RFC 822, plain text) | `auth_email` / `unauth_email` / unknown, by the sender |
| `slack` | `data/raw/slack/users.json`, `channels.json`, `<channel>/<YYYY-MM-DD>.json` (Slack export layout) | the same, by the poster's email or `@handle` |
| `note` | `data/raw/notes/<date>_<title>_<n>.md` | `call_note` (never binding) |

Files go in through the Collector: untouched copies, a sha256 line in `data/raw/manifest.csv`, and re-sending the same
batch copies nothing. The Collector also takes a real Slack export folder (`slack/`), `.eml` files and `.md` notes
dropped straight into its inbox.

## The JSON contract (version 1)

```
flock_connector   "1"                                   required
source            "email" | "slack" | "note"            required, one kind per payload
connector         who wrote it, e.g. "creao:slack-watcher"   required
simulated         true for demo data                    optional
items             non-empty list (max 5000)             required
```

**email item:** `from` and `to` (addresses, `Name <a@b>` is fine), `date` (ISO datetime), `subject`, `body` (plain
text; keep quoted replies as `>` lines, they are treated as history), optional `message_id`, `in_reply_to`.

**slack item:** `channel` (name, no `#` needed), `shared` (true for a Slack Connect channel), `user` (`id`, `name`
= the Slack handle, `real_name`, and `email`: **send the email**, authority is matched against the contract's
`authorized_contacts`, which lists emails and `@handles`), `ts` (Slack ts), `text` (the text after any edits),
optional `thread_ts` (replies), `edited` (true or the edit's ts), `reactions` (Slack's shape).

**note item:** `title`, `date` (YYYY-MM-DD), `body` (Markdown), optional `channel` (`doordash`, `ubereats`, ...),
`attendees`.

The connector reports what was said; it never decides authority or extracts terms. The Deal Keeper does that:
rule-based patterns first, then the `reader` model role (`FLOCK_READER_PROVIDER`, also a CREAO agent if configured)
for relevant messages the rules can't parse. Every claim keeps an exact quote that must appear verbatim in the
message, plus where it came from (`source_file` and a locator: the Slack `ts`, or the email/note line). Offline, a
message the rules can't read is listed as unread, never guessed. In a Slack thread, the latest message from an
authorized contact about the same term replaces the earlier ones (a correction wins).

## Example payload (SIMULATED)

A CREAO Slack watcher's batch from a shared channel: an authorized rep waives a fee, then corrects the day in the
thread, plus one message with no deal content (ignored).

```json
{
  "flock_connector": "1",
  "source": "slack",
  "connector": "creao:slack-watcher",
  "simulated": true,
  "items": [
    {
      "channel": "ubereats-partnership",
      "shared": true,
      "user": {"id": "U0SIM00004", "name": "sam.takahashi", "real_name": "Sam Takahashi (simulated)",
               "email": "sam.takahashi@ubereats.example", "title": "Senior Account Executive"},
      "ts": "1768907280.000011",
      "text": "Heads up: we'll waive the Marketing Fee ($0.75/order) for Ringtide Bagels Temescal (S06) on Saturday, January 24 while the street is closed."
    },
    {
      "channel": "ubereats-partnership",
      "shared": true,
      "user": {"id": "U0SIM00004", "name": "sam.takahashi", "real_name": "Sam Takahashi (simulated)",
               "email": "sam.takahashi@ubereats.example"},
      "ts": "1768909380.000012",
      "thread_ts": "1768907280.000011",
      "text": "Correction: we'll waive the Marketing Fee ($0.75/order) for Ringtide Bagels Temescal (S06) on Sunday, January 25, not Saturday. Saturday is billed as normal.",
      "reactions": [{"name": "+1", "users": ["U0SIM00002"], "count": 1}]
    },
    {
      "channel": "ops-promos",
      "user": {"id": "U0SIM00001", "name": "avery.quinn", "real_name": "Avery Quinn",
               "email": "avery.quinn@ringtide-bagels.example"},
      "ts": "1768910000.000013",
      "text": "Who can cover the Saturday open?"
    }
  ]
}
```

Result with the simulated bagel chain's contracts: one claim, `fee = 0` for S06 on 2026-01-25, `auth_email` (Sam is on
the Uber Eats contract's authorized contacts), quoted from the correction with `locator: ts=1768909380.000012
thread_ts=1768907280.000011`; the Saturday claim is superseded; the ops message is ignored.

## CONTRACT.md additions (applied Oct 24; CONTRACT.md is the source of truth)

- Folders: `data/raw/<source>/` also takes `slack/` (Slack export layout) and `notes/` (`.md` meeting notes);
  `emails/` also takes `.eml`. Drop folder for connectors: `data/inbox/connectors/`.
- Contracts carry `authorized_contacts` (emails and `@slack_handles`). A message's authority comes from its sender
  matched against that list: on it -> `auth_email`; same company, not on it -> `unauth_email`; anyone else -> unknown
  (conflict). `sender_authorized` stays as a fallback for files from contracts without a list.
- `auth_email` / `unauth_email` cover any written message (email or Slack); the claim's `medium` says which.
- Deal term adds optional `locator` (Slack `ts`, `line N`), `medium` (`email|slack|call_note`), `sender`,
  `authority_basis`.
- In a thread, the latest binding message about the same channel + store + term_type supersedes earlier messages in
  that thread (before effective() runs).
