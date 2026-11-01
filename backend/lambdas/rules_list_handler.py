"""`RulesListHandler` Lambda: `GET /rules` (design.md "RulesListHandler"
component / "GET /rules" API design section).

Responsibilities:
1. Read every currently-stored `RedactionRule` via `RulesRepository.get_all()`.
2. Map each `RedactionRule` to the wire shape design.md's "GET /rules" API
   design section specifies: `{ "ruleName", "pattern", "enabled",
   "isBuiltIn" }`.
3. Return `200` with `{ "rules": [...] }`.

This handler only reads from `RedactionRules`; it never creates, updates, or
deletes rule records (that is `RuleOperationSubmitHandler`'s and
`RuleOperationProcessor`'s responsibility).
"""
import json
import logging
from typing import Any, Dict, List

from shared.models import RedactionRule
from shared.rules_repository import RulesRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazily-initialized repository so the underlying boto3 DynamoDB resource is
# created once per Lambda execution environment (warm-start reuse) rather
# than per invocation, matching the pattern used by the other handlers.
_rules_repository = None


def _get_rules_repository() -> RulesRepository:
    global _rules_repository
    if _rules_repository is None:
        _rules_repository = RulesRepository()
    return _rules_repository


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _rule_to_response_dict(rule: RedactionRule) -> Dict[str, Any]:
    return {
        "ruleName": rule.ruleName,
        "pattern": rule.pattern,
        "enabled": rule.enabled,
        "isBuiltIn": rule.isBuiltIn,
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    rules: List[RedactionRule] = _get_rules_repository().get_all()
    return _json_response(200, {"rules": [_rule_to_response_dict(rule) for rule in rules]})
