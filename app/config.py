"""Centralized configuration, loaded from environment variables (and a
local .env file, if present).

V1 keeps this deliberately small: read env vars once, expose them as
plain constants, let modules import what they need. No config
frameworks, no validation DSLs, no secrets committed to git -- see
.env.example for the full list of variables and .gitignore for what
is excluded from version control.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Loads variables from a .env file in the current working directory,
# if one exists. Real environment variables always win (override=False
# is the default), so this never clobbers variables set another way
# (e.g. by systemd in production).
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Gmail (read-only; see app.gmail.auth) ---
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# --- Database ---
DATABASE_PATH = os.environ.get("DATABASE_PATH", str(BASE_DIR / "data" / "regency.db"))

# --- Business rules (Phase 4: app.enquiry.status) ---
# The email domain that identifies an "employee" message vs. a
# "customer" message (project spec sections 11/14/16) -- e.g.
# "regencyelectricals.com". Left blank by default so a missing/wrong
# value fails loudly (every sender treated as a customer, logged) in
# app.enquiry.status rather than silently guessing at a domain.
COMPANY_EMAIL_DOMAIN = os.environ.get("COMPANY_EMAIL_DOMAIN", "")

# --- Claude API (used starting Phase 2: enquiry extraction/classification) ---
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

# --- WhatsApp Business Cloud API (the eventual founder-facing channel) ---
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

# --- Telegram (used to prototype the bot flow before the WhatsApp Cloud
# API's business verification is in place -- see README) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
