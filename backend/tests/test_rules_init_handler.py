"""Unit tests for `lambdas/rules_init_handler.py` (`RulesInitHandler`).

Covers wiring the pure `initialize_rule_collection` function to the
real `RedactionRules` table via `RulesRepository` (design.md "RedactionRule"
Data Model initialization paragraph):

- Empty table: after invocation, both Built-in Rules exist and are enabled.
- Table with one Built-in Rule already disabled: after invocation, that
  rule remains disabled and the other Built-in Rule is added as enabled.
- Table with a custom rule: the custom rule is left completely untouched
  (not duplicated/rewritten -- `put` is never called for it).

DynamoDB is mocked with `moto` via `RulesRepository` so no real AWS calls
are made.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

# Ensure `lambdas/` and `shared/` are importable the same way the Lambda
# runtime would, mirroring `test_upload_handler.py` / `test_rules_list_handler.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
sys.path.insert(0, str(BACKEND_ROOT))

from shared.builtin_rules import IDENTITY_CARD_RULE_NAME, MOBILE_NUMBER_RULE_NAME
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
def rules_init_handler_module(aws_credentials, monkeypatch):
    """Import `rules_init_handler` fresh per test with env vars set."""
    monkeypatch.setenv("RULES_TABLE_NAME", RULES_TABLE_NAME)

    sys.modules.pop("rules_init_handler", None)
    import rules_init_handler as module

    return module


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


def _custom_rule(**overrides) -> RedactionRule:
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


def test_empty_table_results_in_both_builtin_rules_enabled(
    rules_init_handler_module, repository
):
    rules_init_handler_module.initialize_rules(repository)

    identity_card_rule = repository.get_by_name(IDENTITY_CARD_RULE_NAME)
    mobile_number_rule = repository.get_by_name(MOBILE_NUMBER_RULE_NAME)

    assert identity_card_rule is not None
    assert identity_card_rule.enabled is True
    assert identity_card_rule.isBuiltIn is True

    assert mobile_number_rule is not None
    assert mobile_number_rule.enabled is True
    assert mobile_number_rule.isBuiltIn is True


def test_existing_disabled_builtin_rule_stays_disabled_other_gets_added(
    rules_init_handler_module, repository
):
    disabled_rule = RedactionRule(
        ruleName=IDENTITY_CARD_RULE_NAME,
        pattern=r"^\d{17}[\dX]$",
        enabled=False,
        isBuiltIn=True,
        createdAt="2023-06-01T00:00:00+00:00",
        updatedAt="2023-06-01T00:00:00+00:00",
    )
    repository.put(disabled_rule)

    rules_init_handler_module.initialize_rules(repository)

    identity_card_rule = repository.get_by_name(IDENTITY_CARD_RULE_NAME)
    mobile_number_rule = repository.get_by_name(MOBILE_NUMBER_RULE_NAME)

    # The pre-existing Built-in Rule's disabled state (and other fields)
    # must survive initialization untouched.
    assert identity_card_rule == disabled_rule

    # The other Built-in Rule, which did not previously exist, is newly
    # added and enabled.
    assert mobile_number_rule is not None
    assert mobile_number_rule.enabled is True
    assert mobile_number_rule.isBuiltIn is True


def test_custom_rule_is_untouched_and_not_duplicated(
    rules_init_handler_module, repository
):
    custom_rule = _custom_rule()
    repository.put(custom_rule)

    with patch.object(repository, "put", wraps=repository.put) as put_spy:
        rules_init_handler_module.initialize_rules(repository)

        # `put` must never be called for the pre-existing custom rule: only
        # newly-added Built-in Rules are written.
        put_calls_for_custom_rule = [
            call for call in put_spy.call_args_list if call.args[0].ruleName == "自定义规则"
        ]
        assert put_calls_for_custom_rule == []

    fetched_custom_rule = repository.get_by_name("自定义规则")
    assert fetched_custom_rule == custom_rule

    all_rule_names = {rule.ruleName for rule in repository.get_all()}
    assert all_rule_names == {
        "自定义规则",
        IDENTITY_CARD_RULE_NAME,
        MOBILE_NUMBER_RULE_NAME,
    }


def test_reinitializing_an_already_initialized_table_is_a_no_op(
    rules_init_handler_module, repository
):
    rules_init_handler_module.initialize_rules(repository)
    first_pass_rules = {rule.ruleName: rule for rule in repository.get_all()}

    with patch.object(repository, "put", wraps=repository.put) as put_spy:
        rules_init_handler_module.initialize_rules(repository)
        assert put_spy.call_args_list == []

    second_pass_rules = {rule.ruleName: rule for rule in repository.get_all()}
    assert second_pass_rules == first_pass_rules


def test_handler_runs_initialization_on_create_and_update_but_not_delete(
    rules_init_handler_module, repository
):
    module = rules_init_handler_module

    with patch.object(module, "RulesRepository", return_value=repository):
        with patch.object(module, "initialize_rules") as initialize_spy:
            module.handler({"RequestType": "Create"}, None)
            module.handler({"RequestType": "Update"}, None)
            module.handler({"RequestType": "Delete"}, None)

    assert initialize_spy.call_count == 2
