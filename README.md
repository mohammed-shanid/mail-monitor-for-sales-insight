# Regency Founder Enquiry Monitor — V1

A small, deterministic system that monitors one company mailbox
(`enquiry@regencyelectrical.com`), tracks customer enquiries, and lets
the founder ask about them over WhatsApp in plain English.

```
Gmail (read-only) -> Python backend -> Claude (only where NLU is genuinely needed)
                            |
                         SQLite
                            |
                  WhatsApp Business Cloud API -> Founder
```

Design philosophy, in order: **correctness, simplicity, deterministic
business logic, security, easy debugging, minimal dependencies.**
Counting, filtering, and date-range logic are always plain SQL/Python
— Claude is only used where natural-language understanding is
genuinely required (see "AI usage" below).

## Status

Being built in phases (see "Development phases"). **Phases 1–6 are
complete** and Phase 7 (end-to-end testing) is underway via a live
Telegram bot:
- Gmail OAuth + read-only message fetching/parsing (verified live
  against a real mailbox).
- Claude-based enquiry extraction and reply-intent classification, with
  strict JSON-schema-constrained responses, Python-side validation, and
  full error handling (malformed JSON, missing fields, rate limits,
  timeouts, connection errors) -- verified both with mocked responses
  and live against real emails.
- A Telegram bot (`@RegencyEnquiryBot`) is also connected and verified
  end-to-end (send + receive) as the V1 prototyping channel; WhatsApp
  Cloud API remains the target channel for the founder rollout (see
  "Key design decisions" below).
- SQLite schema (`emails`, `enquiries`) and repository functions
  (insert/lookup/update, all idempotent) in `app/database/`, backed by
  48 unit tests against temp-file databases.
- Full Gmail → Claude → SQLite ingestion (`app/gmail/sync.py`,
  `app/enquiry/status.py`) with the new/existing-thread branching rule
  and the status/sender business rules — verified live end-to-end
  against your real mailbox.
- The deterministic founder-question router (`app/router/intent.py`,
  `app/router/formatter.py`) and query functions
  (`app/database/queries.py`) — matches a fixed phrase to an intent,
  answers straight from SQLite, formats the reply. No Claude involved
  for any of it. Verified live against your real database.
- A live Telegram bot (`app/telegram/`, `app/router/open_ended.py`) —
  `scripts/run_telegram_bot.py` long-polls Telegram, routes fixed
  questions to SQLite and open-ended "what happened with X" questions
  to Claude (grounded in that enquiry's real stored email thread,
  never invented), and replies. This is `@RegencyEnquiryBot` — the V1
  prototyping channel; WhatsApp Business Cloud API remains the target
  for the founder rollout.

## Project structure

```
app/
  config.py          # env var loading
  gmail/
    auth.py           # OAuth (read-only), token caching/refresh
    client.py         # list/get/parse Gmail messages
    sync.py            # incremental Gmail -> SQLite ingestion
  ai/
    claude.py           # the ONLY module that calls Claude
  database/
    schema.py            # emails/enquiries table definitions
    queries.py            # repository + founder-query functions
  enquiry/
    models.py             # Status/LastSender constants, dataclasses
    status.py              # business rules: sender/status transitions
  router/
    intent.py               # fixed-phrase matching, route_message()
    formatter.py             # query results -> reply text
    open_ended.py             # customer lookup -> Claude summary
  telegram/
    client.py                 # Bot API (send/receive), stdlib urllib
    bot.py                     # long-polling loop
  whatsapp/                   # (future) webhook + send client, once
                               # Meta Business verification is in place
scripts/
  sync_gmail.py        # Gmail -> Claude -> SQLite ingestion CLI
  run_telegram_bot.py   # Telegram long-polling bot CLI
tests/
data/
  regency.db           # SQLite file (created at runtime; gitignored)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values as later phases need them
```

### Gmail OAuth (read-only)

The app only ever requests
`https://www.googleapis.com/auth/gmail.readonly` — it can list and
read messages, never send, modify, or delete anything, and it never
stores the mailbox password.

To connect a real mailbox once company credentials are available:

1. In [Google Cloud Console](https://console.cloud.google.com/), create
   (or reuse) a project and enable the **Gmail API**.
2. Under **APIs & Services > Credentials**, create an OAuth client ID
   of type **Desktop app**. Download it as `credentials.json` and
   place it in the project root (or set `GOOGLE_CREDENTIALS_FILE` in
   `.env` to point elsewhere).
3. Run `python scripts/sync_gmail.py`. A browser window opens asking
   the mailbox owner (`enquiry@regencyelectrical.com`) to sign in and
   grant read-only access.
4. On success, a `token.json` is cached next to the project so future
   runs skip the browser step until the token expires or is revoked.

`credentials.json` and `token.json` are both gitignored — never commit
either.

**No credentials are required to run the test suite** — all Gmail
tests use fixture payloads and mocks.

### Telegram bot (V1 prototyping channel)

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send
   `/newbot`, follow the prompts. It replies with a token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Message your new bot once (bots can't message first).
4. Run `python scripts/run_telegram_bot.py`. It long-polls Telegram
   forever (Ctrl+C to stop) — no public HTTPS webhook needed for this.

`TELEGRAM_BOT_TOKEN` is gitignored via `.env`; never commit it.

## Running tests

```bash
source .venv/bin/activate
pytest
```

## Development phases

Implemented in this order; each phase is tested before the next
external integration is added.

1. **Gmail foundation** — OAuth, read messages, parse
   thread/message id, headers, body. ✅ done
2. **Claude extraction** — sample emails -> validated JSON (enquiry
   detection, customer/product extraction, intent classification). ✅ done
3. **SQLite** — `emails` and `enquiries` tables, query functions. ✅ done
4. **Gmail ingestion** — combine 1–3 into incremental sync with the
   new-thread-vs-existing-thread branching rule. ✅ done
5. **Deterministic founder query layer** — count/filter functions
   used directly by the router (no Claude for counting). ✅ done
6. **Founder bot** — receive founder messages, route them, reply.
   ✅ done, on Telegram (`@RegencyEnquiryBot`) rather than WhatsApp —
   see "Key design decisions".
7. **End-to-end testing** — 🔄 underway live on Telegram. Swapping in
   WhatsApp Business Cloud API here means writing
   `app/whatsapp/webhook.py` + `client.py` against the same
   `app.router.intent.route_message()` that Telegram already uses —
   no router/database/Claude changes needed.
8. **Company integration** — connect the real
   `enquiry@regencyelectricals.com` mailbox (currently pointed at a
   personal test mailbox) and go live.

## Key design decisions / limitations

- **Two tables only** (`emails`, `enquiries`) — no customers,
  employees, quotations, or CRM tables in V1.
- **Thread continuity limitation**: an enquiry is identified by Gmail
  thread ID. If a customer starts a *new* email thread instead of
  replying to the existing one, Gmail assigns a new thread ID and V1
  will treat it as a second enquiry. This is an accepted V1
  limitation — no fuzzy duplicate detection yet. A later version could
  match on customer email + subject + content similarity.
- **Batch ordering**: Gmail's `messages.list` returns newest-first.
  `run_sync` sorts each fetched batch oldest-first before processing,
  so a thread's original message is always seen before its replies
  (otherwise the original would be misclassified as a "reply" on an
  "existing" thread, silently losing the real enquiry — this actually
  happened in a live test run and is why the sort exists). This only
  orders messages *within one fetched batch* — if an original message
  falls outside the batch window and only a later reply is fetched,
  that's the same class of edge case as the thread-continuity
  limitation above.
- **A new thread starting with an internal (employee-domain) message is
  auto-IGNORED without ever calling Claude** — a genuine new customer
  enquiry, by definition, starts with a message from outside the
  company. A live test run found an internal "kindly share pricing"
  email (one employee asking another, both on the company domain) get
  a false-positive `is_enquiry=True`, since Claude's extraction prompt
  has no idea who the sender was. Short-circuiting on sender domain
  before ever calling Claude fixes this deterministically and saves an
  API call. Known edge case: an employee genuinely forwarding an
  external enquiry as the *first* message of a thread would also be
  auto-ignored — rare enough to accept for V1.
- **Employee messages never auto-close an enquiry**, regardless of
  Claude's intent classification (e.g. an order-confirmation reply
  classified as ACCEPTED still results in `REPLIED`, not `CLOSED`) —
  only a customer's own ACCEPTED/REJECTED can close it. `QUOTATION_SENT`
  is detected with a deterministic keyword check on the employee's
  message body, not via Claude.
- **IGNORED enquiries never count** toward "how many enquiries" —
  `count_today`/`count_this_month`/`get_today_summary` all exclude
  them, so the founder's math holds (received == open + closed).
- **"Today" / "this month" are computed in UTC** (SQLite's `date('now')`),
  matching how timestamps are stored. For an India-based business this
  can be off by up to ~5.5 hours around local midnight — a known V1
  simplification; a later version could apply a configured display
  timezone. The same applies to the `Received: HH:MM AM/PM` times shown
  in enquiry lists — they're UTC, not IST.
- **Telegram long-poll timeouts must be caught as `OSError`, not
  `urllib.error.URLError`** — a `getUpdates` call that simply times out
  with no new messages (the normal, expected case) raises a bare
  `socket.timeout`, which urllib does *not* wrap in `URLError`. This
  actually crashed the whole bot process in a live test run before the
  fix in `app/telegram/client.py` — both are `OSError` subclasses, so
  catching `OSError` covers both.
- **"Show pending enquiries"** (the exact example phrase in this spec)
  is wired to the detailed listing (`GET_OPEN_DETAILS`), even though it
  isn't in section 19's illustrative alias list for that intent —
  needed to match the worked example in section 24. Separately, "which
  are open" and "pending enquiries" (no "show"/"list" verb) are wired
  to the plain count (`COUNT_OPEN`), per section 19's literal
  categorization, even though "which are open" reads like it wants a
  list.
- **Unanswered ≠ status.** An enquiry is "waiting on us" when
  `last_sender == "customer"`, not based on the status string —
  status and "who needs to act next" are tracked separately.
- **Claude is never used for counting, filtering, sorting, or
  date-range logic.** Those are always plain SQL/Python. Claude is
  used only for: is-this-an-enquiry detection, customer/product
  extraction, reply-intent classification (a fixed, controlled set of
  intents — never arbitrary strings), and open-ended
  summarization/Q&A about a specific enquiry.
- Every Claude JSON response is validated before it's written to
  SQLite; malformed/incomplete responses are logged and rejected
  rather than trusted.

## Environment variables

See `.env.example`. None are required for Phase 1 tests; Gmail OAuth
needs `GOOGLE_CREDENTIALS_FILE`/`GOOGLE_TOKEN_FILE` (or their defaults)
once you're testing against a real mailbox. `CLAUDE_API_KEY` and the
`WHATSAPP_*` variables are needed starting Phase 2 and Phase 6
respectively.

## Security notes

- Gmail scope is read-only; the mailbox password is never requested
  or stored.
- No WhatsApp Web/browser automation, cookies, or session scraping —
  only the official Meta WhatsApp Business Cloud API.
- Secrets live in `.env` / environment variables only, never in code
  or git (`.env`, `credentials.json`, `token.json` are all
  gitignored).
- The SQLite file is never exposed over the web.
