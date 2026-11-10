"""`RuleOperationSubmitHandler` Lambda: `POST /rules`, `PATCH /rules/{ruleName}`,
`DELETE /rules/{ruleName}` (design.md "RuleOperationSubmitHandler" component /
matching API design sections).

Responsibilities:
1. Synchronously validate the submitted name (1-100 chars) and, for `create`
   only, pattern (1-500 chars) length constraints using the shared length
   validators (`shared/models.py`). A violation returns `400
   INVALID_LENGTH` immediately: no `RuleOperations` record is created and no
   SQS message is sent (design.md "POST /rules" API design section).
2. On a valid request:
   a. Generate a new `operationId` (UUID).
   b. Write a `RuleOperations` record with `status="queued"` via
      `RuleOperationsRepository.create()`.
   c. Send a single SQS FIFO message to `RuleOperationsQueue`
      with a fixed `MessageGroupId` and `MessageDeduplicationId=operationId`,
      carrying the payload `RuleOperationProcessor` needs to
      process the operation later.
   d. Return `202 Accepted` with `{ "operationId": ... }`, without waiting
      for the operation to actually be processed (design.md
      "RuleOperationSubmitHandler" component).

Dispatches on HTTP method (from the API Gateway HTTP API v2 proxy
integration event) rather than on distinct Lambda functions, since all
three routes share the same "validate -> enqueue -> 202" shape:
- `POST /rules`             -> `type="create"`,           body `{ "ruleName", "pattern" }`
- `PATCH /rules/{ruleName}` -> `type="enable"`/`"disable"`, body `{ "enabled" }`, `ruleName` from path
- `DELETE /rules/{ruleName}`-> `type="delete"`,             no body, `ruleName` from path

This handler never touches `RedactionRules` directly and never blocks on the
operation's outcome; validating name uniqueness / pattern compilability /
rule existence and actually mutating `RedactionRules` is
`RuleOperationProcessor`'s job, run asynchronously after this
handler returns.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

from shared.models import (
    RuleOperation,
    is_valid_name_length,
    is_valid_pattern_length,
)
from shared.rule_operations_repository import RuleOperationsRepository
from request_auth import caller_id, is_admin, unauthorized_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Fixed MessageGroupId used by every producer of RuleOperationsQueue
# messages, so the FIFO queue's single-message-group ordering guarantee
# applies across all Rule Operations regardless of type.
RULE_OPERATIONS_MESSAGE_GROUP_ID = "rule-ops"

# Environment variable naming for the queue URL follows the
# `<CONCEPT>_QUEUE_URL` convention (mirrors `RulesRepository`'s /
# `RuleOperationsRepository`'s `<TABLE_CONCEPT>_TABLE_NAME` convention).
RULE_OPERATIONS_QUEUE_URL_ENV_VAR = "RULE_OPERATIONS_QUEUE_URL"

# Lazily-initialized AWS resources/repository so they are created once per
# Lambda execution environment (warm-start reuse) rather than per
# invocation, matching the pattern used by the other handlers.
_rule_operations_repository: Optional[RuleOperationsRepository] = None
_sqs_client = None


def _get_rule_operations_repository() -> RuleOperationsRepository:
    global _rule_operations_repository
    if _rule_operations_repository is None:
        _rule_operations_repository = RuleOperationsRepository()
    return _rule_operations_repository


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error_response(status_code: int, code: str, message: str = "") -> Dict[str, Any]:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _invalid_length_response() -> Dict[str, Any]:
    return _error_response(
        400,
        "INVALID_LENGTH",
        "名称长度需为 1-100 字符，正则长度需为 1-500 字符。",
    )


def _parse_json_body(event: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort parse of the request body as a JSON object.

    Returns `{}` for a missing/empty body or a body that is not valid JSON
    or not a JSON object -- callers then treat any expected field as
    missing, which naturally falls through to the appropriate validation
    failure response rather than raising.
    """
    body = event.get("body") or "{}"
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_rule_name(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("ruleName")


def _http_method(event: Dict[str, Any]) -> str:
    request_context = event.get("requestContext") or {}
    http = request_context.get("http") or {}
    return (http.get("method") or "").upper()


def _submit_operation(
    operation_type: str, target_rule_name: str, pattern: Optional[str] = None
) -> Dict[str, Any]:
    """Create the `RuleOperations` record and enqueue the SQS message for a
    single valid Rule Operation, returning the `202` response.

    This is the one place shared by all three routes once their
    route-specific request validation has already passed.
    """
    operation_id = str(uuid.uuid4())
    timestamp = _now_iso()

    operation = RuleOperation(
        operationId=operation_id,
        type=operation_type,
        targetRuleName=target_rule_name,
        status="queued",
        createdAt=timestamp,
        updatedAt=timestamp,
        pattern=pattern,
    )
    _get_rule_operations_repository().create(operation)

    message_body: Dict[str, Any] = {
        "operationId": operation_id,
        "type": operation_type,
        "targetRuleName": target_rule_name,
    }
    if pattern is not None:
        message_body["pattern"] = pattern

    _get_sqs_client().send_message(
        QueueUrl=os.environ[RULE_OPERATIONS_QUEUE_URL_ENV_VAR],
        MessageBody=json.dumps(message_body),
        MessageGroupId=RULE_OPERATIONS_MESSAGE_GROUP_ID,
        MessageDeduplicationId=operation_id,
    )

    return _json_response(202, {"operationId": operation_id})


def _handle_create(event: Dict[str, Any]) -> Dict[str, Any]:
    """`POST /rules`: submit a `create` Rule Operation."""
    body = _parse_json_body(event)
    rule_name = body.get("ruleName")
    pattern = body.get("pattern")

    if (
        not isinstance(rule_name, str)
        or not isinstance(pattern, str)
        or not is_valid_name_length(rule_name)
        or not is_valid_pattern_length(pattern)
    ):
        return _invalid_length_response()

    return _submit_operation("create", rule_name, pattern=pattern)


def _handle_enable_disable(event: Dict[str, Any]) -> Dict[str, Any]:
    """`PATCH /rules/{ruleName}`: submit an `enable`/`disable` Rule Operation."""
    rule_name = _extract_rule_name(event)
    body = _parse_json_body(event)
    enabled = body.get("enabled")

    if not isinstance(rule_name, str) or not is_valid_name_length(rule_name):
        return _invalid_length_response()
    if not isinstance(enabled, bool):
        return _error_response(
            400, "INVALID_REQUEST", "`enabled` must be a boolean."
        )

    operation_type = "enable" if enabled else "disable"
    return _submit_operation(operation_type, rule_name)


def _handle_delete(event: Dict[str, Any]) -> Dict[str, Any]:
    """`DELETE /rules/{ruleName}`: submit a `delete` Rule Operation."""
    rule_name = _extract_rule_name(event)

    if not isinstance(rule_name, str) or not is_valid_name_length(rule_name):
        return _invalid_length_response()

    return _submit_operation("delete", rule_name)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    # Rules are global and decide what every later job redacts, so only
    # members of the admin group may change them.
    if caller_id(event) is None:
        return unauthorized_response()
    if not is_admin(event):
        return _error_response(403, "FORBIDDEN", "Only administrators can change rules.")

    method = _http_method(event)

    if method == "POST":
        return _handle_create(event)
    if method == "PATCH":
        return _handle_enable_disable(event)
    if method == "DELETE":
        return _handle_delete(event)

    logger.error("Unsupported HTTP method for RuleOperationSubmitHandler: %s", method)
    return _error_response(405, "METHOD_NOT_ALLOWED", "Unsupported method: {}".format(method))
