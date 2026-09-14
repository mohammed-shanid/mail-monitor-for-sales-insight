# PHASE0_DECISIONS.md — Audit Approved, With Amendments

Phase 0 is approved. The audit is accepted as accurate. Below are decisions on all
outstanding questions and the SPEC.md amendments to apply before Stage 1.

Apply the SPEC.md and CLAUDE.md amendments in §B as the **first** action of Stage 1, before
any other code change. Everything else waits for those to land.

---

## A. Answers to Q1–Q18

### Q1 — The bot: option (c), with one addition

Leave all bot code untouched and its tests green. Do not delete anything now. Revisit
deletion after Stage 4 ships.

**Addition the audit did not raise.** The bot's `sync.py` and the report path will both
write the `enquiries` table, with incompatible status vocabularies (`IGNORED`/`REPLIED`
by mutation vs `pending`/`attended`/`closed` by recomputation). Whichever runs last wins,
silently. Therefore:

- `scripts/sync_gmail.py` is **retired from use as of now**. It stays in the tree and stays
  tested, but it is not to be run again against `data/regency.db`.
- Add a one-line docstring note at the top of `scripts/sync_gmail.py` and
  `app/gmail/sync.py` recording that they are superseded by `report.py` for the report path
  and must not be run concurrently with it. Docstring only — no behaviour change.
- `config.py` keeps the old env names as fallbacks so the bot still imports and its tests
  still pass. Nothing is removed.

If at Stage 4 the bot is still unused, we delete it as an explicit approved list.

### Q2 — Naming: accept W1

Keep the existing package names. `app/enquiry/` stands in for `analysis/`, `queries.py` for
`repo.py`, `ai/claude.py` for `ai/client.py`, `telegram/client.py` for `delivery/telegram.py`.
No `git mv`, no rename churn. Amend SPEC §4.2 and CLAUDE.md instead (§B below).

Do **not** create `app/delivery/telegram.py` — extending `app/telegram/client.py` is right,
and a second `_call` would be a duplicate implementation.

### Q3 — DB migration: option (a), backed-up rebuild

Rebuild `emails` and `enquiries` with the SPEC §14.2 types. The live data is 10 rows from a
test mailbox, every one re-derivable from Gmail.

- Back up to `data/regency.db.bak.<timestamp>` before the rebuild, and verify the backup
  exists and is non-zero before proceeding.
- Do **not** backfill `direction` or `sender_domain` on old rows. Let the next ingest
  re-supply them from `internalDate` and the config.
- Do **not** preserve the 6 `IGNORED` enquiry rows. Under SPEC, a non-enquiry thread has no
  `enquiries` row; the verdict lives in `ai_verdicts`.
- Record the rebuild in `schema_meta` as a version step so it never runs twice.

### Q4 — The invariant: option (b)

`Attended` means "not waiting on us" for the received-in-window cohort, and includes
enquiries that were received and closed inside the window. `Received = Attended + Pending`
holds and is asserted in tests. `Closed` remains the independent all-cohorts count, keeping
its "includes enquiries received earlier" label.

Founder's mental model: *of the 24 that came in, 19 are handled, 5 are waiting.*

### Q5 — `new`: drop it

Three as-of statuses only: `pending`, `attended`, `closed`. Remove `new` from SPEC §8.3 and
§14.2. The renderer may describe a never-replied enquiry as "new" in prose; metrics do not
recognise it.

### Q6 — Rejection: yes, it closes

Add to SPEC §8.2 closure evidence: *counterparty explicitly withdraws, rejects, or states
the requirement is no longer live.* Without this, lost leads sit in the pending list forever
with a growing waiting time, which destroys the credibility of that section.

Carry `closure_kind: won | lost | withdrawn | unknown` on the verdict and store it on
`enquiries`. V1 report shows the `Closed` total only; the won/lost split is deferred, but
capture the data now so it is available without a re-analysis later.

### Q7 — Verdict aggregation: accepted as proposed

Enquiry-level fields from the earliest inbound message whose verdict has `is_enquiry=true`.
Later messages may only *add* brands (union) and fill `null`s — never overwrite a non-null
value. Per-message prompt receives preceding thread messages as context and judges the
target message. Cache key stays valid because a message's predecessors never change.

### Q8 — Numeral guard: accepted as proposed

Allowed set = every integer in the metrics payload ∪ the window's day, month, year, and the
hour/minute of both window bounds. Integer comparison, not digit-substring matching. A
summary containing zero numerals is valid and must not be rejected.

### Q9 — Gmail listing: option (a)

Drop `THREAD_LOOKBACK_DAYS` from the list query entirely. List `after:window_start
before:window_end+1day`, then hydrate every touched thread via `threads.get` — which already
pulls pre-window messages, which is the only thing the lookback was for.

Exclude drafts and chats: `-in:drafts -in:chats`. Never write `in:inbox` (Appendix A #1).

`GMAIL_MAX_MESSAGES` applies to the **hydrated** message count, not the listed count.
Keep the abort-on-exceed behaviour.

Daily volume for the target mailbox is not yet known — treat 2000 as provisional and
re-check after the first real `--dry-run`.

### Q10 — Mailbox and token: confirmed target, token must be replaced

Target is `u.ruma@regencyelectricals.com`. The README's `enquiry@regencyelectrical.com`
(missing "s") is wrong and should be corrected; the current `token.json` belongs to a
personal test account and is not the target mailbox.

Manual step before test #45, not a code change:

1. Delete `token.json`.
2. Run the auth flow and consent as `u.ruma@regencyelectricals.com`.
3. Confirm the authorised account matches `REPORT_MAILBOX` at startup and fail fast with a
   clear message if it does not. Use the Gmail profile endpoint for this check; it is a read
   call within the existing scope.

That last check is a **required addition** — silently reporting on the wrong mailbox is the
worst failure mode available to this program.

**Colleague replies.** `INTERNAL_DOMAINS=regencyelectricals.com`, `EMPLOYEE_ALIASES` empty.
For V1, treat any sender on an internal domain as satisfying "attended" — the founder's
question is whether the *customer* is still waiting, and a colleague's reply means they are
not. Store the distinction (`direction=outbound` plus the actual sender) so a future
per-employee report can separate "Ruma answered" from "someone answered", but do not surface
it in V1. Internal-*first*-message threads remain non-enquiries, unchanged.

### Q11 — `TELEGRAM_CHAT_ID`: no code path

Obtain it manually once and put it in `.env`. Do not add `--print-chat-id`. Startup must
fail with exit 2 and a clear message if it is missing or malformed.

### Q12 — Auto-reply detection: accepted as proposed

Deterministic headers first — `Auto-Submitted: auto-replied`, `X-Autoreply`, `X-Autorespond`,
`Precedence: bulk|auto_reply`, and subject prefixes ("Automatic reply:", "Out of Office",
"Out of office:"). Store as `emails.is_auto_reply`. Claude's judgement is a second signal
only when no header is present. Auto-replies never flip `last_sender` and never satisfy
attended.

### Q13 — `computed_as_of`: accepted

`enquiries` is a cache of the most recent run's as-of state. `report_runs.metrics_json` is
the durable, reproducible artefact. Add this sentence to SPEC §14.2 so nobody later mistakes
`enquiries` for history.

### Q14 — Supplier threads: confirmed

Supplier threads are **not** enquiries. They do not count toward Received, Attended, Pending,
or Closed, and do not appear in the brand breakdown. Their inbound quotations count toward
`supplier_quotations_received` and nothing else. State this explicitly in SPEC §7 and §9.

### Q15 — Priority: accepted, with the anchor stated

`is_priority = any(verdict.urgency for messages in thread where received_at <= window_end)`.

The IMPORTANT section lists enquiries that are (received-in-window **or** pending-as-of
window_end) **and** flagged priority, sorted by waiting duration descending, capped at
`PRIORITY_DISPLAY_LIMIT`. Every listed item must carry its evidence string; an item with no
evidence is not listed. Add this to SPEC §11.2 as a metric anchor.

### Q16 — Python 3.9: record, do not upgrade

Log "upgrade to Python 3.11+ recommended" as a known limitation in the final deliverable.
Do not change the interpreter mid-project. Implementation must stay 3.9-compatible: no
`datetime.UTC`, `from __future__ import annotations` for PEP 604 unions, `zoneinfo` from
stdlib.

### Q17 — Body caps: accepted

Store the full cleaned body up to 20 000 chars after quote-stripping. Apply
`AI_BODY_MAX_CHARS=4000` only to the Claude call. Amend SPEC §5.4 to say both numbers.

### Q18 — Track `PHASE0_AUDIT.md` and this file in git

Both are project documentation and part of the final deliverable. Track them. Also track
`SPEC.md` and `CLAUDE.md`, which are currently untracked.

---

## B. SPEC.md and CLAUDE.md amendments to apply first

Apply these before any other Stage 1 work, in one commit, and paste a diff.

| # | File | Change |
|---|---|---|
| 1 | SPEC.md §4.2 | Replace the target layout with the existing names: `app/enquiry/` (not `analysis/`), `app/database/queries.py` (not `repo.py`), `app/ai/claude.py` (not `ai/client.py`), `app/telegram/client.py` (not `delivery/telegram.py`). New files land inside that layout. |
| 2 | CLAUDE.md | "All SQL lives in `app/database/queries.py`." |
| 3 | SPEC.md §8.3 | Drop `new`. Three statuses: `pending`, `attended`, `closed`. |
| 4 | SPEC.md §8.2 | Add rejection/withdrawal as closure evidence. Add `closure_kind`. |
| 5 | SPEC.md §11.2 | State the Q4 invariant resolution. Add the Q15 priority anchor. |
| 6 | SPEC.md §14.1 | Amend "additive only" to permit a backed-up table rebuild where a column type must change, per Q3. |
| 7 | SPEC.md §14.2 | `enquiries` is a most-recent-run cache; `report_runs` is the durable artefact. |
| 8 | SPEC.md §15.3 | `quantity` is a **string**. Add `closure_kind`. Enumerate `quotation_signal`: `rfq_received \| quotation_sent \| quotation_response \| supplier_quotation \| null` (W13). |
| 9 | SPEC.md §15.4 | Numeral guard per Q8. |
| 10 | SPEC.md §5.2 | Drop `THREAD_LOOKBACK_DAYS`. Add `-in:drafts -in:chats`. `GMAIL_MAX_MESSAGES` counts hydrated messages. |
| 11 | SPEC.md §5.4 | Storage cap 20 000; AI cap 4 000. |
| 12 | SPEC.md §7, §9 | Supplier threads are not enquiries (Q14). |
| 13 | SPEC.md §12 | Confirm W14: a future *start* is rejected; a future *end* is clamped. |
| 14 | SPEC.md §13.4 | W9 — the analysing line reports message counts, not thread counts. |
| 15 | SPEC.md §19 | `DATABASE_PATH=data/regency.db`. Keep old env names as documented fallbacks. Add the startup mailbox-identity check from Q10. |
| 16 | SPEC.md §21.1 | Add: the authorised Gmail account must equal `REPORT_MAILBOX` or the run aborts. |
| 17 | SPEC.md Appendix A | Add #11: the bot's write path and the report path must never both write `enquiries`. |
| 18 | `.gitignore` | Add `*.db`, `*.db-wal`, `*.db-shm`, `*.bak.*`, `logs/`. |

---

## C. Stage 1 scope

Once §B is committed:

1. `app/config.py` — all SPEC §19 variables, defaults, `validate()` with fail-fast naming
   the missing variable and never its value, old names accepted as fallbacks.
2. `app/timewindow.py` — `resolve_window(args, now, tz)`, strict `DD/MM/YYYY`, Monday week
   start, inclusive bounds, clamping, all §12 validation → exit 2.
3. `report.py` skeleton — argparse with every §13 mode and modifier, exit-code contract,
   §13.4 terminal output, rotating log at `LOG_PATH`. Prints the resolved window and exits
   0. No Gmail, no Claude, no Telegram yet.
4. `tests/conftest.py` — shared `db_path`, frozen clock, fixture loaders.
5. `tests/test_timewindow.py` — SPEC §20.2 cases 1–10.
6. `tests/test_report_cli.py` — argument validation and exit 2 paths.
7. `.env.example` updated.

Stage 1 is done when `pytest -v` passes with real pasted output, all 177 existing tests
still pass, and each of the five reporting modes prints a correct period header.

Switch to Sonnet for Stage 1. Stop for approval before Stage 2.
