"""`DetectionsHandler` Lambda: `GET /jobs/{jobId}/detections` (design.md
"DetectionsHandler" component / "GET /jobs/{jobId}/detections" API design
section).

Responsibilities:
1. Read the `jobId` path parameter from the API Gateway HTTP API (v2)
   Lambda proxy integration event.
2. Read the current `RedactionJobs` record from DynamoDB.
3. If no record exists for that `jobId`, return `404` with
   `{ "error": { "code": "JOB_NOT_FOUND" } }`.
4. If the record exists but its status is not `"detected"`, return `409`
   with `{ "error": { "code": "DETECTIONS_NOT_AVAILABLE", ... } }`.
5. If the record exists and its status is `"detected"`, fetch
   `{jobId}/detections.json` from the output S3 bucket and return it as a
   200 JSON response.

This handler only reads from `RedactionJobs` and the output documents
bucket; it never writes to either.
"""
import json
import logging
import os
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazily-initialized AWS resources/clients so they are created once per
# Lambda execution environment (warm-start reuse) rather than per
# invocation, matching the pattern used by other handlers.
_jobs_table = None
_s3_client = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


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


def _detections_not_available_response() -> Dict[str, Any]:
    return _error_response(
        409,
        "DETECTIONS_NOT_AVAILABLE",
        "Detection results are only available when job status is 'detected'.",
    )


def _extract_job_id(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("jobId")


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    response = _get_jobs_table().get_item(Key={"jobId": job_id})
    job_item = response.get("Item")
    if job_item is None:
        return _job_not_found_response()

    if job_item.get("status") != "detected":
        return _detections_not_available_response()

    bucket_name = os.environ["OUTPUT_BUCKET_NAME"]
    detections_key = f"{job_id}/detections.json"

    s3_object = _get_s3_client().get_object(Bucket=bucket_name, Key=detections_key)
    detections_body = s3_object["Body"].read().decode("utf-8")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": detections_body,
    }
