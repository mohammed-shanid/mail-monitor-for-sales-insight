"""Centralized configuration, loaded from environment variables (and a
local .env file, if present).

V1 keeps this deliberately small: read env vars once, expose them as
plain constants, let modules import what they need. No config
frameworks, no validation DSLs, no secrets committed to git -- see
.env.example for the full list of variables and .gitignore for what
is excluded from version control.

This module now carries two generations of variable names:
  - The original bot-era names (`GOOGLE_CREDENTIALS_FILE`,
    `CLAUDE_API_KEY`, `COMPANY_EMAIL_DOMAIN`, ...), still read directly
    by `app/gmail/sync.py`, `app/ai/claude.py`, `app/enquiry/status.py`,
    and their tests.
  - The SPEC.md §19 names (`GMAIL_CREDENTIALS_PATH`, `ANTHROPIC_API_KEY`,
    `INTERNAL_DOMAINS`, ...), used by `report.py` and everything under
    it.
Where a SPEC name has a bot-era predecessor, the SPEC name falls back to
the old one when unset (decision Q1/Q2 in PHASE0_DECISIONS.md) -- the
SPEC name always wins when both are set. Nothing is removed: importing
this module never raises, so the bot keeps working exactly as before.
Only `validate()` -- called explicitly by `report.py`, never at import
time -- can raise, and only for the report path's required variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Loads variables from a .env file in the current working directory,
# if one exists. Real environment variables always win (override=False
# is the default), so this never clobbers variables set another way
# (e.g. by systemd in production).
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised only by validate(), and only ever with the offending
    variable's NAME as the message -- never its value, and never a
    partial value. report.py maps this to exit code 2.
    """


def redact(value: Optional[str]) -> str:
    """What every error path prints instead of an actual config value.
    Never let a secret, token, or chat id reach a log line or the
    terminal (SPEC.md §17, §19).
    """
    if not value:
        return "<empty>"
    return "<redacted>"


def _get(name: str, default: str = "", *, legacy: Optional[str] = None) -> str:
    """Read `name` from the environment, falling back to `legacy` (a
    bot-era variable name) only when `name` itself is unset or blank,
    then to `default` if neither is set. `name` always takes precedence
    over `legacy` when both have a non-empty value (SPEC.md §19).

    Deliberately a truthy check, not `is not None`: every key in
    .env.example is shipped blank (`KEY=`), and once python-dotenv has
    loaded a .env file copied from that template, a blank SPEC-name key
    is *present* in os.environ -- an `is not None` check would treat it
    as "set" and never fall through to `legacy` or `default`, silently
    defeating both for anyone who copies the template as-is.
    """
    value = os.environ.get(name)
    if value:
        return value
    if legacy is not None:
        legacy_value = os.environ.get(legacy)
        if legacy_value:
            return legacy_value
    return default


def _parse_list(raw: str) -> List[str]:
    """Comma-separated -> lowercase, whitespace-stripped list. An empty
    (or whitespace-only) string is an empty list, never `[""]`.
    """
    if not raw.strip():
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _parse_positive_int(name: str, raw: str) -> int:
    """Parse `raw` as a positive int, or raise ConfigError(name) --
    never echoing the unparseable value itself.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(name)
    if value <= 0:
        raise ConfigError(name)
    return value


# Every variable below is read through _get(), never a bare
# os.environ.get(name, default) -- .env.example ships every key blank
# (`KEY=`), and os.environ.get()'s own default only applies when the
# key is *absent*, not when it is present-but-empty. A bare
# os.environ.get() would make its default unreachable for anyone who
# copies the template as-is. _get() treats blank the same as absent.

# --- Gmail (read-only; see app.gmail.auth) ---
# Bot-era names, read directly by app.gmail.auth today.
GOOGLE_CREDENTIALS_FILE = _get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = _get("GOOGLE_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# SPEC.md §19 names -- fall back to the bot-era names above when unset.
GMAIL_CREDENTIALS_PATH = _get(
    "GMAIL_CREDENTIALS_PATH", "credentials.json", legacy="GOOGLE_CREDENTIALS_FILE"
)
GMAIL_TOKEN_PATH = _get("GMAIL_TOKEN_PATH", "token.json", legacy="GOOGLE_TOKEN_FILE")
GMAIL_MAX_MESSAGES = _get("GMAIL_MAX_MESSAGES", "2000")

# --- Mailbox identity / business rules ---
REPORT_MAILBOX = _get("REPORT_MAILBOX", "")
EMPLOYEE_ALIASES = _parse_list(_get("EMPLOYEE_ALIASES", ""))

# Bot-era name, read directly by app.enquiry.status today. Left blank by
# default so a missing/wrong value fails loudly (every sender treated
# as a customer, logged) rather than silently guessing at a domain.
COMPANY_EMAIL_DOMAIN = _get("COMPANY_EMAIL_DOMAIN", "")

INTERNAL_DOMAINS = _parse_list(_get("INTERNAL_DOMAINS", "", legacy="COMPANY_EMAIL_DOMAIN"))

# --- Timezone ---
REPORT_TIMEZONE = _get("REPORT_TIMEZONE", "Asia/Kolkata")

# --- Database ---
DATABASE_PATH = _get("DATABASE_PATH", str(BASE_DIR / "data" / "regency.db"))

# --- Claude API ---
# Bot-era name, read directly by app.ai.claude today.
CLAUDE_API_KEY = _get("CLAUDE_API_KEY", "")

ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY", "", legacy="CLAUDE_API_KEY")
CLAUDE_MODEL = _get("CLAUDE_MODEL", "claude-sonnet-4-6")
AI_MAX_RETRIES = _get("AI_MAX_RETRIES", "3")
AI_BODY_MAX_CHARS = _get("AI_BODY_MAX_CHARS", "4000")

# --- WhatsApp Business Cloud API ---
# Kept per decision Q1 -- unused by the report path, still read by
# nothing today (dead even in the bot), removal deferred to Stage 4.
WHATSAPP_ACCESS_TOKEN = _get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = _get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = _get("WHATSAPP_VERIFY_TOKEN", "")

# --- Telegram ---
# TELEGRAM_BOT_TOKEN is shared as-is between the bot and the report path
# -- no rename, no legacy fallback needed.
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHUNK_CHARS = _get("TELEGRAM_CHUNK_CHARS", "3800")
TELEGRAM_MAX_RETRIES = _get("TELEGRAM_MAX_RETRIES", "3")

# --- Reporting / display ---
LOG_PATH = _get("LOG_PATH", "logs/report.log")
PENDING_DISPLAY_LIMIT = _get("PENDING_DISPLAY_LIMIT", "10")
PRIORITY_DISPLAY_LIMIT = _get("PRIORITY_DISPLAY_LIMIT", "5")
MAX_WINDOW_DAYS = _get("MAX_WINDOW_DAYS", "92")


# Required for the report path (report.py); the bot does not call this
# and is unaffected by any of these being unset (SPEC.md §17: "Missing/
# invalid config | Exit 2 before any network call. Name the missing
# variable, never its value.").
_REQUIRED = ("REPORT_MAILBOX", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# Every SPEC.md §19 variable that must be a positive integer.
_POSITIVE_INT_VARS = (
    "GMAIL_MAX_MESSAGES",
    "AI_MAX_RETRIES",
    "AI_BODY_MAX_CHARS",
    "TELEGRAM_CHUNK_CHARS",
    "TELEGRAM_MAX_RETRIES",
    "PENDING_DISPLAY_LIMIT",
    "PRIORITY_DISPLAY_LIMIT",
    "MAX_WINDOW_DAYS",
)


def validate() -> None:
    """Fail-fast config validation for the report path. Raises
    ConfigError naming only the variable name (never its value, never a
    partial value) on the first problem found. Call this before any
    network or DB access -- report.py maps ConfigError to exit code 2.

    Checks, in order: required variables present, integer variables
    parse as positive ints, REPORT_TIMEZONE is a real zoneinfo key,
    TELEGRAM_CHAT_ID is a well-formed Telegram chat id.
    """
    module_globals = globals()

    for name in _REQUIRED:
        if not module_globals[name]:
            raise ConfigError(name)

    for name in _POSITIVE_INT_VARS:
        _parse_positive_int(name, module_globals[name])

    try:
        ZoneInfo(REPORT_TIMEZONE)
    except Exception:
        raise ConfigError("REPORT_TIMEZONE")

    # PHASE0_DECISIONS.md Q11: "Startup must fail ... if it is missing
    # or malformed." Telegram chat ids are always integers (negative
    # for groups/channels) -- never quoted or logged here, per the
    # never-echo-a-value rule.
    try:
        int(TELEGRAM_CHAT_ID)
    except ValueError:
        raise ConfigError("TELEGRAM_CHAT_ID")
