"""Unit tests for ``ApiSettings`` env parsing.

Phase 18 posture field: ``SERMON_API_ENV`` decides whether the boot
guards in ``main.py`` are armed. The parsing rules pinned here are
security-relevant: the default and the compose ``${VAR:-}`` empty-string
case must BOTH resolve to ``"prod"`` (fail closed — guards armed), and
only the explicit ``"dev"`` string may opt out. The lifespan guards
themselves are covered in ``test_main_unit.py``; this file owns the
settings layer.

Phase 19 rate-limit fields: the ``"<max>/<window seconds>"`` strings must
fail at process start (validator → :func:`settings.parse_rate`), never at
request time, and XFF trust must default OFF (fail closed to the TCP
peer). The limiter behavior lives in ``test_ratelimit_unit.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from settings import DEV_JWT_SECRET, ApiSettings, parse_rate


def test_env_defaults_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset SERMON_API_ENV must mean guards armed, not an accidental opt-out."""
    monkeypatch.delenv("SERMON_API_ENV", raising=False)
    assert ApiSettings().env == "prod"


def test_env_dev_is_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERMON_API_ENV", "dev")
    assert ApiSettings().env == "dev"


def test_env_empty_string_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose's ``${VAR:-}`` delivers ``""`` for unset — must fail closed."""
    monkeypatch.setenv("SERMON_API_ENV", "")
    assert ApiSettings().env == "prod"


def test_env_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo like ``production`` must fail loud, not silently arm/disarm."""
    monkeypatch.setenv("SERMON_API_ENV", "production")
    with pytest.raises(ValidationError):
        ApiSettings()


def test_default_jwt_secret_is_the_guard_comparand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One constant serves as field default AND guard comparand — no drift.

    If the field default ever diverges from ``DEV_JWT_SECRET``, the boot
    guard in ``main.py`` would stop recognizing the placeholder and a
    forgotten ``SERMON_API_JWT_SECRET`` would serve forgeable JWTs.
    """
    monkeypatch.delenv("SERMON_API_JWT_SECRET", raising=False)
    assert ApiSettings().jwt_secret == DEV_JWT_SECRET


# ---------------------------------------------------------------------------
# Phase 19 — rate-limit settings


def test_parse_rate_round_trips_the_documented_shape() -> None:
    assert parse_rate("10/60") == (10, 60)
    assert parse_rate("1/2") == (1, 2)


@pytest.mark.parametrize(
    "value",
    ["", "10", "/60", "10/", "0/60", "10/0", "-1/60", "a/60", "10/b", "10/60/5", "10 per 60"],
)
def test_parse_rate_rejects_malformed_strings(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid rate limit"):
        parse_rate(value)


def test_default_rate_buckets_parse() -> None:
    """Shipping defaults must never be the thing that fails a boot."""
    s = ApiSettings()
    for value in (s.ratelimit_signup_ip, s.ratelimit_login_ip, s.ratelimit_summary_user):
        limit, window = parse_rate(value)
        assert limit >= 1 and window >= 1


def test_malformed_rate_env_fails_at_process_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad SERMON_API_RATELIMIT_* must be a boot error, not a request-time 500."""
    monkeypatch.setenv("SERMON_API_RATELIMIT_LOGIN_IP", "ten per minute")
    with pytest.raises(ValidationError, match="Invalid rate limit"):
        ApiSettings()


def test_proxy_header_trust_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: never honor client-supplied X-Forwarded-For unconfigured."""
    monkeypatch.delenv("SERMON_API_TRUST_PROXY_HEADERS", raising=False)
    assert ApiSettings().trust_proxy_headers is False


def test_ratelimit_kill_switch_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERMON_API_RATELIMIT_ENABLED", raising=False)
    assert ApiSettings().ratelimit_enabled is True
