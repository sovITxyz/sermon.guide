"""Unit tests for auth helpers — no DB, no FastAPI client.

JWT roundtrip and password hashing are pure-Python; cover them here so
regressions surface in CI before the e2e verify step exercises the full
stack with docker-compose up.
"""

# Tests exercise module-internals on purpose.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from jose import jwt

from auth import _hash_password, _issue_token, _verify_password
from settings import settings


def test_hash_and_verify_roundtrip() -> None:
    password = "correct-horse-battery-staple"
    digest = _hash_password(password)
    assert digest != password
    assert _verify_password(password, digest)
    assert not _verify_password("wrong-password", digest)


def test_hash_rejects_over_72_bytes() -> None:
    # bcrypt silently truncates at 72 bytes — we surface that as 400 so two
    # long passwords sharing a prefix don't appear equivalent.
    with pytest.raises(HTTPException) as exc:
        _hash_password("a" * 73)
    assert exc.value.status_code == 400


def test_issue_token_encodes_sub_and_exp() -> None:
    user_id = uuid.uuid4()
    token = _issue_token(user_id)
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert claims["sub"] == str(user_id)
    assert "exp" in claims
    assert "iat" in claims
    # exp should land in the future, within the configured TTL.
    now = datetime.now(tz=UTC)
    exp = datetime.fromtimestamp(claims["exp"], tz=UTC)
    assert exp > now
    assert exp <= now + timedelta(seconds=settings.jwt_ttl_seconds + 5)
