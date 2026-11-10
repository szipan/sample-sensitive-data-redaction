"""User management API (admins only) and first-admin bootstrap."""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))

POOL_ID = "us-east-1_TestPool"


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


class FakeCognito:
    """In-memory subset of the Cognito admin API used by the handlers.

    Mirrors a pool whose sign-in alias is email: the internal username is a
    generated id, and admin calls accept either it or the email.
    """

    def __init__(self):
        self.users = {}
        self.groups = {"admins": set()}
        self.sent = []
        self.signed_out = []

    def _find(self, username):
        if username in self.users:
            return self.users[username]
        for user in self.users.values():
            if user["email"] == username:
                return user
        raise _err("UserNotFoundException")

    def _view(self, user, attr_key):
        return {
            "Username": user["Username"],
            attr_key: [
                {"Name": "sub", "Value": user["Username"]},
                {"Name": "email", "Value": user["email"]},
            ],
            "UserStatus": user["status"],
            "Enabled": True,
            "UserCreateDate": user["created"],
        }

    def admin_get_user(self, UserPoolId, Username):
        return self._view(self._find(Username), "UserAttributes")

    def admin_create_user(self, UserPoolId, Username, MessageAction=None, **kw):
        if MessageAction == "RESEND":
            user = self._find(Username)
            self.sent.append(user["email"])
            return {"User": self._view(user, "Attributes")}
        try:
            self._find(Username)
            raise _err("UsernameExistsException")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "UserNotFoundException":
                raise
        name = str(uuid.uuid4())
        user = {"Username": name, "email": Username, "status": "FORCE_CHANGE_PASSWORD",
                "created": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        self.users[name] = user
        self.sent.append(Username)
        return {"User": self._view(user, "Attributes")}

    def admin_delete_user(self, UserPoolId, Username):
        user = self._find(Username)
        del self.users[user["Username"]]
        for members in self.groups.values():
            members.discard(user["Username"])

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].add(self._find(Username)["Username"])

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.groups[GroupName].discard(self._find(Username)["Username"])

    def admin_user_global_sign_out(self, UserPoolId, Username):
        self.signed_out.append(self._find(Username)["Username"])

    def list_users(self, UserPoolId, **kw):
        return {"Users": [self._view(u, "Attributes") for u in self.users.values()]}

    def list_users_in_group(self, UserPoolId, GroupName, **kw):
        return {"Users": [self._view(self.users[n], "Attributes") for n in self.groups[GroupName]]}

    # test helpers
    def activate(self, username):
        self._find(username)["status"] = "CONFIRMED"

    def groups_of(self, username):
        name = self._find(username)["Username"]
        return sorted(g for g, members in self.groups.items() if name in members)


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setenv("USER_POOL_ID", POOL_ID)
    for name in ("users_handler", "bootstrap_admin_handler"):
        sys.modules.pop(name, None)
    import bootstrap_admin_handler
    import users_handler

    fake = FakeCognito()
    users_handler._cognito = fake
    bootstrap_admin_handler._cognito = fake
    yield {"idp": fake, "id": POOL_ID, "users": users_handler, "boot": bootstrap_admin_handler}


def _sub(idp, pool_id, username):
    return idp.admin_get_user(UserPoolId=pool_id, Username=username)["Username"]


def _bootstrap(pool, email="first@example.com"):
    event = {"RequestType": "Create", "ResourceProperties": {"UserPoolId": pool["id"], "Email": email}}
    return pool["boot"].handler(event, None)


def _event(pool, caller_email, method, path, username=None, body=None, admin=True):
    sub = _sub(pool["idp"], pool["id"], caller_email) if caller_email else None
    ctx = {"http": {"method": method, "path": path}}
    if sub:
        claims = {"sub": sub}
        if admin:
            claims["cognito:groups"] = "[admins]"
        ctx["authorizer"] = {"jwt": {"claims": claims}}
    event = {"requestContext": ctx}
    if username is not None:
        event["pathParameters"] = {"username": username}
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _call(pool, *args, **kwargs):
    response = pool["users"].handler(_event(pool, *args, **kwargs), None)
    body = json.loads(response["body"]) if response.get("body") else None
    return response["statusCode"], body


def _groups(pool, username):
    return pool["idp"].groups_of(username)


# --------------------------------------------------------------------------
# First-admin bootstrap
# --------------------------------------------------------------------------
def test_bootstrap_creates_and_promotes_first_admin(pool):
    assert _bootstrap(pool, " First@Example.com ")["PhysicalResourceId"] == "first-admin"
    user = pool["idp"].admin_get_user(UserPoolId=pool["id"], Username="first@example.com")
    assert user["UserStatus"] == "FORCE_CHANGE_PASSWORD"
    assert _groups(pool, "first@example.com") == ["admins"]


def test_bootstrap_is_idempotent_and_never_deletes(pool):
    _bootstrap(pool)
    _bootstrap(pool)
    update = {"RequestType": "Update", "ResourceProperties": {"UserPoolId": pool["id"], "Email": "second@example.com"}}
    pool["boot"].handler(update, None)
    pool["boot"].handler({"RequestType": "Delete", "ResourceProperties": {}}, None)
    emails = sorted(u["email"] for u in pool["idp"].users.values())
    assert emails == ["first@example.com", "second@example.com"]
    assert pool["idp"].groups_of("second@example.com") == ["admins"]
    # Invited once each, not on every deploy.
    assert pool["idp"].sent == ["first@example.com", "second@example.com"]


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------
def test_only_admins_may_manage_users(pool):
    _bootstrap(pool)
    assert _call(pool, None, "GET", "/users")[0] == 401
    status, body = _call(pool, "first@example.com", "GET", "/users", admin=False)
    assert status == 403 and body["error"]["code"] == "FORBIDDEN"


# --------------------------------------------------------------------------
# Create / list
# --------------------------------------------------------------------------
def test_admin_creates_admins_and_users_and_lists_roles(pool):
    _bootstrap(pool)
    assert _call(pool, "first@example.com", "POST", "/users", body={"email": "Admin2@Example.com", "role": "admin"})[0] == 201
    status, body = _call(pool, "first@example.com", "POST", "/users", body={"email": "user@example.com"})
    assert status == 201 and body["user"]["role"] == "user"

    status, body = _call(pool, "first@example.com", "GET", "/users")
    assert status == 200
    roles = {u["email"]: (u["role"], u["isSelf"], u["status"]) for u in body["users"]}
    assert roles == {
        "admin2@example.com": ("admin", False, "FORCE_CHANGE_PASSWORD"),
        "first@example.com": ("admin", True, "FORCE_CHANGE_PASSWORD"),
        "user@example.com": ("user", False, "FORCE_CHANGE_PASSWORD"),
    }


@pytest.mark.parametrize("body,code", [
    ({"email": "nope"}, "INVALID_EMAIL"),
    ({}, "INVALID_EMAIL"),
    ({"email": "x@example.com", "role": "superuser"}, "INVALID_ROLE"),
])
def test_create_validates_input(pool, body, code):
    _bootstrap(pool)
    status, resp = _call(pool, "first@example.com", "POST", "/users", body=body)
    assert status == 400 and resp["error"]["code"] == code


def test_create_duplicate_is_conflict(pool):
    _bootstrap(pool)
    _call(pool, "first@example.com", "POST", "/users", body={"email": "u@example.com"})
    status, body = _call(pool, "first@example.com", "POST", "/users", body={"email": "u@example.com"})
    assert status == 409 and body["error"]["code"] == "USER_EXISTS"


# --------------------------------------------------------------------------
# Role changes, deletion, guards
# --------------------------------------------------------------------------
def _username(pool, email):
    return pool["idp"].admin_get_user(UserPoolId=pool["id"], Username=email)["Username"]


def test_promote_demote_and_delete(pool):
    _bootstrap(pool)
    _call(pool, "first@example.com", "POST", "/users", body={"email": "u@example.com"})
    u = _username(pool, "u@example.com")

    assert _call(pool, "first@example.com", "PATCH", "/users/" + u, username=u, body={"role": "admin"})[0] == 200
    assert _groups(pool, u) == ["admins"]
    assert _call(pool, "first@example.com", "PATCH", "/users/" + u, username=u, body={"role": "user"})[0] == 200
    assert _groups(pool, u) == []
    assert u in pool["idp"].signed_out  # demotion revokes sessions
    assert _call(pool, "first@example.com", "DELETE", "/users/" + u, username=u)[0] == 204
    assert _call(pool, "first@example.com", "DELETE", "/users/" + u, username=u)[0] == 404


def test_admin_cannot_demote_or_delete_self(pool):
    _bootstrap(pool)
    me = _username(pool, "first@example.com")
    _call(pool, "first@example.com", "POST", "/users", body={"email": "a2@example.com", "role": "admin"})
    for method, body in (("PATCH", {"role": "user"}), ("DELETE", None)):
        status, resp = _call(pool, "first@example.com", method, "/users/" + me, username=me, body=body)
        assert status == 409 and resp["error"]["code"] == "CANNOT_MODIFY_SELF"
    assert _groups(pool, me) == ["admins"]


def test_last_admin_cannot_be_removed(pool):
    _bootstrap(pool)
    other = "a2@example.com"
    _call(pool, "first@example.com", "POST", "/users", body={"email": other, "role": "admin"})
    target = _username(pool, "first@example.com")
    # a2 demotes first -> allowed while two admins exist ...
    assert _call(pool, other, "PATCH", "/users/" + target, username=target, body={"role": "user"})[0] == 200
    # ... now a2 is the only admin; another admin account cannot remove it,
    # and a2 cannot remove itself.
    a2 = _username(pool, other)
    status, resp = _call(pool, other, "DELETE", "/users/" + a2, username=a2)
    assert status == 409


def test_last_admin_guard_against_other_admin(pool, monkeypatch):
    _bootstrap(pool)
    _call(pool, "first@example.com", "POST", "/users", body={"email": "u@example.com"})
    first = _username(pool, "first@example.com")
    # A caller claiming the admin group while not in it (stale token) cannot
    # remove the only real admin.
    status, resp = _call(pool, "u@example.com", "DELETE", "/users/" + first, username=first)
    assert status == 409 and resp["error"]["code"] == "LAST_ADMIN"


def test_unknown_user_and_bad_role(pool):
    _bootstrap(pool)
    assert _call(pool, "first@example.com", "DELETE", "/users/nobody", username="nobody")[0] == 404
    status, resp = _call(pool, "first@example.com", "PATCH", "/users/nobody", username="nobody", body={"role": "x"})
    assert status == 400 and resp["error"]["code"] == "INVALID_ROLE"


def test_resend_invite_only_for_pending_users(pool):
    _bootstrap(pool)
    _call(pool, "first@example.com", "POST", "/users", body={"email": "u@example.com"})
    u = _username(pool, "u@example.com")
    assert _call(pool, "first@example.com", "POST", "/users/%s/resend-invite" % u, username=u)[0] == 202
    pool["idp"].activate(u)
    status, resp = _call(pool, "first@example.com", "POST", "/users/%s/resend-invite" % u, username=u)
    assert status == 409 and resp["error"]["code"] == "ALREADY_ACTIVE"
