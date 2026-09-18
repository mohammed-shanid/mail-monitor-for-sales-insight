# SPEC.md — Regency Enquiry Monitor (V1)

**Status:** Authoritative specification. Where this document and any prompt, comment, or
prior code disagree, this document wins.

**Prime directive:** This is an *incremental modification* of an existing repository.
Do not rebuild from scratch. Do not create a parallel application. Inspect first, then
extend.

---

## 0. Glossary

Terms used with precise meaning throughout this spec. Do not substitute synonyms.

| Term | Meaning |
|---|---|
| **Mailbox** | The Gmail account being monitored. V1: exactly one, from `REPORT_MAILBOX`. |
| **Employee** | The mailbox owner. An address is "employee" if it matches `REPORT_MAILBOX` or any entry in `EMPLOYEE_ALIASES`. |
| **Internal** | Any address whose domain is in `INTERNAL_DOMAINS`. Internal ≠ employee. |
| **Counterparty** | The non-employee side of a thread: customer, supplier, internal, or unknown. |
| **Thread** | A Gmail conversation, identified by `gmail_thread_id`. The unit of enquiry identity. |
| **Enquiry** | A thread classified as genuine inbound business demand. One thread → at most one enquiry. |
| **Window** | The reporting period: `[window_start, window_end]`, inclusive, in IST. |
| **As-of state** | Enquiry status computed using only messages with `received_at <= window_end`. |
| **Touched thread** | A thread with ≥1 message whose `received_at` falls inside the window. |
| **Hydrated thread** | A touched thread for which *all* messages have been fetched, including those outside the window. |

---

## 1. Project Objective

A local-first email intelligence tool that reads a Gmail mailbox, identifies genuine
business enquiries, computes exact metrics deterministically in Python, uses Claude only
for semantic judgement, renders a management report, and delivers it to the founder's
Telegram.

The founder is non-technical. The report must be readable on a phone in under 60 seconds
and must never contain a number that cannot be traced to a database row.

Single success criterion:

```
python report.py
```

produces an accurate previous-day report for `REPORT_MAILBOX` and delivers it to Telegram,
or fails loudly with a non-zero exit code.

---

## 2. Current Scope

- Exactly one mailbox, supplied by config, never hardcoded in logic.
- Manual invocation from an office laptop. Run, report, exit.
- Six reporting modes (§13).
- SQLite persistence, local file.
- Telegram as the sole delivery channel.
- Gmail read-only scope.

---

## 3. Explicit Non-Goals

Do not build, add, install, or scaffold any of the following. Presence of any of these in
the diff is a spec violation.

- No server, HTTP API, web UI, or dashboard.
- No always-running worker, scheduler, cron installer, or daemon.
- No Docker, Kubernetes, or cloud deployment.
- No PostgreSQL, MySQL, Redis, Celery, vector database, or ORM migration framework.
- No CRM, no ticketing, no pipeline stages beyond those in §8.
- No WhatsApp, Slack, email-delivery, or any second channel.
- No multi-employee support, no `--employee` flag, no employee table rows beyond one.
- No cross-thread duplicate-enquiry detection or fuzzy customer matching.
- No attachment parsing, no PDF/Excel quotation extraction.
- No sentiment scoring, lead scoring, or revenue forecasting.
- No framework introduction (FastAPI, Django, Flask, LangChain, etc.).
- No rewrite of working code. No file deletions outside the approved plan.

---

## 4. Architecture

### 4.1 Mandatory Phase 0 — Audit Before Code

Before writing or editing any file, produce and stop for approval on:

1. A map of the existing repository: every file, its purpose, its dependencies.
2. Identification of current Gmail, Claude, Telegram, database, enquiry-logic, and CLI code.
3. Current DB schema, dumped from the live file if one exists.
4. A file-by-file change plan: **modify / create / leave untouched**.
5. A list of anything in the existing code that contradicts this spec.

Write no implementation code during Phase 0.

### 4.2 Target Module Layout

Adapt to whatever already exists; do not force a rename of working modules for cosmetic
reasons. If the existing layout is close, extend it in place.

```
report.py                  # entry point, CLI, orchestration, terminal UX only
app/
  config.py                # env loading, validation, fail-fast on missing vars
  timewindow.py            # CLI dates → (start_utc, end_utc), all timezone logic
  gmail/
    client.py              # auth, query building, pagination, batch fetch
    parser.py              # MIME → text, quoted-text strip, signature strip
  database/
    schema.py              # DDL, schema_version, additive migrations
    queries.py             # all SQL. No SQL anywhere else in the codebase.
  ai/
    claude.py              # Claude calls, temperature 0, retries, structured output
    prompts.py             # versioned prompt constants
    cache.py               # verdict cache read/write
  enquiry/
    thread_state.py        # pure functions: messages → enquiry state
    brands.py              # normalisation, alias table
    metrics.py             # all counting. No AI. No I/O.
  reporting/
    render.py              # structured metrics → report text
    summary.py             # Claude-generated prose summary from verified data only
  telegram/
    client.py              # dumb transport: accepts text, sends, reports result
tests/
.env.example
```

These are the existing package names in the repository (`app/enquiry/`, `app/database/queries.py`,
`app/ai/claude.py`, `app/telegram/client.py`) — new files land inside this layout rather than
forcing a rename of working modules.

### 4.3 Layering Rules

- `report.py` orchestrates; it contains no business logic and no SQL.
- `enquiry/metrics.py` must be importable and testable with zero network and zero DB —
  it takes structured objects in, returns numbers out.
- `telegram/client.py` must contain **no** knowledge of enquiries, brands, or metrics.
  Its public surface is `send(text: str) -> DeliveryResult`. This is what makes a second
  channel a 30-line addition later.
- `reporting/render.py` produces channel-agnostic plain text. Chunking for Telegram's
  message limit happens in the delivery layer, not the renderer.
- All timezone conversion happens in `timewindow.py` and the renderer. Nowhere else.

---

## 5. Gmail Integration

### 5.1 Auth

- Scope: `https://www.googleapis.com/auth/gmail.readonly`. Nothing wider.
- Reuse the existing `credentials.json` / `token.json` flow if present.
- On expired token: attempt refresh; on failure, print re-auth instructions and exit 3.
- Never print token contents, refresh tokens, or client secrets.

### 5.2 Query Construction — Critical

**The query must include SENT mail.** Employee replies live in SENT, not INBOX. A query
restricted to `in:inbox` makes every enquiry look pending. This is the single most likely
source of silently wrong output.

The list query covers only the window itself; do not widen it on the lower bound:

```
after:<window_start_ist>  before:<window_end_ist + 1 day>  -in:drafts -in:chats
```

Threads that started earlier are found by hydration (§5.3), which fetches every message of
a touched thread including those outside the fetch window — a separate lower-bound lookback
on the list query is redundant with hydration and is not used.

Gmail's `after:`/`before:` operators are date-granular and timezone-fuzzy. Treat them as a
coarse pre-filter only. **Exact window filtering is done in Python** against
`internalDate`, never by trusting the Gmail query.

### 5.3 Fetching

- Use `internalDate` (epoch ms, UTC) as the authoritative timestamp. Do **not** parse the
  `Date:` header — it is client-supplied and frequently wrong or absent.
- Paginate fully via `nextPageToken`. Never assume one page.
- Use batch requests for message bodies where the client library supports it.
- **Thread hydration:** after identifying touched threads, fetch every message of those
  threads via `threads.get`, including messages outside the fetch window. Enquiry state is
  a function of the whole thread (§8), so partial threads produce wrong statuses.
- Respect `GMAIL_MAX_MESSAGES` (default 2000) as a safety cap on the **hydrated** message
  count (after thread hydration, not the initial list); if exceeded, abort with a clear
  message rather than truncating silently.

### 5.4 Body Extraction

- Prefer `text/plain`; fall back to `text/html` → text conversion.
- **Strip quoted reply chains** (`>` prefixes, `On <date>, <person> wrote:`, Gmail's
  `gmail_quote` div). Without this, Claude re-extracts stale brands and quantities from
  quoted history on every reply, corrupting the data and inflating token cost.
- Strip signature blocks (`-- ` delimiter, and a heuristic for trailing contact blocks).
- Truncate the cleaned body to `AI_BODY_MAX_CHARS` (default 4000) before sending to Claude.
  Store the full cleaned body in SQLite regardless, capped at 20 000 chars — a generous
  ceiling; nothing legitimate needs more.
- Attachments: record filename and MIME type only. Do not download or parse contents.

---

## 6. Email / Thread Model

Per message, capture: `gmail_message_id`, `gmail_thread_id`, `sender`, `recipients`,
`subject`, `body`, `received_at` (from `internalDate`), `direction`, `counterparty_type`.

### 6.1 Direction

Derived deterministically in Python, never by AI:

- `sender` matches employee identity → `direction = outbound`
- otherwise → `direction = inbound`

### 6.2 Counterparty Classification

For each thread, the counterparty is the set of non-employee participants.

1. Domain in `INTERNAL_DOMAINS` → `internal`
2. Claude classifies `customer` vs `supplier` vs `unknown` from thread content
3. Threads classified `internal` are never enquiries

### 6.3 Thread Identity

`gmail_thread_id` is the sole conversation key. One thread maps to at most one enquiry row.

**Documented V1 limitation:** if a customer starts a fresh Gmail thread for the same
business matter, it becomes a second enquiry. This is accepted. Do not build de-duplication.

---

## 7. Enquiry Definition

A thread is a genuine enquiry when an inbound message from a non-internal counterparty
expresses business demand: product enquiry, requirement, RFQ, quotation or pricing request,
availability check, quantity requirement, or a purchase-related request.

Never an enquiry: spam, newsletters, OTPs and verification codes, automated notifications,
delivery/no-reply system mail, marketing blasts, personal correspondence, internal
administration, calendar invites, out-of-office replies.

Threads whose counterparty is classified `supplier` (§6.2) are also never an enquiry,
regardless of content — a supplier's quotation or price list is not inbound business demand
from a customer. Their inbound quotations are captured only for
`supplier_quotations_received` (§9); they never contribute to Received, Attended, Pending,
Closed, or brand counts.

Classification is Claude's job. The decision is stored, cached, and auditable (§15).

An enquiry is **created only from an inbound message**. An outbound-first thread (employee
cold outreach) is not an enquiry unless and until a counterparty replies with demand.

---

## 8. Attended / Pending / Closed Rules

### 8.1 State Is Derived, Never Mutated

Enquiry status is a **pure function** of the thread's ordered message list plus a
cut-off timestamp:

```python
def compute_state(messages: list[Message], as_of: datetime) -> EnquiryState
```

Only messages with `received_at <= as_of` are considered. Nothing is toggled in place;
state is recomputed from scratch on every run. This makes the system idempotent,
replayable, and immune to the mutation-order bugs that plague status-flag designs.

### 8.2 Definitions

**`last_sender`** — direction of the most recent *relevant* message at or before `as_of`.
Values: `customer` | `employee`. Auto-replies and out-of-office messages are not relevant
and do not change `last_sender`.

**ATTENDED** — the employee has sent a relevant reply *after* the counterparty's latest
enquiry message. Equivalently: `last_sender == employee` and at least one inbound enquiry
message precedes it.

**PENDING** — `last_sender == customer` and no employee message follows it.

Explicitly **not** attendance: opening, reading, viewing, downloading, or labelling an
email. These signals are not used and must not be inferred.

**CLOSED** — requires positive evidence in message content:

- Counterparty accepts the quotation
- Counterparty confirms the order or issues a PO
- Counterparty explicitly instructs to proceed
- Employee explicitly records closure or order booking
- Counterparty explicitly withdraws, rejects, or states the requirement is no longer live

Explicitly **insufficient** for closure: employee replied; quotation sent; customer said
"thanks"; customer said "we will check / get back / review internally"; thread went quiet.

Every closure carries a `closure_kind`: `won | lost | withdrawn | unknown`, recording which
of the above applied. V1's report shows only the aggregate `Closed` count; the won/lost/
withdrawn split is captured now so it is available without a re-analysis later.

Claude interprets intent and returns a closure signal with evidence. Python applies the
state transition. Claude never sets the status directly.

### 8.3 Status Values

Three states: `pending`, `attended`, `closed`.

```
pending   : last_sender == customer, not closed
attended  : last_sender == employee, not closed
closed    : closure evidence present at or before as_of
```

`closed` is terminal within a window. A closed enquiry that receives a new inbound message
reverts to `pending` — real business does this, and the model should reflect it.

---

## 9. Quotation Rules

Four distinct, separately-counted concepts. Do not merge them under one label.

| Metric | Definition |
|---|---|
| `rfq_received` | Inbound message from a customer requesting a quotation, price, or rate |
| `quotations_sent` | Outbound message from the employee containing or attaching a quotation |
| `quotation_responses` | Inbound customer message reacting to a received quotation (accept / negotiate / query / reject) |
| `supplier_quotations_received` | Inbound quotation from a counterparty classified as `supplier` |

Supplier threads (§7) never count toward Received, Attended, Pending, Closed, or the brand
breakdown — only toward this one metric.

Detection is Claude's, per message, with a boolean plus a short evidence string. Counting
is Python's.

**Insufficient-evidence rule:** if a metric cannot be determined reliably from available
data, omit the line entirely or render it as `not determinable`. Never print a fabricated
or estimated number. An absent metric is acceptable; a wrong one is not.

Render with explicit labels:

```
RFQ / quotation requests received: 11
Quotations sent: 7
```

---

## 10. Brand Rules

### 10.1 Extraction

Extract brands from actual enquiry content. A hardcoded list may be used for
*normalisation*, never as the sole source of *detection* — unknown brands must still be
captured.

### 10.2 Normalisation

Maintain an alias table in `enquiry/brands.py`:

```
schneider electric, schneider-electric, sneider  → Schneider
siemens ag, seimens                              → Siemens
abb ltd, a.b.b.                                  → ABB
larsen & toubro, larsen and toubro, l and t, lnt → L&T
legrand, legrande                                → Legrand
```

Normalise case and whitespace. Correct only unambiguous misspellings. **Do not guess.**
Brand is `Unknown` whenever identification is not reliable, and `Unknown` appears in the
breakdown like any other value — it is signal, not a gap to be hidden.

### 10.3 Counting Rule — Locked

**An enquiry contributes one count to each distinct brand it mentions.**

Consequence: the sum of brand counts may exceed the total enquiry count. This is correct
and intentional. The report must carry the footnote:

```
Multi-brand enquiries are counted under each brand.
```

Brands are stored in a separate `enquiry_brands` table, not as a comma-joined string —
comma-joining makes correct counting impossible.

---

## 11. Historical Reporting Rules

### 11.1 As-Of Semantics — Locked Decision

**All enquiry state is computed as of `window_end`, not as of "now".**

Running `python report.py --date 10/09/2026` today must produce byte-identical metrics to
running it on 11 September, assuming the same underlying mail. Reports are reproducible
artefacts, not snapshots of present mood.

### 11.2 Metric Anchoring

| Metric | Basis |
|---|---|
| Received | Enquiries whose first inbound message falls inside the window |
| Attended | Of those received, how many are **not** waiting on the counterparty as of `window_end` |
| Pending | Of those received, how many were pending as of `window_end` |
| Closed | Enquiries whose closure evidence timestamp falls inside the window |
| Quotation metrics | Messages inside the window |
| Brand counts | Enquiries received inside the window |
| Priority | Enquiries received inside the window **or** pending as of `window_end`, flagged urgent by any message at or before `window_end` |

**Attended, defined for this table:** an enquiry counts as Attended when its as-of state at
`window_end` is `attended` **or** `closed` — both mean the counterparty is not the one being
waited on. This is what makes the invariant hold: every received enquiry is, as of
`window_end`, either waiting on the counterparty (`Pending`) or not (`Attended`).

`Received = Attended + Pending` must hold as an invariant and be asserted in tests.
`Closed` is counted independently and may reference enquiries received before the window —
label it so the founder is not confused by `Closed > Received`.

### 11.3 Waiting Duration Anchor

```
waiting_duration = window_end - timestamp_of_last_customer_message
```

Anchored to `window_end`, never to `datetime.now()`. A report for last Tuesday must not
show a 6-day wait time for something answered on Wednesday.

### 11.4 Late-Arriving Mail

A re-run over a past window will incorporate messages that arrived after the original run
but still fall inside the window. This is correct behaviour — the window is the truth, not
the run time.

---

## 12. Date / Timezone Rules

- Reporting timezone: `Asia/Kolkata`, from `REPORT_TIMEZONE`, configurable but defaulted.
- Use `zoneinfo`. No `pytz`. No naive datetimes anywhere in the codebase.
- **Storage is UTC**, as integer epoch milliseconds. Conversion to IST happens only at CLI
  parsing and report rendering.
- CLI date format: **`DD/MM/YYYY`**, strictly. `10/09/2026` is 10 September.
- Week starts **Monday**. Derived from `Asia/Kolkata`, never from system locale.
- Boundaries are inclusive: `00:00:00.000` through `23:59:59.999` IST.
- Windows that end in the future are clamped to the execution time; future-dated emails are
  never included. This clamping applies to the window's *end* only — a window whose *start*
  is in the future is rejected outright (see Validation below), never silently clamped or
  swapped.

### Validation — all produce exit code 2 with a usage example

- Malformed date (`2026-09-10`, `32/01/2026`, `13/13/2026`)
- `--from` later than `--to` — **error, never silently swapped**
- `--from` without `--to`, or `--to` without `--from`
- Any combination of two mode flags (`--today --week`, `--date` with `--from`)
- A start date in the future
- A window longer than `MAX_WINDOW_DAYS` (default 92)

---

## 13. CLI

### 13.1 Reporting Modes — mutually exclusive

| Command | Window (IST) |
|---|---|
| `python report.py` | Yesterday 00:00:00 → 23:59:59 |
| `python report.py --today` | Today 00:00:00 → execution time |
| `python report.py --week` | Monday 00:00:00 → execution time |
| `python report.py --date 10/09/2026` | 10 Sep 00:00:00 → 10 Sep 23:59:59 |
| `python report.py --from 10/09/2026 --to 12/09/2026` | 10 Sep 00:00:00 → 12 Sep 23:59:59 |

### 13.2 Modifier Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Full pipeline, render report, print to terminal, **do not send**. |
| `--no-send` | Alias of `--dry-run`. |
| `--json PATH` | Additionally write the structured metrics payload to PATH. |
| `--reprocess` | Ignore the AI verdict cache; re-analyse all messages in window. |
| `--limit N` | Cap messages fetched. Development aid. |
| `--verbose` | Per-thread decisions and timings to stderr. |
| `--quiet` | Suppress progress, print final status line only. |

`--dry-run` is not optional to implement. Without it, every test run spams the founder.

### 13.3 Exit Codes

```
0  success — report generated and delivered (or rendered, under --dry-run)
2  invalid CLI arguments or date range
3  Gmail auth/API failure
4  Claude API failure that prevents report generation
5  Telegram delivery failure (report was generated but not delivered)
6  Database error
1  unexpected/unhandled error
```

`--dry-run` never returns 5.

### 13.4 Terminal Output

```
----------------------------------------
Regency Enquiry Monitor
----------------------------------------
Mailbox:
u.ruma@regencyelectricals.com

Report period:
13 September 2026
00:00 IST → 23:59 IST

Connecting to Gmail...          ok
Fetching messages...            142 messages, 38 threads
Hydrating threads...            38 threads complete
Analyzing enquiry data...       31 messages cached, 7 messages analyzed
Calculating metrics...          24 enquiries
Generating report...            ok
Sending Telegram report...      ok (2 parts)

✅ Report sent successfully.
----------------------------------------
```

The "Analyzing" line counts messages (verdicts), not threads — a single thread can
contribute several messages to either the cached or analyzed count.

Failures print the failing stage, a human-readable cause, and a log-file path. Never a bare
traceback as the primary output. Never a secret in any line.

---

## 14. SQLite Schema

### 14.1 Migration Policy

- Inspect the existing DB first and dump its current schema.
- **Additive by default**: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN`. No `DROP`,
  no destructive `ALTER` without an explicit backup step. Where a column's *type* must
  change — SQLite cannot `ALTER COLUMN` — a backed-up table rebuild is permitted: back up
  first, verify the backup is non-zero, then rebuild, and record the rebuild as a version
  step in `schema_meta` so it never runs twice.
- A `schema_meta` table tracks version; migrations run on startup and are idempotent.
- Back up the DB file to `<name>.bak.<timestamp>` before the first migration run.
- Enable `PRAGMA foreign_keys = ON` and `PRAGMA journal_mode = WAL`.

### 14.2 Tables

```sql
CREATE TABLE IF NOT EXISTS schema_meta (
  key     TEXT PRIMARY KEY,
  value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emails (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  gmail_message_id   TEXT    NOT NULL UNIQUE,
  gmail_thread_id    TEXT    NOT NULL,
  sender             TEXT    NOT NULL,
  sender_domain      TEXT,
  recipient          TEXT,
  subject            TEXT,
  body               TEXT,
  received_at        INTEGER NOT NULL,   -- epoch ms UTC, from internalDate
  direction          TEXT    NOT NULL,   -- inbound | outbound
  is_auto_reply      INTEGER NOT NULL DEFAULT 0,
  has_attachments    INTEGER NOT NULL DEFAULT 0,
  ingested_at        INTEGER NOT NULL,
  processed          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_emails_thread   ON emails(gmail_thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);

CREATE TABLE IF NOT EXISTS enquiries (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  gmail_thread_id    TEXT    NOT NULL UNIQUE,
  mailbox            TEXT    NOT NULL,   -- forward-compat for multi-employee
  customer_name      TEXT,
  company            TEXT,
  customer_email     TEXT,
  counterparty_type  TEXT,               -- customer | supplier | internal | unknown
  subject            TEXT,
  product            TEXT,
  requirement        TEXT,
  quantity           TEXT,               -- TEXT: "250 nos", "2 lots" are real inputs
  status             TEXT    NOT NULL,   -- pending | attended | closed
  last_sender        TEXT,               -- customer | employee
  is_priority        INTEGER NOT NULL DEFAULT 0,
  priority_evidence  TEXT,
  received_at        INTEGER NOT NULL,
  last_activity_at   INTEGER,
  closed_at          INTEGER,
  closure_evidence   TEXT,
  closure_kind       TEXT,               -- won | lost | withdrawn | unknown
  computed_as_of     INTEGER,            -- window_end used for this computation
  updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_enq_received ON enquiries(received_at);
CREATE INDEX IF NOT EXISTS idx_enq_status   ON enquiries(status);

-- `enquiries` reflects only the most recently completed run's as-of state — it is a cache,
-- not history. `report_runs.metrics_json` (below) is the durable, reproducible artefact for
-- any given window; do not treat `enquiries` rows as a historical record of past reports.

-- Brands normalised out. Never store as a comma-joined string.
CREATE TABLE IF NOT EXISTS enquiry_brands (
  enquiry_id   INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  brand        TEXT    NOT NULL,         -- normalised; 'Unknown' is a valid value
  raw_mention  TEXT,
  PRIMARY KEY (enquiry_id, brand)
);

-- Per-message Claude verdicts. Makes runs cheap, fast, deterministic, auditable.
CREATE TABLE IF NOT EXISTS ai_verdicts (
  gmail_message_id  TEXT    NOT NULL,
  prompt_version    TEXT    NOT NULL,
  model             TEXT    NOT NULL,
  verdict_json      TEXT    NOT NULL,
  created_at        INTEGER NOT NULL,
  PRIMARY KEY (gmail_message_id, prompt_version)
);

-- Every delivered report, for audit and re-send.
CREATE TABLE IF NOT EXISTS report_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  window_start   INTEGER NOT NULL,
  window_end     INTEGER NOT NULL,
  mode           TEXT    NOT NULL,
  mailbox        TEXT    NOT NULL,
  metrics_json   TEXT    NOT NULL,
  report_text    TEXT    NOT NULL,
  delivered      INTEGER NOT NULL DEFAULT 0,
  delivery_error TEXT,
  created_at     INTEGER NOT NULL
);
```

---

## 15. AI / Claude Rules

### 15.1 The Boundary

**Python owns every number. Claude owns every judgement.**

Python computes: totals, attended/pending/closed counts, brand counts, quotation counts,
date filtering, waiting durations, sorting, ranking.

Claude decides: is this a genuine enquiry; who is the customer/company; what product,
quantity, brand; is this a supplier or customer; what does quotation language mean; does
this message constitute acceptance or closure; is there evidence of urgency; and — from
already-verified structured numbers — the prose summary.

Claude is never asked "how many enquiries were there". Claude never sees raw email in the
same call that produces the summary.

### 15.2 Determinism

- `temperature = 0` on every call.
- Structured output via tool-use / JSON schema. Never regex over free text.
- Prompts live in `ai/prompts.py` as versioned constants (`ENQUIRY_CLASSIFY_V1`).
- Every verdict is persisted in `ai_verdicts` keyed on `(gmail_message_id, prompt_version)`.
  Re-runs read from cache. Bumping a prompt version invalidates naturally. `--reprocess`
  bypasses the cache.

### 15.3 Message-Level Verdict Schema

```json
{
  "is_enquiry": true,
  "counterparty_type": "customer",
  "customer_name": "Ramesh Kumar",
  "company": "ABC Industries",
  "product": "MCCB 250A",
  "requirement": "Price and availability for 20 units",
  "quantity": "20 nos",
  "brands": ["Schneider"],
  "quotation_signal": "rfq_received",
  "closure_signal": false,
  "closure_evidence": null,
  "closure_kind": null,
  "urgency": false,
  "urgency_evidence": null,
  "confidence": "high"
}
```

`quotation_signal` is one of `rfq_received | quotation_sent | quotation_response |
supplier_quotation | null`. `closure_kind` is one of `won | lost | withdrawn | unknown |
null`, set only when `closure_signal` is true.

Every field is nullable. **`null` is always preferable to a guess.** A field the model is
unsure about must come back `null` and render as absent, not as a plausible invention.

### 15.4 Summary Generation

The summary call receives **only the computed metrics object** — no raw email bodies. It is
instructed to use no number that is not present in its input, and to write 2–4 sentences of
plain business English.

Post-generation guard: extract every integer from the summary and assert it is a member of
the allowed set — every integer present in the metrics payload, plus the window's day,
month, and year, and the hour and minute of both window bounds. Compare as integers, not as
digit substrings. A summary containing zero numerals is valid and must not be rejected. On
any numeral outside the allowed set, discard the summary and fall back to a deterministic
template. An LLM that invents a number must not reach the founder.

### 15.5 Failure Behaviour

- Retry with exponential backoff on 429/5xx, `AI_MAX_RETRIES` (default 3).
- On persistent failure: **preserve all raw email data**, mark affected messages
  unanalysed, and continue.
- If enquiry classification failed for part of the window, the report carries an explicit
  caveat line stating that N messages could not be analysed. Never quietly under-report.
- If classification failed for the whole window: exit 4, send nothing.
- If only the summary failed: send the report with the template fallback, exit 0.
- **Never fabricate extracted fields to fill a gap.**

---

## 16. Telegram Rules

### 16.1 Transport-Only Contract

```python
def send(text: str) -> DeliveryResult
```

No business logic. No formatting decisions about content. No awareness of enquiries.
Swapping in WhatsApp later must require zero changes to the reporting engine.

### 16.2 Message Limit — Handle It

Telegram's hard cap is 4096 characters. A busy day with many pending enquiries **will**
exceed this and the API will reject the send. Split at section boundaries (never
mid-enquiry) at `TELEGRAM_CHUNK_CHARS` (default 3800), prefixing continuation parts:

```
(Part 2/3)
```

Independently, cap visible list items: `PENDING_DISPLAY_LIMIT` (default 10),
`PRIORITY_DISPLAY_LIMIT` (default 5), with a trailing `…and 7 more` line. The *counts* in
the summary remain complete and accurate regardless of display truncation.

### 16.3 Formatting

Send as **plain text with no `parse_mode`.** The report contains `━`, emoji, `&`, `.`, `-`,
`(`, `)` — all of which require escaping under MarkdownV2 and will otherwise produce HTTP
400s. This is a common and avoidable failure.

### 16.4 Delivery Semantics

- Retry on 429 honouring `retry_after`, and on 5xx; `TELEGRAM_MAX_RETRIES` default 3.
- Multi-part sends are sequential; a mid-sequence failure is reported as partial delivery,
  naming which parts landed.
- **Never print a success message unless the API confirmed delivery.**
- Record the outcome in `report_runs`.

---

## 17. Error Handling

| Failure | Behaviour |
|---|---|
| Missing/invalid config | Exit 2 before any network call. Name the missing variable, never its value. |
| Gmail auth | Exit 3. Print re-auth steps. No DB writes. |
| Gmail API / network | Exit 3. Already-ingested data stays valid and uncorrupted. |
| Partial Gmail fetch | Abort the run rather than report on a partial window. A partial report is worse than no report. |
| Claude failure | Per §15.5. Raw data preserved, affected messages retryable, no invented fields. |
| Telegram failure | Exit 5. Report text is persisted and printed to terminal so the work is not lost. |
| DB locked / corrupt | Exit 6. Do not delete or recreate the DB. |
| Empty window | Not an error. Send a valid report showing zeros and a plain-English note. Exit 0. |

Logging: rotating file at `LOG_PATH` (default `logs/report.log`), terminal stays clean.
`--verbose` mirrors detail to stderr. **Redact all secrets from every log path.**

---

## 18. Idempotency

Two concepts that must not be conflated — conflating them is how status updates silently
stop working:

1. **Ingestion idempotency** — `gmail_message_id` is `UNIQUE`. Writes use
   `INSERT ... ON CONFLICT DO NOTHING`. Re-running never duplicates an email row.
2. **Analysis idempotency** — verdicts cached on `(gmail_message_id, prompt_version)`. The
   same message is never paid for twice.
3. **State recomputation is NOT skipped.** Enquiry state is recomputed from the full thread
   on every run (§8.1). A `processed = 1` flag must never cause a thread to be skipped
   wholesale, or new replies will never update status.

Required guarantee: running any command twice over the same window produces identical DB
state and an identical report, and creates zero duplicate rows.

---

## 19. Security

- No secrets in source. Ever. `.env` via `python-dotenv`, validated at startup.
- Gitignored: `.env`, `credentials.json`, `token.json`, `*.db`, `*.db-wal`, `*.db-shm`,
  `*.bak.*`, `logs/`.
- Commit `.env.example` with keys and empty values only.
- Gmail read-only scope. Never request write, send, or modify.
- On startup, confirm the OAuth-authorised account matches `REPORT_MAILBOX` exactly (via
  the Gmail profile endpoint) and abort immediately on any mismatch — see §21.1.
- Never log or print: API keys, bot tokens, chat IDs, OAuth tokens, full email bodies.
- Redact in error output: any value from an env var, any string matching a token pattern.
- Customer email addresses appear in the report by design; treat the DB file as containing
  business PII and do not commit it.

### Required `.env`

```bash
REPORT_MAILBOX=u.ruma@regencyelectricals.com
EMPLOYEE_ALIASES=                       # optional, comma-separated
INTERNAL_DOMAINS=regencyelectricals.com

REPORT_TIMEZONE=Asia/Kolkata

GMAIL_CREDENTIALS_PATH=credentials.json
GMAIL_TOKEN_PATH=token.json
GMAIL_MAX_MESSAGES=2000

ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-4-6
AI_MAX_RETRIES=3
AI_BODY_MAX_CHARS=4000

TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CHUNK_CHARS=3800
TELEGRAM_MAX_RETRIES=3

DATABASE_PATH=data/regency.db
LOG_PATH=logs/report.log
PENDING_DISPLAY_LIMIT=10
PRIORITY_DISPLAY_LIMIT=5
MAX_WINDOW_DAYS=92
```

**Legacy fallback names** (from the pre-SPEC bot config) are still accepted where the SPEC
name is unset, with the SPEC name always taking precedence when both are set:
`CLAUDE_API_KEY` → `ANTHROPIC_API_KEY`, `GOOGLE_CREDENTIALS_FILE` → `GMAIL_CREDENTIALS_PATH`,
`GOOGLE_TOKEN_FILE` → `GMAIL_TOKEN_PATH`, `COMPANY_EMAIL_DOMAIN` → `INTERNAL_DOMAINS`.

---

## 20. Testing

No claim of working software without pasted, real test output. "It should work" is not a
test result.

### 20.1 Rules

- `pytest`. Fixtures under `tests/fixtures/` as realistic Gmail API JSON.
- Gmail, Claude, and Telegram are mocked in all automated tests. Zero network.
- `enquiry/` and `timewindow.py` must reach high coverage — that is where wrong numbers
  come from.
- Time is injected, never `datetime.now()` inside logic, so window tests are stable.

### 20.2 Required Cases

**Time & CLI (1–10)**
1. Default → yesterday full day IST
2. `--today` → midnight to injected now, excludes future messages
3. `--week` → Monday midnight IST, verified on a Wednesday and on a Monday
4. `--date` → correct single full day
5. `--from/--to` → both bounds inclusive
6. Invalid date format rejected, exit 2
7. Reversed range rejected, exit 2, **not swapped**
8. Mode flags combined → exit 2
9. Window exceeding `MAX_WINDOW_DAYS` → exit 2
10. DST-free IST boundary correctness at 00:00:00 and 23:59:59.999

**Gmail & ingestion (11–16)**
11. Auth success and expired-token path
12. Pagination across multiple pages
13. Thread grouping by `gmail_thread_id`
14. Thread hydration pulls pre-window messages
15. Quoted-text and signature stripping
16. `internalDate` used, malformed `Date:` header ignored

**Enquiry logic (17–26)**
17. New enquiry detected from inbound message
18. Existing thread updates, creates no second enquiry
19. Non-enquiry filtering: newsletter, OTP, no-reply, out-of-office, internal
20. Outbound-first thread is not an enquiry
21. Pending detection — customer last
22. Attended detection — employee reply after customer (requires SENT mail present)
23. Closed detection on explicit acceptance
24. **Not** closed on "thanks" / "we'll check" / quotation-sent-only
25. Closed enquiry reopened by new inbound message
26. Auto-reply does not flip `last_sender`

**Brands & quotations (27–31)**
27. Single brand extraction and normalisation
28. Multiple brands → counted under each, footnote present
29. Unknown brand surfaces as `Unknown`
30. Four quotation types counted separately
31. Insufficient evidence → metric omitted, not zero, not invented

**Metrics & history (32–36)**
32. Invariant: `Received == Attended + Pending`
33. As-of state — historical window ignores later messages
34. Waiting duration anchored to `window_end`
35. `Closed > Received` renders with its clarifying label
36. Empty window → valid zero report, exit 0

**Robustness (37–44)**
37. Idempotency: identical run twice → identical DB, zero duplicate rows
38. AI verdict cache hit avoids a second API call
39. `--reprocess` bypasses cache
40. Claude failure → raw data preserved, caveat line present, no invented fields
41. Summary containing an unsupported number is rejected, template used
42. Gmail failure → exit 3, DB uncorrupted
43. Telegram failure → exit 5, no success message printed, report text preserved
44. Report exceeding 4096 chars splits correctly at section boundaries

**Manual (45–47)** — run and paste output
45. `python report.py --dry-run` against the real mailbox
46. `python report.py` real Telegram delivery, screenshot or message confirmation
47. Migration against the existing DB file, before/after schema dump

---

## 21. Acceptance Criteria

### 21.1 Primary

```
python report.py
```

connects to `u.ruma@regencyelectricals.com`, reports yesterday 00:00–23:59 IST, and
delivers to the founder's Telegram. Exit 0.

Before fetching any mail, the run confirms the OAuth-authorised account is exactly
`u.ruma@regencyelectricals.com` and aborts with exit 3 on any mismatch, rather than silently
reporting on the wrong mailbox.

### 21.2 Secondary

Each produces the correct window, verified against its printed header:

```
python report.py --today
python report.py --week
python report.py --date 10/09/2026
python report.py --from 10/09/2026 --to 12/09/2026
```

### 21.3 Report Format

```
📊 REGENCY ENQUIRY REPORT
📅 13 September 2026
⏰ 00:00 IST → 23:59 IST

━━━━━━━━━━━━━━━━━━
📩 ENQUIRY SUMMARY
━━━━━━━━━━━━━━━━━━

Received: 24
Attended: 19
Pending: 5
Closed: 8  (includes enquiries received earlier)

━━━━━━━━━━━━━━━━━━
📋 QUOTATION ACTIVITY
━━━━━━━━━━━━━━━━━━

RFQ / quotation requests received: 11
Quotations sent: 7

━━━━━━━━━━━━━━━━━━
🏷️ TOP BRANDS
━━━━━━━━━━━━━━━━━━

1. Schneider — 8
2. Siemens — 5
3. ABB — 4
4. Legrand — 3

Multi-brand enquiries are counted under each brand.

━━━━━━━━━━━━━━━━━━
⚠️ PENDING ENQUIRIES
━━━━━━━━━━━━━━━━━━

1. ABC Industries
   MCCB 250A | Schneider | 20 nos
   Received: 10:42 AM
   Waiting: 5h 18m

2. XYZ Electricals
   Contactor | Siemens
   Received: 2:15 PM
   Waiting: 3h 45m

━━━━━━━━━━━━━━━━━━
🔥 IMPORTANT
━━━━━━━━━━━━━━━━━━

1. ABC Industries — customer states requirement is urgent
   "need delivery by Friday"

━━━━━━━━━━━━━━━━━━
🧠 MANAGEMENT SUMMARY
━━━━━━━━━━━━━━━━━━

<2–4 sentences, plain English, no number absent from the metrics payload>
```

Rules: header always states the period unambiguously; a section with no data is omitted
rather than shown empty, except `ENQUIRY SUMMARY` which always renders; no fabricated
monetary values; priority items always carry their evidence.

### 21.4 Final Deliverable Report

1. Existing architecture discovered (Phase 0 output)
2. Files modified
3. Files created
4. Files deliberately left untouched
5. Schema changes, with before/after dump
6. Required `.env` variables
7. Exact commands supported
8. Example report output — real, not illustrative
9. Telegram delivery confirmation
10. Test results — **pasted pytest output**, with any skips explained
11. Known limitations

---

## 22. Future Extensions

Design for these; implement none of them now.

**Multi-employee** — `python report.py --employee Rahul`. The `mailbox` column on
`enquiries` and mailbox-as-config already accommodate this. Future work adds an employee
config map and a mailbox filter on queries. No reporting-engine changes should be required.

**Second delivery channel** — WhatsApp or email. The `send(text) -> DeliveryResult`
contract means a new channel is a new module plus a config switch.

**Consolidated reports** — combining multiple mailboxes into one founder report.

**Cross-thread linking** — recognising that a new thread continues an old enquiry.
Explicitly out of scope for V1 (§6.3).

**Attachment parsing** — reading quotation PDFs for values and line items.

**Scheduling** — a cron entry calling the same CLI. Requires no code change by design.

---

## Appendix A — Non-Negotiables

The failures most likely to produce a silently wrong report:

1. Gmail query must include **SENT**, or everything looks pending.
2. Threads must be **hydrated** beyond the window, or state is wrong at boundaries.
3. `internalDate`, never the `Date:` header.
4. State **recomputed**, never mutated; `processed` must not skip threads.
5. Brands in their **own table**, counted per distinct brand.
6. Waiting duration anchored to **`window_end`**, not `now()`.
7. Telegram: **plain text**, chunked under 4096.
8. Claude at **temperature 0**, structured output, verdicts cached.
9. Summary numerals **validated** against the metrics payload before sending.
10. `--dry-run` implemented **first**, before any real send.
11. The bot's ingestion path (`app/gmail/sync.py`, `scripts/sync_gmail.py`) and the report
    path must **never both write** to `enquiries` — their status vocabularies are
    incompatible and whichever runs last silently wins. `scripts/sync_gmail.py` is retired
    from use once `report.py` exists.

---

## Appendix B — Implementation Status (paused 2026-09-18)

Not part of the specification itself — a snapshot for whoever resumes this project, so the
first hour back isn't spent re-deriving what's already true. If this section and the rest of
SPEC.md ever disagree about what *should* be built, SPEC.md's body still wins; this appendix
only records what *has been* built and what is still open.

**Complete:** all four implementation stages (config/CLI/timewindow; Gmail
ingestion/idempotency; Claude analysis with cached verdicts; metrics/render/Telegram
delivery). 474 automated tests pass. `python report.py --dry-run` has run end-to-end against
the real mailbox with real Gmail auth, real Claude classification, and a correctly formatted
report. See `PHASE0_AUDIT.md` (repository state before this work started) and
`PHASE0_DECISIONS.md` (the 18 decisions that shaped this build, including the amendments
already folded into the body of this document) for the full history.

**Open, unresolved:**
- A real (non-`--dry-run`) Telegram send has never been performed. `app/telegram/client.py`'s
  `send()` is implemented and tested against mocks only.
- A live diagnostic (`--date 12/09/2026` returning 0 enquiries despite one manually-verified
  genuine enquiry) traced the cause to §6.2's internal-domain exclusion applied literally to
  the sender's address: a colleague on `INTERNAL_DOMAINS` emailed the mailbox owner with
  subject "Enquiry", and per §6.2 that message is never sent to Claude and the thread can
  never become an enquiry, regardless of content. The code matches this document exactly.
  Left open: whether §6.2 should carve out an exception for a staff member relaying an
  external customer's request through their own internal address, which this document
  currently has no mechanism to detect (direction is deterministic from the `From:` header
  only, per §6.1 — never content, never AI). No change has been made pending a decision.
- `.gitignore`'s last line (`PHASE0_AUDIT.md PHASE0_DECISIONS.md`, space-separated) does not
  actually ignore either file — cosmetic, both files are tracked and contain no secrets.
