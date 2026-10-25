# Client login and vendor email

## Login for the finance team (`flock/auth.py`)

Reviewers sign in with a username and password instead of pasting `FLOCK_API_TOKEN`. A reviewer's session can approve
and reject; the audit log records the signed-in user's name (a name sent by the browser is ignored). A viewer can
sign in and read, not decide. Sessions last 12 hours (`data/state/sessions.json`, stored by token hash). Five wrong
passwords in five minutes lock that username for the rest of the window.

**Demo login (simulated restaurant, for the team and the judges' walkthrough):**

| Username | Password | Name | Role |
| --- | --- | --- | --- |
| `noahs-finance` | `bagels-reconcile-2026` | Preet Karia | reviewer |

The password lives only here. The repo stores its pbkdf2-sha256 hash (`data/users.example.json`).

**Your own users on Railway:** set `FLOCK_USERS` to a JSON list; it replaces the example file.

```bash
python -m flock.auth hash 'a-strong-password'      # prints pbkdf2_sha256$200000$<salt>$<hash>
```
```json
[{"username": "maja", "password_hash": "pbkdf2_sha256$200000$...", "name": "Maja", "role": "reviewer", "org": "Ringtide"}]
```

API: `POST /api/login {"username", "password"}` → `{token, user, expires_at}`; send `Authorization: Bearer <token>`
on writes. `GET /api/me` → the user (401 without a session). `POST /api/logout`. `FLOCK_API_TOKEN` keeps working for
scripts and agents (then the body's `actor` is used, as before).

# Vendor follow-up email

When a human approves a vendor follow-up and it is sent, Flock emails it with `flock/mailer.py`. With no mail settings
the email goes to the **outbox** (`data/state/outbox.jsonl`, `GET /api/outbox`), so the demo still shows exactly what
would have been sent. Every message, sent or not, is kept there. Passwords and keys are never written to it.

## Gmail on Railway (the demo setup)

You create the app password yourself (Flock never asks for your Google password):

1. Google Account → Security → turn on 2-Step Verification (required for app passwords).
2. Google Account → Security → **App passwords** → create one named "Flock". Copy the 16 characters.
3. Railway → project → service **web** → Variables, add:

| Variable | Value |
| --- | --- |
| `FLOCK_SMTP_HOST` | `smtp.gmail.com` |
| `FLOCK_SMTP_PORT` | `587` |
| `FLOCK_SMTP_USER` | your Gmail address |
| `FLOCK_SMTP_PASSWORD` | the app password |
| `FLOCK_VENDOR_EMAIL_TO` | the inbox that receives every vendor email in the demo (e.g. your own address) |

Railway redeploys on save (check `GET /api/health` for `running_job: null` first: a redeploy kills a running job).

`FLOCK_VENDOR_EMAIL_TO` matters: the vendor addresses in the simulated data are `.example` addresses that can't receive
mail. With it set, each email goes to that inbox and starts with a line naming the vendor address it was meant for.

## Alternatives

- **Resend:** `RESEND_API_KEY` and `FLOCK_MAIL_FROM` (a sender on a domain verified in Resend).
- **Any SMTP server:** the same four `FLOCK_SMTP_*` variables. Port 465 uses implicit TLS, anything else STARTTLS.

A failed send (wrong password, network) never breaks the approval: the message lands in the outbox with the error.
