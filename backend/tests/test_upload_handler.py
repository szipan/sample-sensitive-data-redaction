"""Unit tests for `lambdas/upload_handler.py`.

Covers the three main behaviors described in design.md's "UploadHandler"
component:

- A valid `.docx` upload succeeds and creates a `processing` Redaction Job.
- An unsupported file extension is rejected (400) and no Job is created.
- An S3 write failure results in a `failed` Job and a 502 failure response.

S3 and DynamoDB are mocked with `moto` so no real AWS calls are made.
"""
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

# Ensure `lambdas/` and `shared/` are importable the same way the Lambda
# runtime would (each top-level backend package added to `sys.path`),
# mirroring the layout used by `infra/redaction_stack.py`
# (`lambda_.Code.from_asset("lambdas")`).
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))

JOBS_TABLE_NAME = "RedactionJobs-test"
SOURCE_BUCKET_NAME = "source-documents-test"


def _build_multipart_body(file_name: str, file_bytes: bytes, field_name: str = "file"):
    boundary = "test-boundary-123"
    parts = []
    parts.append("--" + boundary)
    parts.append(
        'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
            field_name, file_name
        )
    )
    parts.append("Content-Type: application/octet-stream")
    parts.append("")
    header = ("\r\n".join(parts) + "\r\n").encode("utf-8")
    footer = ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    body = header + file_bytes + footer
    content_type = "multipart/form-data; boundary=" + boundary
    return content_type, body


def _build_multipart_body_with_fields(
    file_name: str, file_bytes: bytes, text_fields: dict
):
    """Multipart body carrying the `file` part plus one or more simple text
    form fields (e.g. `addressDetectionSource`)."""
    boundary = "test-boundary-123"
    chunks = []
    for name, value in text_fields.items():
        chunks.append("--" + boundary)
        chunks.append('Content-Disposition: form-data; name="{}"'.format(name))
        chunks.append("")
        chunks.append(value)
    file_header = "\r\n".join(
        [
            "--" + boundary,
            'Content-Disposition: form-data; name="file"; filename="{}"'.format(
                file_name
            ),
            "Content-Type: application/octet-stream",
            "",
            "",
        ]
    )
    prefix = ("\r\n".join(chunks) + "\r\n").encode("utf-8") if chunks else b""
    body = prefix + file_header.encode("utf-8") + file_bytes + (
        "\r\n--" + boundary + "--\r\n"
    ).encode("utf-8")
    content_type = "multipart/form-data; boundary=" + boundary
    return content_type, body


def _api_gateway_event(content_type: str, body: bytes) -> dict:
    return {
        "version": "2.0",
        "routeKey": "POST /jobs",
        "headers": {"content-type": content_type},
        "isBase64Encoded": True,
        "body": base64.b64encode(body).decode("ascii"),
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "user-1"}}},
            "http": {"method": "POST", "path": "/jobs"},
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
def upload_handler_module(aws_credentials, monkeypatch):
    """Import `upload_handler` fresh per test with env vars set, and reset
    its lazily-initialized AWS client/resource globals so each test gets
    clients bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("SOURCE_BUCKET_NAME", SOURCE_BUCKET_NAME)
    monkeypatch.setenv("JOBS_TABLE_NAME", JOBS_TABLE_NAME)

    sys.modules.pop("upload_handler", None)
    import upload_handler as module

    module._s3_client = None
    module._jobs_table = None
    return module


@pytest.fixture
def mocked_aws_resources(aws_credentials):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=SOURCE_BUCKET_NAME)

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


def test_valid_docx_upload_creates_processing_job(
    upload_handler_module, mocked_aws_resources
):
    file_bytes = b"fake docx content"
    content_type, body = _build_multipart_body("sample.docx", file_bytes)
    event = _api_gateway_event(content_type, body)

    response = upload_handler_module.handler(event, None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["status"] == "processing"
    assert payload["fileName"] == "sample.docx"
    job_id = payload["jobId"]
    uuid.UUID(job_id)  # raises if not a valid UUID

    stored_job = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})[
        "Item"
    ]
    assert stored_job["status"] == "processing"
    assert stored_job["sourceExtension"] == ".docx"
    assert stored_job["fileName"] == "sample.docx"
    assert stored_job["sourceKey"] == "{}/sample.docx".format(job_id)

    stored_object = mocked_aws_resources["s3"].get_object(
        Bucket=SOURCE_BUCKET_NAME, Key=stored_job["sourceKey"]
    )
    assert stored_object["Body"].read() == file_bytes


def test_unsupported_extension_is_rejected_without_creating_job(
    upload_handler_module, mocked_aws_resources
):
    content_type, body = _build_multipart_body("sample.txt", b"not a supported doc")
    event = _api_gateway_event(content_type, body)

    response = upload_handler_module.handler(event, None)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert ".docx" in payload["error"]["message"]
    assert ".pdf" in payload["error"]["message"]

    scan_result = mocked_aws_resources["jobs_table"].scan()
    assert scan_result["Items"] == []


def test_s3_write_failure_marks_job_failed(
    upload_handler_module, mocked_aws_resources
):
    content_type, body = _build_multipart_body("sample.pdf", b"%PDF- fake content")
    event = _api_gateway_event(content_type, body)

    from botocore.exceptions import ClientError

    s3_client = upload_handler_module._get_s3_client()
    with patch.object(
        s3_client,
        "put_object",
        side_effect=ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}},
            "PutObject",
        ),
    ):
        response = upload_handler_module.handler(event, None)

    assert response["statusCode"] == 502
    payload = json.loads(response["body"])
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "STORAGE_WRITE_FAILED"
    job_id = payload["jobId"]

    stored_job = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})[
        "Item"
    ]
    assert stored_job["status"] == "failed"
    assert stored_job["errorMessage"]


# ---------------------------------------------------------------------------
# addressDetectionSource form field (detection-source-switch feature)
# ---------------------------------------------------------------------------


def test_default_detection_sources_are_llm_when_field_absent(
    upload_handler_module, mocked_aws_resources
):
    """A client that does not send `addressDetectionSource`/`nameDetectionSource`
    gets the default `"llm"` persisted for both."""
    content_type, body = _build_multipart_body("sample.docx", b"fake docx")
    response = upload_handler_module.handler(
        _api_gateway_event(content_type, body), None
    )
    job_id = json.loads(response["body"])["jobId"]
    stored = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})["Item"]
    assert stored["addressDetectionSource"] == "llm"
    assert stored["nameDetectionSource"] == "llm"


@pytest.mark.parametrize("choice", ["both", "llm", "guardrails"])
def test_valid_name_detection_source_is_stored(
    upload_handler_module, mocked_aws_resources, choice
):
    content_type, body = _build_multipart_body_with_fields(
        "sample.pdf", b"%PDF- fake", {"nameDetectionSource": choice}
    )
    response = upload_handler_module.handler(
        _api_gateway_event(content_type, body), None
    )
    assert response["statusCode"] == 200
    job_id = json.loads(response["body"])["jobId"]
    stored = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})["Item"]
    assert stored["nameDetectionSource"] == choice
    # The unset address switch gets the default.
    assert stored["addressDetectionSource"] == "llm"


def test_both_switches_can_be_set_independently(
    upload_handler_module, mocked_aws_resources
):
    content_type, body = _build_multipart_body_with_fields(
        "sample.pdf",
        b"%PDF- fake",
        {"addressDetectionSource": "guardrails", "nameDetectionSource": "llm"},
    )
    response = upload_handler_module.handler(
        _api_gateway_event(content_type, body), None
    )
    assert response["statusCode"] == 200
    job_id = json.loads(response["body"])["jobId"]
    stored = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})["Item"]
    assert stored["addressDetectionSource"] == "guardrails"
    assert stored["nameDetectionSource"] == "llm"


@pytest.mark.parametrize("choice", ["both", "llm", "guardrails"])
def test_valid_address_detection_source_is_stored(
    upload_handler_module, mocked_aws_resources, choice
):
    content_type, body = _build_multipart_body_with_fields(
        "sample.pdf", b"%PDF- fake", {"addressDetectionSource": choice}
    )
    response = upload_handler_module.handler(
        _api_gateway_event(content_type, body), None
    )
    assert response["statusCode"] == 200
    job_id = json.loads(response["body"])["jobId"]
    stored = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})["Item"]
    assert stored["addressDetectionSource"] == choice


def test_unrecognized_address_detection_source_falls_back_to_default(
    upload_handler_module, mocked_aws_resources
):
    """An unknown value does not reject the upload; it falls back to the
    default (`"llm"`) rather than failing the request."""
    content_type, body = _build_multipart_body_with_fields(
        "sample.pdf", b"%PDF- fake", {"addressDetectionSource": "banana"}
    )
    response = upload_handler_module.handler(
        _api_gateway_event(content_type, body), None
    )
    assert response["statusCode"] == 200
    job_id = json.loads(response["body"])["jobId"]
    stored = mocked_aws_resources["jobs_table"].get_item(Key={"jobId": job_id})["Item"]
    assert stored["addressDetectionSource"] == "llm"
