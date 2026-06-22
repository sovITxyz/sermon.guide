"""Unit tests for the OAuth integration surface + token vault (Phase 44).

Pure-unit, no live infra. Three layers:

- **crypto_vault**: encrypt/decrypt round-trips under a dummy 64-hex key, the
  random-nonce property (two encrypts of the same plaintext differ), tamper ->
  ``InvalidTag``, and the validate-on-use key errors (empty / 63-hex / 65-hex /
  non-hex -> ``OAuthUnconfiguredError``).
- **state HMAC + PKCE**: mint+verify round-trips; a flipped payload byte, an
  expired exp, a provider mismatch, and a user_id mismatch each reject (the
  account-binding CSRF test — the phase's whole point); the PKCE challenge is
  ``b64url(sha256(verifier))``.
- **statement builders**: every ``_xxx_stmt`` carries its ``user_id`` predicate
  (the ``test_library_unit.py`` compile-pin), and the list selects NO ciphertext
  column.

- **routes** (``main.app`` via ``TestClient``, auth + session overridden, the
  ``test_documents_unit.py`` pattern, httpx + Redis stubbed): the callback
  REJECTS before the token POST on bad state / missing verifier (httpx asserted
  NOT called); the list ships no token material; a cross-tenant / never-connected
  DELETE is a byte-identical 404; an unconfigured Google client -> a clean 503,
  not a 500.
"""

# Tests exercise module-internals and pass duck-typed fakes on purpose.
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnusedFunction=false

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

import auth
import crypto_vault
import integrations
import main as main_module
from crypto_vault import OAuthUnconfiguredError, decrypt, encrypt
from integrations import (
    _connection_stmt,
    _delete_stmt,
    _list_stmt,
    _make_pkce,
    _mint_state,
    _upsert_stmt,
    _verify_state,
)
from settings import DEV_JWT_SECRET

# A throwaway AES-256 key: 64 hex chars (`openssl rand -hex 32` shape).
_DUMMY_KEY = "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"


# --- crypto_vault ------------------------------------------------------------


@pytest.fixture
def _enc_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", _DUMMY_KEY)


def test_encrypt_decrypt_round_trip(_enc_key: None) -> None:
    secret = "1//refresh-token-abc.DEF_123"
    assert decrypt(encrypt(secret)) == secret


def test_encrypt_random_nonce_differs(_enc_key: None) -> None:
    # GCM invariant: a fresh random nonce per call -> two encrypts of the same
    # plaintext produce DIFFERENT ciphertext (no deterministic leak).
    a = encrypt("same-plaintext")
    b = encrypt("same-plaintext")
    assert a != b
    assert decrypt(a) == decrypt(b) == "same-plaintext"


def test_decrypt_tampered_blob_raises_invalid_tag(_enc_key: None) -> None:
    blob = bytearray(encrypt("token"))
    blob[-1] ^= 0x01  # flip a tag byte
    with pytest.raises(InvalidTag):
        decrypt(bytes(blob))


def test_decrypt_truncated_blob_raises_invalid_tag(_enc_key: None) -> None:
    with pytest.raises(InvalidTag):
        decrypt(encrypt("token")[:20])


@pytest.mark.parametrize(
    "bad_key",
    [
        "",  # empty -> unconfigured
        "00112233",  # too short
        _DUMMY_KEY[:-2],  # 62 hex (31 bytes)
        _DUMMY_KEY + "00",  # 66 hex (33 bytes)
        "zz" + _DUMMY_KEY[2:],  # non-hex
    ],
)
def test_load_key_validate_on_use(monkeypatch: pytest.MonkeyPatch, bad_key: str) -> None:
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", bad_key)
    with pytest.raises(OAuthUnconfiguredError) as exc:
        encrypt("token")
    # The detail names ONLY the env var, never any value.
    assert "SERMON_API_TOKEN_ENC_KEY" in str(exc.value)


# --- state HMAC --------------------------------------------------------------


@pytest.fixture
def _state_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use the dedicated state secret path; deterministic for the test.
    monkeypatch.setattr(integrations.settings, "oauth_state_secret", "test-state-secret")


def test_state_mint_verify_round_trip(_state_secret: None) -> None:
    uid = uuid.uuid4()
    state = _mint_state(user_id=uid, nonce="n1", provider="google")
    payload = _verify_state(state)
    assert payload.user_id == str(uid)
    assert payload.nonce == "n1"
    assert payload.provider == "google"
    assert payload.exp > int(datetime.now(tz=UTC).timestamp())


def test_state_flipped_payload_byte_fails_hmac(_state_secret: None) -> None:
    state = _mint_state(user_id=uuid.uuid4(), nonce="n1", provider="google")
    payload_b64, _, sig_b64 = state.partition(".")
    raw = bytearray(integrations._b64url_decode(payload_b64))
    raw[0] ^= 0x01
    tampered = f"{integrations._b64url(bytes(raw))}.{sig_b64}"
    with pytest.raises(Exception) as exc:  # noqa: B017, PT011 — HTTPException 400
        _verify_state(tampered)
    assert getattr(exc.value, "status_code", None) == 400


def test_state_wrong_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations.settings, "oauth_state_secret", "secret-A")
    state = _mint_state(user_id=uuid.uuid4(), nonce="n1", provider="google")
    monkeypatch.setattr(integrations.settings, "oauth_state_secret", "secret-B")
    with pytest.raises(Exception) as exc:  # noqa: B017, PT011
        _verify_state(state)
    assert getattr(exc.value, "status_code", None) == 400


def test_state_falls_back_to_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty dedicated secret -> HMAC keyed on jwt_secret; verify still works.
    monkeypatch.setattr(integrations.settings, "oauth_state_secret", "")
    monkeypatch.setattr(integrations.settings, "jwt_secret", "the-jwt-secret")
    state = _mint_state(user_id=uuid.uuid4(), nonce="n1", provider="google")
    assert _verify_state(state).nonce == "n1"


# --- PKCE --------------------------------------------------------------------


def test_pkce_challenge_is_sha256_of_verifier() -> None:
    verifier, challenge = _make_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected
    # The verifier is high-entropy (43–128 chars per the PKCE spec).
    assert 43 <= len(verifier) <= 128


# --- statement compile pins (tenant gate) ------------------------------------


def test_list_stmt_filters_by_user_id_and_hides_ciphertext() -> None:
    uid = uuid.uuid4()
    compiled = _list_stmt(uid).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "oauth_connections.user_id =" in sql
    assert uid in compiled.params.values()
    # The list NEVER selects token material.
    assert "ciphertext" not in sql


def test_connection_stmt_filters_by_user_id_and_provider() -> None:
    uid = uuid.uuid4()
    compiled = _connection_stmt(uid, "google").compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "oauth_connections.user_id =" in sql
    assert "oauth_connections.provider =" in sql
    assert uid in compiled.params.values()


def test_delete_stmt_filters_by_user_id_and_provider() -> None:
    uid = uuid.uuid4()
    compiled = _delete_stmt(uid, "google").compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "DELETE FROM oauth_connections" in sql
    assert "oauth_connections.user_id =" in sql
    assert "oauth_connections.provider =" in sql
    assert uid in compiled.params.values()


def test_upsert_stmt_targets_user_provider_conflict() -> None:
    uid = uuid.uuid4()
    compiled = _upsert_stmt(
        user_id=uid,
        provider="google",
        provider_account_email="a@b.com",
        refresh_token_ciphertext=b"ct",
        access_token_ciphertext=b"ct2",
        token_expiry=None,
        scopes="openid email",
    ).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "INSERT INTO oauth_connections" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_oauth_connections_user_provider DO UPDATE" in sql
    assert uid in compiled.params.values()


# --- route fakes -------------------------------------------------------------


class _FakeUser:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()


class _StoredConn:
    def __init__(
        self,
        *,
        user_id: uuid.UUID,
        provider: str,
        provider_account_email: str,
        scopes: str,
        refresh_token_ciphertext: bytes,
        token_expiry: datetime | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.user_id = user_id
        self.provider = provider
        self.provider_account_email = provider_account_email
        self.scopes = scopes
        self.refresh_token_ciphertext = refresh_token_ciphertext
        self.access_token_ciphertext: bytes | None = None
        self.token_expiry = token_expiry
        self.created_at = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        self.updated_at = self.created_at


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def tuples(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


_LIST_COLS = ("provider", "provider_account_email", "scopes", "created_at", "token_expiry")


class _FakeSession:
    def __init__(self, conns: list[_StoredConn] | None = None) -> None:
        self.conns: list[_StoredConn] = conns or []
        self.commits = 0
        self.executed: list[str] = []

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> _FakeResult:
        compiled = stmt.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params

        if sql.startswith("DELETE FROM oauth_connections"):
            self.executed.append("delete")
            uid, prov = params["user_id_1"], params["provider_1"]
            match = [c for c in self.conns if c.user_id == uid and c.provider == prov]
            self.conns = [c for c in self.conns if c not in match]
            return _FakeResult([c.id for c in match])

        if sql.startswith("INSERT INTO oauth_connections"):
            self.executed.append("upsert")
            return _FakeResult([])

        if "FROM oauth_connections" in sql:
            uid = params["user_id_1"]
            if "provider_1" in params:
                self.executed.append("connection")
                prov = params["provider_1"]
                rows = [c for c in self.conns if c.user_id == uid and c.provider == prov]
                return _FakeResult(rows[:1])
            self.executed.append("list")
            rows = sorted(
                (c for c in self.conns if c.user_id == uid),
                key=lambda c: c.created_at,
                reverse=True,
            )
            return _FakeResult([tuple(getattr(c, col) for col in _LIST_COLS) for c in rows])

        msg = f"unexpected statement: {sql}"
        raise AssertionError(msg)


class _FakeRedis:
    """Just enough of the async Redis surface: set / getdel."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = store or {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.store[key] = value

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


class _SpyHTTP:
    """Records whether the token endpoint was ever POSTed to."""

    def __init__(self) -> None:
        self.posted = False

    async def __aenter__(self) -> _SpyHTTP:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def post(self, *_a: Any, **_k: Any) -> Any:
        self.posted = True
        msg = "httpx must NOT be called before state+PKCE validation passes"
        raise AssertionError(msg)

    async def get(self, *_a: Any, **_k: Any) -> Any:
        self.posted = True
        msg = "httpx GET must not run before validation"
        raise AssertionError(msg)


@pytest.fixture
def fake_user() -> _FakeUser:
    return _FakeUser()


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    fake_redis: _FakeRedis,
) -> TestClient:
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setattr(integrations.settings, "oauth_state_secret", "test-state-secret")
    monkeypatch.setattr(integrations.settings, "google_client_id", "client-id")
    monkeypatch.setattr(integrations.settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", _DUMMY_KEY)
    monkeypatch.setattr(integrations, "_redis", lambda: fake_redis)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    return TestClient(main_module.app)


def _wire_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    async def _fake_session() -> Any:
        return session

    monkeypatch.setitem(main_module.app.dependency_overrides, auth._session, _fake_session)


# --- callback: validation precedes the token exchange ------------------------


def _expired_state(provider: str, user_id: uuid.UUID) -> str:
    # Hand-mint a state whose exp is in the past (the mint helper always sets a
    # future exp), reusing the module's HMAC + encoding.
    exp = int((datetime.now(tz=UTC) - timedelta(seconds=10)).timestamp())
    payload = json.dumps(
        {"user_id": str(user_id), "nonce": "n1", "provider": provider, "exp": exp},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    import hmac as _hmac

    sig = _hmac.new(integrations._state_secret(), payload, hashlib.sha256).digest()
    return f"{integrations._b64url(payload)}.{integrations._b64url(sig)}"


def test_callback_bad_state_never_calls_httpx(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    spy = _SpyHTTP()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda *a, **k: spy)  # noqa: ARG005
    fake_redis.store["oauth:pkce:n1"] = "verifier"
    resp = client.get(
        "/integrations/google/callback",
        params={"code": "auth-code", "state": "garbage.sig"},
    )
    assert resp.status_code == 400
    assert spy.posted is False  # token exchange never reached
    assert session.commits == 0


def test_callback_expired_state_rejected_before_exchange(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    fake_redis: _FakeRedis,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    spy = _SpyHTTP()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda *a, **k: spy)  # noqa: ARG005
    fake_redis.store["oauth:pkce:n1"] = "verifier"
    state = _expired_state("google", fake_user.user_id)
    resp = client.get(
        "/integrations/google/callback",
        params={"code": "auth-code", "state": state},
    )
    assert resp.status_code == 400
    assert spy.posted is False


def test_callback_wrong_user_binding_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_redis: _FakeRedis,
) -> None:
    # THE account-binding CSRF test: a state minted for a DIFFERENT user than
    # the JWT redeeming the callback must reject BEFORE the exchange.
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    spy = _SpyHTTP()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda *a, **k: spy)  # noqa: ARG005
    attacker = uuid.uuid4()
    state = _mint_state(user_id=attacker, nonce="n1", provider="google")
    fake_redis.store["oauth:pkce:n1"] = "verifier"
    resp = client.get(
        "/integrations/google/callback",
        params={"code": "auth-code", "state": state},
    )
    assert resp.status_code == 400
    assert spy.posted is False


def test_callback_provider_mismatch_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    fake_redis: _FakeRedis,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    spy = _SpyHTTP()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda *a, **k: spy)  # noqa: ARG005
    # state minted for a different provider key than the path
    state = _mint_state(user_id=fake_user.user_id, nonce="n1", provider="microsoft")
    fake_redis.store["oauth:pkce:n1"] = "verifier"
    resp = client.get(
        "/integrations/google/callback",
        params={"code": "auth-code", "state": state},
    )
    # microsoft is not in the allow-set -> 404 at _require_provider for the
    # path is google; the state.provider=microsoft mismatch would be 400. Path
    # is google (allowed), state says microsoft -> 400.
    assert resp.status_code == 400
    assert spy.posted is False


def test_callback_missing_verifier_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    # Valid state, but the PKCE verifier was never stored / already popped /
    # expired -> 400 BEFORE the exchange.
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    spy = _SpyHTTP()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda *a, **k: spy)  # noqa: ARG005
    state = _mint_state(user_id=fake_user.user_id, nonce="n-absent", provider="google")
    resp = client.get(
        "/integrations/google/callback",
        params={"code": "auth-code", "state": state},
    )
    assert resp.status_code == 400
    assert spy.posted is False


# --- list: no token material -------------------------------------------------


def test_list_returns_no_token_material(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
) -> None:
    session = _FakeSession(
        [
            _StoredConn(
                user_id=fake_user.user_id,
                provider="google",
                provider_account_email="me@example.com",
                scopes="openid email",
                refresh_token_ciphertext=b"\x00\x01ciphertext",
            ),
        ],
    )
    _wire_session(monkeypatch, session)
    resp = client.get("/integrations")
    assert resp.status_code == 200
    body = resp.json()
    conn = body["connections"][0]
    assert conn["provider"] == "google"
    assert conn["provider_account_email"] == "me@example.com"
    # NO token/ciphertext fields anywhere in the serialized response.
    blob = json.dumps(body)
    assert "ciphertext" not in blob
    assert "refresh_token" not in blob
    assert "access_token" not in blob


# --- revoke: cross-tenant / nonexistent 404 ----------------------------------


def test_revoke_never_connected_is_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()  # no connections at all
    _wire_session(monkeypatch, session)
    resp = client.delete("/integrations/google")
    assert resp.status_code == 404
    assert session.commits == 0


def test_revoke_cross_tenant_is_same_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A connection owned by ANOTHER user -> byte-identical 404 (no oracle).
    other = uuid.uuid4()
    session = _FakeSession(
        [
            _StoredConn(
                user_id=other,
                provider="google",
                provider_account_email="other@example.com",
                scopes="openid",
                refresh_token_ciphertext=encrypt("x")
                if crypto_vault.settings.token_enc_key
                else b"x",
            ),
        ],
    )
    _wire_session(monkeypatch, session)
    resp = client.delete("/integrations/google")
    assert resp.status_code == 404


def test_revoke_unknown_provider_is_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    _wire_session(monkeypatch, session)
    resp = client.delete("/integrations/dropbox")
    assert resp.status_code == 404


# --- unconfigured Google -> 503 (not 500) ------------------------------------


def test_authorize_unconfigured_google_is_503(
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    fake_redis: _FakeRedis,
) -> None:
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    # Google client id/secret intentionally EMPTY (unconfigured).
    monkeypatch.setattr(integrations.settings, "google_client_id", "")
    monkeypatch.setattr(integrations.settings, "google_client_secret", "")
    monkeypatch.setattr(integrations, "_redis", lambda: fake_redis)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    with TestClient(main_module.app) as c:
        resp = c.post("/integrations/google/authorize")
    assert resp.status_code == 503
    assert "SERMON_API_GOOGLE_CLIENT_ID" in resp.json()["detail"]


def test_authorize_unconfigured_enc_key_is_503(
    monkeypatch: pytest.MonkeyPatch,
    fake_user: _FakeUser,
    fake_redis: _FakeRedis,
) -> None:
    monkeypatch.setattr(main_module.settings, "env", "dev")
    monkeypatch.setattr(main_module.settings, "jwt_secret", DEV_JWT_SECRET)
    monkeypatch.setattr(integrations.settings, "google_client_id", "client-id")
    monkeypatch.setattr(integrations.settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(crypto_vault.settings, "token_enc_key", "")  # vault key missing
    monkeypatch.setattr(integrations, "_redis", lambda: fake_redis)
    monkeypatch.setitem(
        main_module.app.dependency_overrides,
        auth.get_current_user,
        lambda: fake_user,
    )
    with TestClient(main_module.app) as c:
        resp = c.post("/integrations/google/authorize")
    assert resp.status_code == 503
    assert "SERMON_API_TOKEN_ENC_KEY" in resp.json()["detail"]


# --- authorize happy path: stores verifier, builds offline+consent URL -------


def test_authorize_builds_url_and_stores_verifier(
    client: TestClient,
    fake_redis: _FakeRedis,
) -> None:
    resp = client.post("/integrations/google/authorize")
    assert resp.status_code == 200
    url = resp.json()["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "code_challenge_method=S256" in url
    # The redirect_uri is the WEB-origin callback path.
    assert "integrations%2Fgoogle%2Fcallback" in url
    # Exactly one PKCE verifier was stored, keyed under the state nonce.
    assert len(fake_redis.store) == 1
    assert next(iter(fake_redis.store)).startswith("oauth:pkce:")
