"""Unit tests for `agent/tools/storage.py` (`store_redacted_document`).

Covers design.md's `store_redacted_document` tool description (key format
`{jobId}/{fileName}`):

- `store_redacted_document` writes the bytes to the expected
  `{jobId}/{fileName}` key in the output bucket and returns that key.
- The stored object's bytes round-trip identically (a subsequent
  `get_object` returns exactly what was put) - this is the invariant
  `DownloadHandler` relies on when it reads the object back by
  the returned `redactedKey`.

S3 is mocked with `moto` so no real AWS calls are made.
"""
import importlib
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

OUTPUT_BUCKET_NAME = "output-documents-test"


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def storage_module(aws_credentials, monkeypatch):
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", OUTPUT_BUCKET_NAME)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=OUTPUT_BUCKET_NAME)

        import agent.tools.storage as storage

        # Reload so the lazily-initialized module-level client is created
        # fresh inside this test's mocked AWS context.
        storage = importlib.reload(storage)
        yield storage, s3


def test_store_redacted_document_returns_job_id_slash_file_name_key(storage_module):
    storage, _s3 = storage_module

    redacted_key = storage.store_redacted_document(
        b"redacted-bytes", "job-123", "report.docx"
    )

    assert redacted_key == "job-123/report.docx"


def test_store_redacted_document_writes_bytes_to_expected_key(storage_module):
    storage, s3 = storage_module

    redacted_key = storage.store_redacted_document(
        b"redacted-bytes", "job-123", "report.docx"
    )

    stored = s3.get_object(Bucket=OUTPUT_BUCKET_NAME, Key=redacted_key)
    assert stored["Body"].read() == b"redacted-bytes"


def test_store_redacted_document_round_trips_bytes_identically(storage_module):
    storage, s3 = storage_module

    # Non-trivial binary payload (a real .docx is a zip, i.e. binary) to
    # confirm the write/read cycle is byte-exact, not text-mangled.
    original_bytes = bytes(range(256)) * 8

    redacted_key = storage.store_redacted_document(
        original_bytes, "job-abc", "scan.pdf"
    )

    stored = s3.get_object(Bucket=OUTPUT_BUCKET_NAME, Key=redacted_key)
    assert stored["Body"].read() == original_bytes


def test_store_redacted_document_uses_output_bucket_name_env_var(
    aws_credentials, monkeypatch
):
    other_bucket = "some-other-output-bucket"
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", other_bucket)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=other_bucket)

        import agent.tools.storage as storage

        storage = importlib.reload(storage)

        redacted_key = storage.store_redacted_document(
            b"x", "job-9", "a.docx"
        )

        # Object landed in the bucket named by OUTPUT_BUCKET_NAME.
        stored = s3.get_object(Bucket=other_bucket, Key=redacted_key)
        assert stored["Body"].read() == b"x"
