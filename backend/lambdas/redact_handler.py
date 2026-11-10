"""`RedactHandler` Lambda: `POST /jobs/{jobId}/redact` (design.md
"RedactHandler" component / "POST /jobs/{jobId}/redact" API design section).

Responsibilities:
1. Read the `jobId` path parameter from the API Gateway HTTP API (v2)
   Lambda proxy integration event.
2. Parse request body as JSON; validate it is `{ "selectedIds": [int, ...] }`.
   If invalid → 400 `INVALID_REQUEST_BODY`.
3. Conditional DynamoDB `update_item`:
   - Condition: `status = "detected" AND attribute_not_exists(confirmedSelections)`
   - Update: `SET confirmedSelections = :ids, updatedAt = :ts`
4. If `ConditionalCheckFailedException`:
   - Re-read the record to distinguish:
     - Not found → 404 `JOB_NOT_FOUND`
     - Status ≠ "detected" → 409 `CONFIRMATION_NOT_ALLOWED`
     - `confirmedSelections` already exists → 409 `ALREADY_CONFIRMED`
5. On success → 202 `{ "status": "accepted" }`

Environment variables: `JOBS_TABLE_NAME`.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from request_auth import caller_id, unauthorized_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazily-initialized AWS resource so it is created once per Lambda execution
# environment (warm-start reuse) rather than per invocation, matching the
# pattern used by the other handlers.
_jobs_table = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error_response(status_code: int, code: str, message: str) -> Dict[str, Any]:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _job_not_found_response() -> Dict[str, Any]:
    return _error_response(404, "JOB_NOT_FOUND", "No Redaction Job exists for the given jobId.")


def _extract_job_id(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("jobId")


def _parse_selected_ids(event: Dict[str, Any]) -> Optional[List[int]]:
    """Parse and validate the request body.

    Returns a list of integer IDs if valid, or None if the body is malformed.
    """
    raw_body = event.get("body")
    if raw_body is None:
        return None

    # Handle base64-encoded bodies from API Gateway
    if event.get("isBase64Encoded"):
        import base64
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:
            return None

    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    selected_ids = parsed.get("selectedIds")
    if not isinstance(selected_ids, list):
        return None

    # Validate every element is an integer (not a float, not a string, etc.)
    for item in selected_ids:
        if not isinstance(item, int) or isinstance(item, bool):
            return None

    return selected_ids


def _resolve_conditional_failure(job_id: str, caller: str) -> Dict[str, Any]:
    """Distinguish the reason for a ConditionalCheckFailedException.

    Re-reads the record to determine whether:
    - The job does not exist, or belongs to another user → 404
    - The job status is not "detected" → 409 CONFIRMATION_NOT_ALLOWED
    - The job already has confirmedSelections → 409 ALREADY_CONFIRMED
    """
    response = _get_jobs_table().get_item(Key={"jobId": job_id})
    job_item = response.get("Item")

    if job_item is None or job_item.get("ownerId") != caller:
        return _job_not_found_response()

    if job_item.get("status") != "detected":
        return _error_response(
            409, "CONFIRMATION_NOT_ALLOWED", "Job is not in 'detected' status."
        )

    # Status is "detected" but condition still failed → confirmedSelections exists
    return _error_response(
        409, "ALREADY_CONFIRMED", "Selections have already been submitted for this job."
    )


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    caller = caller_id(event)
    if caller is None:
        return unauthorized_response()

    selected_ids = _parse_selected_ids(event)
    if selected_ids is None:
        return _error_response(
            400,
            "INVALID_REQUEST_BODY",
            "Body must be a JSON object with a 'selectedIds' array of integers.",
        )

    # Conditional update: status must be "detected" AND confirmedSelections
    # must not exist. This ensures exactly-once semantics for confirmation.
    try:
        _get_jobs_table().update_item(
            Key={"jobId": job_id},
            UpdateExpression="SET confirmedSelections = :ids, updatedAt = :ts",
            ConditionExpression=(
                "ownerId = :owner AND #s = :detected"
                " AND attribute_not_exists(confirmedSelections)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":ids": selected_ids,
                ":detected": "detected",
                ":owner": caller,
                ":ts": _now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resolve_conditional_failure(job_id, caller)
        raise

    return _json_response(202, {"status": "accepted"})
