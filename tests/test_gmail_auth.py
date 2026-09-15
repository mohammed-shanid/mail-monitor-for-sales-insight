"""Unit tests for app.gmail.auth.

No real Google credentials are used or required -- these only check
our own error handling around missing/invalid files.
"""

from __future__ import annotations

import pytest

import app.gmail.auth as auth_module


def test_load_credentials_raises_clear_error_when_nothing_available(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "GOOGLE_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setattr(auth_module, "GOOGLE_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))

    with pytest.raises(auth_module.GmailAuthError, match="credentials.json"):
        auth_module.load_credentials()


def test_load_credentials_ignores_corrupt_token_file_and_reports_missing_credentials(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "token.json"
    token_path.write_text("not valid json")
    monkeypatch.setattr(auth_module, "GOOGLE_TOKEN_FILE", str(token_path))
    monkeypatch.setattr(auth_module, "GOOGLE_CREDENTIALS_FILE", str(tmp_path / "credentials.json"))

    with pytest.raises(auth_module.GmailAuthError):
        auth_module.load_credentials()


# =============================================================================
# Report path (SPEC.md §19, §21.1), added Stage 2.
# =============================================================================

from unittest.mock import MagicMock


def test_load_credentials_explicit_paths_override_module_defaults(tmp_path, monkeypatch):
    # Zero-arg behaviour (used by the bot) reads GOOGLE_TOKEN_FILE/
    # GOOGLE_CREDENTIALS_FILE; explicit args (used by the report path)
    # must take priority over those module-level defaults.
    monkeypatch.setattr(auth_module, "GOOGLE_TOKEN_FILE", str(tmp_path / "bot-token.json"))
    monkeypatch.setattr(auth_module, "GOOGLE_CREDENTIALS_FILE", str(tmp_path / "bot-creds.json"))

    with pytest.raises(auth_module.GmailAuthError, match="report-creds.json"):
        auth_module.load_credentials(
            credentials_path=str(tmp_path / "report-creds.json"),
            token_path=str(tmp_path / "report-token.json"),
        )


def test_verify_mailbox_identity_passes_on_exact_match():
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "u.ruma@regencyelectricals.com"
    }
    auth_module.verify_mailbox_identity(service, "u.ruma@regencyelectricals.com")  # must not raise


def test_verify_mailbox_identity_is_case_insensitive():
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "U.Ruma@RegencyElectricals.com"
    }
    auth_module.verify_mailbox_identity(service, "u.ruma@regencyelectricals.com")  # must not raise


def test_verify_mailbox_identity_raises_on_mismatch():
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "someone.else@gmail.com"
    }
    with pytest.raises(auth_module.GmailAuthError, match="does not match"):
        auth_module.verify_mailbox_identity(service, "u.ruma@regencyelectricals.com")


def test_verify_mailbox_identity_raises_on_api_failure():
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = RuntimeError("network down")
    with pytest.raises(auth_module.GmailAuthError):
        auth_module.verify_mailbox_identity(service, "u.ruma@regencyelectricals.com")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
