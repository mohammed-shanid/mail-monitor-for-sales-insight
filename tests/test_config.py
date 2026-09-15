"""app.config: required-variable fail-fast, legacy-name fallbacks,
comma-list parsing, integer/timezone validation, redact().

Every test uses the `tmp_env` fixture (conftest.py), which sets env
vars via monkeypatch and reloads app.config so its module-level
constants reflect them -- config reads the environment once at import
time, so a bare monkeypatch.setenv() after the fact has no effect.
Each test explicitly sets or unsets every variable it cares about, so
results never depend on what happens to be in the real `.env` file.
"""

from __future__ import annotations

import pytest

_VALID_REQUIRED = {
    "REPORT_MAILBOX": "u.ruma@regencyelectricals.com",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "TELEGRAM_BOT_TOKEN": "bot-token-test",
    "TELEGRAM_CHAT_ID": "123456789",
}


def _env_with(overrides: dict) -> dict:
    """A fully-specified, valid required-variable env, overridden by
    `overrides` (e.g. {"REPORT_MAILBOX": None} to simulate it missing).
    """
    env = dict(_VALID_REQUIRED)
    env.update(overrides)
    return env


# --- Required variables: fail-fast, name only -------------------------------


@pytest.mark.parametrize("missing", ["REPORT_MAILBOX", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_missing_required_variable_raises_naming_only_the_variable(tmp_env, missing):
    config = tmp_env(_env_with({missing: None, "CLAUDE_API_KEY": None}))

    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()

    assert str(exc_info.value) == missing


def test_empty_string_required_variable_counts_as_missing(tmp_env):
    config = tmp_env(_env_with({"TELEGRAM_CHAT_ID": ""}))

    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()

    assert str(exc_info.value) == "TELEGRAM_CHAT_ID"


def test_error_message_never_contains_the_value():
    # The contract is structural (ConfigError's message IS the bare
    # variable name), not a string-search over a rendered message --
    # this pins that down directly.
    from app.config import ConfigError

    err = ConfigError("ANTHROPIC_API_KEY")
    assert str(err) == "ANTHROPIC_API_KEY"
    assert "sk-" not in str(err)


def test_all_required_present_and_valid_does_not_raise(tmp_env):
    config = tmp_env(_env_with({}))
    config.validate()  # must not raise


# --- Legacy name fallbacks ---------------------------------------------------


def test_anthropic_api_key_falls_back_to_legacy_claude_api_key(tmp_env):
    config = tmp_env(_env_with({"ANTHROPIC_API_KEY": None, "CLAUDE_API_KEY": "legacy-value"}))
    assert config.ANTHROPIC_API_KEY == "legacy-value"


def test_anthropic_api_key_prefers_spec_name_when_both_set(tmp_env):
    config = tmp_env(_env_with({"ANTHROPIC_API_KEY": "spec-value", "CLAUDE_API_KEY": "legacy-value"}))
    assert config.ANTHROPIC_API_KEY == "spec-value"


def test_gmail_credentials_path_falls_back_to_google_credentials_file(tmp_env):
    config = tmp_env(_env_with({"GMAIL_CREDENTIALS_PATH": None, "GOOGLE_CREDENTIALS_FILE": "legacy-creds.json"}))
    assert config.GMAIL_CREDENTIALS_PATH == "legacy-creds.json"


def test_gmail_token_path_falls_back_to_google_token_file(tmp_env):
    config = tmp_env(_env_with({"GMAIL_TOKEN_PATH": None, "GOOGLE_TOKEN_FILE": "legacy-token.json"}))
    assert config.GMAIL_TOKEN_PATH == "legacy-token.json"


def test_internal_domains_falls_back_to_company_email_domain(tmp_env):
    config = tmp_env(_env_with({"INTERNAL_DOMAINS": None, "COMPANY_EMAIL_DOMAIN": "regencyelectricals.com"}))
    assert config.INTERNAL_DOMAINS == ["regencyelectricals.com"]


def test_internal_domains_prefers_spec_name_when_both_set(tmp_env):
    config = tmp_env(_env_with({"INTERNAL_DOMAINS": "spec.com", "COMPANY_EMAIL_DOMAIN": "legacy.com"}))
    assert config.INTERNAL_DOMAINS == ["spec.com"]


def test_bot_era_constants_still_populated_when_only_legacy_names_set(tmp_env):
    # The bot imports GOOGLE_CREDENTIALS_FILE / CLAUDE_API_KEY /
    # COMPANY_EMAIL_DOMAIN directly -- confirm those still work
    # untouched, independent of the new SPEC names.
    config = tmp_env(_env_with({
        "GOOGLE_CREDENTIALS_FILE": "bot-creds.json",
        "CLAUDE_API_KEY": "bot-key",
        "COMPANY_EMAIL_DOMAIN": "regencyelectricals.com",
    }))
    assert config.GOOGLE_CREDENTIALS_FILE == "bot-creds.json"
    assert config.CLAUDE_API_KEY == "bot-key"
    assert config.COMPANY_EMAIL_DOMAIN == "regencyelectricals.com"


# --- Comma-list parsing ------------------------------------------------------


def test_internal_domains_parses_comma_list_lowercase_stripped(tmp_env):
    config = tmp_env(_env_with({"INTERNAL_DOMAINS": " RegencyElectricals.com , Foo.Bar "}))
    assert config.INTERNAL_DOMAINS == ["regencyelectricals.com", "foo.bar"]


def test_internal_domains_empty_string_is_empty_list_not_list_of_empty_string(tmp_env):
    # Also clear the legacy fallback -- otherwise a real COMPANY_EMAIL_DOMAIN
    # in the developer's own .env would resolve through it and mask this
    # assertion.
    config = tmp_env(_env_with({"INTERNAL_DOMAINS": "", "COMPANY_EMAIL_DOMAIN": None}))
    assert config.INTERNAL_DOMAINS == []


def test_employee_aliases_whitespace_only_is_empty_list(tmp_env):
    config = tmp_env(_env_with({"EMPLOYEE_ALIASES": "   "}))
    assert config.EMPLOYEE_ALIASES == []


def test_employee_aliases_parses_comma_list(tmp_env):
    config = tmp_env(_env_with({"EMPLOYEE_ALIASES": "Rahul@regencyelectricals.com, Priya@regencyelectricals.com"}))
    assert config.EMPLOYEE_ALIASES == ["rahul@regencyelectricals.com", "priya@regencyelectricals.com"]


# --- Integer variable validation --------------------------------------------


@pytest.mark.parametrize(
    "var",
    [
        "GMAIL_MAX_MESSAGES",
        "AI_MAX_RETRIES",
        "AI_BODY_MAX_CHARS",
        "TELEGRAM_CHUNK_CHARS",
        "TELEGRAM_MAX_RETRIES",
        "PENDING_DISPLAY_LIMIT",
        "PRIORITY_DISPLAY_LIMIT",
        "MAX_WINDOW_DAYS",
    ],
)
def test_non_numeric_integer_variable_raises_naming_the_variable(tmp_env, var):
    config = tmp_env(_env_with({var: "not-a-number"}))

    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()

    assert str(exc_info.value) == var


def test_zero_is_not_a_positive_integer(tmp_env):
    config = tmp_env(_env_with({"GMAIL_MAX_MESSAGES": "0"}))
    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()
    assert str(exc_info.value) == "GMAIL_MAX_MESSAGES"


def test_negative_integer_rejected(tmp_env):
    config = tmp_env(_env_with({"AI_MAX_RETRIES": "-1"}))
    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()
    assert str(exc_info.value) == "AI_MAX_RETRIES"


def test_valid_positive_integers_do_not_raise(tmp_env):
    config = tmp_env(_env_with({"GMAIL_MAX_MESSAGES": "500", "AI_MAX_RETRIES": "5"}))
    config.validate()  # must not raise


def test_default_integer_values_match_spec_defaults(tmp_env):
    config = tmp_env(_env_with({
        "GMAIL_MAX_MESSAGES": None,
        "AI_MAX_RETRIES": None,
        "AI_BODY_MAX_CHARS": None,
        "TELEGRAM_CHUNK_CHARS": None,
        "TELEGRAM_MAX_RETRIES": None,
        "PENDING_DISPLAY_LIMIT": None,
        "PRIORITY_DISPLAY_LIMIT": None,
        "MAX_WINDOW_DAYS": None,
    }))
    assert config.GMAIL_MAX_MESSAGES == "2000"
    assert config.AI_MAX_RETRIES == "3"
    assert config.AI_BODY_MAX_CHARS == "4000"
    assert config.TELEGRAM_CHUNK_CHARS == "3800"
    assert config.TELEGRAM_MAX_RETRIES == "3"
    assert config.PENDING_DISPLAY_LIMIT == "10"
    assert config.PRIORITY_DISPLAY_LIMIT == "5"
    assert config.MAX_WINDOW_DAYS == "92"
    config.validate()  # defaults themselves must be valid


# --- Timezone validation ------------------------------------------------------


def test_unknown_timezone_raises_naming_report_timezone(tmp_env):
    config = tmp_env(_env_with({"REPORT_TIMEZONE": "Not/AZone"}))
    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()
    assert str(exc_info.value) == "REPORT_TIMEZONE"


# --- TELEGRAM_CHAT_ID format (PHASE0_DECISIONS.md Q11) -----------------------


def test_malformed_telegram_chat_id_raises_naming_the_variable(tmp_env):
    config = tmp_env(_env_with({"TELEGRAM_CHAT_ID": "not-a-chat-id"}))
    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()
    assert str(exc_info.value) == "TELEGRAM_CHAT_ID"


def test_negative_telegram_chat_id_is_valid(tmp_env):
    # Groups/channels use negative chat ids -- must not be rejected.
    config = tmp_env(_env_with({"TELEGRAM_CHAT_ID": "-100123456789"}))
    config.validate()  # must not raise


def test_default_timezone_is_valid(tmp_env):
    config = tmp_env(_env_with({"REPORT_TIMEZONE": None}))
    assert config.REPORT_TIMEZONE == "Asia/Kolkata"
    config.validate()  # must not raise


def test_alternate_valid_timezone_accepted(tmp_env):
    config = tmp_env(_env_with({"REPORT_TIMEZONE": "America/New_York"}))
    config.validate()  # must not raise


# --- Ordering: required vars checked before integers/timezone ---------------


def test_missing_required_reported_before_unrelated_integer_error(tmp_env):
    config = tmp_env(_env_with({"REPORT_MAILBOX": None, "GMAIL_MAX_MESSAGES": "garbage"}))
    with pytest.raises(config.ConfigError) as exc_info:
        config.validate()
    assert str(exc_info.value) == "REPORT_MAILBOX"


# --- redact() -----------------------------------------------------------------


def test_redact_none_is_empty_marker():
    from app.config import redact

    assert redact(None) == "<empty>"


def test_redact_empty_string_is_empty_marker():
    from app.config import redact

    assert redact("") == "<empty>"


def test_redact_never_returns_the_original_value():
    from app.config import redact

    secret = "sk-ant-super-secret-12345"
    result = redact(secret)
    assert result != secret
    assert secret not in result
    assert result == "<redacted>"
