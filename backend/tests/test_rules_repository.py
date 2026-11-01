"""Integration tests for `shared/rules_repository.py` (`RulesRepository`).

Covers the five repository operations against a `moto`-mocked
`RedactionRules` DynamoDB table (design.md "RedactionRule" data model):

- `put` followed by `get_by_name` round-trips a `RedactionRule` correctly.
- `get_all` returns every inserted rule.
- `update_enabled` changes only the `enabled` field (and `updatedAt`) on an
  existing item, leaving `pattern`/`isBuiltIn`/`createdAt` untouched.
- `delete` removes the item, and a subsequent `get_by_name` returns `None`.
"""
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.models import RedactionRule
from shared.rules_repository import RulesRepository

RULES_TABLE_NAME = "RedactionRules-test"


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
            TableName=RULES_TABLE_NAME,
            KeySchema=[{"AttributeName": "ruleName", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "ruleName", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield RulesRepository(table_name=RULES_TABLE_NAME, dynamodb_resource=dynamodb)


def _sample_rule(**overrides) -> RedactionRule:
    defaults = dict(
        ruleName="自定义规则",
        pattern=r"foo\d+",
        enabled=True,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return RedactionRule(**defaults)


def test_put_then_get_by_name_round_trips(repository):
    rule = _sample_rule()

    repository.put(rule)
    fetched = repository.get_by_name("自定义规则")

    assert fetched == rule


def test_get_by_name_on_nonexistent_name_returns_none(repository):
    assert repository.get_by_name("does-not-exist") is None


def test_get_all_returns_every_inserted_rule(repository):
    rule_a = _sample_rule(ruleName="规则A", pattern=r"a\d+")
    rule_b = _sample_rule(ruleName="规则B", pattern=r"b\d+", isBuiltIn=True)

    repository.put(rule_a)
    repository.put(rule_b)
    all_rules = repository.get_all()

    assert {rule.ruleName for rule in all_rules} == {"规则A", "规则B"}
    assert sorted(all_rules, key=lambda r: r.ruleName) == sorted(
        [rule_a, rule_b], key=lambda r: r.ruleName
    )


def test_get_all_on_empty_table_returns_empty_list(repository):
    assert repository.get_all() == []


def test_update_enabled_changes_only_enabled_field(repository):
    rule = _sample_rule(ruleName="规则C", enabled=True)
    repository.put(rule)

    repository.update_enabled("规则C", False)
    fetched = repository.get_by_name("规则C")

    assert fetched.enabled is False
    assert fetched.pattern == rule.pattern
    assert fetched.isBuiltIn == rule.isBuiltIn
    assert fetched.createdAt == rule.createdAt
    # `updatedAt` should have been refreshed away from the original value.
    assert fetched.updatedAt != rule.updatedAt


def test_delete_removes_item_and_subsequent_get_returns_none(repository):
    rule = _sample_rule(ruleName="规则D")
    repository.put(rule)
    assert repository.get_by_name("规则D") is not None

    repository.delete("规则D")

    assert repository.get_by_name("规则D") is None
