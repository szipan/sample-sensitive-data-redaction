"""`RuleOperationProcessor` Lambda: SQS FIFO consumer of `RuleOperationsQueue`
(design.md "RuleOperationProcessor" component).

Responsibilities, per SQS record (each record's
`body` is the JSON payload `RuleOperationSubmitHandler` sent:
`{operationId, type, targetRuleName, pattern?}`):

1. Update the `RuleOperations` record's status to `"processing"` via
   `RuleOperationsRepository.update_status(operationId, "processing")`.
2. Read the current rule collection via `RulesRepository.get_all()` and
   convert it to the `dict[ruleName, RedactionRule]` shape the pure
   validation functions in `shared/rule_validation.py` expect.
3. Dispatch by `type` and validate against that collection:
   - `"create"`: `validate_create(existing_rules, targetRuleName, pattern)`.
     If valid, persist the new enabled, non-built-in `RedactionRule` via
     `RulesRepository.put()`.
   - `"enable"` / `"disable"`: `validate_enable_disable(existing_rules,
     targetRuleName)`. If valid, `RulesRepository.update_enabled(
     targetRuleName, enabled=True/False)`.
   - `"delete"`: `validate_delete(existing_rules, targetRuleName)`. If
     valid, `RulesRepository.delete(targetRuleName)`.
4. Write the Terminal Result back to `RuleOperations`:
   - Validation succeeded (and the mutation was applied):
     `RuleOperationsRepository.update_status(operationId, "applied")`.
   - Validation failed: `RuleOperationsRepository.update_status(
     operationId, "rejected", error_reason=<ValidationResult.errorReason>)`
     -- `RedactionRules` is never touched in this case.

The SQS FIFO queue (`RuleOperationsQueue`) plus its `BatchSize=1` event
source mapping together mean this handler is only ever invoked
with a single record in practice. This handler nonetheless processes
however many records `event["Records"]` actually contains, one at a time
and in list order, for robustness against that assumption changing (e.g. a
future batch-size increase).

A failure while processing one record (e.g. an unexpected exception) is
logged and does not abort processing of subsequent records in the same
invocation, mirroring `lambdas/job_dispatcher.py`'s per-record isolation
style.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.models import RedactionRule
from shared.rule_collection import RuleCollection
from shared.rule_operations_repository import RuleOperationsRepository
from shared.rule_validation import (
    ValidationResult,
    validate_create,
    validate_delete,
    validate_enable_disable,
)
from shared.rules_repository import RulesRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PROCESSING_STATUS = "processing"
APPLIED_STATUS = "applied"
REJECTED_STATUS = "rejected"

# Lazily-initialized repositories so they are created once per Lambda
# execution environment (warm-start reuse) rather than per invocation,
# matching the pattern used by the other handlers.
_rules_repository: Optional[RulesRepository] = None
_rule_operations_repository: Optional[RuleOperationsRepository] = None


def _get_rules_repository() -> RulesRepository:
    global _rules_repository
    if _rules_repository is None:
        _rules_repository = RulesRepository()
    return _rules_repository


def _get_rule_operations_repository() -> RuleOperationsRepository:
    global _rule_operations_repository
    if _rule_operations_repository is None:
        _rule_operations_repository = RuleOperationsRepository()
    return _rule_operations_repository


def _load_existing_rules(rules_repository: RulesRepository) -> RuleCollection:
    return {rule.ruleName: rule for rule in rules_repository.get_all()}


def _new_rule_from_create(rule_name: str, pattern: str) -> RedactionRule:
    """Build the new `RedactionRule` for a validated `create` operation.

    `enabled=True`, `isBuiltIn=False`.
    `createdAt`/`updatedAt` are set by `RulesRepository`-adjacent helpers
    elsewhere (e.g. `apply_create`); this Lambda constructs the item
    directly (rather than calling `apply_create`, which returns a whole new
    in-memory collection dict this handler has no other use for) but keeps
    the same timestamp convention (ISO-8601 UTC, both fields equal on
    creation).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    return RedactionRule(
        ruleName=rule_name,
        pattern=pattern,
        enabled=True,
        isBuiltIn=False,
        createdAt=timestamp,
        updatedAt=timestamp,
    )


def _process_create(
    existing_rules: RuleCollection,
    rules_repository: RulesRepository,
    target_rule_name: str,
    pattern: Optional[str],
) -> ValidationResult:
    result = validate_create(existing_rules, target_rule_name, pattern or "")
    if result.ok:
        rules_repository.put(_new_rule_from_create(target_rule_name, pattern or ""))
    return result


def _process_enable_disable(
    existing_rules: RuleCollection,
    rules_repository: RulesRepository,
    target_rule_name: str,
    enabled: bool,
) -> ValidationResult:
    result = validate_enable_disable(existing_rules, target_rule_name)
    if result.ok:
        rules_repository.update_enabled(target_rule_name, enabled)
    return result


def _process_delete(
    existing_rules: RuleCollection,
    rules_repository: RulesRepository,
    target_rule_name: str,
) -> ValidationResult:
    result = validate_delete(existing_rules, target_rule_name)
    if result.ok:
        rules_repository.delete(target_rule_name)
    return result


def process_operation(
    payload: Dict[str, Any],
    rules_repository: RulesRepository,
    rule_operations_repository: RuleOperationsRepository,
) -> ValidationResult:
    """Process a single Rule Operation payload end to end, writing both the
    `"processing"` transition and the final Terminal Result to
    `RuleOperations`, and (only on success) the corresponding mutation to
    `RedactionRules`.

    Returns the `ValidationResult` for the operation, mainly to make this
    function easy to unit test / call directly.
    """
    operation_id = payload["operationId"]
    operation_type = payload["type"]
    target_rule_name = payload["targetRuleName"]
    pattern = payload.get("pattern")

    rule_operations_repository.update_status(operation_id, PROCESSING_STATUS)

    existing_rules = _load_existing_rules(rules_repository)

    if operation_type == "create":
        result = _process_create(existing_rules, rules_repository, target_rule_name, pattern)
    elif operation_type == "enable":
        result = _process_enable_disable(
            existing_rules, rules_repository, target_rule_name, True
        )
    elif operation_type == "disable":
        result = _process_enable_disable(
            existing_rules, rules_repository, target_rule_name, False
        )
    elif operation_type == "delete":
        result = _process_delete(existing_rules, rules_repository, target_rule_name)
    else:
        logger.error("Unknown Rule Operation type: %s", operation_type)
        result = ValidationResult.failure("UNKNOWN_RULE_NAME")

    if result.ok:
        rule_operations_repository.update_status(operation_id, APPLIED_STATUS)
    else:
        rule_operations_repository.update_status(
            operation_id, REJECTED_STATUS, error_reason=result.errorReason
        )

    return result


def _safe_operation_id(record: Dict[str, Any]) -> Optional[str]:
    """Best-effort `operationId` for a failed record's log line.

    Called from inside an `except` block where `json.loads(record["body"])`
    may be exactly what failed, so this must never raise: a malformed body,
    a missing body, or a body that is not a JSON object all yield `None`
    rather than propagating and masking the original failure.

    Only the `operationId` is read. The record's `body` also carries the
    user-authored `pattern`, which is a Sensitive_Value
    and must not reach a log record.
    """
    try:
        payload = json.loads(record["body"])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    operation_id = payload.get("operationId")
    return operation_id if isinstance(operation_id, str) else None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = event.get("Records") or []

    rules_repository = _get_rules_repository()
    rule_operations_repository = _get_rule_operations_repository()

    processed_count = 0
    for record in records:
        try:
            payload = json.loads(record["body"])
            process_operation(payload, rules_repository, rule_operations_repository)
            processed_count += 1
        except Exception:
            # Do not let one bad record fail the whole batch; log and move
            # on to the next record (mirrors `lambdas/job_dispatcher.py`).
            # Identify the record by Safe_Reference only: the whole record
            # carries the user-authored rule
            # `pattern` in its `body`.
            logger.exception(
                "Failed to process Rule Operation record (messageId %s, operationId %s)",
                record.get("messageId"),
                _safe_operation_id(record),
            )

    return {"processedCount": processed_count}
