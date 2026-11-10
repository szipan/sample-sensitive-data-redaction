"""Unit tests for `lambdas/download_handler.py`.

Covers the behaviors described in design.md's "JobStatusHandler /
DownloadHandler" component:

- A `completed` Job with an object present in the output bucket returns 200
  with the correct base64 body and `Content-Disposition` header.
- A `processing` Job returns 409 (`REDACTED_DOCUMENT_UNAVAILABLE`).
- A `failed` Job returns 409 (`REDACTED_DOCUMENT_UNAVAILABLE`).
- A missing job returns 404 (`JOB_NOT_FOUND`).

Also asserts the handler re-reads the Job's status fresh from DynamoDB on
every invocation rather than trusting any caller-supplied value (the
race-condition check point called out in design.md) -- this is the key test
that download succeeds iff the Job is `completed` at request
time.

S3 and DynamoDB are mocked with `moto` so no real AWS calls are made.
"""
import base64
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# Ensure `lambdas/` is importable the same way the Lambda runtime would,
# mirroring `test_upload_handler.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))

JOBS_TABLE_NAME = "RedactionJobs-test"
OUTPUT_BUCKET_NAME = "output-documents-test"
TEST_USER_ID = "user-1"


def _api_gateway_event(job_id: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "GET /jobs/{jobId}/download",
        "pathParameters": {"jobId": job_id},
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": TEST_USER_ID}}},
            "http": {"method": "GET", "path": "/jobs/{}/download".format(job_id)},
        },
    }


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def download_handler_module(aws_credentials, monkeypatch):
    """Import `download_handler` fresh per test with env vars set, and
    reset its lazily-initialized AWS client/resource globals so each test
    gets clients bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("JOBS_TABLE_NAME", JOBS_TABLE_NAME)
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", OUTPUT_BUCKET_NAME)

    sys.modules.pop("download_handler", None)
    import download_handler as module

    module._jobs_table = None
    module._s3_client = None
    return module


@pytest.fixture
def mocked_aws_resources(aws_credentials):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=OUTPUT_BUCKET_NAME)

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=JOBS_TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "jobId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"s3": s3, "jobs_table": dynamodb.Table(JOBS_TABLE_NAME)}


def _put_job(jobs_table, job_id: str, status: str, **extra):
    item = {
        "jobId": job_id,
        "status": status,
        "sourceKey": "{}/source.docx".format(job_id),
        "sourceExtension": ".docx",
        "fileName": "source.docx",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "updatedAt": "2024-01-01T00:00:00+00:00",
    }
    item.setdefault("ownerId", TEST_USER_ID)
    item.update(extra)
    jobs_table.put_item(Item=item)


def test_completed_job_returns_200_with_body_and_content_disposition(
    download_handler_module, mocked_aws_resources
):
    job_id = "job-completed"
    redacted_key = "{}/redacted.docx".format(job_id)
    file_bytes = b"fake redacted docx bytes"
    _put_job(
        mocked_aws_resources["jobs_table"],
        job_id,
        status="completed",
        fileName="redacted.docx",
        redactedKey=redacted_key,
    )
    mocked_aws_resources["s3"].put_object(
        Bucket=OUTPUT_BUCKET_NAME, Key=redacted_key, Body=file_bytes
    )

    response = download_handler_module.handler(_api_gateway_event(job_id), None)

    assert response["statusCode"] == 200
    assert response["isBase64Encoded"] is True
    assert base64.b64decode(response["body"]) == file_bytes
    assert response["headers"]["Content-Disposition"] == (
        'attachment; filename="redacted.docx"'
    )
    assert response["headers"]["Content-Type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_processing_job_returns_409(download_handler_module, mocked_aws_resources):
    job_id = "job-processing"
    _put_job(mocked_aws_resources["jobs_table"], job_id, status="processing")

    response = download_handler_module.handler(_api_gateway_event(job_id), None)

    assert response["statusCode"] == 409
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "REDACTED_DOCUMENT_UNAVAILABLE"


def test_failed_job_returns_409(download_handler_module, mocked_aws_resources):
    job_id = "job-failed"
    _put_job(
        mocked_aws_resources["jobs_table"],
        job_id,
        status="failed",
        errorMessage="something went wrong",
    )

    response = download_handler_module.handler(_api_gateway_event(job_id), None)

    assert response["statusCode"] == 409
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "REDACTED_DOCUMENT_UNAVAILABLE"


def test_missing_job_returns_404(download_handler_module, mocked_aws_resources):
    response = download_handler_module.handler(
        _api_gateway_event("no-such-job"), None
    )

    assert response["statusCode"] == 404
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "JOB_NOT_FOUND"


def test_handler_re_reads_status_fresh_and_ignores_stale_or_spoofed_status(
    download_handler_module, mocked_aws_resources
):
    """Download succeeds iff the Job is `completed` at request
    time -- the handler must always re-read the current status from
    DynamoDB rather than trusting any caller-supplied or previously-seen
    value, so a status change between two reads (or an attempt to smuggle a
    status via the event) is reflected in the very next call."""
    job_id = "job-race"
    jobs_table = mocked_aws_resources["jobs_table"]
    _put_job(jobs_table, job_id, status="processing")

    # An event that attempts to smuggle a "completed" status via extra
    # fields must be ignored entirely: only pathParameters.jobId is ever
    # consulted, and the handler must still hit DynamoDB for the real
    # status rather than trusting this.
    spoofed_event = _api_gateway_event(job_id)
    spoofed_event["queryStringParameters"] = {"status": "completed"}
    spoofed_event["headers"] = {"x-job-status": "completed"}

    first_response = download_handler_module.handler(spoofed_event, None)
    assert first_response["statusCode"] == 409

    # Now the Job genuinely transitions to completed in the store; a fresh
    # call (fresh read) must pick this up without any code change or
    # caller-supplied hint.
    redacted_key = "{}/redacted.docx".format(job_id)
    jobs_table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET #s = :status, redactedKey = :redactedKey, fileName = :fileName",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "completed",
            ":redactedKey": redacted_key,
            ":fileName": "redacted.docx",
        },
    )
    mocked_aws_resources["s3"].put_object(
        Bucket=OUTPUT_BUCKET_NAME, Key=redacted_key, Body=b"final bytes"
    )

    second_response = download_handler_module.handler(_api_gateway_event(job_id), None)
    assert second_response["statusCode"] == 200
    assert base64.b64decode(second_response["body"]) == b"final bytes"
