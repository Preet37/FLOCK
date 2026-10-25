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
