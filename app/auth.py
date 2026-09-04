import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta

import jwt
from pydantic import BaseModel

from .models import User

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + _b64(salt) + "$" + _b64(digest)


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt, digest = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), **_SCRYPT)
    return hmac.compare_digest(candidate, _unb64(digest))


class TokenClaims(BaseModel):
    sub: str
    username: str
    admin: bool
    iat: int
    exp: int


def create_token(user: User, *, secret: str, ttl_minutes: int) -> str:
    issued = datetime.now(UTC)
    claims = TokenClaims(
        sub=user.id,
        username=user.username,
        admin=user.is_admin,
        iat=int(issued.timestamp()),
        exp=int((issued + timedelta(minutes=ttl_minutes)).timestamp()),
    )
    return jwt.encode(claims.model_dump(), secret, algorithm="HS256")


def decode_token(token: str, *, secret: str) -> TokenClaims:
    """Raises jwt.PyJWTError on a bad signature, expiry or malformed payload."""
    payload = jwt.decode(token, secret, algorithms=["HS256"])
    return TokenClaims.model_validate(payload)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
