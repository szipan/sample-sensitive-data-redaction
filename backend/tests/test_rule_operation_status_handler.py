"""Unit tests for `lambdas/rule_operation_status_handler.py`
.

Covers the behaviors described in design.md's "RuleOperationStatusHandler"
component and "GET /rule-operations/{operationId}" API design section, and

- An existing Rule Operation returns 200 with its current status, for each
  possible status (`queued` / `processing` / `applied` / `rejected`).
- An existing `rejected` operation's response includes the `errorReason`.
- A missing operation returns 404 with `OPERATION_NOT_FOUND`.

DynamoDB is mocked with `moto` so no real AWS calls are made.
"""
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# Ensure `lambdas/` and `shared/` are importable the same way the Lambda
# runtime would, mirroring `test_job_status_handler.py` /
# `test_rule_operation_submit_handler.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
sys.path.insert(0, str(BACKEND_ROOT))

RULE_OPERATIONS_TABLE_NAME = "RuleOperations-test"


def _api_gateway_event(operation_id: str) -> dict:
    return {
        "version": "2.0",
        "routeKey": "GET /rule-operations/{operationId}",
        "headers": {},
        "pathParameters": {"operationId": operation_id},
        "requestContext": {
            "http": {"method": "GET", "path": "/rule-operations/" + operation_id}
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
def rule_operation_status_handler_module(aws_credentials, monkeypatch):
    """Import `rule_operation_status_handler` fresh per test with env vars
    set, and reset its lazily-initialized repository global so each test
    gets a repository bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("RULE_OPERATIONS_TABLE_NAME", RULE_OPERATIONS_TABLE_NAME)

    sys.modules.pop("rule_operation_status_handler", None)
    import rule_operation_status_handler as module

    module._rule_operations_repository = None
    return module


@pytest.fixture
def mocked_rule_operations_table(aws_credentials):
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
        yield dynamodb.Table(RULE_OPERATIONS_TABLE_NAME)


@pytest.mark.parametrize("status", ["queued", "processing", "applied"])
def test_existing_operation_returns_200_with_null_error_reason(
    rule_operation_status_handler_module, mocked_rule_operations_table, status
):
    mocked_rule_operations_table.put_item(
        Item={
            "operationId": "op-1",
            "type": "create",
            "targetRuleName": "custom-email",
            "pattern": r"^[^@]+@[^@]+$",
            "status": status,
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:00:00+00:00",
        }
    )

    response = rule_operation_status_handler_module.handler(
        _api_gateway_event("op-1"), None
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {
        "operationId": "op-1",
        "status": status,
        "errorReason": None,
    }


def test_existing_rejected_operation_includes_error_reason(
    rule_operation_status_handler_module, mocked_rule_operations_table
):
    mocked_rule_operations_table.put_item(
        Item={
            "operationId": "op-2",
            "type": "create",
            "targetRuleName": "custom-email",
            "pattern": "(",
            "status": "rejected",
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:05:00+00:00",
            "errorReason": "INVALID_PATTERN",
        }
    )

    response = rule_operation_status_handler_module.handler(
        _api_gateway_event("op-2"), None
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {
        "operationId": "op-2",
        "status": "rejected",
        "errorReason": "INVALID_PATTERN",
    }


def test_missing_operation_returns_404(
    rule_operation_status_handler_module, mocked_rule_operations_table
):
    response = rule_operation_status_handler_module.handler(
        _api_gateway_event("does-not-exist"), None
    )

    assert response["statusCode"] == 404
    payload = json.loads(response["body"])
    assert payload["error"]["code"] == "OPERATION_NOT_FOUND"
