"""Unit tests for `agent/tools/job_status_update.py` (`update_job_status`).

Covers design.md's "update_job_status" tool.5
(`completed` + `redactedKey`) and 3.6 (`failed` + `errorMessage`):

- Seeding a `processing` job then transitioning it to `completed` sets
  `status`/`redactedKey`/`updatedAt` while leaving the other attributes
  written by `UploadHandler` (`sourceKey`, `fileName`, `createdAt`, ...)
  intact.
- Transitioning a `processing` job to `failed` sets
  `status`/`errorMessage`/`updatedAt`.
- An unsupported status, or a `completed` transition missing `redacted_key`,
  raises `ValueError`.

DynamoDB is mocked with `moto` so no real AWS calls are made.
"""
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

JOBS_TABLE_NAME = "RedactionJobs-test"


@pytest.fixture
def aws_credentials():
    import os

    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def job_status_update_module(aws_credentials, monkeypatch):
    """Import `agent.tools.job_status_update` fresh per test with env vars
    set, and reset its lazily-initialized DynamoDB resource global so each
    test gets a resource bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("JOBS_TABLE_NAME", JOBS_TABLE_NAME)

    sys.modules.pop("agent.tools.job_status_update", None)
    from agent.tools import job_status_update as module

    module._jobs_table = None
    return module


@pytest.fixture
def mocked_jobs_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=JOBS_TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb.Table(JOBS_TABLE_NAME)


def _seed_processing_job(table, job_id: str) -> dict:
    item = {
        "jobId": job_id,
        "status": "processing",
        "sourceKey": "{}/sample.docx".format(job_id),
        "sourceExtension": ".docx",
        "fileName": "sample.docx",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    }
    table.put_item(Item=item)
    return item


def test_completed_sets_status_and_redacted_key_and_preserves_other_fields(
    job_status_update_module, mocked_jobs_table
):
    seeded = _seed_processing_job(mocked_jobs_table, "job-1")

    job_status_update_module.update_job_status(
        "job-1", "completed", redacted_key="job-1/sample.docx"
    )

    item = mocked_jobs_table.get_item(Key={"jobId": "job-1"})["Item"]
    assert item["status"] == "completed"
    assert item["redactedKey"] == "job-1/sample.docx"
    # Other attributes written by UploadHandler are left intact.
    assert item["sourceKey"] == seeded["sourceKey"]
    assert item["sourceExtension"] == seeded["sourceExtension"]
    assert item["fileName"] == seeded["fileName"]
    assert item["createdAt"] == seeded["createdAt"]
    # updatedAt is refreshed.
    assert item["updatedAt"] != seeded["updatedAt"]
    # A completed job carries no errorMessage.
    assert "errorMessage" not in item


def test_failed_sets_status_and_error_message_and_preserves_other_fields(
    job_status_update_module, mocked_jobs_table
):
    seeded = _seed_processing_job(mocked_jobs_table, "job-2")

    job_status_update_module.update_job_status(
        "job-2", "failed", error_message="Structure verification failed."
    )

    item = mocked_jobs_table.get_item(Key={"jobId": "job-2"})["Item"]
    assert item["status"] == "failed"
    assert item["errorMessage"] == "Structure verification failed."
    assert item["sourceKey"] == seeded["sourceKey"]
    assert item["fileName"] == seeded["fileName"]
    assert item["createdAt"] == seeded["createdAt"]
    assert item["updatedAt"] != seeded["updatedAt"]
    # A failed job (via this path) sets no redactedKey.
    assert "redactedKey" not in item


def test_completed_without_redacted_key_raises(
    job_status_update_module, mocked_jobs_table
):
    _seed_processing_job(mocked_jobs_table, "job-3")

    with pytest.raises(ValueError):
        job_status_update_module.update_job_status("job-3", "completed")


def test_unsupported_status_raises(job_status_update_module, mocked_jobs_table):
    _seed_processing_job(mocked_jobs_table, "job-4")

    with pytest.raises(ValueError):
        job_status_update_module.update_job_status("job-4", "processing")
