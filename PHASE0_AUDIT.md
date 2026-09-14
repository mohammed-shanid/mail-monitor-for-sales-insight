# PHASE0_AUDIT.md — Repository Audit

**Date:** 2026-09-14
**Scope:** Read-only audit per SPEC.md §4.1. No files other than this one were created or
modified. No repository or database state was changed. SQLite was opened with
`sqlite3 -readonly`. No secret values were read or printed.

---

## Headline

**This repository is a different product from the one SPEC.md describes.** What exists is
an interactive **Telegram Q&A bot** (a long-polling daemon) backed by an **incremental Gmail
→ Claude → SQLite sync script**, where enquiry status is a mutable state-machine flag. What
SPEC.md describes is a **one-shot, windowed, reproducible report generator** where state is
a pure function of a hydrated thread. The foundations overlap (Gmail read-only OAuth, MIME
parsing, an `anthropic` JSON-schema call path, a stdlib Telegram sender, two SQLite tables,
177 passing tests) and are worth keeping. The *pipeline* — `sync.py` → `status.py` →
`queries.py` mutation → `router/` — does not map onto SPEC.md and cannot be edited into it
incrementally.

Nine of the ten Appendix A non-negotiables are currently unmet (see §13). The one that is
met — SENT mail is fetched — is met by accident (no query filter at all), and is the
easiest to break when a date-window query is introduced.

---

# A. Repository

## 1. Complete file map

Line counts from `wc -l`. Everything is Python 3 unless noted.

### Root

| Path | Lines | Purpose | Notes |
|---|---|---|---|
| `CLAUDE.md` | 70 | Working rules for Claude Code. Markdown. | **Untracked.** References `app/database/repo.py`, which does not exist (it is `queries.py`). |
| `SPEC.md` | 1013 | Authoritative spec. Markdown. | **Untracked.** |
| `README.md` | 262 | Describes the *bot* product, phases 1–8, design decisions. Markdown. | Out of date relative to SPEC.md: names the mailbox as `enquiry@regencyelectrical.com` (note missing "s"), calls WhatsApp the "target channel", documents UTC-only behaviour as accepted. Contains several valuable live-run lessons (batch ordering, internal-first-message, socket.timeout). |
| `requirements.txt` | 8 | Unpinned dependency list. | See §11. |
| `.gitignore` | 8 | | See §2. |
| `.env` | 27 | Local secrets. **Not read.** Keys only enumerated (§2). | Gitignored. |
| `.env.example` | 31 | Template with empty values. | Keys do not match SPEC.md §19. |
| `credentials.json` | 1 | Google OAuth "installed app" client. Structure only inspected: `installed.{client_id, project_id, auth_uri, token_uri, auth_provider_x509_cert_url, client_secret, redirect_uris}`. | Gitignored. |
| `token.json` | 1 | Cached OAuth user token. Keys: `token, refresh_token, token_uri, client_id, client_secret, scopes, universe_domain, account, expiry`. Scope is exactly `gmail.readonly`. `expiry` is 2026-09-12 (expired; refresh token present). | Gitignored. |
| `data/.gitkeep` | 0 | Keeps `data/` in git. | |
| `data/regency.db` | — | Live SQLite file, 61 KB, 10 emails / 8 enquiries. | Gitignored. Schema in §7. |
| `.venv/` | — | Python 3.9.6 virtualenv. | Gitignored. |
| `.pytest_cache/` | — | | Gitignored. No last-failed entries. |

### `app/` — application package

| Path | Lines | Purpose | Assessment |
|---|---|---|---|
| `app/__init__.py` | 0 | package marker | |
| `app/config.py` | 52 | `load_dotenv()` then plain module constants: `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `GMAIL_SCOPES`, `DATABASE_PATH`, `COMPANY_EMAIL_DOMAIN`, `CLAUDE_API_KEY`, `WHATSAPP_*` (3), `TELEGRAM_BOT_TOKEN`. | No validation, no fail-fast. `WHATSAPP_*` is dead (never imported anywhere). Env var names differ from SPEC.md §19. |
| `app/gmail/__init__.py` | 0 | | |
| `app/gmail/auth.py` | 94 | OAuth: load cached token → refresh → interactive `InstalledAppFlow.run_local_server`. Writes token file. `get_gmail_service()` builds the Gmail client. | Solid. Read-only scope. Never logs token contents. Keep. |
| `app/gmail/client.py` | 215 | `EmailMessage` dataclass; `extract_body()` (MIME walk, text/plain > text/html, stdlib `HTMLParser` strip, 20 000-char cap); `parse_message()`; `list_message_ids()` (single `messages.list` page); `get_raw_message()` (`messages.get format=full`); `fetch_messages()` (N+1 loop). | Pure parsing functions are good and tested. **Uses `Date:` header** for `received_at`; **no pagination**; **no `internalDate`**; **no threads.get**; no batch; no Cc capture; no attachment metadata; API errors return `[]`/`None` (silent partial fetch). |
| `app/gmail/sync.py` | 265 | The bot's ingestion pipeline: fetch N most recent → sort by `Date:` header → per message: skip if `processed=1`, insert email, classify sender by domain, branch new-thread (Claude `extract_new_enquiry`) vs existing-thread (Claude `classify_reply_intent`) → mutate `enquiries` row. | Well-commented and works for the bot. **Architecturally opposite to SPEC.md** (mutation, `processed` skip, no hydration, Claude un-cached). See §15 for the rewrite recommendation. |
| `app/ai/__init__.py` | 0 | | |
| `app/ai/claude.py` | 343 | The only Claude caller. `_request()` (SDK call, `output_config={"effort":"low","format":{json_schema}}`, all SDK exceptions → `None`), `_call_claude()` (JSON parse + object check), `extract_new_enquiry()`, `classify_reply_intent()`, `summarize_enquiry()` (free text over raw thread). Model hardcoded `claude-opus-5`. | The plumbing (`_request`, `_call_claude`, `_response_text`, validation style) is reusable. No `temperature`, no retries, no cache, no prompt versioning, prompts inline. `quantity` is typed **integer**. `summarize_enquiry` sends raw email bodies to produce prose — the bot feature, not SPEC §15.4. |
| `app/database/__init__.py` | 0 | | |
| `app/database/db.py` | 52 | `get_connection()` context manager: mkdir parent, `sqlite3.connect`, `Row` factory, `busy_timeout=5000`, `foreign_keys=ON`, `init_db()`, commit/rollback/close. | Good. No WAL. Calls `init_db` on every connection (fine — idempotent). |
| `app/database/schema.py` | 80 | DDL for `emails` and `enquiries` + 2 indexes. `init_db()` runs `CREATE ... IF NOT EXISTS`. | No `schema_meta`, no migrations, no backup. Timestamps are ISO-8601 **TEXT**. |
| `app/database/queries.py` | 425 | All SQL. Write repo (`insert_email`, `mark_email_processed`, `create_enquiry`, `update_enquiry_activity`, `close_enquiry`) + read repo (`get_email_by_message_id`, `get_enquiry_by_thread`, `get_emails_by_thread`) + **founder-query layer** (`count_today`, `count_this_month`, `count_open`, `count_closed`, `get_today_summary`, `get_unanswered`, `get_today_enquiries`, `get_open_enquiries`, `get_enquiry_by_customer`). | SQL-only-here discipline is respected (verified by grep — no SQL outside `app/database/`). Founder-query half uses SQLite `date('now')` (UTC, uncontrollable clock) — bot-only. `now_iso()` uses `datetime.now()` inside the data layer. |
| `app/enquiry/__init__.py` | 0 | | |
| `app/enquiry/models.py` | 79 | `Status` (NEW/REPLIED/QUOTATION_SENT/CUSTOMER_REPLIED/CLOSED/IGNORED), `LastSender` (customer/employee/other), `Email` and `Enquiry` dataclasses mirroring the tables. | Status vocabulary is not SPEC's. `quantity: Optional[int]`. |
| `app/enquiry/status.py` | 125 | `extract_email_address()`, `classify_sender(from, company_domain)`, `is_quotation_message()` (keyword match), `next_status_for_message(sender_type, intent, body, current_status)` — a state-machine step. | `extract_email_address` and `classify_sender` are reusable. The transition rules **contradict SPEC §8** on four points (see §13). Pure-function style is right; the function's *shape* (`current_status` in → next status out) is the mutation model SPEC forbids. |
| `app/router/__init__.py` | 0 | | |
| `app/router/intent.py` | 221 | Fixed-phrase founder-question matcher → SQL → formatter; `route_message()` falls through to open-ended. | **Bot-only.** Out of SPEC scope. No overlap with the report generator. |
| `app/router/formatter.py` | 85 | `format_elapsed()`, `format_count()`, `format_today_summary()`, `format_enquiry_list()`. | Bot-only. Uses `datetime.now()` default and shows UTC times. Not reusable for SPEC §21.3 format. |
| `app/router/open_ended.py` | 117 | Regex lead-in strip → `get_enquiry_by_customer` → `summarize_enquiry` over raw stored thread. | Bot-only. Sends raw email to Claude for prose (SPEC §15.1 forbids this *for the report summary*; irrelevant to bot). |
| `app/telegram/__init__.py` | 0 | | |
| `app/telegram/client.py` | 80 | Stdlib `urllib` Bot API: `_call()` (checks `ok`, raises `TelegramError`, never leaks token), `get_updates()` (long-poll), `send_message(chat_id, text)`. | `send_message` is plain text, **no `parse_mode`** — correct per §16.3. Success is only reported if the API returned `ok:true` — correct per §16.4. No chunking, no retries, no `retry_after`, no `DeliveryResult`, chat id is a call argument (comes from the inbound update; there is no `TELEGRAM_CHAT_ID` config). |
| `app/telegram/bot.py` | 73 | Long-polling loop → `route_message` → `send_message`. | **A daemon** — SPEC §3 non-goal. Bot-only. |

### `scripts/`

| Path | Lines | Purpose | Assessment |
|---|---|---|---|
| `scripts/__init__.py` | 0 | | |
| `scripts/sync_gmail.py` | 53 | CLI: `python scripts/sync_gmail.py [max_results]` → `run_sync`. Exit 1 on auth failure. | Current ingestion entry point. Positional arg only. |
| `scripts/run_telegram_bot.py` | 48 | CLI: starts the polling loop forever. | Daemon entry point. Bot-only. |

### `tests/` — 177 tests, all pass (§10)

| Path | Tests | Covers | Relevance to SPEC |
|---|---|---|---|
| `tests/test_claude.py` | 22 | `_client` patched; happy paths, validation rejections, SDK exception classes (uses `httpx.Request` to build them). | Pattern reusable. Asserts `quantity=int`. |
| `tests/test_database.py` | 16 | Table creation, idempotent insert, update/close, rollback. | Reusable, will need extension. |
| `tests/test_enquiry_status.py` | 28 | `classify_sender`, `extract_email_address`, quotation keywords, transition table incl. "CLOSED is terminal", "employee never closes", "REJECTED closes". | Half encode rules SPEC overturns. |
| `tests/test_formatter.py` | 11 | Bot reply formatting. | Bot-only. |
| `tests/test_founder_queries.py` | 16 | `date('now')`-based counts. | Bot-only. |
| `tests/test_gmail_auth.py` | 2 | Missing credentials / corrupt token paths. | Reusable. No expired-token/refresh test. |
| `tests/test_gmail_client.py` | 13 | `extract_body` MIME variants, `parse_message`, HttpError → empty. | Reusable; fixtures are inline dicts, no `tests/fixtures/`. |
| `tests/test_gmail_sync.py` | 14 | New/existing/ignored/retry/idempotent/ordering paths of `sync.py`. | Will be obsoleted with `sync.py`. |
| `tests/test_intent.py` | 35 | Phrase matching, routing. | Bot-only. |
| `tests/test_open_ended.py` | 7 | Customer lookup → Claude. | Bot-only. |
| `tests/test_telegram_bot.py` | 6 | Polling loop. | Bot-only. |
| `tests/test_telegram_client.py` | 7 | `_call` success/ok:false/network/socket.timeout. | Reusable. |

No `tests/conftest.py`; the `db_path` fixture is duplicated in 5 test files.

### Dead code / abandoned / duplicate flags

- **`WHATSAPP_*` config** (`config.py`, `.env`, `.env.example`, README): never imported by any module. Dead. Also a named SPEC §3 non-goal.
- **The entire bot surface** — `app/router/*`, `app/telegram/bot.py`, `scripts/run_telegram_bot.py`, `app/ai/claude.py::summarize_enquiry`, the founder-query half of `queries.py`, `formatter.py` — is live and tested but **orthogonal to SPEC.md**, and one piece (`bot.py`) is a daemon SPEC forbids. Whether this is "dead" depends on a decision I need from you (§16, Q1).
- **`email_exists()` in `queries.py`** duplicates `get_email_by_message_id(...) is not None`. Minor.
- **`db_path` fixture** duplicated across 5 test files. Minor.
- **Two timestamp paths for the same fact:** `sync.py::_to_iso` (Date header → ISO) and `queries.py::now_iso` fallback. Minor, superseded by `internalDate`.
- **No duplicate implementations of any capability** were found — each concern has exactly one home today. That is a good starting point.

## 2. Git state

- **Git repo:** yes. Branch `main`, single commit `f56c2f2` ("first commit", 2026-09-14). No remotes, no other branches.
- **Working tree:** clean except two **untracked** files: `CLAUDE.md`, `SPEC.md`.
- **Tracked sensitive files:** none. `git ls-files` confirms `.env`, `credentials.json`, `token.json`, `data/regency.db` are **not** tracked; `git check-ignore` confirms each is matched by `.gitignore`.
- **`.gitignore` today:**
  ```
  .venv/
  __pycache__/
  .pytest_cache/
  credentials.json
  token.json
  .env
  *.pyc
  data/*.db
  ```
  **Missing vs SPEC §19:** `*.db-wal`, `*.db-shm`, `*.bak.*`, `logs/`, and `*.db` is only ignored under `data/` (SPEC's default `DATABASE_PATH=regency.db` is at repo root and would *not* be ignored).
- **`logs/`** does not exist.
- **Env var names present in `.env`** (values not read): `CLAUDE_API_KEY`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `TELEGRAM_BOT_TOKEN`, `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_TOKEN_FILE`, `DATABASE_PATH`, `COMPANY_EMAIL_DOMAIN`. Nine keys. SPEC §19 requires ~22, with different names for five of these.

## 3. Runtime baseline

- **Python:** 3.9.6 (both system `python3` and `.venv`). Python 3.9 is past EOL; `google-auth` and `google-api-core` emit `FutureWarning`s about it. `zoneinfo` is stdlib in 3.9 and `ZoneInfo("Asia/Kolkata")` resolves correctly on this machine (verified). Note for implementation: no `datetime.UTC` alias (3.11+), and PEP 604 `X | None` only works under `from __future__ import annotations` (already used everywhere).
- **Invocation today:** `python scripts/sync_gmail.py [N]` (ingest N most recent messages) and `python scripts/run_telegram_bot.py` (daemon). There is **no `report.py`** and no windowed/report mode of any kind.
- **Does it run?** All modules import cleanly (`import app.config, app.gmail.sync, app.telegram.bot, app.router.intent` → ok). The test suite passes. I did not execute either script (both perform network I/O and `sync_gmail.py` writes to the DB and could trigger the OAuth browser flow because the cached token has expired). The live DB shows a successful sync ran on 2026-09-12, so the sync path evidently worked then.
- **Installed but undeclared:** `httpx` (imported by `tests/test_claude.py`; transitive via `anthropic`), `requests` (needed by `google.auth.transport.requests`; transitive via `google-auth`). See §11.

---

# B. Existing subsystems

## 4. Gmail integration

**Files:** `app/gmail/auth.py`, `app/gmail/client.py`, `app/gmail/sync.py`, `app/config.py`.

| Aspect | Current state |
|---|---|
| Auth flow | Cached token file → `Credentials.from_authorized_user_file` → if expired & refresh_token: `creds.refresh()` → on any failure fall back to interactive `InstalledAppFlow.run_local_server(port=0)` → save token. `GmailAuthError` only if `credentials.json` is missing. |
| Scopes | `["https://www.googleapis.com/auth/gmail.readonly"]` — exactly SPEC §5.1. The live `token.json` confirms this scope and nothing wider. |
| Credential paths | `GOOGLE_CREDENTIALS_FILE` (default `credentials.json`), `GOOGLE_TOKEN_FILE` (default `token.json`). SPEC names them `GMAIL_CREDENTIALS_PATH` / `GMAIL_TOKEN_PATH`. |
| Query construction | **None.** `fetch_messages(service, max_results=25)` calls `messages.list(userId="me", maxResults=N, q=None)`. No `after:`/`before:`, no label filter. |
| SENT mail | **Fetched today — by default, not by design.** With no `q` and no `labelIds`, `messages.list` returns everything except SPAM/TRASH, which includes SENT (and DRAFT). The live DB contains 5 messages from the company domain inside customer threads, confirming SENT is ingested. This is Appendix A #1 satisfied by accident; adding `in:inbox` at any point would break it. |
| Pagination | **None.** One page, `maxResults` honoured, `nextPageToken` never read. Gmail caps a page at 500. |
| Timestamp | **`Date:` header** (`parse_message` → `received_at: Optional[str]` raw RFC 2822 → `sync._to_iso` → UTC ISO text). Falls back to `now_iso()` if unparseable — i.e. a bad header silently becomes "now". `internalDate` is never read. Appendix A #3 violated. |
| Body/MIME | `extract_body()`: recursive walk, prefers first `text/plain`, else `text/html` through a minimal `HTMLParser` (drops script/style), any other `text/*` as plain; URL-safe base64 with padding repair; 20 000-char cap. Tested for plain, html, alternative, mixed+attachment, empty, malformed base64. **No quoted-reply stripping, no signature stripping, no `gmail_quote` handling.** |
| Headers captured | `From`, `To`, `Subject`, `Date`. **No `Cc`**, no `Message-ID`, no `In-Reply-To`, no `Auto-Submitted`/`X-Autoreply` (needed for §8.2 auto-reply detection). |
| Attachments | Ignored entirely; no filename/MIME record (§5.4 wants metadata). |
| Thread hydration | **None.** Only listed messages are fetched; no `threads.get`. Appendix A #2 violated. |
| Batch | No; N+1 `messages.get` calls. |
| Error handling | `list_message_ids` returns `[]` and `get_raw_message` returns `None` on `HttpError` — a partial fetch is **silently accepted**. SPEC §17 requires abort. |
| Quality | Parsing is clean, tested, and worth keeping. Fetching is a prototype: fine for "last 25 messages", unusable for a windowed report. |

## 5. Claude integration

**File:** `app/ai/claude.py` (only caller; verified by grep of `anthropic` imports).

| Aspect | Current state |
|---|---|
| Client | `anthropic.Anthropic(api_key=CLAUDE_API_KEY)` constructed lazily per call. SDK 0.125.0 installed. |
| Model | Hardcoded `MODEL = "claude-opus-5"`. Not configurable. SPEC's `.env` template suggests `CLAUDE_MODEL=claude-sonnet-4-6`. |
| Temperature | **Not set.** Uses `output_config={"effort": "low"}` instead. SDK 0.125.0 supports both `temperature` and `output_config` (verified in `resources/messages/messages.py`). Appendix A #8 violated. |
| Structured output | `output_config.format = {"type":"json_schema","schema":...}`, then `json.loads` of the first text block, then hand-written type validation. This satisfies SPEC §15.2 "tool-use / JSON schema, never regex". Good. |
| Prompts | Three inline system-prompt strings. Not versioned. Not in a `prompts.py`. |
| Retry | **None.** Every SDK exception (`RateLimitError`, `APITimeoutError`, `APIConnectionError`, `APIStatusError`, `AnthropicError`) is logged and returns `None`. Callers treat `None` as "leave unprocessed, retry next run". |
| Caching | **None.** Every run that finds an unprocessed message pays again; the same message is never analysed twice only because `sync.py` sets `processed=1`. |
| What the model is asked | (a) `extract_new_enquiry(subject, body)` → `{is_enquiry, customer_name, customer_email, product, quantity:int|null}` on the **first message only** of a new thread. (b) `classify_reply_intent(body)` → one of `ACCEPTED/REJECTED/NEEDS_INFORMATION/NEGOTIATING/FOLLOW_UP/GENERAL_REPLY`, given the reply body with **no thread context**. (c) `summarize_enquiry(...)` → free prose from the raw thread (bot). |
| Numbers | The model **is** asked for one number: `quantity` as a JSON `integer`. SPEC §14.2/§15.3 make it TEXT ("250 nos"). It is never asked for counts, totals, or metrics. `summarize_enquiry` is unconstrained prose — no numeral guard — but it is the bot's feature, not the report summary. |
| Missing vs §15.3 | `company`, `counterparty_type`, `requirement`, `brands[]`, `quotation_signal`, `closure_signal/evidence`, `urgency/evidence`, `confidence`. |
| Tests | 22 tests mock `_client` and cover the failure taxonomy well. The pattern is directly reusable. |
| Quality | The transport/validation scaffold is good and should be extended, not replaced. The *contracts* (two separate prompts, per-message with no context, no cache) are wrong for SPEC. |

## 6. Telegram integration

**Files:** `app/telegram/client.py` (transport), `app/telegram/bot.py` (daemon), `scripts/run_telegram_bot.py`.

| Aspect | Current state |
|---|---|
| Send path | `send_message(chat_id, text)` → `_call("sendMessage", {chat_id, text}, timeout=15)` → `urllib.request` POST form-encoded. |
| `parse_mode` | **None** — plain text. Correct per §16.3 / Appendix A #7. |
| Chunking | **None.** A >4096-char text will get HTTP 400 from Telegram and raise `TelegramError`. |
| Retries | **None.** No 429/`retry_after`, no 5xx backoff. |
| Success/failure | `_call` raises `TelegramError` on network error, malformed JSON, or `ok: false`. Returns the `result` dict only when Telegram confirmed. Success is therefore never claimed without API confirmation — correct per §16.4. Bot token is never included in error text (the URL is formatted but not logged). |
| Chat id | Taken from the inbound update. **There is no `TELEGRAM_CHAT_ID` config.** For a push report the founder's chat id must be configured. |
| Enquiry knowledge | `client.py` has none — correct layering. `bot.py` imports `route_message` (business logic) — acceptable for a bot, but it is the daemon. |
| `DeliveryResult` | Does not exist. |
| Quality | `_call` is a good 30-line transport. The `OSError`-catches-`socket.timeout` lesson is documented and tested. Extending this file with `send(text) -> DeliveryResult` is straightforward. |

## 7. Database

**Location:** `data/regency.db` (from `DATABASE_PATH` default `BASE_DIR/data/regency.db`; `.env` also sets it — value not read). 61 440 bytes. Opened read-only.

**Full schema dump (`sqlite3 -readonly data/regency.db .schema`):**

```sql
CREATE TABLE emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL UNIQUE,
    gmail_thread_id TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    processed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_emails_gmail_thread_id
    ON emails (gmail_thread_id)
;
CREATE TABLE enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_thread_id TEXT NOT NULL UNIQUE,
    customer_name TEXT,
    customer_email TEXT,
    subject TEXT NOT NULL DEFAULT '',
    product TEXT,
    quantity INTEGER,
    status TEXT NOT NULL DEFAULT 'NEW',
    last_sender TEXT NOT NULL DEFAULT 'customer',
    received_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX idx_enquiries_status ON enquiries (status)
;
```

**Indexes:** `sqlite_autoindex_emails_1` (UNIQUE on `gmail_message_id`), `idx_emails_gmail_thread_id`, `sqlite_autoindex_enquiries_1` (UNIQUE on `gmail_thread_id`), `idx_enquiries_status`.

**Constraints:** `emails.gmail_message_id` **is UNIQUE today** (10 rows, 10 distinct). `enquiries.gmail_thread_id` UNIQUE. No foreign keys. No CHECK constraints on `status`/`last_sender` (enforced in Python only).

**PRAGMAs (read):** `journal_mode = delete` (not WAL), `user_version = 0`, `foreign_keys = 0` at rest (set to ON per-connection by `db.py`).

**Row counts:** `emails` = 10, `enquiries` = 8.

**Data shape (no PII printed):**
- `emails.received_at` is ISO-8601 text, e.g. `2026-09-11T05:44:02+00:00`; all 10 rows `processed = 1`; all dated 11–12 Sep 2026.
- Sender domains present: `gmail.com` (2), `regencyelectricals.com` (5), `email.openai.com`, `google.com`, `accounts.google.com` — consistent with README's "currently pointed at a personal test mailbox".
- 8 threads; two have 2 messages each, six have 1.
- `enquiries.status`: `IGNORED`×6 (3 with `last_sender=customer`, 3 `employee`), `REPLIED`×2. **Zero rows with status NEW/CLOSED/QUOTATION_SENT/CUSTOMER_REPLIED.** `quantity` and `product` are NULL in all 8; `customer_name` set in 2.
- Referential integrity holds: every enquiry thread has emails and vice versa.

**What SPEC §14 would lose or invalidate:**

| Item | Impact |
|---|---|
| `emails.received_at TEXT` (ISO) vs SPEC `INTEGER` epoch ms | **Cannot be changed additively** — SQLite `ALTER TABLE` cannot alter a column type. Options are a parallel column or a backed-up table rebuild. All 10 existing values are re-derivable from Gmail (`internalDate`) on the next ingest. |
| `emails` missing `sender_domain, direction, is_auto_reply, has_attachments, ingested_at` | `ADD COLUMN ... NOT NULL DEFAULT x` works. But `direction` would be defaulted (wrong) for the 10 existing rows unless backfilled from `sender` vs config. |
| `enquiries.status` vocabulary `NEW/REPLIED/QUOTATION_SENT/CUSTOMER_REPLIED/CLOSED/IGNORED` vs `new/pending/attended/closed` | The 8 existing rows carry values SPEC does not define. `IGNORED` has no SPEC equivalent (SPEC's model: non-enquiry threads simply have no `enquiries` row; the verdict lives in `ai_verdicts`). Under recompute-every-run these rows would be overwritten or orphaned anyway. |
| `enquiries.quantity INTEGER` vs SPEC `TEXT` | SQLite affinity: storing `"20 nos"` in an INTEGER column keeps it text, but `"20"` is coerced to integer `20`. Reads become mixed-type. Existing values are all NULL, so nothing is lost. |
| `enquiries.received_at / last_activity_at / closed_at TEXT` vs `INTEGER` | Same as emails. |
| `enquiries` missing `mailbox NOT NULL` | `ADD COLUMN mailbox TEXT NOT NULL DEFAULT ''` is the only additive form; SPEC intends a real value. |
| `enquiries.last_sender NOT NULL DEFAULT 'customer'` with value `other` in the vocabulary | SPEC has `customer|employee`, nullable. |
| Missing tables `schema_meta, enquiry_brands, ai_verdicts, report_runs` | Pure additions. No loss. |
| `PRAGMA journal_mode=WAL` | Changes on-disk mode; creates `-wal`/`-shm` files. Not currently gitignored. |

**Net assessment:** the live DB holds two days of throw-away data from a test mailbox, every row of which Gmail can re-supply. The honest recommendation is a backed-up rebuild rather than contorting the SPEC schema around `TEXT` timestamps — but that is your call (§16, Q3).

## 8. Enquiry-processing logic

**Files:** `app/gmail/sync.py`, `app/enquiry/status.py`, `app/enquiry/models.py`, `app/database/queries.py`.

| Question | Current answer |
|---|---|
| How enquiries are identified | On the **first message of a new thread only**: if sender domain == `COMPANY_EMAIL_DOMAIN` → row created with `IGNORED`, no Claude call. Otherwise Claude `extract_new_enquiry(subject, body)` → `NEW` if `is_enquiry` else `IGNORED`. Later messages never revisit `is_enquiry`. An `IGNORED` thread stays ignored forever, and an outbound-first thread that a customer later replies to with demand (§7) is permanently `IGNORED`. |
| Thread grouping | By `gmail_thread_id` — one `enquiries` row per thread (UNIQUE). Matches §6.3. But messages of a thread are only those that happened to be in the fetched batch; there is no hydration. |
| Status determination | **Mutated in place.** `next_status_for_message(sender_type, intent, body, current_status)` returns the next state; `sync.py` calls `update_enquiry_activity`/`close_enquiry`. Rules: CLOSED is terminal; employee message → `QUOTATION_SENT` (keyword) or `REPLIED`; customer `ACCEPTED`/`REJECTED` → CLOSED; other customer intents → `CUSTOMER_REPLIED`; `OTHER` sender → unchanged. |
| Attended / pending | Not concepts in the code. The closest is the bot's "unanswered" = `last_sender == customer AND status NOT IN (CLOSED, IGNORED)`, and `last_sender` is simply the sender of the last processed message (auto-replies included). |
| Closed | Only by customer `ACCEPTED` or `REJECTED` intent. Employee messages never close. `closed_at` = that message's timestamp. Never reopens. |
| Brands | **Not handled at all.** No extraction, no table, no normalisation. |
| Quotations | `is_quotation_message()` keyword match on employee body → status `QUOTATION_SENT`. No RFQ detection, no response detection, no supplier concept, no per-message flags, no counts. |
| Counterparty type | Not a concept. `classify_sender` gives customer/employee/other by domain only. "Employee" and "internal" are the same thing (`COMPANY_EMAIL_DOMAIN`). |
| Direction | Implicit in `sender_type`; not stored on `emails`. |
| As-of / windows | Not a concept. Everything is "latest state now". |
| State mutated or recomputed? | **Mutated.** `processed = 1` causes `sync_message` to return early — exactly Appendix A #4. |
| Clock injection | `now_iso()` and `date('now')` are called inside the data layer; `format_elapsed` defaults to `datetime.now()`. Not injectable except in the formatter. |

**Reusable pieces:** `classify_sender` (needs to grow employee-vs-internal distinction and alias list), `extract_email_address`, the pure-function discipline, the "internal-first-message" and "sort oldest-first" lessons.

## 9. CLI and reporting

- **Entry points:** `scripts/sync_gmail.py [max_results:int]`, `scripts/run_telegram_bot.py` (no args). No `argparse` anywhere. No `report.py`.
- **Arguments supported:** one positional integer. Nothing else.
- **Date/timezone handling:** none. All timestamps UTC. Bot's "today"/"this month" via SQLite `date('now')` (UTC). Displayed times are UTC formatted as `%I:%M %p` with no zone label. README documents this as a known simplification.
- **Report rendering:** only the bot's ad-hoc reply strings in `router/formatter.py`. Nothing resembling §21.3.
- **Terminal output:** `logging.basicConfig(level=INFO)` to stderr plus one `print` line. Full INFO logs include thread ids and "Claude extraction successful" lines; no secrets are logged (verified by reading every `logger.*` call). No log file.
- **Exit codes:** `SystemExit(1)` on auth failure / missing token; 0 otherwise. No 2/3/4/5/6.
- **`--dry-run`:** does not exist.

## 10. Tests

- **Command:** `.venv/bin/python -m pytest -q` (or `pytest` with the venv activated).
- **Result (real output, 2026-09-14):**
  ```
  ........................................................................ [ 40%]
  ........................................................................ [ 81%]
  .................................                                        [100%]
  177 passed, 4 warnings in 3.07s
  ```
  The 4 warnings are Python-3.9-EOL notices from `google-auth`/`google-api-core` and a `urllib3` LibreSSL notice. No failures, no skips.
- **What is covered:** see the table in §1. Roughly 100 tests cover reusable foundations (Claude transport, DB repo, Gmail parsing, Telegram transport, sender classification); ~75 cover bot-only surface; ~20 (`test_gmail_sync`, half of `test_enquiry_status`) assert behaviour SPEC overturns.
- **Zero network:** yes — Claude via `patch("app.ai.claude._client")`, Gmail via `monkeypatch`/`MagicMock`, Telegram via patched `urlopen`.
- **Gaps vs SPEC §20:** no `tests/fixtures/` with Gmail API JSON; no timewindow tests (nothing to test yet); no pagination, hydration, internalDate, quoted-text, idempotency-twice, cache-hit, invariant, or chunking tests; no `conftest.py`.

## 11. Dependencies

**Declared (`requirements.txt`, unpinned):** `google-api-python-client`, `google-auth-httplib2`, `google-auth-oauthlib`, `python-dotenv`, `anthropic`, `pytest` (dev). No `pyproject.toml`, no lockfile.

**Installed in `.venv`:** anthropic 0.125.0, google-api-python-client 2.198.0, google-auth 2.50.0, google-auth-oauthlib 1.3.1, google-auth-httplib2 0.3.1, python-dotenv 1.2.1, pytest 8.4.2, plus transitive (httpx, requests, pydantic, …).

**Actually imported (third-party) across `app/`, `scripts/`, `tests/`:** `anthropic`, `dotenv`, `googleapiclient` (from google-api-python-client), `google.auth` / `google.oauth2` (google-auth), `google_auth_oauthlib`, `pytest`, `httpx` (tests only).

| Finding | Detail |
|---|---|
| Declared but unused | **None.** `google-auth-httplib2` is not imported directly but is required by `googleapiclient.discovery.build` — keep. |
| Imported but undeclared | `httpx` (`tests/test_claude.py`) — arrives transitively via `anthropic`. `requests` — arrives transitively via `google-auth`; used implicitly by `google.auth.transport.requests`. Both are safe today but not pinned. |
| SPEC needs | Nothing new. `zoneinfo`, `argparse`, `logging.handlers.RotatingFileHandler`, `sqlite3`, `urllib` are stdlib. No `tzdata` needed on macOS (system tz database present; verified). |
| Recommendation | Pin versions in `requirements.txt` (at least `anthropic`, since `output_config` semantics are version-sensitive). Not a SPEC requirement; flagging only. |

---

# C. Gap analysis

## 12. Already satisfies SPEC.md — keep as-is

| Item | SPEC § | Where |
|---|---|---|
| Gmail scope exactly `gmail.readonly`; live token confirms | §5.1, §19 | `config.py`, `auth.py`, `token.json` |
| `credentials.json` / `token.json` flow, refresh-then-reauth, token never logged | §5.1 | `auth.py` |
| MIME walk, text/plain preferred, HTML → text fallback, base64 padding repair | §5.4 (first bullet) | `client.py::extract_body` |
| `gmail_message_id UNIQUE`, `gmail_thread_id` as thread key, one enquiry row per thread | §6.3, §14.2, §18 | `schema.py` |
| All SQL in one module; no SQL elsewhere (grep-verified) | §4.2, CLAUDE.md | `queries.py` |
| Claude structured output via JSON schema + Python re-validation; controlled enums; `null` allowed for every extracted field | §15.2, §15.3 | `claude.py` |
| Claude never asked to count; SDK exceptions handled by class | §15.1 | `claude.py` |
| Telegram plain text, no `parse_mode`; success only on `ok:true`; token never in error text; stdlib only | §16.3, §16.4, §19 | `telegram/client.py` |
| Secrets via `.env` + `python-dotenv`; `.env`, `credentials.json`, `token.json`, `data/*.db` gitignored and untracked | §19 | `.gitignore` |
| `PRAGMA foreign_keys = ON`, `busy_timeout`, commit/rollback context manager | §14.1 | `db.py` |
| Direction-by-domain is deterministic Python, never AI | §6.1 | `status.py::classify_sender` |
| "Internal-first-message thread is never an enquiry" rule | §6.2 (3) | `sync.py::_handle_new_thread` |
| Customer "thanks" does not close | §8.2 | `status.py` (`GENERAL_REPLY → CUSTOMER_REPLIED`) |
| Tests mock all three externals; zero network | §20.1 | `tests/` |
| No server, no framework, no ORM, no Docker, no Postgres | §3 | (absence) |

## 13. Conflicts with SPEC.md — by severity

### Critical — would produce silently wrong output (Appendix A)

| # | Conflict | SPEC § | Where |
|---|---|---|---|
| C1 | **Timestamps from `Date:` header**, stored as ISO text, unparseable header → `now()`. | §5.3, §12, App. A #3 | `client.py:158`, `sync.py:78-89` |
| C2 | **No thread hydration.** State is computed from whichever messages fell in the fetched batch. | §5.3, App. A #2 | `sync.py::run_sync` |
| C3 | **State mutated in place; `processed=1` skips the message wholesale.** A reply that failed once and later succeeds, or a re-run over history, cannot correct status. | §8.1, §18, App. A #4 | `sync.py:210-213`, `queries.py::update_enquiry_activity/close_enquiry` |
| C4 | **No pagination.** `maxResults` single page; a window with >500 messages (or >N) is silently truncated. | §5.3 | `client.py::list_message_ids` |
| C5 | **Partial fetch silently accepted** — `HttpError` → `[]`/`None`, run continues and reports on what it has. | §17 "Partial Gmail fetch" | `client.py:176-178, 195-197` |
| C6 | **No quoted-reply / signature stripping** — every reply's body carries the full quoted chain to Claude and DB. | §5.4 | `client.py::extract_body` |
| C7 | **`is_enquiry` decided once, from the first message only.** Outbound-first threads (§7) and "not an enquiry yet" threads are permanently `IGNORED`; a customer's later demand is never re-evaluated. | §7, §8.1 | `sync.py::_handle_new_thread` |
| C8 | **Auto-replies / out-of-office flip `last_sender`.** No detection at all. | §8.2 | `sync.py`, `status.py` |
| C9 | **Waiting time anchored to `now()`** and **"today" is UTC** — the only elapsed/period logic in the repo uses the live clock. | §11.3, §12, App. A #6 | `formatter.py:28`, `queries.py` (`date('now')`) |
| C10 | **No `temperature=0`, no verdict cache.** Uses `effort: low`; non-deterministic across runs. | §15.2, App. A #8 | `claude.py:133-144` |
| C11 | **SENT mail is included only because there is no query.** Not a defect today, but the moment `after:/before:` is added, an `in:inbox` slip re-creates App. A #1. Calling this out so the future query is written with `-in:` exclusions, never `in:inbox`. | §5.2, App. A #1 | `client.py::fetch_messages` |
| C12 | **Employee = internal.** `COMPANY_EMAIL_DOMAIN` makes every colleague an "employee", so a colleague's reply to a customer thread would count as *attended* by the mailbox owner. | §0 Glossary, §6.1, §6.2 | `status.py::classify_sender`, `config.py` |

### Major — violates an explicit rule or non-goal

| # | Conflict | SPEC § | Where |
|---|---|---|---|
| M1 | **Long-running daemon** (`run_polling_loop` forever). | §3 "No always-running worker … daemon" | `telegram/bot.py`, `scripts/run_telegram_bot.py` |
| M2 | **WhatsApp config** present (`WHATSAPP_*`). | §3 "No WhatsApp" | `config.py`, `.env`, `.env.example`, README |
| M3 | **CLOSED is terminal; no reopen on new inbound.** | §8.3 | `status.py:113-114` |
| M4 | **Employee can never close.** SPEC lists "Employee explicitly records closure or order booking" as valid closure evidence. | §8.2 | `status.py:116-117`, README |
| M5 | **`REJECTED` closes.** SPEC's closure list is acceptance/PO/proceed/employee-records-closure only. (Also a SPEC gap — see §14.) | §8.2 | `status.py:52` |
| M6 | **Quotation detection by keyword regex on employee body**, not Claude per-message signal; only one of the four quotation concepts exists. | §9 | `status.py::is_quotation_message` |
| M7 | **No brands** — no extraction, no `enquiry_brands` table, no normalisation. | §10, App. A #5 | (absent) |
| M8 | **`quantity` is JSON `integer` / SQL `INTEGER`.** "250 nos" cannot be captured. | §14.2, §15.3 | `claude.py:64`, `schema.py:49`, `models.py:74` |
| M9 | **Claude retries absent** (`AI_MAX_RETRIES`), no prompt versioning, prompts not in `prompts.py`, model hardcoded. | §15.2, §15.5 | `claude.py` |
| M10 | **Telegram: no chunking, no retries, no `DeliveryResult`, no `TELEGRAM_CHAT_ID`.** | §16.2, §16.4, App. A #7 | `telegram/client.py` |
| M11 | **No `report.py`, no reporting modes, no `--dry-run`, no exit-code contract, no argparse.** | §13, App. A #10 | (absent) |
| M12 | **Config has no validation / fail-fast**; five variable names differ from §19 (`CLAUDE_API_KEY`→`ANTHROPIC_API_KEY`, `GOOGLE_CREDENTIALS_FILE`→`GMAIL_CREDENTIALS_PATH`, `GOOGLE_TOKEN_FILE`→`GMAIL_TOKEN_PATH`, `COMPANY_EMAIL_DOMAIN`→`INTERNAL_DOMAINS`; `REPORT_MAILBOX` absent); ~13 SPEC variables absent. | §17, §19 | `config.py` |
| M13 | **Schema:** no `schema_meta`, no migrations, no backup step, no WAL, no `ai_verdicts`/`enquiry_brands`/`report_runs`, timestamp types, status vocabulary. | §14 | `schema.py` |
| M14 | **`.gitignore` missing** `*.db-wal`, `*.db-shm`, `*.bak.*`, `logs/`, root `*.db`. | §19 | `.gitignore` |
| M15 | **`datetime.now()` inside data/business layers** (`queries.now_iso`, SQL `date('now')`). | §12, §20.1, CLAUDE.md | `queries.py:25,298,308,345,381` |
| M16 | **No `Cc` captured**; `recipient` is the `To` header only. Counterparty set (§6.2) is incomplete. | §6, §6.2 | `client.py::parse_message` |
| M17 | **No attachment metadata** recorded. | §5.4 | `client.py` |
| M18 | **No log file**, no rotation, no `LOG_PATH`. | §17 | `scripts/*` |
| M19 | **Summary path sends raw email to Claude** (`summarize_enquiry`). Fine for the bot; must not be reused for §15.4. | §15.1, §15.4 | `claude.py:307-343` |

### Minor — cosmetic or structural

| # | Conflict | SPEC § | Where |
|---|---|---|---|
| m1 | Module names: `app/database/queries.py` (SPEC/CLAUDE.md: `repo.py`); `app/ai/claude.py` (SPEC: `ai/client.py`); `app/enquiry/` (SPEC: `analysis/`); `app/telegram/client.py` (SPEC: `delivery/telegram.py`). SPEC §4.2 itself says not to rename for cosmetic reasons. | §4.2 | — |
| m2 | `Status` constants uppercase (`NEW`) vs SPEC lowercase (`new`). | §8.3, §14.2 | `models.py` |
| m3 | `MAX_BODY_CHARS = 20_000` hardcoded vs `AI_BODY_MAX_CHARS` (4000, for the AI call only; full body stored). | §5.4 | `client.py:28` |
| m4 | README describes the bot product and the wrong mailbox / WhatsApp target. | §21.4 (docs) | `README.md` |
| m5 | `.env.example` has WhatsApp keys, lacks SPEC keys. | §19 | `.env.example` |
| m6 | No `tests/fixtures/`, no `conftest.py`; `db_path` fixture ×5. | §20.1 | `tests/` |
| m7 | `requirements.txt` unpinned. | — | |
| m8 | `email_exists()` duplicates `get_email_by_message_id`. | — | `queries.py:62` |

## 14. Where SPEC.md is wrong, impossible, or needlessly disruptive

These are the places I recommend amending SPEC.md **before** Stage 1. Ordered by how much they affect the plan.

**W1. §4.2 layout vs. the existing package names (contradiction with §4.2's own "do not rename" rule).**
The repo has `app/enquiry/`, `app/database/queries.py`, `app/ai/claude.py`, `app/telegram/client.py`. SPEC wants `analysis/`, `repo.py`, `ai/client.py`, `delivery/telegram.py`; CLAUDE.md hard-codes "All SQL lives in `app/database/repo.py`". Renaming `queries.py` touches 10 test files and every import for zero behavioural gain, and creating `delivery/telegram.py` beside `telegram/client.py` would be a duplicate implementation of `_call`. **Recommendation:** amend SPEC §4.2 and CLAUDE.md to the existing names — `app/enquiry/` stands in for `analysis/`, `queries.py` for `repo.py`, `ai/claude.py` for `ai/client.py`, `telegram/client.py` for `delivery/telegram.py` — and add the new files (`thread_state.py`, `brands.py`, `metrics.py`, `prompts.py`, `cache.py`, `timewindow.py`, `reporting/`) inside that layout. (Q2)

**W2. §11.2 invariant `Received = Attended + Pending` contradicts §8.3.**
§8.3 defines three mutually exclusive as-of states: `pending`, `attended`, `closed`. An enquiry received inside the window *and* closed inside the window is `closed` as of `window_end` — so it is neither attended nor pending, and `Received ≠ Attended + Pending`. Either (a) the invariant is `Received = Attended + Pending + Closed-of-received`, (b) closed enquiries are also counted as attended (closure implies an employee acted), or (c) `Closed` in the summary excludes received-in-window enquiries. §20.2 #32 asserts the invariant in tests, so this must be decided, not discovered. **My recommendation:** (b) — `Attended` = "not waiting on us" (attended ∪ closed) for the *received-in-window* cohort, and `Closed` stays the independent all-cohorts count with its "includes enquiries received earlier" label. This keeps the founder's mental model ("of the 24 that came in, 19 are handled, 5 are waiting"). (Q4)

**W3. §8.3 lists a `new` status that the pure function can never produce.**
`compute_state` yields `pending` (customer last), `attended` (employee last), or `closed`. There is no message configuration that yields `new` — a freshly received enquiry with no reply is `pending`. `new` appears in the `enquiries.status` comment and in `new → attended → closed`. **Recommendation:** drop `new` from §8.3/§14.2, or define it as "pending with zero employee messages ever" (a sub-state the renderer may use but the metrics do not). (Q5)

**W4. §8.2 closure evidence omits rejection.**
A customer saying "we have placed the order elsewhere" or "no longer required" is not acceptance, PO, proceed, or employee-recorded closure, so under SPEC it stays `pending` forever and appears in every future pending list with a growing waiting time. The existing code closes on `REJECTED`. **Recommendation:** add "counterparty explicitly withdraws or rejects" as closure evidence, with a `closure_kind: won | lost | withdrawn` on the verdict so the report can (later) distinguish. If you do *not* want lost enquiries closed, SPEC should say what state they occupy. (Q6)

**W5. §15.3 is a per-message schema but half its fields are thread-level facts.**
`is_enquiry`, `customer_name`, `company`, `product`, `requirement`, `quantity`, `brands` describe the *enquiry*; `quotation_signal`, `closure_signal`, `urgency` describe the *message*. A reply "ok, please send the quote" would return `is_enquiry` — true or false? Which message's `product` wins? Per-message caching (`ai_verdicts` PK) is right, but SPEC needs an aggregation rule. **Recommendation:** (1) the enquiry-level fields are taken from the **earliest inbound message whose verdict has `is_enquiry=true`**, with later messages allowed only to *add* brands (union) and fill `null`s; (2) the per-message prompt receives the preceding thread messages (cleaned, truncated) as *context* and is asked to judge the *target* message — this keeps the cache key valid because a message's predecessors never change. (Q7)

**W6. §15.4 numeral guard as written will reject valid summaries.**
"Extract all numerals and assert each appears in the metrics payload" — a summary saying "5 enquiries are still waiting as of 23:59" or "on 13 September" contains `23`, `59`, `13` that are not metric values. **Recommendation:** define the allowed set as every integer appearing in the metrics payload **plus** the window's day/month/year and the hour/minute of the window bounds, and compare integers (not digit substrings). Also state that a summary with *zero* numerals is acceptable. (Q8)

**W7. §14.2 `received_at INTEGER` on a table that already has `received_at TEXT` — impossible additively.**
SQLite cannot `ALTER COLUMN`. §14.1 allows "table rebuilds with an explicit backup step". The alternatives are a second column (`received_at_ms`) with two sources of truth, or a backed-up rebuild. Given the live DB holds 10 rows of test-mailbox data that Gmail will re-supply, the rebuild is the honest answer, but §14.1's "additive only" headline should be amended to say so explicitly. (Q3)

**W8. §5.2 query and `GMAIL_MAX_MESSAGES=2000`.**
`after:(window_start − 30 days)` with no label restriction pulls **30 days of INBOX + SENT** for every daily report. For a busy enquiry mailbox that can exceed 2000 easily, and SPEC says "abort". The lookback exists to find thread *starts*; but hydration via `threads.get` already fetches pre-window messages for touched threads. **Recommendation:** either (a) list only messages `after:window_start before:window_end+1` (coarse), hydrate their threads, and drop `THREAD_LOOKBACK_DAYS` from the listing query entirely; or (b) keep the lookback but make the cap apply to *hydrated* message count and default it higher. (a) is cheaper and equally correct given §5.3 hydration. Also: the query should exclude drafts and chats (`-in:drafts -in:chats`) — SPEC does not mention them, but `messages.list` returns drafts, which have no meaningful `internalDate` semantics for a report. (Q9)

**W9. §13.4 "Analyzing enquiry data… 31 cached, 7 analyzed" implies per-*thread* counts, but verdicts are per-*message*.** Cosmetic; suggest the line report messages.

**W10. §19 `DATABASE_PATH=regency.db` (repo root) vs the existing `data/regency.db`.** Moving the DB is disruptive for no reason; `.gitignore` would also need `*.db` at root. **Recommendation:** SPEC default becomes `data/regency.db`.

**W11. §21.1 mailbox `u.ruma@regencyelectricals.com` vs README's `enquiry@regencyelectrical.com` and the live token.** README says the project is "currently pointed at a personal test mailbox". The cached token belongs to whichever account authorised it (I did not print it). Manual test #45 will need a fresh consent for the real mailbox; nothing in code cares, but the plan should include "delete/replace `token.json` for the target mailbox" as a manual step, not a code change. (Q10)

**W12. §3 "No rewrite of working code" vs. the bot.** The bot works and is tested. SPEC does not say whether it should survive. It also violates §3 (daemon) by existing. See Q1.

**W13. §9 `quotation_signal` values are never enumerated.** The example shows `"rfq_received"`; the four metrics imply `rfq_received | quotation_sent | quotation_response | supplier_quotation | null`. Should be stated as the enum so the JSON schema can constrain it.

**W14. §12 "start date in the future → exit 2" vs "windows ending in the future are clamped".** Fine as written, but `--date <today>` produces an end in the future (23:59:59 today) that is clamped — and `--from <today> --to <tomorrow>` has a *start* that is not in the future, so it is clamped rather than rejected. Worth one sentence confirming that is intended.

---

# D. Plan

## 15. File-by-file change plan

Stages: **S1** config/CLI/timewindow · **S2** schema migration, Gmail ingestion, idempotency · **S3** AI layer with cached verdicts · **S4** metrics, renderer, Telegram delivery.

Assumes W1 is accepted (existing package names kept). If not, the CREATE paths change but the content does not.

### MODIFY

| File | Stage | What changes and why |
|---|---|---|
| `app/config.py` | S1 | Add every §19 variable with defaults; add `validate()` that raises a `ConfigError` naming the missing variable (never its value) → exit 2 before network. Accept the old names (`CLAUDE_API_KEY`, `GOOGLE_*_FILE`, `COMPANY_EMAIL_DOMAIN`) as fallbacks so the existing scripts/tests keep working; SPEC names take precedence. Remove nothing yet (Q1). |
| `.env.example` | S1 | Add §19 keys with empty values. Keep/drop `WHATSAPP_*` per Q1. |
| `.gitignore` | S1 | Add `*.db`, `*.db-wal`, `*.db-shm`, `*.bak.*`, `logs/`, `PHASE0_AUDIT.md`? (no — keep it tracked; your call). |
| `CLAUDE.md` | S1 | `repo.py` → `queries.py` (or whatever Q2 decides). One-line change; it is your file — I will not touch it without approval. |
| `app/database/schema.py` | S2 | Add `schema_meta`, `enquiry_brands`, `ai_verdicts`, `report_runs` DDL; `ALTER TABLE ADD COLUMN` for the new `emails`/`enquiries` columns; versioned, idempotent `migrate(conn)`; backup-before-first-migration to `<name>.bak.<ts>`; resolution of the `TEXT`→`INTEGER` timestamp question per Q3. |
| `app/database/db.py` | S2 | `PRAGMA journal_mode=WAL`; call `migrate()` instead of bare `init_db()`; map `sqlite3.OperationalError`/`DatabaseError` to a `DatabaseError` the CLI turns into exit 6. |
| `app/database/queries.py` | S2, S3, S4 | Add: `upsert_email` (`INSERT … ON CONFLICT(gmail_message_id) DO NOTHING`), `get_emails_in_range(start_ms, end_ms)`, `get_thread_ids_touched(...)`, `get_thread_messages(thread_id)`, verdict get/put (S3), `upsert_enquiry_state` + `replace_enquiry_brands` (S4), `insert_report_run` / `mark_delivered` (S4). Existing write functions and the founder-query half stay (bot). Replace `now_iso()` callers in new code with an injected `now_ms`. |
| `app/gmail/client.py` | S2 | `EmailMessage` gains `internal_date_ms`, `cc`, `direction`-inputs, `attachments: list[(filename, mime)]`, `is_auto_reply` header flags; `parse_message` reads `internalDate`; new `build_query(start, end, …)`; `list_message_ids` → full `nextPageToken` pagination with `GMAIL_MAX_MESSAGES` abort; new `get_thread(service, thread_id)`; batch `messages.get` via `service.new_batch_http_request()`; `HttpError` **raises** `GmailFetchError` instead of returning `[]`/`None` (the `fetch_messages` behaviour used by `sync.py` can keep its swallow semantics behind a flag, or `sync.py` catches — decide in S2). Keep `extract_body`/`_html_to_text` in place. |
| `app/gmail/auth.py` | S2 | On refresh failure in non-interactive context, raise `GmailAuthError` with re-auth steps (→ exit 3) rather than silently launching a browser; read `GMAIL_CREDENTIALS_PATH`/`GMAIL_TOKEN_PATH`. Small edit. |
| `app/enquiry/models.py` | S2–S4 | Add `Message` (with `received_at_ms`, `direction`, `is_auto_reply`), `Verdict`, `EnquiryState`, `EnquiryStatus` (`pending/attended/closed`), `CounterpartyType`. Keep old `Status`/`LastSender`/`Email`/`Enquiry` for the bot. |
| `app/enquiry/status.py` | S2 | Extend `classify_sender` → `classify_address(addr, mailbox, aliases, internal_domains) -> employee | internal | external`. Keep `extract_email_address`. Leave `next_status_for_message` / `is_quotation_message` untouched (bot). |
| `app/ai/claude.py` | S3 | `_request` gains `temperature=0`, `model` from config, exponential-backoff retry on `RateLimitError`/5xx/`APITimeoutError`/`APIConnectionError` up to `AI_MAX_RETRIES`; new `classify_message(target, context) -> Verdict | None` using the §15.3 schema (with `quantity` as string, `quotation_signal` enum, `brands` array); new `generate_summary(metrics) -> str | None` receiving only the metrics dict. Existing `extract_new_enquiry`/`classify_reply_intent`/`summarize_enquiry` untouched (bot). |
| `app/telegram/client.py` | S4 | Add `DeliveryResult` dataclass, `chunk(text, limit)` at section boundaries with `(Part i/n)` prefixes, `send(text) -> DeliveryResult` that reads `TELEGRAM_CHAT_ID`, sends parts sequentially, retries 429 (`retry_after`) and 5xx up to `TELEGRAM_MAX_RETRIES`, reports partial delivery. `_call`, `get_updates`, `send_message` untouched. |
| `README.md` | S4 | Document `report.py`, modes, exit codes, `.env`; mark the bot section per Q1. |
| `requirements.txt` | S1 | Pin versions only (no additions). Optional; flagged, not required. |
| `tests/test_gmail_client.py` | S2 | Extend for `internalDate`, pagination, `threads.get`, `Cc`, attachments, raise-on-error. |
| `tests/test_database.py` | S2–S4 | Extend for migration (before/after schema), `ON CONFLICT`, WAL, new repo functions. |
| `tests/test_claude.py` | S3 | Extend for `temperature=0` asserted on the call, retries, new schema validation, summary call receives no email text. |
| `tests/test_telegram_client.py` | S4 | Extend for `send`, chunking, retry, partial delivery, no success without `ok`. |
| `tests/test_enquiry_status.py` | S2 | Extend for employee/internal/alias classification. Existing transition tests stay (bot). |

### CREATE

| File | Stage | What and why |
|---|---|---|
| `report.py` | S1 (skeleton + window + `--dry-run` path) → S4 (full) | `argparse` with §13 modes/flags; exit-code contract; §13.4 terminal UX; rotating file log at `LOG_PATH`; orchestration only. |
| `app/timewindow.py` | S1 | `resolve_window(args, now: datetime, tz) -> Window(start_ms, end_ms, label, mode)`; strict `DD/MM/YYYY`; Monday week start; inclusive bounds; clamp; all §12 validation → `WindowError` (exit 2). |
| `app/gmail/parser.py` | S2 | `strip_quoted(text)`, `strip_signature(text)`, `clean_body(text)`; `gmail_quote` div handling for HTML. Pure functions. |
| `app/gmail/ingest.py` | S2 | The report-path ingestion: `build_query` → list (paginated, capped) → filter by `internalDate` in Python → group by thread → hydrate via `threads.get` → parse/clean → `upsert_email` idempotently. Replaces `sync.py`'s role for the report path without touching it (Q1 decides `sync.py`'s fate). |
| `app/ai/prompts.py` | S3 | `MESSAGE_VERDICT_V1`, `SUMMARY_V1` constants + schema dicts + version strings. |
| `app/ai/cache.py` | S3 | `get_verdict(conn, msg_id, version)` / `put_verdict(...)` thin wrappers over `queries.py`; `--reprocess` bypass. |
| `app/enquiry/thread_state.py` | S2 (pure logic, testable without AI: direction/auto-reply/last_sender) → S3 (closure from verdicts) | `compute_state(messages, verdicts, as_of_ms) -> EnquiryState`. Zero I/O. |
| `app/enquiry/brands.py` | S3 | Alias table, `normalise(raw) -> str`, `Unknown` rule. |
| `app/enquiry/metrics.py` | S4 | `compute_metrics(enquiry_states, messages, verdicts, window) -> Metrics`; invariant assertions; waiting anchored to `window_end`; four quotation counts; brand counts per distinct brand; "not determinable" handling. Zero I/O, zero AI. |
| `app/reporting/__init__.py`, `app/reporting/render.py` | S4 | §21.3 layout, IST conversion, display limits with "…and N more", section omission rules, footnote, caveat line. Channel-agnostic text. |
| `app/reporting/summary.py` | S4 | Calls `claude.generate_summary(metrics)`; numeral guard per W6; template fallback. |
| `tests/conftest.py` | S1 | Shared `db_path`, fixed `now`, fixture loaders. |
| `tests/fixtures/*.json` | S2 | Realistic Gmail `messages.list`/`messages.get`/`threads.get` payloads (multi-page, multipart, quoted replies, auto-reply headers, malformed `Date:`). |
| `tests/test_timewindow.py` | S1 | SPEC cases 1–10. |
| `tests/test_parser.py` | S2 | Case 15. |
| `tests/test_ingest.py` | S2 | Cases 11–14, 16, 37, 42. |
| `tests/test_thread_state.py` | S2/S3 | Cases 17–26. |
| `tests/test_brands.py` | S3 | Cases 27–29. |
| `tests/test_cache.py` | S3 | Cases 38–40. |
| `tests/test_metrics.py` | S4 | Cases 30–36. |
| `tests/test_render.py`, `tests/test_summary.py` | S4 | Cases 35, 36, 41, 44. |
| `tests/test_report_cli.py` | S1 (arg validation) → S4 (exit codes 3/4/5/6, 43) | End-to-end with all three externals mocked. |

### LEAVE UNTOUCHED

Explicitly, pending Q1:

| File | Reason |
|---|---|
| `app/router/intent.py`, `app/router/formatter.py`, `app/router/open_ended.py`, `app/router/__init__.py` | Bot-only. No overlap with the report path. |
| `app/telegram/bot.py` | Bot daemon. Not modified; not invoked by `report.py`. |
| `scripts/run_telegram_bot.py`, `scripts/sync_gmail.py`, `scripts/__init__.py` | Bot/legacy entry points. |
| `app/gmail/sync.py` | See rewrite note below. Left as the bot's ingestion path; `report.py` never imports it. |
| `app/ai/claude.py::extract_new_enquiry / classify_reply_intent / summarize_enquiry` | Bot callers only. New functions are added beside them. |
| `app/database/queries.py` founder-query section (lines 269–425) | Bot-only. |
| `app/enquiry/status.py::next_status_for_message / is_quotation_message / _CUSTOMER_INTENT_STATUS` | Bot-only state machine. |
| `tests/test_intent.py`, `tests/test_open_ended.py`, `tests/test_telegram_bot.py`, `tests/test_formatter.py`, `tests/test_founder_queries.py`, `tests/test_gmail_sync.py` | Bot-only; keep green. |
| `app/__init__.py` and all package `__init__.py` files | Empty markers. |
| `credentials.json`, `token.json`, `.env`, `data/regency.db` | Secrets/data. Never edited by code changes (the migration will create a `.bak` copy of the DB first). |
| `SPEC.md` | Yours. Amendments proposed in §14 are for you to apply. |
| `data/.gitkeep` | |

### Rewrite-vs-edit flags

- **`app/gmail/sync.py` — honest answer: do not edit it; build `ingest.py` beside it and let `sync.py` become bot-only or be deleted (Q1).** Every function in it is shaped around the mutation model: `sync_message` early-returns on `processed`, `_handle_new_thread` decides `is_enquiry` once and writes status, `_handle_existing_thread` steps the state machine, `run_sync` sorts by `Date:` header. There is no line that survives an incremental conversion to "fetch window → hydrate threads → upsert raw emails → stop". Editing it in place would produce a file where 90% of lines changed under the old name and all 14 of its tests break — that is a rewrite wearing an edit's clothes. Creating `ingest.py` is *not* a parallel app: it is the report path's ingestion, and the two share `client.py`, `queries.py`, `auth.py`.
- **`app/enquiry/status.py::next_status_for_message`** — same reasoning in miniature: `compute_state(messages, as_of)` is a different function with a different signature and different rules. Create `thread_state.py`; do not bend `next_status_for_message` into it. The helpers in `status.py` are kept and extended.
- **`app/router/formatter.py`** — not reusable for §21.3; `reporting/render.py` is created fresh. Not a rewrite because nothing is replaced.
- Everything else is a genuine incremental edit.

## 16. Risks, ambiguities, and assumptions — questions for you

**Q1. What happens to the bot?** `app/router/*`, `app/telegram/bot.py`, `scripts/run_telegram_bot.py`, `scripts/sync_gmail.py`, `app/gmail/sync.py`, `summarize_enquiry`, the founder-query SQL, and ~75 tests. Options: (a) leave untouched and green, coexisting with the report path (my plan above assumes this); (b) delete them in an approved list — SPEC §3 forbids the daemon and WhatsApp config, §3 also forbids deletions outside an approved plan; (c) leave now, delete after the report path ships. The choice affects whether `config.py` must keep old env names and whether `sync.py`'s `processed` semantics must keep working.

**Q2. Module naming (W1).** Keep `app/enquiry/`, `queries.py`, `ai/claude.py`, `telegram/client.py` and amend SPEC §4.2 + CLAUDE.md? Or rename to SPEC's names via `git mv` in Stage 1 (touches ~15 import sites, zero behaviour)?

**Q3. DB migration strategy (W7).** For the `TEXT`→`INTEGER` timestamp columns: (a) backed-up table rebuild (recommended; live data is 10 test rows re-fetchable from Gmail); (b) parallel `*_ms` columns and treat the old ones as legacy; (c) keep ISO text and store epoch ms only in new tables — violates §12/§14. Also: should the migration backfill `direction`/`sender_domain` on the 10 existing rows, or leave them to be overwritten by the next ingest?

**Q4. The `Received = Attended + Pending` invariant (W2).** Which resolution — closed-of-received counted under Attended (recommended), a three-term invariant, or Closed excluding the received cohort?

**Q5. Is `new` a real status (W3)?** Drop it, or define it as "pending, zero employee messages"?

**Q6. Rejection / withdrawal (W4).** Is a customer's explicit "not proceeding" closure evidence? If yes, should the verdict carry `closure_kind: won|lost|withdrawn`? If no, what state does a lost enquiry occupy?

**Q7. Verdict aggregation (W5).** Enquiry-level fields from the earliest inbound `is_enquiry=true` verdict, brands unioned across messages, per-message prompt sees prior thread as context — accept?

**Q8. Numeral guard rule (W6).** Allowed set = metrics integers ∪ window date/time components, integer comparison, zero-numeral summaries allowed — accept?

**Q9. Gmail listing strategy (W8).** Drop `THREAD_LOOKBACK_DAYS` from the list query and rely on hydration (cheaper, recommended), or keep the 30-day lookback and raise/redefine `GMAIL_MAX_MESSAGES`? Exclude `drafts`/`chats` in the query? Do you know the mailbox's rough daily volume (INBOX + SENT)?

**Q10. Target mailbox and token.** The cached `token.json` is for whatever account authorised it (README: a personal test mailbox). Manual tests #45–46 need consent from `u.ruma@regencyelectricals.com`. Confirm that is the target, and that `INTERNAL_DOMAINS=regencyelectricals.com` while `EMPLOYEE_ALIASES` is empty (i.e. any *other* `@regencyelectricals.com` address is *internal*, not employee — this changes what "attended" means when a colleague replies on the thread).

**Q11. `TELEGRAM_CHAT_ID`.** It does not exist in config today. Do you have the founder's chat id, or should Stage 4 include a one-off helper (`report.py --print-chat-id` reading `getUpdates` once) to discover it? That helper is a read call, not a daemon, but it is not in SPEC.

**Q12. Auto-reply detection.** SPEC §8.2 says auto-replies do not flip `last_sender` but does not say how to detect them. Proposed: deterministic first (`Auto-Submitted: auto-replied`, `X-Autoreply`, `X-Autorespond`, `Precedence: bulk/auto_reply`, subject prefixes like "Automatic reply:"/"Out of Office") stored as `emails.is_auto_reply`, with Claude's verdict as a second signal only when headers are absent. Accept?

**Q13. `computed_as_of` on `enquiries` when the same thread is reported in two windows.** The row is a cache of the *last run's* as-of state; `report_runs.metrics_json` is the durable artefact. Accept that `enquiries` reflects the most recent run only?

**Q14. Supplier threads.** §9 `supplier_quotations_received` needs `counterparty_type=supplier`, which is a Claude judgement, and §6.2 says internal threads are never enquiries — but it does not say whether supplier threads *are* enquiries. Assumption: supplier threads are **not** enquiries (they do not count toward Received/Pending), but their inbound quotations are counted for the one metric. Confirm.

**Q15. Priority ("IMPORTANT" section).** §21.3 shows urgency evidence; `is_priority` is on `enquiries`. Assumption: `is_priority = any(verdict.urgency for messages in thread ≤ as_of)`, and the section lists received-in-window *or* pending-as-of enquiries with urgency? SPEC does not anchor this metric in §11.2.

**Q16. Python 3.9.** Everything SPEC needs works on 3.9, but `google-auth` has announced it will stop shipping fixes for it. Not a blocker; do you want the audit to record "upgrade to 3.11+ recommended" as a known limitation, or leave it?

**Q17. `AI_BODY_MAX_CHARS` vs `MAX_BODY_CHARS`.** Client currently truncates the *stored* body at 20 000. SPEC stores the full cleaned body. Assumption: keep a generous storage cap (20 000 after quote-stripping) and apply 4 000 only to the AI call. Confirm.

**Q18. `PHASE0_AUDIT.md` itself** — track it in git, or gitignore it?

---

*End of audit. No further work will begin until you approve, amend, or answer the above.*
