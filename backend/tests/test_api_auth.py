"""Authentication, job ownership and admin-only rule changes on the API."""
import base64
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))

import request_auth  # noqa: E402

JOBS_TABLE = "RedactionJobs-auth-test"
SOURCE_BUCKET = "source-auth-test"
OUTPUT_BUCKET = "output-auth-test"
OWNER = "owner-sub"
OTHER = "other-sub"


def _event(sub=None, groups=None, method="GET", path="/", job_id=None, body=None, headers=None):
    ctx = {"http": {"method": method, "path": path}}
    if sub is not None:
        claims = {"sub": sub}
        if groups is not None:
            claims["cognito:groups"] = groups
        ctx["authorizer"] = {"jwt": {"claims": claims}}
    event = {"version": "2.0", "requestContext": ctx, "headers": headers or {}}
    if job_id is not None:
        event["pathParameters"] = {"jobId": job_id}
    if body is not None:
        event["body"] = body
    return event


# --------------------------------------------------------------------------
# request_auth
# --------------------------------------------------------------------------
def test_caller_id_and_groups_parsing():
    assert request_auth.caller_id(_event()) is None
    assert request_auth.caller_id(_event(sub="")) is None
    assert request_auth.caller_id(_event(sub="abc")) == "abc"
    assert request_auth.caller_groups(_event(sub="a", groups="[admins users]")) == {"admins", "users"}
    assert request_auth.caller_groups(_event(sub="a", groups=["admins"])) == {"admins"}
    assert request_auth.caller_groups(_event(sub="a", groups="[]")) == set()
    assert request_auth.is_admin(_event(sub="a", groups="[admins]"))
    assert not request_auth.is_admin(_event(sub="a", groups="[administrators]"))
    assert not request_auth.is_admin(_event(sub="a"))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def aws(monkeypatch):
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("JOBS_TABLE_NAME", JOBS_TABLE)
    monkeypatch.setenv("SOURCE_BUCKET_NAME", SOURCE_BUCKET)
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", OUTPUT_BUCKET)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=SOURCE_BUCKET)
        s3.create_bucket(Bucket=OUTPUT_BUCKET)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=JOBS_TABLE,
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {"s3": s3, "table": ddb.Table(JOBS_TABLE)}


def _module(name):
    sys.modules.pop(name, None)
    module = __import__(name)
    for attr in ("_jobs_table", "_s3_client"):
        if hasattr(module, attr):
            setattr(module, attr, None)
    return module


def _seed(table, s3, job_id, status, owner=OWNER):
    item = {
        "jobId": job_id, "status": status, "sourceKey": job_id + "/a.docx",
        "sourceExtension": ".docx", "fileName": "a.docx",
        "createdAt": "t", "updatedAt": "t",
    }
    if owner is not None:
        item["ownerId"] = owner
    if status == "completed":
        item["redactedKey"] = job_id + "/a.docx"
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=item["redactedKey"], Body=b"doc")
    if status == "detected":
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=job_id + "/detections.json", Body=b"[]")
    table.put_item(Item=item)


def _code(response):
    return json.loads(response["body"])["error"]["code"]


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
def _multipart(name="a.docx"):
    boundary = "XBOUNDARY"
    body = (
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{n}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\nhello\r\n--{b}--\r\n"
    ).format(b=boundary, n=name).encode()
    return "multipart/form-data; boundary=" + boundary, base64.b64encode(body).decode()


def test_upload_requires_identity(aws):
    upload = _module("upload_handler")
    ctype, body = _multipart()
    event = _event(method="POST", path="/jobs", body=body, headers={"content-type": ctype})
    event["isBase64Encoded"] = True
    response = upload.handler(event, None)
    assert response["statusCode"] == 401
    assert aws["table"].scan()["Items"] == []
    assert "Contents" not in aws["s3"].list_objects_v2(Bucket=SOURCE_BUCKET)


def test_upload_records_owner(aws):
    upload = _module("upload_handler")
    ctype, body = _multipart()
    event = _event(sub=OWNER, method="POST", path="/jobs", body=body, headers={"content-type": ctype})
    event["isBase64Encoded"] = True
    response = upload.handler(event, None)
    assert response["statusCode"] == 200
    job_id = json.loads(response["body"])["jobId"]
    assert aws["table"].get_item(Key={"jobId": job_id})["Item"]["ownerId"] == OWNER


# --------------------------------------------------------------------------
# Job reads: status, download, detections
# --------------------------------------------------------------------------
@pytest.mark.parametrize("handler_name,status", [
    ("job_status_handler", "processing"),
    ("download_handler", "completed"),
    ("detections_handler", "detected"),
])
def test_job_reads_are_scoped_to_owner(aws, handler_name, status):
    _seed(aws["table"], aws["s3"], "job-a", status)
    _seed(aws["table"], aws["s3"], "job-legacy", status, owner=None)
    handler = _module(handler_name)

    assert handler.handler(_event(sub=OWNER, job_id="job-a"), None)["statusCode"] == 200
    other = handler.handler(_event(sub=OTHER, job_id="job-a"), None)
    missing = handler.handler(_event(sub=OTHER, job_id="no-such-job"), None)
    # Someone else's job looks exactly like a missing one.
    assert other["statusCode"] == missing["statusCode"] == 404
    assert other["body"] == missing["body"]
    # Records written before ownership existed belong to nobody.
    assert handler.handler(_event(sub=OWNER, job_id="job-legacy"), None)["statusCode"] == 404
    assert handler.handler(_event(job_id="job-a"), None)["statusCode"] == 401


# --------------------------------------------------------------------------
# Confirming selections
# --------------------------------------------------------------------------
def test_redact_confirmation_is_scoped_to_owner(aws):
    _seed(aws["table"], aws["s3"], "job-r", "detected")
    redact = _module("redact_handler")
    body = json.dumps({"selectedIds": [1, 2]})

    other = redact.handler(_event(sub=OTHER, method="POST", job_id="job-r", body=body), None)
    assert other["statusCode"] == 404
    assert "confirmedSelections" not in aws["table"].get_item(Key={"jobId": "job-r"})["Item"]
    assert redact.handler(_event(method="POST", job_id="job-r", body=body), None)["statusCode"] == 401

    ok = redact.handler(_event(sub=OWNER, method="POST", job_id="job-r", body=body), None)
    assert ok["statusCode"] == 202
    # Once confirmed, another user still only sees "not found", never
    # "already confirmed".
    again = redact.handler(_event(sub=OTHER, method="POST", job_id="job-r", body=body), None)
    assert again["statusCode"] == 404


# --------------------------------------------------------------------------
# Rule changes
# --------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_rule_changes_need_admin_group(aws, monkeypatch, method):
    submit = _module("rule_operation_submit_handler")
    sent = []
    monkeypatch.setattr(submit, "_submit_operation", lambda *a, **k: sent.append(a) or {"statusCode": 202})
    body = json.dumps({"ruleName": "r", "pattern": "x"}) if method == "POST" else json.dumps({"enabled": False})
    path_params = None if method == "POST" else {"ruleName": "r"}

    def ev(**kw):
        e = _event(method=method, path="/rules", body=body, **kw)
        if path_params:
            e["pathParameters"] = path_params
        return e

    assert submit.handler(ev(), None)["statusCode"] == 401
    denied = submit.handler(ev(sub=OTHER, groups="[users]"), None)
    assert denied["statusCode"] == 403 and _code(denied) == "FORBIDDEN"
    assert submit.handler(ev(sub=OTHER), None)["statusCode"] == 403
    assert sent == []
