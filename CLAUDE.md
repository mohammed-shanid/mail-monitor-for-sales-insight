# CLAUDE.md

Regency Enquiry Monitor — local-first Gmail → enquiry analysis → Telegram report tool.

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
