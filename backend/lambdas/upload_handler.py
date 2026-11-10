"""`UploadHandler` Lambda: `POST /jobs` (design.md "UploadHandler" component).

Responsibilities:
1. Validate that the uploaded file's extension is a Supported Document
   (`.docx` or `.pdf`); reject anything else with `400 UNSUPPORTED_FILE_TYPE`
   and create no Redaction Job.
2. Write the accepted document's bytes to the source S3 bucket.
3. On a successful S3 write, create a `RedactionJobs` record with
   `status="processing"` and return a reference to it.
4. On a failed S3 write, create a `RedactionJobs` record with
   `status="failed"` and return a failure response instead of leaving the
   Job stuck or absent.

Triggered via an API Gateway HTTP API (v2) Lambda proxy integration. The
request body is `multipart/form-data` with a single relevant field, `file`
(design.md "API Design" -> `POST /jobs`). API Gateway may base64-encode the
body for this binary content type (`isBase64Encoded=True`); both encoded and
raw bodies are handled.

Multipart parsing intentionally avoids the deprecated/removed stdlib `cgi`
module and does not pull in a third-party dependency: it re-uses the
standard library `email` package, which already implements MIME multipart
parsing, by prepending a synthetic `Content-Type` header to the raw body so
`email.message_from_bytes` can treat it as a MIME message.
"""
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from request_auth import caller_id, unauthorized_response

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SUPPORTED_EXTENSIONS = (".docx", ".pdf")
SUPPORTED_EXTENSIONS_MESSAGE = "支持的扩展名：.docx, .pdf"
UPLOAD_FILE_FIELD_NAME = "file"

# Optional `multipart/form-data` text fields letting the caller choose which
# detection source(s) may contribute a given entity type's spans for this Job
# (detection-source-switch feature). Absent or unrecognized -> `"llm"` (see
# `shared.models.DETECTION_SOURCE_DEFAULT` for why). Stored verbatim on the
# RedactionJobs record and read by the orchestrator; see
# `shared.models.DetectionSource`.
DETECTION_SOURCE_VALUES = ("both", "llm", "guardrails")
DETECTION_SOURCE_DEFAULT = "llm"
ADDRESS_DETECTION_SOURCE_FIELD_NAME = "addressDetectionSource"
NAME_DETECTION_SOURCE_FIELD_NAME = "nameDetectionSource"
# Back-compat aliases.
ADDRESS_DETECTION_SOURCE_VALUES = DETECTION_SOURCE_VALUES
ADDRESS_DETECTION_SOURCE_DEFAULT = DETECTION_SOURCE_DEFAULT

# Optional `multipart/form-data` text field selecting the processing workflow
# for this Job (detection-review-mode feature). `"auto"` preserves the existing
# fully-automatic pipeline; `"review"` enables the detect-then-confirm two-phase
# workflow. Absent or unrecognized -> `"auto"`.
JOB_MODE_VALUES = ("auto", "review")
JOB_MODE_DEFAULT = "auto"
JOB_MODE_FIELD_NAME = "mode"

# Lazily-initialized AWS clients/resources so they are created once per
# Lambda execution environment (warm-start reuse) rather than per invocation.
_s3_client = None
_jobs_table = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _json_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error_response(status_code: int, code: str, message: str) -> Dict[str, Any]:
    return _json_response(status_code, {"error": {"code": code, "message": message}})


def _unsupported_file_type_response() -> Dict[str, Any]:
    return _error_response(400, "UNSUPPORTED_FILE_TYPE", SUPPORTED_EXTENSIONS_MESSAGE)


def _get_header(event: Dict[str, Any], header_name: str) -> Optional[str]:
    headers = event.get("headers") or {}
    header_name_lower = header_name.lower()
    for key, value in headers.items():
        if key.lower() == header_name_lower:
            return value
    return None


def _decode_body(event: Dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    if isinstance(body, bytes):
        return body
    return body.encode("utf-8")


_CONTENT_DISPOSITION_LINE = re.compile(rb"(?im)^content-disposition:[^\r\n]*")
_QUOTED_FILENAME = re.compile(r'filename="((?:[^"\\]|\\.)*)"')


def _filename_from_raw_body(raw_body: bytes) -> Optional[str]:
    """The `file` part's filename, read straight from the raw bytes as UTF-8.

    Browsers send a non-ASCII filename as raw UTF-8 in the part's
    `Content-Disposition` header. `email.message_from_bytes` only decodes
    headers as ASCII and turns every other byte into U+FFFD, so
    `part.get_filename()` returns e.g. `中新旭德` as twelve replacement
    characters and the real name is gone by the time the parser returns.
    """
    for line in _CONTENT_DISPOSITION_LINE.findall(raw_body):
        text = line.decode("utf-8", errors="replace")
        if 'name="{}"'.format(UPLOAD_FILE_FIELD_NAME) not in text:
            continue
        match = _QUOTED_FILENAME.search(text)
        if match:
            return re.sub(r"\\(.)", r"\1", match.group(1))
    return None


def _parse_multipart_fields(
    content_type: str, raw_body: bytes
) -> Tuple[Optional[str], Optional[bytes], Dict[str, str]]:
    """Extract the `file` field's filename/content bytes AND any simple text
    fields from a `multipart/form-data` body.

    Returns `(filename, content, text_fields)` where `text_fields` maps each
    non-file form field's name to its decoded string value. `(None, None, {})`
    is returned if the body is not a well-formed multipart message; a
    well-formed body with no `file` field yields `(None, None, text_fields)`.
    """
    synthetic_header = (
        "Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8")
    message = message_from_bytes(synthetic_header + raw_body)
    if not message.is_multipart():
        return None, None, {}

    file_name: Optional[str] = None
    file_content: Optional[bytes] = None
    text_fields: Dict[str, str] = {}

    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        filename = part.get_filename()
        if field_name == UPLOAD_FILE_FIELD_NAME and filename:
            if "\ufffd" in filename:
                # Lossy ASCII-only header decoding (see `_filename_from_raw_body`).
                filename = _filename_from_raw_body(raw_body) or filename
            file_name = filename
            file_content = part.get_payload(decode=True)
            continue
        if filename:
            # A non-`file` field that nonetheless carries a filename is not a
            # simple text field; ignore it.
            continue
        payload = part.get_payload(decode=True)
        if payload is not None:
            text_fields[field_name] = payload.decode("utf-8", errors="replace").strip()

    return file_name, file_content, text_fields


def _normalize_detection_source(raw_value: Optional[str], field_name: str) -> str:
    """Coerce a caller-supplied detection-source value (`addressDetectionSource`
    or `nameDetectionSource`) to one of the allowed values, defaulting to
    `DETECTION_SOURCE_DEFAULT` (`"llm"`).

    Lenient by design: an absent, blank, or unrecognized value falls back to
    the default rather than rejecting the whole upload, so a client that
    omits the field — or sends a typo — still gets a working Job.
    """
    value = (raw_value or "").strip()
    if value in DETECTION_SOURCE_VALUES:
        return value
    if value:
        logger.warning(
            "Ignoring unrecognized %s=%r; defaulting to %r",
            field_name,
            value,
            DETECTION_SOURCE_DEFAULT,
        )
    return DETECTION_SOURCE_DEFAULT


def _normalize_job_mode(raw_value: Optional[str]) -> str:
    """Coerce a caller-supplied `mode` value to one of the allowed values,
    defaulting to `"auto"`.

    Lenient by design: an absent, blank, or unrecognized value falls back to
    `"auto"` (the safe, pre-feature behavior) rather than rejecting the whole
    upload, so a client that omits the field still gets a working Job with
    automatic processing.
    """
    value = (raw_value or "").strip()
    if value in JOB_MODE_VALUES:
        return value
    if value:
        logger.warning(
            "Ignoring unrecognized %s=%r; defaulting to %r",
            JOB_MODE_FIELD_NAME,
            value,
            JOB_MODE_DEFAULT,
        )
    return JOB_MODE_DEFAULT


def _extension_of(file_name: str) -> str:
    _, extension = os.path.splitext(file_name)
    return extension.lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_epoch() -> int:
    """Epoch seconds at which DynamoDB TTL may delete a job record; matches
    the S3 lifecycle retention (`JOB_RETENTION_DAYS`, default 1)."""
    try:
        days = int(os.environ.get("JOB_RETENTION_DAYS", "1"))
    except ValueError:
        days = 1
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def _put_job_item(job_item: Dict[str, Any]) -> None:
    item = dict(job_item)
    item.setdefault("expiresAt", _expires_at_epoch())
    _get_jobs_table().put_item(Item=item)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    owner_id = caller_id(event)
    if owner_id is None:
        return unauthorized_response()

    content_type = _get_header(event, "content-type")
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        return _unsupported_file_type_response()

    try:
        raw_body = _decode_body(event)
        file_name, file_bytes, text_fields = _parse_multipart_fields(
            content_type, raw_body
        )
    except Exception:
        logger.exception("Failed to parse multipart upload request body")
        return _unsupported_file_type_response()

    if not file_name or file_bytes is None:
        return _unsupported_file_type_response()

    extension = _extension_of(file_name)
    if extension not in SUPPORTED_EXTENSIONS:
        return _unsupported_file_type_response()

    address_detection_source = _normalize_detection_source(
        text_fields.get(ADDRESS_DETECTION_SOURCE_FIELD_NAME),
        ADDRESS_DETECTION_SOURCE_FIELD_NAME,
    )
    name_detection_source = _normalize_detection_source(
        text_fields.get(NAME_DETECTION_SOURCE_FIELD_NAME),
        NAME_DETECTION_SOURCE_FIELD_NAME,
    )
    mode = _normalize_job_mode(text_fields.get(JOB_MODE_FIELD_NAME))

    job_id = str(uuid.uuid4())
    source_key = "{}/{}".format(job_id, file_name)
    timestamp = _now_iso()
    bucket_name = os.environ["SOURCE_BUCKET_NAME"]

    try:
        _get_s3_client().put_object(Bucket=bucket_name, Key=source_key, Body=file_bytes)
    except ClientError:
        logger.exception("Failed to write source document to S3 for job %s", job_id)
        error_message = "Failed to store the uploaded document."
        try:
            _put_job_item(
                {
                    "jobId": job_id,
                    "ownerId": owner_id,
                    "status": "failed",
                    "sourceKey": source_key,
                    "sourceExtension": extension,
                    "fileName": file_name,
                    "createdAt": timestamp,
                    "updatedAt": timestamp,
                    "addressDetectionSource": address_detection_source,
                    "nameDetectionSource": name_detection_source,
                    "mode": mode,
                    "errorMessage": error_message,
                    # Machine-readable reason; the UI translates it
                    # (codes are listed in agent/tools/job_status_update.py).
                    "errorCode": "STORAGE_WRITE_FAILED",
                }
            )
        except ClientError:
            logger.exception(
                "Failed to persist failed RedactionJobs record for job %s", job_id
            )
        return _json_response(
            502,
            {
                "jobId": job_id,
                "status": "failed",
                "error": {"code": "STORAGE_WRITE_FAILED", "message": error_message},
            },
        )

    _put_job_item(
        {
            "jobId": job_id,
            "ownerId": owner_id,
            "status": "processing",
            "sourceKey": source_key,
            "sourceExtension": extension,
            "fileName": file_name,
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "addressDetectionSource": address_detection_source,
            "nameDetectionSource": name_detection_source,
            "mode": mode,
        }
    )

    return _json_response(
        200, {"jobId": job_id, "status": "processing", "mode": mode, "fileName": file_name}
    )
