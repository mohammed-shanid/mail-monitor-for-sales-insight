"""Gmail OAuth authentication for the monitored mailbox.

Read-only access only (app.config.GMAIL_SCOPES is the single source of
truth for the scope). This module never sees or stores the mailbox
password, and never touches browser cookies/sessions -- it relies
entirely on Google's standard OAuth desktop-app consent flow.

Flow:
    1. Look for a cached token (GOOGLE_TOKEN_FILE).
    2. If it's expired but refreshable, refresh it.
    3. Otherwise, run the interactive OAuth consent flow using
       GOOGLE_CREDENTIALS_FILE (a Google Cloud "Desktop app" OAuth
       client, downloaded once by a developer -- see README.md).
    4. Cache the resulting token so future runs skip the browser step.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import GMAIL_SCOPES, GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE

logger = logging.getLogger(__name__)


class GmailAuthError(RuntimeError):
    """Raised when Gmail credentials cannot be obtained (e.g. missing
    credentials.json, or a token that can't be refreshed and there's
    no way to run an interactive flow), or when the authorised account
    does not match the configured mailbox (see verify_mailbox_identity,
    added Stage 2)."""


def load_credentials(
    credentials_path: Optional[str] = None, token_path: Optional[str] = None
) -> Credentials:
    """Return valid OAuth credentials for the mailbox, refreshing a
    cached token or starting interactive consent as needed.

    `credentials_path`/`token_path` default to the bot-era
    GOOGLE_CREDENTIALS_FILE/GOOGLE_TOKEN_FILE module constants when
    omitted (unchanged zero-arg behaviour for the bot and its tests).
    The report path (app.gmail.ingest) passes SPEC.md §19's
    GMAIL_CREDENTIALS_PATH/GMAIL_TOKEN_PATH explicitly instead --
    otherwise those variables would have no actual effect on which
    files get used, since a bare os.environ-backed default here would
    never see them.

    Never logs token or credential file contents.
    """
    creds_path = credentials_path if credentials_path is not None else GOOGLE_CREDENTIALS_FILE
    tok_path = token_path if token_path is not None else GOOGLE_TOKEN_FILE

    creds: Credentials | None = None

    if os.path.exists(tok_path):
        try:
            creds = Credentials.from_authorized_user_file(tok_path, GMAIL_SCOPES)
        except (ValueError, OSError) as exc:
            logger.warning("Could not read cached Gmail token (%s); re-authenticating", exc)
            creds = None

    if creds and creds.valid:
        return creds

    # Tracks whether we obtained/changed a token in this call, so we
    # only rewrite the cache file when there's actually something new
    # to save.
    needs_save = False

    if creds and creds.expired and creds.refresh_token:
        logger.info("Gmail token expired; refreshing")
        try:
            creds.refresh(Request())
            needs_save = True
        except Exception as exc:  # refresh_token can be revoked, network can fail, etc.
            logger.warning("Gmail token refresh failed (%s); falling back to interactive auth", exc)
            creds = None

    if not creds or not creds.valid:
        if not os.path.exists(creds_path):
            raise GmailAuthError(
                f"'{creds_path}' not found. Download an OAuth "
                "'Desktop app' client ID from Google Cloud Console "
                "(APIs & Services > Credentials), save it there, or set "
                "GOOGLE_CREDENTIALS_FILE (bot) / GMAIL_CREDENTIALS_PATH "
                "(report.py) to point at it. See README.md."
            )
        logger.info("No valid cached Gmail token; starting interactive OAuth consent flow")
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        needs_save = True

    if needs_save:
        with open(tok_path, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("Gmail token cached to %s", tok_path)

    return creds


def get_gmail_service(credentials_path: Optional[str] = None, token_path: Optional[str] = None):
    """Authenticate and return an authorized, read-only Gmail API client."""
    creds = load_credentials(credentials_path, token_path)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# =============================================================================
# Report path (SPEC.md §19, §21.1), added Stage 2.
# =============================================================================


def verify_mailbox_identity(service, expected_mailbox: str) -> None:
    """Confirm the OAuth-authorised account is exactly `expected_mailbox`
    (SPEC.md §19, §21.1, PHASE0_DECISIONS.md Q10) -- a read call within
    the existing read-only scope (`users.getProfile`). Called before
    any mail is fetched; raises GmailAuthError on any mismatch or API
    failure, so a report is never silently generated for the wrong
    mailbox -- the worst failure mode available to this program.
    """
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        raise GmailAuthError(f"Could not verify the authorised Gmail account: {exc}") from exc

    authorised = (profile.get("emailAddress") or "").strip().lower()
    expected = (expected_mailbox or "").strip().lower()
    if authorised != expected:
        raise GmailAuthError(
            f"Authorised Gmail account ({authorised!r}) does not match "
            f"REPORT_MAILBOX ({expected_mailbox!r}). Delete the cached token "
            "and re-run the OAuth consent flow as the correct account."
        )
