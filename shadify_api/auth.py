"""Identity from a canonical shell-admitted Authentication request.

The shell verifies the complete logical request and Authentication-owned headers.
This adapter validates their identity/expiry binding; it never authenticates an
arbitrary browser assertion or implements a second cryptographic verifier.
"""
import base64
import json
import re
import time
from dataclasses import dataclass
from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Identity:
    account_id: str
    session_id: str
    auth_level: str
    expires_at: int


def unique_pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def identity_from_admitted_authentication(request: Request) -> Identity:
    try:
        from app_security_shell._native import InboundRequest
        from app_security_shell.transport import TrustedPeer
    except ImportError:
        raise HTTPException(503, "Authentication admission unavailable") from None
    if not isinstance(request.scope.get("app_security.admitted"), (InboundRequest, TrustedPeer)):
        raise HTTPException(503, "Authentication admission required")
    try:
        def header(name):
            rows = [v for k,v in request.scope.get("headers", []) if k.lower() == name]
            if len(rows) != 1:
                raise ValueError()
            return rows[0].decode("ascii")

        token = header(b"x-auth-assertion")
        if len(token) > 8192:
            raise ValueError()
        version, encoded, signature = token.rsplit(".", 2)
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", version)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", signature)):
            raise ValueError()
        payload_bytes = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(payload_bytes).decode().rstrip("=") != encoded:
            raise ValueError()
        claims = json.loads(payload_bytes, object_pairs_hook=unique_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(claims, dict) or claims.get("purpose") != "auth-assertion":
            raise ValueError()
        expected = {"domain_app": (b"x-authenticated-domain-app", "shadify"),
                    "proxy_app": (b"x-authenticated-proxy-app", "shadify_api")}
        for claim, (name, value) in expected.items():
            if claims.get(claim) != value or header(name) != value:
                raise ValueError()
        for claim, name, maximum in (("account_id", b"x-authenticated-account-id", 36),
                                     ("session_id", b"x-authenticated-session-id", 36),
                                     ("auth_level", b"x-authenticated-auth-level", 32)):
            value = claims.get(claim)
            if (type(value) is not str or not 1 <= len(value) <= maximum
                    or value != value.strip() or any(ord(c) < 33 or ord(c) > 126 for c in value)
                    or header(name) != value):
                raise ValueError()
        issued, expires = claims.get("iat"), claims.get("exp")
        now = int(time.time())
        if (type(issued) is not int or type(expires) is not int or expires <= issued
                or issued > now + 60 or expires <= now):
            raise ValueError()
        return Identity(claims["account_id"], claims["session_id"], claims["auth_level"], expires)
    except Exception:
        raise HTTPException(401, "Invalid Authentication identity") from None


def require_account(request: Request) -> str:
    identity = identity_from_admitted_authentication(request)
    request.state.auth_identity = identity
    return identity.account_id


def observed_account(repository):
    """Server-owned provenance hook. No request body/header can supply proof."""
    def admitted_account(request: Request):
        account = require_account(request)
        if repository is not None:
            repository.observe_identity(request.state.auth_identity)
        return account
    return admitted_account
