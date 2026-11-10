"""Deploy-time creation of the first administrator.

Invoked by a CloudFormation custom resource with ``UserPoolId`` and
``Email``. On Create and Update it makes sure a user with that email exists
(inviting them by email with a temporary password when they do not) and is
a member of the admin group. It never deletes or demotes anyone: changing the
email on a later deploy adds another admin, and stack deletion removes the
whole user pool anyway.
"""
import logging
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

from request_auth import ADMIN_GROUP

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PHYSICAL_ID = "first-admin"

_cognito = None


def _client():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


def ensure_admin(pool_id: str, email: str) -> str:
    """Create (if missing) and promote the user; returns their username."""
    email = email.strip().lower()
    try:
        username = _client().admin_get_user(UserPoolId=pool_id, Username=email)["Username"]
        logger.info("First admin already exists")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UserNotFoundException":
            raise
        username = _client().admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )["User"]["Username"]
        logger.info("First admin invited")
    _client().admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=ADMIN_GROUP)
    return username


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if event.get("RequestType") in ("Create", "Update"):
        props = event.get("ResourceProperties") or {}
        ensure_admin(props["UserPoolId"], props["Email"])
    return {"PhysicalResourceId": PHYSICAL_ID}
