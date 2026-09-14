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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
