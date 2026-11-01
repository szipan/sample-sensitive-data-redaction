"""`store_redacted_document` Agent tool.

Contract of `store_redacted_document`:

    Input:  the Redacted Document bytes and the `jobId`
    Output: the S3 output object key
    Notes:  writes to the output bucket; the key format is
            `{jobId}/{fileName}`

This module implements the storage step ONLY: it writes the Redacted
Document bytes to the output documents S3 bucket at the key
`{job_id}/{file_name}` and returns that key. The returned key is the
`redactedKey` that `update_job_status` persists on
the `RedactionJobs` record and that `DownloadHandler`
(`lambdas/download_handler.py`) later reads via
`job_item["redactedKey"]` to fetch the object back out of the same output
bucket. Keeping the key format identical on both the write side (here) and
the read side (`DownloadHandler`) is what makes the download round-trip
work.

The output bucket name is read from the `OUTPUT_BUCKET_NAME` environment
variable - the exact same variable `DownloadHandler` reads for the same
bucket - so the writer and reader always target one bucket.

`update_job_status` is intentionally NOT implemented here; this
task is scoped to storage only.
"""
import os

import boto3

# Lazily-initialized so a single client is reused across invocations within
# the same Agent Runtime process (warm-start reuse), consistent with the
# lazy-client pattern used elsewhere in this backend (e.g.
# `lambdas/download_handler.py`, `agent/tools/ocr.py`).
_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _build_redacted_key(job_id: str, file_name: str) -> str:
    """Build the output object key in the `{jobId}/{fileName}` format
    defined by design.md's `store_redacted_document` tool description."""
    return "{}/{}".format(job_id, file_name)


def store_redacted_document(
    redacted_bytes: bytes, job_id: str, file_name: str
) -> str:
    """Write `redacted_bytes` to the output documents S3 bucket at key
    `{job_id}/{file_name}` and return that key.

    The returned value is the `redactedKey` consumed by `update_job_status`
    and `DownloadHandler`. The output bucket name
    comes from the `OUTPUT_BUCKET_NAME` environment variable.
    """
    bucket_name = os.environ["OUTPUT_BUCKET_NAME"]
    redacted_key = _build_redacted_key(job_id, file_name)

    _get_s3_client().put_object(
        Bucket=bucket_name, Key=redacted_key, Body=redacted_bytes
    )

    return redacted_key
