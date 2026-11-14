"""Verified Muse account identity, independent of cookies, devices and workspaces.

This module does not authenticate anything. Callers may only set ``verified`` after
a successful upstream authentication or a completed email OTP flow plus assigned
session. Never pass an identity claimed by an unauthenticated import request.
"""
from __future__ import annotations

import re
import unicodedata


class AccountIdentityConflict(ValueError):
    """A merge would cross a verified identity or an explicit proxy boundary."""


def _identifier(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    if not value or len(value) > 256 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    return value


def normalize_email(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value.strip())
    if len(value) > 254 or value.count("@") != 1 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    local, domain = value.rsplit("@", 1)
    if not local or len(local) > 64 or not domain or any(c in value for c in "<>\";,\\"):
        return None
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
        return None
    # Do not strip plus tags or dots: these can identify different mailboxes.
    return local.casefold() + "@" + domain


def normalize_identity(value: dict | None) -> dict | None:
    """Whitelist an internal, verified identity; discard all upstream secrets."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("verified") is not True:
        raise ValueError("Account identity must be verified by the upstream authentication flow")
    if value.get("provider", "muse") != "muse":
        raise ValueError("Unsupported account identity provider")
    uid = _identifier(value.get("user_id"))
    email = normalize_email(value.get("email"))
    if (value.get("user_id") is not None and uid is None
            or value.get("email") is not None and email is None
            or not (uid or email)):
        raise ValueError("A verified stable user ID or email is required")
    source = value.get("source", "session")
    if source not in ("session", "profile", "otp"):
        raise ValueError("Unsupported account identity source")
    out = {"provider": "muse", "verified": True, "source": source}
    if uid:
        out["user_id"] = uid
    if email:
        out["email"] = email
    return out


def identity_from_session(payload, verified: bool = True, *, source: str = "session") -> dict | None:
    """Extract only an observed upstream account field, never a generic ``id``.

    The public Muse native-auth frontend reads
    ``auth_meta_session.frl_account_id`` as its logged-in identifier. The actual
    assigned ``/api/session`` payload currently contains VM state only and must
    consequently return None. Do not recursively guess an ID from that payload,
    parse an unverified cookie/JWT, or retain ``frl_access_token``.
    """
    if verified is not True or not isinstance(payload, dict):
        return None
    auth = payload.get("auth_meta_session")
    uid = _identifier(auth.get("frl_account_id")) if isinstance(auth, dict) else None
    if not uid:
        return None
    return normalize_identity({"provider": "muse", "verified": True,
                               "source": source, "user_id": uid})


def verified_email_identity(email: str, *, source: str = "otp") -> dict:
    """For internal use after successful email OTP AND authenticated session."""
    return normalize_identity({"verified": True, "source": source, "email": email})


def identity_keys(identity: dict | None) -> set[tuple[str, str]]:
    return {(field, identity[field]) for field in ("user_id", "email")
            if identity and identity.get(field)}


def identities_conflict(left: dict | None, right: dict | None) -> bool:
    return bool(left and right and any(left.get(k) and right.get(k)
                and left[k] != right[k] for k in ("provider", "user_id", "email")))


def merge_identities(left: dict | None, right: dict | None) -> dict | None:
    if identities_conflict(left, right):
        raise AccountIdentityConflict("Conflicting verified account identities; no accounts were changed")
    if not left:
        return dict(right) if right else None
    if not right:
        return dict(left)
    out = {**left, **right}
    if left.get("source") != "otp":
        out["source"] = left["source"]
    return out
