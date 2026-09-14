"""CLI entry point for Gmail -> SQLite ingestion.

For each recent Gmail message: skip if already fully processed;
otherwise store it and either (a) run Claude's new-enquiry extraction
(brand-new thread) or (b) run Claude's reply-intent classification
(existing thread), then apply the business rules in
app.enquiry.status to update SQLite. See app/gmail/sync.py for the
implementation and app/enquiry/status.py for the status rules.

Usage:
    python scripts/sync_gmail.py [max_results]

One-time setup: see README.md (Gmail OAuth via credentials.json,
CLAUDE_API_KEY, COMPANY_EMAIL_DOMAIN in .env).
"""

from __future__ import annotations

import logging
import os
import sys

# Allow running as `python scripts/sync_gmail.py` from anywhere without
# requiring the project to be pip-installed or invoked with `-m`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.db import get_connection  # noqa: E402
from app.gmail.auth import GmailAuthError, get_gmail_service  # noqa: E402
from app.gmail.sync import run_sync  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> None:
    max_results = int(sys.argv[1]) if len(sys.argv) > 1 else 25

    try:
        service = get_gmail_service()
    except GmailAuthError as exc:
        logger.error("Gmail authentication failed: %s", exc)
        raise SystemExit(1)

    with get_connection() as conn:
        fetched = run_sync(conn, service, max_results=max_results)

    print(f"Sync complete: fetched {fetched} message(s) from Gmail.")


if __name__ == "__main__":
    main()
