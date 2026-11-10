"""Unit tests for `lambdas/rule_operation_submit_handler.py`
.

Covers the behaviors described in design.md's "RuleOperationSubmitHandler"
component and the "POST /rules" / "PATCH /rules/{ruleName}" /
"DELETE /rules/{ruleName}" API design sections,:

- A valid `create` request (`POST /rules`) returns 202 with an
  `operationId`, creates a `queued` `RuleOperations` record, and sends
  exactly one SQS message with the correct `MessageGroupId` /
  `MessageDeduplicationId`.
- Valid `enable`/`disable` requests (`PATCH /rules/{ruleName}`) and a valid
  `delete` request (`DELETE /rules/{ruleName}`) behave the same way.
- A name or pattern violating the length constraints (or empty) returns 400
  `INVALID_LENGTH` and creates NO `RuleOperations` record and sends NO SQS
  message.

DynamoDB and SQS are mocked with `moto` so no real AWS calls are made.
"""
import json
import os
import sys
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# Ensure `lambdas/` and `shared/` are importable the same way the Lambda
# runtime would, mirroring `test_upload_handler.py` / `test_rules_list_handler.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
sys.path.insert(0, str(BACKEND_ROOT))

RULE_OPERATIONS_TABLE_NAME = "RuleOperations-test"
QUEUE_NAME = "RuleOperationsQueue-test.fifo"


def _api_gateway_event(method: str, path: str, body: dict = None, path_parameters: dict = None) -> dict:
    event = {
        "version": "2.0",
        "routeKey": "{} {}".format(method, path),
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "admin-1", "cognito:groups": "[admins]"}}},
            "http": {"method": method, "path": path},
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if path_parameters is not None:
        event["pathParameters"] = path_parameters
    return event


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def mocked_aws_resources(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=RULE_OPERATIONS_TABLE_NAME,
            KeySchema=[{"AttributeName": "operationId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "operationId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sqs = boto3.client("sqs", region_name="us-east-1")
        queue_url = sqs.create_queue(
            QueueName=QUEUE_NAME,
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )["QueueUrl"]

        yield {
            "operations_table": dynamodb.Table(RULE_OPERATIONS_TABLE_NAME),
            "sqs": sqs,
            "queue_url": queue_url,
        }


@pytest.fixture
def handler_module(aws_credentials, mocked_aws_resources, monkeypatch):
    """Import `rule_operation_submit_handler` fresh per test with env vars
    set (including the queue URL created by `mocked_aws_resources`), and
    reset its lazily-initialized repository/client globals so each test
    gets resources bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("RULE_OPERATIONS_TABLE_NAME", RULE_OPERATIONS_TABLE_NAME)
    monkeypatch.setenv("RULE_OPERATIONS_QUEUE_URL", mocked_aws_resources["queue_url"])

    sys.modules.pop("rule_operation_submit_handler", None)
    import rule_operation_submit_handler as module

    module._rule_operations_repository = None
    module._sqs_client = None
    return module


def _receive_all_messages(sqs, queue_url):
    return sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, AttributeNames=["All"]
    ).get("Messages", [])


def test_valid_create_request_returns_202_and_enqueues_one_message(
    handler_module, mocked_aws_resources
):
    event = _api_gateway_event(
        "POST", "/rules", body={"ruleName": "custom-email", "pattern": r"^[^@]+@[^@]+$"}
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 202
    payload = json.loads(response["body"])
    operation_id = payload["operationId"]
    uuid.UUID(operation_id)  # raises if not a valid UUID

    stored = mocked_aws_resources["operations_table"].get_item(
        Key={"operationId": operation_id}
    )["Item"]
    assert stored["status"] == "queued"
    assert stored["type"] == "create"
    assert stored["targetRuleName"] == "custom-email"
    assert stored["pattern"] == r"^[^@]+@[^@]+$"

    messages = _receive_all_messages(
        mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
    )
    assert len(messages) == 1
    message = messages[0]
    assert message["Attributes"]["MessageGroupId"] == "rule-ops"
    assert message["Attributes"]["MessageDeduplicationId"] == operation_id
    body = json.loads(message["Body"])
    assert body["operationId"] == operation_id
    assert body["type"] == "create"
    assert body["targetRuleName"] == "custom-email"
    assert body["pattern"] == r"^[^@]+@[^@]+$"


@pytest.mark.parametrize("enabled", [True, False])
def test_valid_patch_request_returns_202_and_enqueues_one_message(
    handler_module, mocked_aws_resources, enabled
):
    event = _api_gateway_event(
        "PATCH",
        "/rules/{ruleName}",
        body={"enabled": enabled},
        path_parameters={"ruleName": "custom-email"},
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 202
    payload = json.loads(response["body"])
    operation_id = payload["operationId"]

    stored = mocked_aws_resources["operations_table"].get_item(
        Key={"operationId": operation_id}
    )["Item"]
    expected_type = "enable" if enabled else "disable"
    assert stored["status"] == "queued"
    assert stored["type"] == expected_type
    assert stored["targetRuleName"] == "custom-email"
    assert "pattern" not in stored

    messages = _receive_all_messages(
        mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
    )
    assert len(messages) == 1
    assert messages[0]["Attributes"]["MessageGroupId"] == "rule-ops"
    assert messages[0]["Attributes"]["MessageDeduplicationId"] == operation_id
    body = json.loads(messages[0]["Body"])
    assert body["type"] == expected_type
    assert body["targetRuleName"] == "custom-email"


def test_valid_delete_request_returns_202_and_enqueues_one_message(
    handler_module, mocked_aws_resources
):
    event = _api_gateway_event(
        "DELETE", "/rules/{ruleName}", path_parameters={"ruleName": "custom-email"}
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 202
    payload = json.loads(response["body"])
    operation_id = payload["operationId"]

    stored = mocked_aws_resources["operations_table"].get_item(
        Key={"operationId": operation_id}
    )["Item"]
    assert stored["status"] == "queued"
    assert stored["type"] == "delete"
    assert stored["targetRuleName"] == "custom-email"
    assert "pattern" not in stored

    messages = _receive_all_messages(
        mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
    )
    assert len(messages) == 1
    assert messages[0]["Attributes"]["MessageGroupId"] == "rule-ops"
    assert messages[0]["Attributes"]["MessageDeduplicationId"] == operation_id


@pytest.mark.parametrize(
    "rule_name,pattern",
    [
        ("", "^valid$"),  # name too short (0 chars)
        ("a" * 101, "^valid$"),  # name too long (101 chars)
        ("valid-name", ""),  # pattern too short (0 chars)
        ("valid-name", "a" * 501),  # pattern too long (501 chars)
    ],
)
def test_create_with_invalid_length_returns_400_without_side_effects(
    handler_module, mocked_aws_resources, rule_name, pattern
):
    event = _api_gateway_event(
        "POST", "/rules", body={"ruleName": rule_name, "pattern": pattern}
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "INVALID_LENGTH"

    scan_result = mocked_aws_resources["operations_table"].scan()
    assert scan_result["Items"] == []
    assert (
        _receive_all_messages(
            mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
        )
        == []
    )


@pytest.mark.parametrize("rule_name", ["", "a" * 101])
def test_patch_with_invalid_name_length_returns_400_without_side_effects(
    handler_module, mocked_aws_resources, rule_name
):
    event = _api_gateway_event(
        "PATCH",
        "/rules/{ruleName}",
        body={"enabled": True},
        path_parameters={"ruleName": rule_name},
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "INVALID_LENGTH"

    scan_result = mocked_aws_resources["operations_table"].scan()
    assert scan_result["Items"] == []
    assert (
        _receive_all_messages(
            mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
        )
        == []
    )


@pytest.mark.parametrize("rule_name", ["", "a" * 101])
def test_delete_with_invalid_name_length_returns_400_without_side_effects(
    handler_module, mocked_aws_resources, rule_name
):
    event = _api_gateway_event(
        "DELETE", "/rules/{ruleName}", path_parameters={"ruleName": rule_name}
    )

    response = handler_module.handler(event, None)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "INVALID_LENGTH"

    scan_result = mocked_aws_resources["operations_table"].scan()
    assert scan_result["Items"] == []
    assert (
        _receive_all_messages(
            mocked_aws_resources["sqs"], mocked_aws_resources["queue_url"]
        )
        == []
    )
