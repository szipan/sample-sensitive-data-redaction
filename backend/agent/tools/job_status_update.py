"""`update_job_status` Agent tool.

Transitions a `RedactionJobs` record out of its non-terminal `processing`
state into one of the two terminal states the AgentCore Redaction Agent can
produce:

- `completed`: the Agent finished the Redaction Job and
  stored the Redacted Document. The item's `status` becomes `completed` and
  its `redactedKey` is set to the output object key returned by
  `store_redacted_document`.
- `failed`: the Agent could not produce a structure- and
  format-preserving Redacted Document. The item's `status` becomes `failed`
  and its `errorMessage` records why. `errorCode` (and optional
  `errorParams`) carry the same reason in a machine-readable form so the UI
  can show it in the user's language; `errorMessage` stays an English
  diagnostic and is not meant to be shown to the user.

In both cases `updatedAt` is refreshed. The update is performed with a
DynamoDB `update_item` `UpdateExpression` so the record's other attributes
(`sourceKey`, `sourceExtension`, `fileName`, `createdAt`, ...) written by
`UploadHandler` are preserved rather than overwritten.

Reads the `RedactionJobs` table name from the `JOBS_TABLE_NAME` environment
variable and reuses a lazily-initialized boto3 resource across invocations
(warm-start reuse), matching `lambdas/upload_handler.py` and
`lambdas/job_status_handler.py`.
"""
import os
from datetime import datetime, timezone
from typing import Optional

import boto3

STATUS_COMPLETED = "completed"
STATUS_DETECTED = "detected"
STATUS_FAILED = "failed"

# `errorCode` values written with a `failed` transition. The frontend
# translates `job.error.<CODE>` (frontend/src/i18n/index.tsx), so a new code
# needs a dictionary entry there. `errorParams` holds the values the message
# interpolates, e.g. `{"page": 3}` for OCR_PAGE_FAILED (1-based page number).
ERROR_REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
ERROR_STRUCTURE_VERIFICATION_FAILED = "STRUCTURE_VERIFICATION_FAILED"
ERROR_OCR_PAGE_FAILED = "OCR_PAGE_FAILED"
ERROR_OCR_FAILED = "OCR_FAILED"
ERROR_PROCESSING_FAILED = "PROCESSING_FAILED"

# Lazily-initialized DynamoDB resource so it is created once per execution
# environment (warm-start reuse) rather than per invocation.
_jobs_table = None


def _get_jobs_table():
    global _jobs_table
    if _jobs_table is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _jobs_table = boto3.resource("dynamodb").Table(table_name)
    return _jobs_table


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_job_status(
    job_id: str,
    status: str,
    redacted_key: Optional[str] = None,
    error_message: Optional[str] = None,
    detection_summary: Optional[dict] = None,
    error_code: Optional[str] = None,
    error_params: Optional[dict] = None,
) -> None:
    """Move the `RedactionJobs` record `job_id` into a new state.

    For ``status == "detected"``: sets ``status`` to
    ``"detected"`` and refreshes ``updatedAt``. No other attributes are
    modified. This is a non-terminal status indicating detection is complete
    and the job is awaiting user confirmation.

    For ``status == "completed"``: sets ``status`` to
    ``"completed"`` and ``redactedKey`` to `redacted_key`, and refreshes
    ``updatedAt``. `redacted_key` is required in this case. When
    `detection_summary` is supplied it is additionally written to
    ``detectionSummary`` — the per-path region counts and
    per-page health signal built by ``orchestration._build_detection_summary``.
    It stays absent for `.docx` Jobs and for any Job whose summary could not
    be built, matching ``RedactionJob.detectionSummary``'s ``None`` case.

    For ``status == "failed"``: sets ``status`` to
    ``"failed"`` and ``errorMessage`` to `error_message`, and refreshes
    ``updatedAt``. When `error_code` is given it is stored as ``errorCode``
    (with `error_params`, if any, as ``errorParams``). A ``failed`` transition
    never carries a Detection_Summary.

    Uses a DynamoDB ``update_item`` ``UpdateExpression`` so attributes not
    named here (``sourceKey``, ``sourceExtension``, ``fileName``,
    ``createdAt``, ...) are left intact. This is what makes recording a
    Detection_Summary leave the identity attributes unchanged hold
    *by construction*: `update_item` only touches the attributes its
    expression names. Do not replace this with `put_item` or otherwise
    rewrite the whole item — that would silently break that guarantee.

    Raises ``ValueError`` for an unsupported `status`, or for a ``completed``
    transition missing `redacted_key`.
    """
    if status == STATUS_DETECTED:
        update_expression = "SET #status = :status, updatedAt = :updatedAt"
        expression_attribute_values = {
            ":status": STATUS_DETECTED,
            ":updatedAt": _now_iso(),
        }
    elif status == STATUS_COMPLETED:
        if redacted_key is None:
            raise ValueError(
                "redacted_key is required when status is 'completed'"
            )
        update_expression = "SET #status = :status, redactedKey = :redactedKey, updatedAt = :updatedAt"
        expression_attribute_values = {
            ":status": STATUS_COMPLETED,
            ":redactedKey": redacted_key,
            ":updatedAt": _now_iso(),
        }
        if detection_summary is not None:
            # The summary holds only ints, strs, lists and maps, which the
            # `boto3.resource` (Table) serializer used by `_get_jobs_table`
            # accepts directly — no explicit type descriptors needed.
            update_expression += ", detectionSummary = :detectionSummary"
            expression_attribute_values[":detectionSummary"] = detection_summary
    elif status == STATUS_FAILED:
        update_expression = "SET #status = :status, errorMessage = :errorMessage, updatedAt = :updatedAt"
        expression_attribute_values = {
            ":status": STATUS_FAILED,
            ":errorMessage": error_message,
            ":updatedAt": _now_iso(),
        }
        if error_code is not None:
            update_expression += ", errorCode = :errorCode"
            expression_attribute_values[":errorCode"] = error_code
            if error_params:
                update_expression += ", errorParams = :errorParams"
                expression_attribute_values[":errorParams"] = error_params
    else:
        raise ValueError(
            "Unsupported status '{}': expected 'detected', 'completed', or 'failed'".format(status)
        )

    _get_jobs_table().update_item(
        Key={"jobId": job_id},
        UpdateExpression=update_expression,
        # `status` is a DynamoDB reserved word, so alias it via a name
        # placeholder.
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=expression_attribute_values,
    )
