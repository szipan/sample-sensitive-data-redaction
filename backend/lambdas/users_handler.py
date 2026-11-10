"""User management API for administrators.

Routes (all require a caller in the admin group):

- ``GET    /users``                          list users with role and status
- ``POST   /users``                          invite a user ``{"email", "role"}``
- ``PATCH  /users/{username}``               change role ``{"role"}``
- ``DELETE /users/{username}``               delete a user
- ``POST   /users/{username}/resend-invite`` send a new temporary password

There are two roles: ``admin`` (member of the Cognito admin group: manages
users and rules, and uses the system) and ``user`` (everyone else: uploads
documents and works with their own jobs). Invitations are e-mailed by Cognito
with a temporary password that must be changed at first sign-in.

Two guards keep the system administrable: an admin cannot demote or delete
themselves, and the last admin cannot be demoted or deleted. When a user is
demoted or deleted their refresh tokens are revoked; an access token already
issued stays valid until it expires (at most one hour).
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import boto3
from botocore.exceptions import ClientError

from request_auth import ADMIN_GROUP, caller_id, is_admin, unauthorized_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

# A pragmatic shape check; Cognito does the authoritative validation.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_EMAIL_LENGTH = 254

_cognito = None


def _client():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def _pool_id() -> str:
    return os.environ["USER_POOL_ID"]


def _json(status: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error(status: int, code: str, message: str) -> Dict[str, Any]:
    return _json(status, {"error": {"code": code, "message": message}})


def _body(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = event.get("body")
    if not raw:
        return None
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _attributes(user: Dict[str, Any]) -> Dict[str, str]:
    attrs = user.get("Attributes") or user.get("UserAttributes") or []
    return {a["Name"]: a["Value"] for a in attrs}


def _admin_usernames() -> Set[str]:
    names: Set[str] = set()
    kwargs = {"UserPoolId": _pool_id(), "GroupName": ADMIN_GROUP}
    while True:
        page = _client().list_users_in_group(**kwargs)
        names.update(u["Username"] for u in page.get("Users", []))
        token = page.get("NextToken")
        if not token:
            return names
        kwargs["NextToken"] = token


def _user_view(user: Dict[str, Any], admins: Set[str], caller: str) -> Dict[str, Any]:
    attrs = _attributes(user)
    created = user.get("UserCreateDate")
    return {
        "username": user["Username"],
        "email": attrs.get("email"),
        "role": ROLE_ADMIN if user["Username"] in admins else ROLE_USER,
        "status": user.get("UserStatus"),
        "enabled": user.get("Enabled", True),
        "createdAt": created.isoformat() if hasattr(created, "isoformat") else created,
        "isSelf": attrs.get("sub") == caller,
    }


def _get_user(username: str) -> Optional[Dict[str, Any]]:
    try:
        return _client().admin_get_user(UserPoolId=_pool_id(), Username=username)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "UserNotFoundException":
            return None
        raise


def _list_users(caller: str) -> Dict[str, Any]:
    admins = _admin_usernames()
    users: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"UserPoolId": _pool_id()}
    while True:
        page = _client().list_users(**kwargs)
        users.extend(_user_view(u, admins, caller) for u in page.get("Users", []))
        token = page.get("PaginationToken")
        if not token:
            break
        kwargs["PaginationToken"] = token
    users.sort(key=lambda u: (u["email"] or "").lower())
    return _json(200, {"users": users})


def _create_user(event: Dict[str, Any], caller: str) -> Dict[str, Any]:
    body = _body(event) or {}
    email = body.get("email")
    role = body.get("role", ROLE_USER)
    if (
        not isinstance(email, str)
        or len(email.strip()) > _MAX_EMAIL_LENGTH
        or not _EMAIL_RE.match(email.strip())
    ):
        return _error(400, "INVALID_EMAIL", "A valid email address is required.")
    if role not in ROLES:
        return _error(400, "INVALID_ROLE", "Role must be 'admin' or 'user'.")
    email = email.strip().lower()
    try:
        created = _client().admin_create_user(
            UserPoolId=_pool_id(),
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )["User"]
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "UsernameExistsException":
            return _error(409, "USER_EXISTS", "A user with this email already exists.")
        if code in ("InvalidParameterException", "CodeDeliveryFailureException"):
            return _error(400, "INVALID_EMAIL", "The invitation could not be sent to this address.")
        raise
    if role == ROLE_ADMIN:
        _client().admin_add_user_to_group(
            UserPoolId=_pool_id(), Username=created["Username"], GroupName=ADMIN_GROUP
        )
    admins = {created["Username"]} if role == ROLE_ADMIN else set()
    logger.info("User %s invited with role %s", created["Username"], role)
    return _json(201, {"user": _user_view(created, admins, caller)})


def _guard_self_and_last_admin(
    target: Dict[str, Any], caller: str, losing_admin: bool
) -> Optional[Dict[str, Any]]:
    if _attributes(target).get("sub") == caller:
        return _error(409, "CANNOT_MODIFY_SELF", "You cannot change or delete your own account.")
    if losing_admin:
        admins = _admin_usernames()
        if target["Username"] in admins and len(admins) <= 1:
            return _error(409, "LAST_ADMIN", "The last administrator cannot be removed.")
    return None


def _revoke_sessions(username: str) -> None:
    try:
        _client().admin_user_global_sign_out(UserPoolId=_pool_id(), Username=username)
    except ClientError:
        logger.warning("Could not sign out user %s", username)


def _set_role(event: Dict[str, Any], username: str, caller: str) -> Dict[str, Any]:
    role = (_body(event) or {}).get("role")
    if role not in ROLES:
        return _error(400, "INVALID_ROLE", "Role must be 'admin' or 'user'.")
    target = _get_user(username)
    if target is None:
        return _error(404, "USER_NOT_FOUND", "No such user.")
    blocked = _guard_self_and_last_admin(target, caller, losing_admin=role == ROLE_USER)
    if blocked:
        return blocked
    if role == ROLE_ADMIN:
        _client().admin_add_user_to_group(
            UserPoolId=_pool_id(), Username=target["Username"], GroupName=ADMIN_GROUP
        )
    else:
        _client().admin_remove_user_from_group(
            UserPoolId=_pool_id(), Username=target["Username"], GroupName=ADMIN_GROUP
        )
        # Tokens carry the groups they were issued with; force a new sign-in.
        _revoke_sessions(target["Username"])
    logger.info("User %s role set to %s", target["Username"], role)
    admins = {target["Username"]} if role == ROLE_ADMIN else set()
    return _json(200, {"user": _user_view(target, admins, caller)})


def _delete_user(username: str, caller: str) -> Dict[str, Any]:
    target = _get_user(username)
    if target is None:
        return _error(404, "USER_NOT_FOUND", "No such user.")
    blocked = _guard_self_and_last_admin(target, caller, losing_admin=True)
    if blocked:
        return blocked
    _revoke_sessions(target["Username"])
    _client().admin_delete_user(UserPoolId=_pool_id(), Username=target["Username"])
    logger.info("User %s deleted", target["Username"])
    return {"statusCode": 204, "headers": {}, "body": ""}


def _resend_invite(username: str) -> Dict[str, Any]:
    target = _get_user(username)
    if target is None:
        return _error(404, "USER_NOT_FOUND", "No such user.")
    if target.get("UserStatus") != "FORCE_CHANGE_PASSWORD":
        return _error(
            409, "ALREADY_ACTIVE", "This user has already set a password; no invitation is needed."
        )
    _client().admin_create_user(
        UserPoolId=_pool_id(),
        Username=target["Username"],
        MessageAction="RESEND",
        DesiredDeliveryMediums=["EMAIL"],
    )
    return _json(202, {"status": "sent"})


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    caller = caller_id(event)
    if caller is None:
        return unauthorized_response()
    if not is_admin(event):
        return _error(403, "FORBIDDEN", "Only administrators can manage users.")

    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    path = ((event.get("requestContext") or {}).get("http") or {}).get("path", "")
    username = (event.get("pathParameters") or {}).get("username")

    if username is None:
        if method == "GET":
            return _list_users(caller)
        if method == "POST":
            return _create_user(event, caller)
    else:
        if method == "PATCH":
            return _set_role(event, username, caller)
        if method == "DELETE":
            return _delete_user(username, caller)
        if method == "POST" and path.endswith("/resend-invite"):
            return _resend_invite(username)
    return _error(405, "METHOD_NOT_ALLOWED", "Unsupported method.")
