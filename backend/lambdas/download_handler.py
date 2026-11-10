"""`DownloadHandler` Lambda: `GET /jobs/{jobId}/download` (design.md
"JobStatusHandler / DownloadHandler" component / "GET /jobs/{jobId}/download"
API design section).

Responsibilities:
1. Read the `jobId` path parameter from the API Gateway HTTP API (v2)
   Lambda proxy integration event.
2. Immediately before returning the file, re-read the current status of the
   corresponding `RedactionJobs` record from DynamoDB. This exact read is
   the race-condition check point (design.md): the handler never trusts a
   caller-supplied or previously-cached status, only what this fresh read
   returns.
3. If no record exists for that `jobId`, return `404` with
   `{ "error": { "code": "JOB_NOT_FOUND" } }`.
4. If the record exists but its status (at this exact read) is not
   `completed`, return `409` with
   `{ "error": { "code": "REDACTED_DOCUMENT_UNAVAILABLE", ... } }` and do
   not touch the output bucket at all.
5. If the record exists and its status (at this exact read) is `completed`,
   fetch the Redacted Document from the output S3 bucket using the
   record's `redactedKey` and return it as a binary response
   (`isBase64Encoded=True`, base64-encoded body) with a `Content-Disposition:
   attachment; filename="..."` header using the record's `fileName`.

This handler only reads from `RedactionJobs` and the output documents
bucket; it never writes to either.
"""
import base64
import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

import boto3

from request_auth import caller_id, unauthorized_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# `Content-Type` per Supported Document extension (design.md "UploadHandler"
# / `SUPPORTED_EXTENSIONS`); Redacted Documents keep the source document's
# extension, so the same mapping applies here.
CONTENT_TYPE_BY_EXTENSION = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"

# Lazily-initialized AWS resources/clients so they are created once per
# Lambda execution environment (warm-start reuse) rather than per
# invocation, matching the pattern used by `upload_handler.py` and
# `job_status_handler.py`.
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


def _redacted_document_unavailable_response() -> Dict[str, Any]:
    return _error_response(
        409, "REDACTED_DOCUMENT_UNAVAILABLE", "Job 未处于 completed 状态"
    )


def _extract_job_id(event: Dict[str, Any]) -> Optional[str]:
    path_parameters = event.get("pathParameters") or {}
    return path_parameters.get("jobId")


def _content_type_for(file_name: str) -> str:
    _, extension = os.path.splitext(file_name)
    return CONTENT_TYPE_BY_EXTENSION.get(extension.lower(), DEFAULT_CONTENT_TYPE)


def _content_disposition(file_name: str) -> str:
    """`Content-Disposition` for a download of `file_name`.

    An ASCII name keeps the plain `filename="..."` form. A name with other
    characters (e.g. Chinese) cannot go in the plain parameter: header values
    are ASCII/latin-1, so it gets an ASCII fallback in `filename` plus the
    RFC 5987 `filename*=UTF-8''<percent-encoded>` form that browsers and
    Windows use to show the real name.
    """
    safe = file_name.replace("\r", "").replace("\n", "").replace('"', "'").replace("\\", "_")
    if safe.isascii():
        return 'attachment; filename="{}"'.format(safe)
    fallback = "".join(ch if ch.isascii() else "_" for ch in safe)
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
        fallback, quote(safe, safe="")
    )


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    job_id = _extract_job_id(event)
    if not job_id:
        return _job_not_found_response()

    # Fresh read: this is the sole source of truth for the current status.
    # No query parameter, header, or earlier-cached value from elsewhere in
    # the request is ever consulted for the completed-status check below.
    caller = caller_id(event)
    if caller is None:
        return unauthorized_response()

    response = _get_jobs_table().get_item(Key={"jobId": job_id})
    job_item = response.get("Item")
    # A job owned by someone else is reported exactly like a missing one,
    # so job ids cannot be probed.
    if job_item is None or job_item.get("ownerId") != caller:
        return _job_not_found_response()

    if job_item.get("status") != "completed":
        return _redacted_document_unavailable_response()

    bucket_name = os.environ["OUTPUT_BUCKET_NAME"]
    redacted_key = job_item["redactedKey"]
    file_name = job_item["fileName"]

    s3_object = _get_s3_client().get_object(Bucket=bucket_name, Key=redacted_key)
    file_bytes = s3_object["Body"].read()

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": _content_type_for(file_name),
            "Content-Disposition": _content_disposition(file_name),
        },
        "isBase64Encoded": True,
        "body": base64.b64encode(file_bytes).decode("ascii"),
    }
