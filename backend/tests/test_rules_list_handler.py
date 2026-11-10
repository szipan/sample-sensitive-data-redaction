"""Unit tests for `lambdas/rules_list_handler.py`.

Covers the behaviors described in design.md's "RulesListHandler" component
and "GET /rules" API design section,:

- An empty `RedactionRules` table returns `200` with `{ "rules": [] }`.
- A table with multiple rules (a mix of built-in/custom and enabled/
  disabled) returns all of them, each mapped to
  `{ "ruleName", "pattern", "enabled", "isBuiltIn" }`.

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
# runtime would, mirroring `test_upload_handler.py` / `test_job_status_handler.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
sys.path.insert(0, str(BACKEND_ROOT))

RULES_TABLE_NAME = "RedactionRules-test"


def _api_gateway_event() -> dict:
    return {
        "version": "2.0",
        "routeKey": "GET /rules",
        "headers": {},
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"sub": "admin-1", "cognito:groups": "[admins]"}}},
            "http": {"method": "GET", "path": "/rules"},
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
def rules_list_handler_module(aws_credentials, monkeypatch):
    """Import `rules_list_handler` fresh per test with env vars set, and
    reset its lazily-initialized repository global so each test gets a
    client bound to that test's `mock_aws()` context."""
    monkeypatch.setenv("RULES_TABLE_NAME", RULES_TABLE_NAME)

    sys.modules.pop("rules_list_handler", None)
    import rules_list_handler as module

    module._rules_repository = None
    return module


@pytest.fixture
def mocked_rules_table(aws_credentials):
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=RULES_TABLE_NAME,
            KeySchema=[{"AttributeName": "ruleName", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "ruleName", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield dynamodb.Table(RULES_TABLE_NAME)


def test_empty_table_returns_empty_rules_list(
    rules_list_handler_module, mocked_rules_table
):
    response = rules_list_handler_module.handler(_api_gateway_event(), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload == {"rules": []}


def test_multiple_rules_are_all_returned_with_correct_fields(
    rules_list_handler_module, mocked_rules_table
):
    mocked_rules_table.put_item(
        Item={
            "ruleName": "身份证号",
            "pattern": r"^\d{17}[\dX]$",
            "enabled": True,
            "isBuiltIn": True,
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:00:00+00:00",
        }
    )
    mocked_rules_table.put_item(
        Item={
            "ruleName": "大陆手机号",
            "pattern": r"^1[3-9]\d{9}$",
            "enabled": False,
            "isBuiltIn": True,
            "createdAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-02T00:00:00+00:00",
        }
    )
    mocked_rules_table.put_item(
        Item={
            "ruleName": "custom-email",
            "pattern": r"^[^@]+@[^@]+$",
            "enabled": True,
            "isBuiltIn": False,
            "createdAt": "2024-01-03T00:00:00+00:00",
            "updatedAt": "2024-01-03T00:00:00+00:00",
        }
    )

    response = rules_list_handler_module.handler(_api_gateway_event(), None)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    rules_by_name = {rule["ruleName"]: rule for rule in payload["rules"]}
    assert set(rules_by_name.keys()) == {"身份证号", "大陆手机号", "custom-email"}

    assert rules_by_name["身份证号"] == {
        "ruleName": "身份证号",
        "pattern": r"^\d{17}[\dX]$",
        "enabled": True,
        "isBuiltIn": True,
    }
    assert rules_by_name["大陆手机号"] == {
        "ruleName": "大陆手机号",
        "pattern": r"^1[3-9]\d{9}$",
        "enabled": False,
        "isBuiltIn": True,
    }
    assert rules_by_name["custom-email"] == {
        "ruleName": "custom-email",
        "pattern": r"^[^@]+@[^@]+$",
        "enabled": True,
        "isBuiltIn": False,
    }
