"""Unit tests for `lambdas/job_status_handler.py`.

Covers the behaviors described in design.md's "JobStatusHandler" component
and "GET /jobs/{jobId}" API design section,:

- An existing Job returns 200 with its current status.
- An existing `failed` Job's response includes the `errorMessage`.
- A missing Job returns 404 with `JOB_NOT_FOUND`.

DynamoDB is mocked with `moto` so no real AWS calls are made.
"""
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
TEST_USER_ID = "user-1"


def _api_gateway_event(job_id: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "GET /jobs/{jobId}",
        "headers": {},
        "pathParameters": {"jobId": job_id},
        "requestContext": {"authorizer": {"jwt": {"claims": {"sub": TEST_USER_ID}}}, "http": {"method": "GET", "path": "/jobs/" + job_id}},
    }


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def job_status_handler_module(aws_credentials, monkeypatch):
    """Import `job_status_handler` fresh per test with env vars set, and
    reset its lazily-initialized DynamoDB resource global so each test gets
    a client bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("JOBS_TABLE_NAME", JOBS_TABLE_NAME)

    sys.modules.pop("job_status_handler", None)
    import job_status_handler as module

    module._jobs_table = None
    return module


@pytest.fixture
def mocked_jobs_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=JOBS_TABLE_NAME,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "jobId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb.Table(JOBS_TABLE_NAME)


def test_existing_processing_job_returns_200(
    job_status_handler_module, mocked_jobs_table
):
    mocked_jobs_table.put_item(
        Item={
            "jobId": "job-1",
            "ownerId": TEST_USER_ID,
            "status": "processing",
            "sourceKey": "job-1/sample.docx",
            "sourceExtension": ".docx",
            "fileName": "sample.docx",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:00:00+00:00",
        }
    )

    response = job_status_handler_module.handler(_api_gateway_event("job-1"), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {
        "jobId": "job-1",
        "status": "processing",
        "mode": "auto",
        "errorMessage": None,
        "errorCode": None,
        "errorParams": None,
        "detectionSummary": None,
    }


def test_existing_completed_job_returns_200_with_status(
    job_status_handler_module, mocked_jobs_table
):
    mocked_jobs_table.put_item(
        Item={
            "jobId": "job-2",
            "ownerId": TEST_USER_ID,
            "status": "completed",
            "sourceKey": "job-2/sample.pdf",
            "sourceExtension": ".pdf",
            "redactedKey": "job-2/sample.pdf",
            "fileName": "sample.pdf",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:05:00+00:00",
        }
    )

    response = job_status_handler_module.handler(_api_gateway_event("job-2"), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {
        "jobId": "job-2",
        "status": "completed",
        "mode": "auto",
        "errorMessage": None,
        "errorCode": None,
        "errorParams": None,
        "detectionSummary": None,
    }


def test_existing_failed_job_includes_error_message(
    job_status_handler_module, mocked_jobs_table
):
    mocked_jobs_table.put_item(
        Item={
            "jobId": "job-3",
            "ownerId": TEST_USER_ID,
            "status": "failed",
            "sourceKey": "job-3/sample.docx",
            "sourceExtension": ".docx",
            "fileName": "sample.docx",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:05:00+00:00",
            "errorMessage": "Failed to store the uploaded document.",
            "errorCode": "OCR_PAGE_FAILED",
            "errorParams": {"page": 3},
        }
    )

    response = job_status_handler_module.handler(_api_gateway_event("job-3"), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {
        "jobId": "job-3",
        "status": "failed",
        "mode": "auto",
        "errorMessage": "Failed to store the uploaded document.",
        # DynamoDB hands numbers back as Decimal; they must reach the client
        # as plain JSON numbers.
        "errorCode": "OCR_PAGE_FAILED",
        "errorParams": {"page": 3},
        "detectionSummary": None,
    }


def test_completed_job_returns_detection_summary(
    job_status_handler_module, mocked_jobs_table
):
    """A Job record carrying a `detectionSummary` returns it under that key,
    with DynamoDB's `Decimal` numbers converted back to ints.
    """
    mocked_jobs_table.put_item(
        Item={
            "jobId": "job-4",
            "ownerId": TEST_USER_ID,
            "status": "completed",
            "sourceKey": "job-4/sample.pdf",
            "sourceExtension": ".pdf",
            "redactedKey": "job-4/sample.pdf",
            "fileName": "sample.pdf",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:05:00+00:00",
            "detectionSummary": {
                "textLayerRegions": 1,
                "ocrRegions": 3,
                "agreedRegions": 1,
                "pageHealth": [
                    {"pageIndex": 0, "textLayerChars": 19, "ocrChars": 23}
                ],
            },
        }
    )

    response = job_status_handler_module.handler(_api_gateway_event("job-4"), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["detectionSummary"] == {
        "textLayerRegions": 1,
        "ocrRegions": 3,
        "agreedRegions": 1,
        "pageHealth": [{"pageIndex": 0, "textLayerChars": 19, "ocrChars": 23}],
    }


def test_missing_job_returns_404(job_status_handler_module, mocked_jobs_table):
    response = job_status_handler_module.handler(
        _api_gateway_event("does-not-exist"), None
    )

    assert response["statusCode"] == 404
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "JOB_NOT_FOUND"
