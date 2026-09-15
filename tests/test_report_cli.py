"""report.py argument validation and terminal output, through Stage 3.

`report.main(argv)` returns an int exit code rather than calling
`sys.exit()` directly, so tests call it in-process and assert on the
return value plus captured stdout/stderr (pytest's `capsys`).

The system clock is frozen via `monkeypatch.setattr(report, "_now_utc",
...)` -- the one seam report.py exposes for it. Required config is
supplied via the shared `tmp_env` fixture, and DATABASE_PATH is always
pointed at a temp file.

CRITICAL: `mock_pipeline` (autouse) replaces every Gmail/DB/Claude call
report.py makes -- app.database.db.get_report_connection,
app.gmail.auth.get_gmail_service/verify_mailbox_identity,
app.gmail.ingest.ingest_window, app.database.queries.
get_touched_thread_ids, and app.ai.analyze.analyze_window -- with
in-process fakes, for EVERY test in this file, whether or not the test
asks for it by name. Once report.py started calling real Gmail/DB/
Claude code, a test that forgot this mock would perform a real OAuth
token refresh, a real Gmail API fetch, and a real schema migration
against the file at DATABASE_PATH -- exactly what happened once
already during this project (see the Stage 2 completion report).
DATABASE_PATH is also pinned to a temp file as a second, independent
layer of protection: even a test that somehow bypasses the mock still
cannot reach the real data/regency.db.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import report
from report import EXIT_AI, EXIT_ARGS, EXIT_DB, EXIT_GMAIL, EXIT_OK, EXIT_TELEGRAM
from app.ai.analyze import AnalysisResult
from app.gmail.auth import GmailAuthError
from app.gmail.client import GmailFetchError
from app.gmail.ingest import IngestAbortedError, IngestResult
from app.database.db import DatabaseError
from app.telegram.client import DeliveryResult

IST = ZoneInfo("Asia/Kolkata")

_VALID_ENV = {
    "REPORT_MAILBOX": "u.ruma@regencyelectricals.com",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "TELEGRAM_BOT_TOKEN": "bot-token-test",
    "TELEGRAM_CHAT_ID": "123456789",
}

_EMPTY_RESULT = IngestResult(
    messages_listed=0, threads_touched=0, messages_hydrated=0, messages_new=0
)
_EMPTY_ANALYSIS = AnalysisResult(states={}, cached_count=0, analyzed_count=0, failed_message_ids=[])


def _fake_state(thread_id="t1"):
    from app.enquiry.models import EnquiryState, ReportStatus

    return EnquiryState(
        gmail_thread_id=thread_id, status=ReportStatus.PENDING, last_sender="customer",
        customer_name="ABC Industries", company="ABC Industries", customer_email="a@abc.com",
        counterparty_type="customer", subject="s", product="p", requirement="r", quantity="q",
        brands=[], is_priority=False, priority_evidence=None,
        received_at=1_000_000, last_activity_at=1_000_000, closed_at=None,
        closure_evidence=None, closure_kind=None,
    )


@pytest.fixture
def valid_config(tmp_env, tmp_path):
    """A fully-valid required-config environment, with DATABASE_PATH
    pinned to a temp file -- belt-and-suspenders alongside
    `mock_pipeline` below, so no test in this file can ever reach the
    real data/regency.db even if a mock is accidentally bypassed.
    """
    return tmp_env({**_VALID_ENV, "DATABASE_PATH": str(tmp_path / "test-report.db")})


@pytest.fixture(autouse=True)
def mock_pipeline(monkeypatch):
    """Replace every real Gmail/DB/Claude call report.py can make with
    an in-process fake. Autouse: applies to every test in this file
    unconditionally -- see module docstring for why that is not
    optional. A test that wants to exercise a specific failure
    re-patches the relevant piece on top of this baseline.
    """
    fake_conn = MagicMock(name="fake_sqlite_connection")
    monkeypatch.setattr(
        report.db, "get_report_connection", lambda *a, **k: contextlib.nullcontext(fake_conn)
    )
    monkeypatch.setattr(report.auth, "get_gmail_service", lambda *a, **k: MagicMock(name="fake_gmail_service"))
    monkeypatch.setattr(report.auth, "verify_mailbox_identity", lambda *a, **k: None)
    monkeypatch.setattr(report.ingest, "ingest_window", lambda *a, **k: _EMPTY_RESULT)
    monkeypatch.setattr(report.queries, "get_touched_thread_ids", lambda *a, **k: [])
    monkeypatch.setattr(report.analyze, "analyze_window", lambda *a, **k: _EMPTY_ANALYSIS)
    # CRITICAL: without these two, a test that doesn't pass --dry-run
    # would call the REAL app.ai.claude.generate_summary (a real Claude
    # API call) and the REAL app.telegram.client.send (a real Telegram
    # send) -- report.py's own module-level names, patched directly so
    # every call site (however it imported them) sees the fake.
    monkeypatch.setattr(report, "generate_summary_text", lambda *a, **k: "Test summary.")
    monkeypatch.setattr(
        report.telegram, "send",
        lambda *a, **k: DeliveryResult(success=True, parts_sent=1, parts_total=1),
    )
    return fake_conn


def _freeze(monkeypatch, year, month, day, hour=10, minute=0, second=0):
    fixed_now = datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(
        ZoneInfo("UTC")
    )
    monkeypatch.setattr(report, "_now_utc", lambda: fixed_now)


# --- Every mode produces the right period header ----------------------------


def test_default_mode_period_header(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)  # Monday

    exit_code = report.main([])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Report period:" in out
    assert "13 September 2026" in out
    assert "00:00 IST → 23:59 IST" in out
    assert "Mailbox:" in out
    assert "u.ruma@regencyelectricals.com" in out


def test_today_mode_period_header(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 14, 32, 0)

    exit_code = report.main(["--today"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "14 September 2026" in out
    assert "00:00 IST → 14:32 IST" in out


def test_week_mode_period_header(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 16, 11, 0, 0)  # Wednesday; Monday = 14 Sep

    exit_code = report.main(["--week"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "14 September 2026 → 16 September 2026" in out
    assert "00:00 IST → 11:00 IST" in out


def test_date_mode_period_header(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--date", "10/09/2026"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "10 September 2026" in out
    assert "00:00 IST → 23:59 IST" in out


def test_range_mode_period_header(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--from", "10/09/2026", "--to", "12/09/2026"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "10 September 2026 → 12 September 2026" in out
    assert "00:00 IST → 23:59 IST" in out


# --- Rejection cases: exit 2, usage shown -----------------------------------


def test_mutually_exclusive_mode_flags_rejected(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--today", "--week"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ARGS
    assert "Error:" in captured.err
    assert "Examples:" in captured.err
    assert "python report.py" in captured.err
    assert "Report period:" not in captured.out


def test_from_without_to_rejected(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--from", "10/09/2026"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ARGS
    assert "Error:" in captured.err
    assert "Examples:" in captured.err


def test_to_without_from_rejected(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--to", "10/09/2026"])

    assert exit_code == EXIT_ARGS


def test_malformed_date_rejected(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--date", "2026-09-10"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ARGS
    assert "Error:" in captured.err
    assert "Examples:" in captured.err


def test_reversed_range_rejected_and_not_swapped(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--from", "12/09/2026", "--to", "10/09/2026"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ARGS
    assert "Error:" in captured.err
    # Not silently swapped into a valid 10->12 window: no success output
    # of any kind reached stdout.
    assert captured.out == ""


def test_future_date_rejected(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--date", "20/09/2026"])

    assert exit_code == EXIT_ARGS


def test_missing_required_config_exit_2(tmp_env, tmp_path, monkeypatch, capsys):
    tmp_env({**_VALID_ENV, "REPORT_MAILBOX": None, "DATABASE_PATH": str(tmp_path / "test-report.db")})
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ARGS
    assert "Error:" in captured.err
    assert "REPORT_MAILBOX" in captured.err
    assert "Examples:" in captured.err


# --- --dry-run and --no-send are equivalent ---------------------------------


def test_dry_run_and_no_send_produce_identical_output(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    exit_code_dry = report.main(["--dry-run"])
    out_dry = capsys.readouterr().out

    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    exit_code_no_send = report.main(["--no-send"])
    out_no_send = capsys.readouterr().out

    assert exit_code_dry == exit_code_no_send == EXIT_OK
    assert out_dry == out_no_send


# --- --quiet / --verbose -----------------------------------------------------


def test_quiet_mode_prints_only_final_status_line(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--quiet"])

    out = capsys.readouterr().out.strip()
    assert exit_code == EXIT_OK
    assert out == "OK: 0 enquiries, sent"


def test_verbose_mode_mirrors_log_to_stderr(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main(["--verbose"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert "Window resolved" in captured.err


# --- Stage 2/3 pipeline: Gmail connect/fetch/analyze progress ---------------


def test_successful_run_shows_gmail_and_analysis_progress(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    fake_result = IngestResult(messages_listed=5, threads_touched=2, messages_hydrated=6, messages_new=4)
    monkeypatch.setattr(report.ingest, "ingest_window", lambda *a, **k: fake_result)
    monkeypatch.setattr(report.queries, "get_touched_thread_ids", lambda *a, **k: ["t1", "t2"])
    fake_analysis = AnalysisResult(states={"t1": _fake_state("t1")}, cached_count=3, analyzed_count=2, failed_message_ids=[])
    monkeypatch.setattr(report.analyze, "analyze_window", lambda *a, **k: fake_analysis)

    exit_code = report.main([])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Connecting to Gmail..." in out
    assert "ok" in out
    assert "Fetching messages..." in out
    assert "5 messages, 2 threads" in out
    assert "Hydrating threads" in out
    assert "2 threads complete" in out
    assert "Analyzing enquiry data..." in out
    assert "3 messages cached, 2 messages analyzed" in out
    assert "Calculating metrics" in out
    assert "1 enquiries" in out
    assert "Generating report..." in out
    assert "Sending Telegram report..." in out
    assert "✅ Report sent successfully." in out


def test_partial_analysis_failure_shows_caveat_but_still_succeeds(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    fake_analysis = AnalysisResult(
        states={"t1": _fake_state("t1")}, cached_count=0, analyzed_count=3, failed_message_ids=["m1"]
    )
    monkeypatch.setattr(report.analyze, "analyze_window", lambda *a, **k: fake_analysis)

    # --dry-run so the rendered report body (which carries the caveat
    # line) is printed to stdout instead of only handed to telegram.send.
    exit_code = report.main(["--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "1 message(s) could not be analysed" in out


def test_total_analysis_failure_maps_to_exit_4(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    fake_analysis = AnalysisResult(states={}, cached_count=0, analyzed_count=2, failed_message_ids=["m1", "m2"])
    monkeypatch.setattr(report.analyze, "analyze_window", lambda *a, **k: fake_analysis)

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_AI
    assert "failed for the entire window" in captured.err


def test_ingest_window_receives_resolved_window_and_config(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    captured_kwargs = {}

    def fake_ingest(conn, service, **kwargs):
        captured_kwargs.update(kwargs)
        return _EMPTY_RESULT

    monkeypatch.setattr(report.ingest, "ingest_window", fake_ingest)

    report.main(["--date", "10/09/2026", "--limit", "50"])

    assert captured_kwargs["mailbox"] == "u.ruma@regencyelectricals.com"
    assert captured_kwargs["limit"] == 50
    assert captured_kwargs["window_start_ms"] < captured_kwargs["window_end_ms"]


def test_gmail_auth_error_maps_to_exit_3(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    monkeypatch.setattr(
        report.auth,
        "verify_mailbox_identity",
        lambda *a, **k: (_ for _ in ()).throw(GmailAuthError("account mismatch")),
    )

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_GMAIL
    assert "Gmail error" in captured.err
    assert "account mismatch" in captured.err


def test_gmail_fetch_error_maps_to_exit_3(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    def raise_fetch_error(*a, **k):
        raise GmailFetchError("Gmail API error while listing messages: boom")

    monkeypatch.setattr(report.ingest, "ingest_window", raise_fetch_error)

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_GMAIL
    assert "Gmail error" in captured.err


def test_ingest_aborted_error_maps_to_exit_3(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    def raise_aborted(*a, **k):
        raise IngestAbortedError("2500 messages exceed GMAIL_MAX_MESSAGES (2000)")

    monkeypatch.setattr(report.ingest, "ingest_window", raise_aborted)

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_GMAIL
    assert "exceed GMAIL_MAX_MESSAGES" in captured.err


def test_database_error_maps_to_exit_6(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    def raise_db_error(*a, **k):
        raise DatabaseError("Migration backup verification failed: empty backup file")

    monkeypatch.setattr(report.db, "get_report_connection", raise_db_error)

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_DB
    assert "Database error" in captured.err


# --- Stage 4: metrics/render/persistence/Telegram delivery ------------------


def test_dry_run_prints_report_and_never_calls_telegram_send(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    send_calls = []
    monkeypatch.setattr(report.telegram, "send", lambda text: send_calls.append(text) or DeliveryResult(True, 1, 1))

    exit_code = report.main(["--dry-run"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert send_calls == []  # never called under --dry-run
    assert "📊 REGENCY ENQUIRY REPORT" in out
    assert "📩 ENQUIRY SUMMARY" in out
    assert "🧠 MANAGEMENT SUMMARY" in out
    assert "Test summary." in out


def test_real_send_mode_does_not_print_full_report_to_stdout(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)

    exit_code = report.main([])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    # The full rendered report body is handed to telegram.send(), not
    # printed to the terminal, when actually sending.
    assert "📩 ENQUIRY SUMMARY" not in out
    assert "✅ Report sent successfully." in out


def test_telegram_delivery_failure_maps_to_exit_5(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    monkeypatch.setattr(
        report.telegram, "send",
        lambda text: DeliveryResult(success=False, parts_sent=0, parts_total=1, error="Telegram API error: boom"),
    )

    exit_code = report.main([])

    captured = capsys.readouterr()
    assert exit_code == EXIT_TELEGRAM
    assert "Telegram delivery failed" in captured.err
    # Report text preserved and shown, not silently lost (SPEC.md §17).
    assert "📩 ENQUIRY SUMMARY" in captured.err


def test_telegram_failure_is_recorded_in_report_run(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    monkeypatch.setattr(
        report.telegram, "send",
        lambda text: DeliveryResult(success=False, parts_sent=0, parts_total=1, error="Telegram API error: boom"),
    )
    recorded = {}
    monkeypatch.setattr(
        report.queries, "insert_report_run",
        lambda conn, **kwargs: recorded.update(kwargs) or 1,
    )

    exit_code = report.main([])

    assert exit_code == EXIT_TELEGRAM
    assert recorded["delivered"] is False
    assert recorded["delivery_error"] == "Telegram API error: boom"


def test_dry_run_never_returns_exit_5_even_if_send_would_fail(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    monkeypatch.setattr(
        report.telegram, "send",
        lambda text: DeliveryResult(success=False, parts_sent=0, parts_total=1, error="should never be called"),
    )

    exit_code = report.main(["--dry-run"])

    assert exit_code == EXIT_OK  # --dry-run never returns 5 (SPEC.md §13.3)


def test_json_flag_writes_metrics_payload(valid_config, monkeypatch, capsys, tmp_path):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    json_path = tmp_path / "metrics.json"

    exit_code = report.main(["--dry-run", "--json", str(json_path)])

    assert exit_code == EXIT_OK
    payload = json.loads(json_path.read_text())
    assert payload["received"] == 0
    assert "brand_counts" in payload


def test_persists_enquiry_state_and_brands(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    fake_analysis = AnalysisResult(states={"t1": _fake_state("t1")}, cached_count=1, analyzed_count=0, failed_message_ids=[])
    monkeypatch.setattr(report.analyze, "analyze_window", lambda *a, **k: fake_analysis)

    persisted = {}

    def fake_upsert(conn, *, mailbox, state, computed_as_of, updated_at):
        persisted["state"] = state
        persisted["mailbox"] = mailbox
        return 42

    brands_persisted = {}
    monkeypatch.setattr(report.queries, "upsert_enquiry_state", fake_upsert)
    monkeypatch.setattr(
        report.queries, "replace_enquiry_brands",
        lambda conn, enquiry_id, brands: brands_persisted.update({enquiry_id: brands}),
    )

    exit_code = report.main(["--dry-run"])

    assert exit_code == EXIT_OK
    assert persisted["state"].gmail_thread_id == "t1"
    assert persisted["mailbox"] == "u.ruma@regencyelectricals.com"
    assert 42 in brands_persisted


def test_records_report_run(valid_config, monkeypatch, capsys):
    _freeze(monkeypatch, 2026, 9, 14, 10, 0, 0)
    recorded = {}

    def fake_insert(conn, **kwargs):
        recorded.update(kwargs)
        return 1

    monkeypatch.setattr(report.queries, "insert_report_run", fake_insert)

    exit_code = report.main(["--dry-run"])

    assert exit_code == EXIT_OK
    assert recorded["mailbox"] == "u.ruma@regencyelectricals.com"
    assert recorded["delivered"] is False  # dry-run never delivers
    assert "REGENCY ENQUIRY REPORT" in recorded["report_text"]
