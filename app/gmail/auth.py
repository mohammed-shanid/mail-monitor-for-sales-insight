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

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import GMAIL_SCOPES, GOOGLE_CREDENTIALS_FILE, GOOGLE_TOKEN_FILE

logger = logging.getLogger(__name__)


class GmailAuthError(RuntimeError):
    """Raised when Gmail credentials cannot be obtained (e.g. missing
    credentials.json, or a token that can't be refreshed and there's
    no way to run an interactive flow)."""


def load_credentials() -> Credentials:
    """Return valid OAuth credentials for the mailbox, refreshing a
    cached token or starting interactive consent as needed.

    Never logs token or credential file contents.
    """
    creds: Credentials | None = None

    if os.path.exists(GOOGLE_TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GMAIL_SCOPES)
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
        if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
            raise GmailAuthError(
                f"'{GOOGLE_CREDENTIALS_FILE}' not found. Download an OAuth "
                "'Desktop app' client ID from Google Cloud Console "
                "(APIs & Services > Credentials), save it there, or set "
                "GOOGLE_CREDENTIALS_FILE to point at it. See README.md."
            )
        logger.info("No valid cached Gmail token; starting interactive OAuth consent flow")
        flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
        needs_save = True

    if needs_save:
        with open(GOOGLE_TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("Gmail token cached to %s", GOOGLE_TOKEN_FILE)

    return creds


def get_gmail_service():
    """Authenticate and return an authorized, read-only Gmail API client."""
    creds = load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
