"""`JobStatusHandler` Lambda: `GET /jobs/{jobId}` (design.md "JobStatusHandler"
component / "GET /jobs/{jobId}" API design section).

Responsibilities:
1. Read the `jobId` path parameter from the API Gateway HTTP API (v2)
   Lambda proxy integration event.
2. Look up the corresponding record in the `RedactionJobs` table.
3. If found, return `200` with the Job's current `status`
   (`processing` | `completed` | `failed`), `errorMessage` (`null` unless
   `status == "failed"`), and the Job's `detectionSummary` (`null` when the
   record carries none).
4. If no record exists for that `jobId`, return `404` with
   `{ "error": { "code": "JOB_NOT_FOUND", ... } }`.

This handler only reads from `RedactionJobs`; it does not create, update, or
delete Job records (that is `UploadHandler`'s and the Agent's
`update_job_status` tool's responsibility).
"""
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3

from request_auth import caller_id, unauthorized_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazily-initialized AWS resource so it is created once per Lambda execution
# environment (warm-start reuse) rather than per invocation, matching the
# pattern used by `upload_handler.py`.
_jobs_table = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _to_jsonable(value: Any) -> Any:
    """Recursively convert DynamoDB-native values into JSON-serializable ones.

    The `boto3.resource` interface deserializes every DynamoDB number as
    `decimal.Decimal`, and `json.dumps` raises `TypeError` on `Decimal`. The
    `detectionSummary` attribute is a nested map holding lists of maps of
    numbers (design.md "Data Models"), so the conversion has to walk the whole
    structure rather than only its top level.

    Integral decimals become `int` so the counts read back as the integers they
    were written as; non-integral decimals become `float`.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    return value


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(_to_jsonable(payload)),
    }


def _error_response(status_code: int, code: str, message: str) -> Dict[str, Any]:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _job_not_found_response() -> Dict[str, Any]:
    return _error_response(404, "JOB_NOT_FOUND", "No Redaction Job exists for the given jobId.")


def _extract_job_id(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("jobId")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    caller = caller_id(event)
    if caller is None:
        return unauthorized_response()

    response = _get_jobs_table().get_item(Key={"jobId": job_id})
    job_item = response.get("Item")
    # A job owned by someone else is reported exactly like a missing one,
    # so job ids cannot be probed.
    if job_item is None or job_item.get("ownerId") != caller:
        return _job_not_found_response()

    return _json_response(
        200,
        {
            "jobId": job_item["jobId"],
            "status": job_item["status"],
            "mode": job_item.get("mode", "auto"),
            "errorMessage": job_item.get("errorMessage"),
            # Machine-readable failure reason (and the values it interpolates,
            # e.g. the OCR page number) for the UI to translate; `null` when
            # absent, e.g. for jobs recorded before these fields existed.
            "errorCode": job_item.get("errorCode"),
            "errorParams": job_item.get("errorParams"),
            # The summary when present; `null` when absent.
            "detectionSummary": job_item.get("detectionSummary"),
        },
    )
