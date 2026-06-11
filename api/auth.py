"""JWT auth — signup, login, ``get_current_user`` dependency.

Routes mounted under ``/auth``; the bearer-token dependency is exported
so other routers can require an authenticated user. Tenant invariants
(repo-root ``CLAUDE.md``):

- ``user_id`` is ALWAYS the JWT's ``sub`` claim — never read from the
  request body, query params, or path.
- The token carries only ``sub`` + ``exp`` + ``iat``. Email is not in
  the token; if a route needs it, look it up from ``users``.
- bcrypt for password hashing (passlib's recommended deprecated-and-still-
  the-default scheme); JWT signed HS256 against
  ``SERMON_API_JWT_SECRET``. HS256 is symmetric because there is only
  one verifier (this service). When a second verifier appears (e.g.
  a separate search service), switch to RS256 + a shared public key.

Failure-mode notes:

- ``/auth/login`` returns a single 401 for "no such user" and "wrong
  password" — exposing the difference is an email-enumeration vector.
- bcrypt has a built-in 72-byte input cap. Inputs longer than that are
  silently truncated; we reject them at the API instead so two
  72+-byte passwords sharing a prefix don't appear equivalent.
- python-jose raises ``JWTError`` for any decode failure (bad sig,
  expired, malformed); all collapse to a single 401 here.
"""

# passlib ships without `py.typed`; same relaxation pattern as the worker.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from db import User, get_session_factory
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# ``auto_error=False`` so we can raise a uniformly-shaped 401 from
# get_current_user regardless of which step failed (missing header,
# malformed token, expired, unknown user).
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt input cap. See module docstring.
_BCRYPT_MAX_BYTES = 72


class SignupRequest(BaseModel):
    # Phase 18 posture: unknown fields in inbound bodies are a hard 422,
    # never a silently-dropped key (a smuggled ``user_id`` must fail loud —
    # tenant invariant). Applies to every request model in api/.
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class SignupResponse(BaseModel):
    user_id: uuid.UUID
    email: EmailStr


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Phase 18 — see SignupRequest

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 bearer scheme literal, not a credential


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt. Rejects inputs over bcrypt's 72-byte cap."""
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password exceeds 72-byte bcrypt input limit.",
        )
    return _pwd_context.hash(password)


def _verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verify. Truncates internally; we cap above."""
    return _pwd_context.verify(password, password_hash)


def _issue_token(user_id: uuid.UUID) -> str:
    """Encode an HS256 JWT for *user_id* with ``exp`` set per settings."""
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_ttl_seconds)).timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def _session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that hands out a single async DB session.

    The async generator pattern is the idiomatic FastAPI dance for
    session lifecycle: yield once, FastAPI calls ``__aexit__`` after the
    request finishes so the connection returns to the pool.
    """
    sf = get_session_factory()
    async with sf() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_session)]


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    response_model=SignupResponse,
)
async def signup(payload: SignupRequest, session: SessionDep) -> SignupResponse:
    """Create a new user. 409 on email collision."""
    user = User(
        email=payload.email,
        password_hash=_hash_password(payload.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Email is the only unique constraint on `users`, so any IntegrityError
        # here is the email-already-taken case.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered.",
        ) from exc
    await session.refresh(user)
    return SignupResponse(user_id=user.user_id, email=user.email)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    """Authenticate and return a bearer JWT. Single 401 on any failure."""
    stmt = select(User).where(User.email == payload.email)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not _verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(access_token=_issue_token(user.user_id))


async def get_current_user(
    token: Annotated[str | None, Depends(_oauth2_scheme)],
    session: SessionDep,
) -> User:
    """Decode the bearer token and load the ``users`` row.

    Every failure mode (missing header, bad signature, expired, unknown
    ``sub``, deleted user) collapses to a single 401 so the client can't
    differentiate.
    """
    unauth = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise unauth
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise unauth from exc
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise unauth
    try:
        user_id = uuid.UUID(sub)
    except ValueError as exc:
        raise unauth from exc
    user = await session.get(User, user_id)
    if user is None:
        raise unauth
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
