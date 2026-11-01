"""`RuleOperationsRepository`: DynamoDB access for the `RuleOperations` table.

Wraps the `RuleOperations` table (see design.md "RuleOperation" data model
and `infra/redaction_stack.py`'s `RuleOperationsTable`, PK `operationId`) so
callers (`RuleOperationSubmitHandler`, `RuleOperationStatusHandler`,
and `RuleOperationProcessor`) never touch boto3
directly and always exchange the shared `RuleOperation` dataclass
(`shared/models.py`) rather than raw DynamoDB items.

Supported operations:
- `create`: write a new operation item (typically `status="queued"`).
- `update_status`: transition an existing operation's `status` (and,
  optionally, `errorReason`), bumping `updatedAt`.
- `get_by_id`: fetch a single operation by its partition key, or `None` if
  it does not exist.

This module intentionally mirrors the structure of `RulesRepository`
(`shared/rules_repository.py`): a small class wrapping a lazily
resolved `boto3` DynamoDB `Table` resource, with private
item<->dataclass conversion helpers.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3

from shared.models import RuleOperation

# Environment variable naming follows the convention already used by
# `infra/redaction_stack.py` for other tables (e.g. `JOBS_TABLE_NAME` for
# `RedactionJobs`): `<TABLE_CONCEPT>_TABLE_NAME`.
RULE_OPERATIONS_TABLE_NAME_ENV_VAR = "RULE_OPERATIONS_TABLE_NAME"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _operation_to_item(operation: RuleOperation) -> Dict[str, Any]:
    """Convert a `RuleOperation` dataclass instance to a DynamoDB item dict.

    Optional fields (`pattern`, `errorReason`) are omitted entirely rather
    than stored as an explicit `None`/`NULL` when unset, matching the style
    already used by `lambdas/upload_handler.py` for optional attributes.
    """
    item: Dict[str, Any] = {
        "operationId": operation.operationId,
        "type": operation.type,
        "targetRuleName": operation.targetRuleName,
        "status": operation.status,
        "createdAt": operation.createdAt,
        "updatedAt": operation.updatedAt,
    }
    if operation.pattern is not None:
        item["pattern"] = operation.pattern
    if operation.errorReason is not None:
        item["errorReason"] = operation.errorReason
    return item


def _item_to_operation(item: Dict[str, Any]) -> RuleOperation:
    """Convert a DynamoDB item dict back into a `RuleOperation` dataclass instance."""
    return RuleOperation(
        operationId=item["operationId"],
        type=item["type"],
        targetRuleName=item["targetRuleName"],
        status=item["status"],
        createdAt=item["createdAt"],
        updatedAt=item["updatedAt"],
        pattern=item.get("pattern"),
        errorReason=item.get("errorReason"),
    )


class RuleOperationsRepository:
    """Repository wrapping the `RuleOperations` DynamoDB table.

    By default, resolves the table name from the
    `RULE_OPERATIONS_TABLE_NAME` environment variable and creates its own
    `boto3` DynamoDB resource. Both can be overridden (e.g. in tests, or by
    a caller that already holds a `boto3.resource("dynamodb")`) via the
    constructor arguments.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dynamodb_resource: Optional[Any] = None,
    ) -> None:
        resolved_table_name = table_name or os.environ[RULE_OPERATIONS_TABLE_NAME_ENV_VAR]
        resource = dynamodb_resource if dynamodb_resource is not None else boto3.resource("dynamodb")
        self._table = resource.Table(resolved_table_name)

    def create(self, operation: RuleOperation) -> None:
        """Write a new `RuleOperation` item (typically `status="queued"`)."""
        self._table.put_item(Item=_operation_to_item(operation))

    def update_status(
        self,
        operation_id: str,
        status: str,
        error_reason: Optional[str] = None,
    ) -> None:
        """Update an existing operation's `status` (and `updatedAt`).

        If `error_reason` is provided, it is also written to the
        `errorReason` attribute (typically alongside `status="rejected"`).
        `error_reason=None` leaves any existing `errorReason` attribute
        untouched (it is not cleared).
        """
        update_expression = "SET #status = :status, updatedAt = :updatedAt"
        expression_attribute_names = {"#status": "status"}
        expression_attribute_values: Dict[str, Any] = {
            ":status": status,
            ":updatedAt": _now_iso(),
        }
        if error_reason is not None:
            update_expression += ", errorReason = :errorReason"
            expression_attribute_values[":errorReason"] = error_reason

        self._table.update_item(
            Key={"operationId": operation_id},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values,
        )

    def get_by_id(self, operation_id: str) -> Optional[RuleOperation]:
        """Return the `RuleOperation` with the given id, or `None` if not found."""
        response = self._table.get_item(Key={"operationId": operation_id})
        item = response.get("Item")
        if item is None:
            return None
        return _item_to_operation(item)
