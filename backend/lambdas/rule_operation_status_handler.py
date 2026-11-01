"""`RuleOperationStatusHandler` Lambda: `GET /rule-operations/{operationId}`
(design.md "RuleOperationStatusHandler" component / "GET /rule-operations/{operationId}"
API design section).

Responsibilities:
1. Read the `operationId` path parameter from the API Gateway HTTP API (v2)
   Lambda proxy integration event.
2. Look up the corresponding record in the `RuleOperations` table via
   `RuleOperationsRepository.get_by_id()`.
3. If found, return `200` with the operation's current `status`
   (`queued` | `processing` | `applied` | `rejected`) and `errorReason`
   (`null` unless `status == "rejected"`).
4. If no record exists for that `operationId`, return `404` with
   `{ "error": { "code": "OPERATION_NOT_FOUND", ... } }`, mirroring the
   `JOB_NOT_FOUND` pattern used by `job_status_handler.py`.

This handler only reads from `RuleOperations`; it never creates, updates, or
deletes records (that is `RuleOperationSubmitHandler`'s and
`RuleOperationProcessor`'s responsibility).
"""
import json
import logging
from typing import Any, Dict, Optional

from shared.rule_operations_repository import RuleOperationsRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazily-initialized repository so it is created once per Lambda execution
# environment (warm-start reuse) rather than per invocation, matching the
# pattern used by `rule_operation_submit_handler.py`.
_rule_operations_repository: Optional[RuleOperationsRepository] = None


def _get_rule_operations_repository() -> RuleOperationsRepository:
    global _rule_operations_repository
    if _rule_operations_repository is None:
        _rule_operations_repository = RuleOperationsRepository()
    return _rule_operations_repository


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error_response(status_code: int, code: str, message: str) -> Dict[str, Any]:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _operation_not_found_response() -> Dict[str, Any]:
    return _error_response(
        404,
        "OPERATION_NOT_FOUND",
        "No Rule Operation exists for the given operationId.",
    )


def _extract_operation_id(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("operationId")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    operation_id = _extract_operation_id(event)
    if not operation_id:
        return _operation_not_found_response()

    operation = _get_rule_operations_repository().get_by_id(operation_id)
    if operation is None:
        return _operation_not_found_response()

    return _json_response(
        200,
        {
            "operationId": operation.operationId,
            "status": operation.status,
            "errorReason": operation.errorReason,
        },
    )
