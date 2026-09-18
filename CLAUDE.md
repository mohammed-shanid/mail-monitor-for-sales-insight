# CLAUDE.md

Regency Enquiry Monitor — local-first Gmail → enquiry analysis → Telegram report tool.

## Status: PAUSED (2026-09-18) — read this before resuming

The project is feature-complete through Stage 4 and paused here, not abandoned. Before
touching code, read `PHASE0_AUDIT.md` and `PHASE0_DECISIONS.md` for how we got here, then
this section for where "here" actually is.

**Built and working:**
- All four stages done: config/CLI/timezone (`report.py`, `app/timewindow.py`), Gmail
  ingestion with idempotency (`app/gmail/ingest.py`, `app/gmail/parser.py`), the Claude
  analysis layer with cached verdicts (`app/ai/`), and metrics/render/Telegram delivery
  (`app/enquiry/metrics.py`, `app/reporting/`, `app/telegram/client.py`).
- **474 tests passing** (`pytest -v`, zero network/DB in the suite).
- `python report.py --dry-run` has been run for real against the live mailbox
  (`u.ruma@regencyelectricals.com`) end-to-end: real Gmail auth + mailbox-identity check,
  real Claude classification, correctly formatted report. `.env` has `REPORT_MAILBOX`,
  `TELEGRAM_CHAT_ID`, and the rest of the required config already filled in.
- The bot (`app/router/`, `app/telegram/bot.py`, `scripts/*`) is untouched and still green,
  per decision Q1 — not deleted, not merged with the report path.

**Not done — do these before calling it live:**
1. **A real (non-dry-run) Telegram send has never been smoke-tested.** `send()` is fully
   implemented and unit-tested against mocks, but nobody has run `python report.py` without
   `--dry-run` and confirmed a message actually lands in the founder's Telegram.
2. **Open diagnostic, unresolved:** `--date 12/09/2026` returned 0 enquiries despite one
   manually-verified genuine enquiry existing that day. Root cause traced to
   `app/ai/analyze.py:70-71` / `app/enquiry/thread_state.py:77` — a message from an
   internal-domain sender (`bharat@regencyelectricals.com`, subject "Enquiry", sent *to* the
   mailbox owner) is correctly excluded per SPEC §6.2 ("internal threads are never
   enquiries"), because SPEC derives direction/internal status solely from the `From:`
   header, never from content. This is the code doing exactly what SPEC says — the open
   question is whether SPEC §6.2 should have an exception for a staff member relaying an
   external customer's request through their own internal address. **Nothing was changed.**
   Confirm with the founder whether that Bharath thread is the enquiry they verified before
   deciding whether this is a SPEC amendment or accepted behavior.
3. **Cosmetic:** `.gitignore`'s last line is `PHASE0_AUDIT.md PHASE0_DECISIONS.md`
   (space-separated on one line) — not valid gitignore syntax for two entries, so neither is
   actually ignored (harmless; both are tracked and contain no secrets, just not what
   whoever wrote that line probably intended).

**Git:** everything above is committed and pushed to `origin/main` on the public repo
`mohammed-shanid/mail-monitor-for-sales-insight`. Working tree was clean as of this pause.

## Prime directive

This is an **existing repository**. Modify it incrementally.
Do not rebuild from scratch. Do not create a parallel app. Do not rewrite working code.

## Before doing anything

Read `SPEC.md`. It is authoritative — where SPEC.md and any prompt, comment, or existing
code disagree, **SPEC.md wins**. If a request contradicts SPEC.md, say so before acting.

Work in the stages I give you. When a stage says "stop for approval", stop.

## Commands

```bash
python report.py --dry-run        # full pipeline, no Telegram send — use this by default
python report.py                  # yesterday, real delivery
python report.py --today
python report.py --week
python report.py --date 10/09/2026
python report.py --from 10/09/2026 --to 12/09/2026

pytest -v                         # all tests
pytest tests/test_timewindow.py -v
```

## Hard constraints

- No server, API, web UI, daemon, scheduler, or Docker.
- No Postgres, Redis, Celery, vector DB, ORM, or web framework.
- No WhatsApp or any second delivery channel.
- No multi-employee support, no `--employee` flag.
- No cross-thread duplicate detection, no attachment parsing.
- Gmail scope is **read-only**. Never request write/send/modify.
- Never add a dependency without telling me what and why first.

## Conventions

- All SQL lives in `app/database/queries.py`. Nowhere else.
- Timestamps stored as **UTC epoch ms**. Conversion to IST only in `timewindow.py` and the
  renderer. `zoneinfo` only, never `pytz`, never naive datetimes.
- No `datetime.now()` inside business logic — inject the clock so tests are stable.
- `app/enquiry/` must be testable with zero network and zero DB.
- `app/telegram/client.py` knows nothing about enquiries. Its surface is
  `send(text) -> DeliveryResult`.
- Claude calls: `temperature=0`, structured output, verdicts cached. Never regex over
  free-text model output.
- Enquiry state is **recomputed** from the full thread every run, never mutated in place.

## Safety rails

- Never send to real Telegram while testing. Use `--dry-run`.
- Never print or log secrets, tokens, chat IDs, or full email bodies.
- Never delete, recreate, or destructively alter the SQLite file. Migrations are additive
  only, with a backup first.
- `null` beats a guess. Never invent an extracted field, a metric, or a number.

## Definition of done

A change is not done until tests exist and you have **pasted the real pytest output**.
"It should work" is not a test result. Do not claim the implementation works untested.

## Where this usually goes wrong

See `SPEC.md` Appendix A — the ten failures that produce a silently *wrong* report rather
than a crash. Check that list before declaring any stage complete.
