"""Caller identity for API handlers, read from the Cognito JWT authorizer.

API Gateway validates the token before a handler runs and passes the verified
claims in ``requestContext.authorizer.jwt.claims``. Handlers use them to
scope every job to the user who uploaded it (``ownerId`` = the token's
``sub``) and to restrict rule changes to the admin group.
"""
from typing import Any, Dict, Optional, Set

ADMIN_GROUP = "admins"


def caller_claims(event: Dict[str, Any]) -> Dict[str, Any]:
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    return (authorizer.get("jwt") or {}).get("claims") or {}


def caller_id(event: Dict[str, Any]) -> Optional[str]:
    """The authenticated user's stable id (``sub``), or None."""
    sub = caller_claims(event).get("sub")
    return sub if isinstance(sub, str) and sub else None


def caller_groups(event: Dict[str, Any]) -> Set[str]:
    """Cognito groups of the caller.

    HTTP API passes array claims as a string such as ``"[admins users]"``;
    a real list is accepted too.
    """
    raw = caller_claims(event).get("cognito:groups")
    if isinstance(raw, (list, tuple)):
        return {str(g) for g in raw}
    if isinstance(raw, str):
        return {g for g in raw.strip("[]").replace(",", " ").split() if g}
    return set()


def is_admin(event: Dict[str, Any]) -> bool:
    return ADMIN_GROUP in caller_groups(event)


def unauthorized_response() -> Dict[str, Any]:
    return {
        "statusCode": 401,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error": {"code": "UNAUTHORIZED", "message": "Authentication required."}}',
    }
