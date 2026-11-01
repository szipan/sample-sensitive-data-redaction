"""Integration tests for `shared/rule_operations_repository.py`
.

Covers the three repository operations against a `moto`-mocked
`RuleOperations` DynamoDB table (design.md "RuleOperation" data model):

- `create` followed by `get_by_id` round-trips a `RuleOperation` correctly.
- `update_status` changes `status` (and, when provided, `errorReason`) on
  an existing item, and bumps `updatedAt`.
- `get_by_id` on a nonexistent id returns `None`.
"""
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.models import RuleOperation
from shared.rule_operations_repository import RuleOperationsRepository

RULE_OPERATIONS_TABLE_NAME = "RuleOperations-test"


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def repository(aws_credentials):
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
        yield RuleOperationsRepository(
            table_name=RULE_OPERATIONS_TABLE_NAME, dynamodb_resource=dynamodb
        )


def _sample_operation(**overrides) -> RuleOperation:
    defaults = dict(
        operationId="op-1",
        type="create",
        targetRuleName="自定义规则",
        status="queued",
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
        pattern=r"foo\d+",
        errorReason=None,
    )
    defaults.update(overrides)
    return RuleOperation(**defaults)


def test_create_then_get_by_id_round_trips(repository):
    operation = _sample_operation()

    repository.create(operation)
    fetched = repository.get_by_id("op-1")

    assert fetched == operation


def test_create_then_get_by_id_round_trips_without_pattern(repository):
    operation = _sample_operation(
        operationId="op-2",
        type="disable",
        pattern=None,
        errorReason=None,
    )

    repository.create(operation)
    fetched = repository.get_by_id("op-2")

    assert fetched == operation
    assert fetched.pattern is None


def test_update_status_changes_status_and_error_reason(repository):
    operation = _sample_operation(operationId="op-3", status="processing")
    repository.create(operation)

    repository.update_status("op-3", "rejected", error_reason="DUPLICATE_NAME")
    fetched = repository.get_by_id("op-3")

    assert fetched.status == "rejected"
    assert fetched.errorReason == "DUPLICATE_NAME"
    # `updatedAt` should have been refreshed away from the original value.
    assert fetched.updatedAt != operation.updatedAt


def test_update_status_without_error_reason_leaves_it_unset(repository):
    operation = _sample_operation(operationId="op-4", status="processing")
    repository.create(operation)

    repository.update_status("op-4", "applied")
    fetched = repository.get_by_id("op-4")

    assert fetched.status == "applied"
    assert fetched.errorReason is None


def test_get_by_id_on_nonexistent_id_returns_none(repository):
    assert repository.get_by_id("does-not-exist") is None
