"""Entry point, CLI, orchestration, terminal UX only (SPEC.md §4.2,
§4.3). No business logic, no SQL here.

Full pipeline as of Stage 4: validates config, resolves the reporting
window, opens the report DB connection (migrating the file in place on
first use), authenticates to Gmail and verifies mailbox identity,
ingests the window's messages, analyzes them with Claude (cached),
persists enquiry state, computes metrics, renders the report, and
delivers it to Telegram (or prints it under --dry-run/--no-send).
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import app.ai.analyze as analyze
import app.config as config
import app.database.db as db
import app.database.queries as queries
import app.gmail.auth as auth
import app.gmail.client as client
import app.gmail.ingest as ingest
import app.telegram.client as telegram
from app.enquiry.brands import UNKNOWN_BRAND
from app.enquiry.brands import normalise as normalise_brand
from app.enquiry.metrics import compute_metrics
from app.reporting.render import render_report
from app.reporting.summary import generate_summary_text
from app.timewindow import WindowError, format_period_header, resolve_window

# --- Exit codes (SPEC.md §13.3). Stage 1 can only reach EXIT_OK and
# EXIT_ARGS; the rest are defined now so every later stage maps onto a
# name that already exists. ---
EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_ARGS = 2
EXIT_GMAIL = 3
EXIT_AI = 4
EXIT_TELEGRAM = 5
EXIT_DB = 6

_USAGE_EXAMPLES = """\
Examples:
  python report.py                                   # yesterday, IST
  python report.py --today
  python report.py --week
  python report.py --date 10/09/2026
  python report.py --from 10/09/2026 --to 12/09/2026
  python report.py --dry-run                          # never sends to Telegram
"""

logger = logging.getLogger("report")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report.py",
        description="Regency Enquiry Monitor -- Gmail -> enquiry analysis -> Telegram report.",
    )

    # Reporting modes (SPEC.md §13.1) -- mutually exclusive, enforced in
    # app.timewindow.resolve_window() so the error message and usage
    # example are ours, not argparse's.
    parser.add_argument("--today", action="store_true", help="Today 00:00 IST -> now.")
    parser.add_argument("--week", action="store_true", help="This week (Mon 00:00 IST) -> now.")
    parser.add_argument("--date", metavar="DD/MM/YYYY", help="A single full day.")
    parser.add_argument("--from", dest="from_date", metavar="DD/MM/YYYY", help="Range start (inclusive).")
    parser.add_argument("--to", dest="to_date", metavar="DD/MM/YYYY", help="Range end (inclusive).")

    # Modifier flags (SPEC.md §13.2).
    parser.add_argument("--dry-run", action="store_true", help="Render the report; never send to Telegram.")
    parser.add_argument("--no-send", action="store_true", help="Alias of --dry-run.")
    parser.add_argument("--json", metavar="PATH", help="Also write the structured metrics payload to PATH.")
    parser.add_argument("--reprocess", action="store_true", help="Ignore the AI verdict cache.")
    parser.add_argument("--limit", type=int, metavar="N", help="Cap messages fetched (development aid).")
    parser.add_argument("--verbose", action="store_true", help="Per-thread decisions and timings to stderr.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress; print only the final status line.")

    return parser


def _print_usage_error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    print(file=sys.stderr)
    print(_USAGE_EXAMPLES, file=sys.stderr)


def _setup_logging(verbose: bool) -> None:
    """Rotating file log at LOG_PATH, created if absent. Terminal stays
    clean by default; --verbose additionally mirrors log records to
    stderr (SPEC.md §17).
    """
    log_path = Path(config.LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    if verbose:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(stderr_handler)


def _now_utc() -> datetime:
    """The only place `_run()` reads the system clock. A thin, private
    wrapper so tests can `monkeypatch.setattr(report, "_now_utc", ...)`
    to freeze it -- SPEC.md/CLAUDE.md forbid `datetime.now()` inside
    business logic, but the CLI's actual "now" has to come from
    somewhere, and this is that one seam.
    """
    return datetime.now(timezone.utc)


def _run(args: argparse.Namespace) -> int:
    try:
        config.validate()
    # `config.ConfigError`, not a name imported once at module load --
    # tests reload app.config to exercise different env combinations,
    # and a statically-imported class reference would go stale the
    # moment that happens, silently falling through to the catch-all
    # in main() instead of this branch.
    except config.ConfigError as exc:
        logger.error("Config error: missing or invalid variable %s", exc)
        _print_usage_error(f"missing or invalid configuration variable: {exc}")
        return EXIT_ARGS

    tz = ZoneInfo(config.REPORT_TIMEZONE)
    now = _now_utc()

    try:
        window = resolve_window(args, now=now, tz=tz)
    except WindowError as exc:
        logger.error("Window error: %s", exc)
        _print_usage_error(str(exc))
        return EXIT_ARGS

    logger.info(
        "Window resolved: mode=%s start_ms=%s end_ms=%s",
        window.mode, window.start_ms, window.end_ms,
    )

    quiet = args.quiet
    if not quiet:
        print("-" * 40)
        print("Regency Enquiry Monitor")
        print("-" * 40)
        print("Mailbox:")
        print(config.REPORT_MAILBOX)
        print()
        print(format_period_header(window, tz))
        print()

    try:
        with db.get_report_connection() as conn:
            if not quiet:
                print("Connecting to Gmail...", end=" ", flush=True)
            service = auth.get_gmail_service(config.GMAIL_CREDENTIALS_PATH, config.GMAIL_TOKEN_PATH)
            auth.verify_mailbox_identity(service, config.REPORT_MAILBOX)
            if not quiet:
                print("ok")
                print("Fetching messages...", end=" ", flush=True)

            result = ingest.ingest_window(
                conn,
                service,
                window_start_ms=window.start_ms,
                window_end_ms=window.end_ms,
                tz=tz,
                mailbox=config.REPORT_MAILBOX,
                employee_aliases=config.EMPLOYEE_ALIASES,
                internal_domains=config.INTERNAL_DOMAINS,
                max_messages=int(config.GMAIL_MAX_MESSAGES),
                ingested_at_ms=int(now.timestamp() * 1000),
                limit=args.limit,
            )

            if not quiet:
                print(f"{result.messages_listed} messages, {result.threads_touched} threads")
                print(f"Hydrating threads...            {result.threads_touched} threads complete")

            touched_thread_ids = queries.get_touched_thread_ids(conn, window.start_ms, window.end_ms)

            if not quiet:
                print("Analyzing enquiry data...", end=" ", flush=True)

            analysis = analyze.analyze_window(
                conn,
                touched_thread_ids,
                window_end_ms=window.end_ms,
                mailbox=config.REPORT_MAILBOX,
                employee_aliases=config.EMPLOYEE_ALIASES,
                internal_domains=config.INTERNAL_DOMAINS,
                model=config.CLAUDE_MODEL,
                max_retries=int(config.AI_MAX_RETRIES),
                reprocess=args.reprocess,
                now_ms=int(now.timestamp() * 1000),
            )

            if not quiet:
                print(f"{analysis.cached_count} messages cached, {analysis.analyzed_count} messages analyzed")

            # SPEC.md §15.5: classification failing for the WHOLE window
            # is fatal (exit 4, nothing sent, nothing rendered); a
            # PARTIAL failure is not -- it becomes a caveat line in the
            # rendered report and a warning here, but the run succeeds.
            attempted = analysis.cached_count + analysis.analyzed_count
            successful = attempted - len(analysis.failed_message_ids)
            if attempted > 0 and successful == 0:
                logger.error("Claude classification failed for every message in the window")
                print(
                    "Claude analysis failed for the entire window; no report generated. "
                    "Raw email data is preserved for the next run.",
                    file=sys.stderr,
                )
                return EXIT_AI

            if analysis.failed_message_ids:
                logger.warning(
                    "%d message(s) could not be analysed this run: %s",
                    len(analysis.failed_message_ids), analysis.failed_message_ids,
                )

            now_ms = int(now.timestamp() * 1000)

            # Persist computed state (SPEC.md §14.2: `enquiries` is a
            # most-recent-run cache, always fully recomputed -- never
            # patched -- from the current run's states).
            for state in analysis.states.values():
                enquiry_id = queries.upsert_enquiry_state(
                    conn, mailbox=config.REPORT_MAILBOX, state=state,
                    computed_as_of=window.end_ms, updated_at=now_ms,
                )
                normalised_brands = [normalise_brand(b) for b in state.brands] if state.brands else [UNKNOWN_BRAND]
                queries.replace_enquiry_brands(conn, enquiry_id, normalised_brands)

            if not quiet:
                print(f"Calculating metrics...           {len(analysis.states)} enquiries")

            metrics = compute_metrics(
                analysis.states, analysis.messages, analysis.message_verdicts, window,
                messages_unanalyzed=len(analysis.failed_message_ids),
            )

            if not quiet:
                print("Generating report...", end=" ", flush=True)

            summary_text = generate_summary_text(metrics, window, tz)
            report_text = render_report(
                metrics, window, tz, summary_text,
                pending_display_limit=int(config.PENDING_DISPLAY_LIMIT),
                priority_display_limit=int(config.PRIORITY_DISPLAY_LIMIT),
            )

            if not quiet:
                print("ok")

            if args.json:
                try:
                    Path(args.json).write_text(json.dumps(asdict(metrics), indent=2))
                except OSError as exc:
                    logger.warning("Could not write --json output to %s: %s", args.json, exc)

            dry_run = args.dry_run or args.no_send
            delivered = False
            delivery_error = None

            if dry_run:
                if not quiet:
                    print()
                    print(report_text)
            else:
                if not quiet:
                    print("Sending Telegram report...", end=" ", flush=True)
                delivery_result = telegram.send(report_text)
                delivered = delivery_result.success
                delivery_error = delivery_result.error
                if not quiet:
                    if delivered:
                        parts_label = "part" if delivery_result.parts_total == 1 else "parts"
                        print(f"ok ({delivery_result.parts_total} {parts_label})")
                    else:
                        print("FAILED")

            queries.insert_report_run(
                conn,
                window_start=window.start_ms, window_end=window.end_ms, mode=window.mode,
                mailbox=config.REPORT_MAILBOX, metrics_json=json.dumps(asdict(metrics)),
                report_text=report_text, delivered=delivered, delivery_error=delivery_error,
                created_at=now_ms,
            )
    except db.DatabaseError as exc:
        logger.error("Database error: %s", exc)
        print(f"Database error: {exc}", file=sys.stderr)
        return EXIT_DB
    except (auth.GmailAuthError, client.GmailFetchError, ingest.IngestAbortedError) as exc:
        if not quiet:
            print("FAILED")
        logger.error("Gmail error: %s", exc)
        print(f"Gmail error: {exc}", file=sys.stderr)
        return EXIT_GMAIL

    # SPEC.md §13.3: --dry-run never returns 5 -- `dry_run` short-
    # circuits this check by construction (delivered is only ever
    # False under dry-run because sending was skipped, never attempted).
    if not dry_run and not delivered:
        logger.error("Telegram delivery failed: %s", delivery_error)
        print(f"Telegram delivery failed: {delivery_error}", file=sys.stderr)
        print("Report text has been preserved (report_runs table) and is shown below:", file=sys.stderr)
        print(report_text, file=sys.stderr)
        return EXIT_TELEGRAM

    if not quiet:
        if dry_run:
            print("✅ Report rendered successfully (--dry-run: not sent).")
        else:
            print("✅ Report sent successfully.")
    else:
        mode_label = "dry-run" if dry_run else "sent"
        print(f"OK: {len(analysis.states)} enquiries, {mode_label}")

    return EXIT_OK


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _setup_logging(args.verbose)
    except OSError as exc:
        # Logging failure is not fatal to Stage 1 -- terminal output
        # still works; there's just nowhere to persist it.
        print(f"Warning: could not open log file: {exc}", file=sys.stderr)

    try:
        return _run(args)
    except Exception:
        logger.exception("Unexpected error")
        print(
            "An unexpected error occurred. See the log file for details.",
            file=sys.stderr,
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
