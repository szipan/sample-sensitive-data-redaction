"""`RulesRepository`: DynamoDB access for the `RedactionRules` table.

Wraps the `RedactionRules` table (see design.md "RedactionRule" data model
and `infra/redaction_stack.py`'s `RedactionRulesTable`, PK `ruleName`) so
callers (`RulesListHandler`, `RuleOperationProcessor`,
and the rule-collection initialization wiring) never
touch boto3 directly and always exchange the shared `RedactionRule`
dataclass (`shared/models.py`) rather than raw DynamoDB items.

Supported operations:
- `get_all`: return every stored `RedactionRule`.
- `get_by_name`: fetch a single rule by its partition key, or `None` if it
  does not exist.
- `put`: write/overwrite a rule item (used for creating new rules).
- `update_enabled`: update just the `enabled` field (and `updatedAt`
  timestamp) on an existing item.
- `delete`: remove a rule item by name.

This module intentionally mirrors the structure of `RuleOperationsRepository`
(`shared/rule_operations_repository.py`): a small class wrapping a
lazily resolved `boto3` DynamoDB `Table` resource, with private
item<->dataclass conversion helpers. Timestamps are stored as ISO-8601
strings (per design.md), so no DynamoDB `Decimal` handling is needed for
`createdAt`/`updatedAt`.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

from shared.models import RedactionRule

# Environment variable naming follows the convention already used by
# `infra/redaction_stack.py` for other tables (e.g. `JOBS_TABLE_NAME` for
# `RedactionJobs`, `RULE_OPERATIONS_TABLE_NAME` for `RuleOperations`):
# `<TABLE_CONCEPT>_TABLE_NAME`.
RULES_TABLE_NAME_ENV_VAR = "RULES_TABLE_NAME"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rule_to_item(rule: RedactionRule) -> Dict[str, Any]:
    """Convert a `RedactionRule` dataclass instance to a DynamoDB item dict."""
    return {
        "ruleName": rule.ruleName,
        "pattern": rule.pattern,
        "enabled": rule.enabled,
        "isBuiltIn": rule.isBuiltIn,
        "createdAt": rule.createdAt,
        "updatedAt": rule.updatedAt,
    }


def _item_to_rule(item: Dict[str, Any]) -> RedactionRule:
    """Convert a DynamoDB item dict back into a `RedactionRule` dataclass instance."""
    return RedactionRule(
        ruleName=item["ruleName"],
        pattern=item["pattern"],
        enabled=bool(item["enabled"]),
        isBuiltIn=bool(item["isBuiltIn"]),
        createdAt=item["createdAt"],
        updatedAt=item["updatedAt"],
    )


class RulesRepository:
    """Repository wrapping the `RedactionRules` DynamoDB table.

    By default, resolves the table name from the `RULES_TABLE_NAME`
    environment variable and creates its own `boto3` DynamoDB resource.
    Both can be overridden (e.g. in tests, or by a caller that already
    holds a `boto3.resource("dynamodb")`) via the constructor arguments.
    """

    def __init__(
        self,
        table_name: Optional[str] = None,
        dynamodb_resource: Optional[Any] = None,
    ) -> None:
        resolved_table_name = table_name or os.environ[RULES_TABLE_NAME_ENV_VAR]
        resource = dynamodb_resource if dynamodb_resource is not None else boto3.resource("dynamodb")
        self._table = resource.Table(resolved_table_name)

    def get_all(self) -> List[RedactionRule]:
        """Return every `RedactionRule` currently stored in the table.

        Pages through the full table via `scan` (a POC-appropriate choice
        given the small, bounded number of rules expected); results are in
        no particular guaranteed order.
        """
        rules: List[RedactionRule] = []
        scan_kwargs: Dict[str, Any] = {}
        while True:
            response = self._table.scan(**scan_kwargs)
            rules.extend(_item_to_rule(item) for item in response.get("Items", []))
            last_evaluated_key = response.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_evaluated_key
        return rules

    def get_by_name(self, rule_name: str) -> Optional[RedactionRule]:
        """Return the `RedactionRule` with the given name, or `None` if not found."""
        response = self._table.get_item(Key={"ruleName": rule_name})
        item = response.get("Item")
        if item is None:
            return None
        return _item_to_rule(item)

    def put(self, rule: RedactionRule) -> None:
        """Write/overwrite a rule item (used for creating new rules)."""
        self._table.put_item(Item=_rule_to_item(rule))

    def update_enabled(self, rule_name: str, enabled: bool) -> None:
        """Update just the `enabled` field (and `updatedAt`) on an existing item."""
        self._table.update_item(
            Key={"ruleName": rule_name},
            UpdateExpression="SET enabled = :enabled, updatedAt = :updatedAt",
            ExpressionAttributeValues={
                ":enabled": enabled,
                ":updatedAt": _now_iso(),
            },
        )

    def delete(self, rule_name: str) -> None:
        """Delete a rule item by name."""
        self._table.delete_item(Key={"ruleName": rule_name})
