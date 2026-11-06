from dataclasses import dataclass

import jwt

ROLES = frozenset({"admin", "viewer"})


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str


class AuthError(Exception):
    """The reason is for the gateway log only; clients get a generic message."""


def authenticate(authorization: str | None, secret: str) -> Principal:
    if not authorization:
        raise AuthError("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Authorization header is not 'Bearer <token>'")
    try:
        # algorithms is pinned: never let the token's own header choose it
        # (that is how "alg": "none" tokens get accepted).
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "role"]})
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {type(exc).__name__}") from None
    role = claims["role"]
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}")
    return Principal(subject=str(claims["sub"]), role=role)
