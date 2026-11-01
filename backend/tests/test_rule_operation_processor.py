"""Unit tests for `lambdas/rule_operation_processor.py`
.

Covers wiring the pure validation functions (`shared/rule_validation.py`) to
the real `RedactionRules` / `RuleOperations` tables via
`RulesRepository` / `RuleOperationsRepository` (design.md
"RuleOperationProcessor" component):

- Valid `create` -> operation ends `applied`, new rule appears in
  `RedactionRules`.
- Duplicate-name `create` -> `rejected` / `DUPLICATE_NAME`, `RedactionRules`
  unchanged.
- Invalid-pattern `create` -> `rejected` / `INVALID_PATTERN`.
- Valid `enable`/`disable` -> `applied`, rule's `enabled` field flips.
- Unknown-name `enable`/`disable` -> `rejected` / `UNKNOWN_RULE_NAME`.
- Valid `delete` of a custom rule -> `applied`, rule removed.
- `delete` of a built-in rule -> `rejected` / `BUILT_IN_NOT_DELETABLE`.
- `delete` of an unknown name -> `rejected` / `UNKNOWN_RULE_NAME`.

Both DynamoDB tables (`RedactionRules`, `RuleOperations`) are mocked with
`moto` so no real AWS calls are made. Tests call `handler` with a
`{"Records": [...]}` SQS event shape (each record's `body` is the JSON
payload `RuleOperationSubmitHandler` sends), mirroring the real Lambda
invocation contract.
"""
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))
sys.path.insert(0, str(BACKEND_ROOT))

from shared.models import RedactionRule, RuleOperation
from shared.rule_operations_repository import RuleOperationsRepository
from shared.rules_repository import RulesRepository

RULES_TABLE_NAME = "RedactionRules-test"
RULE_OPERATIONS_TABLE_NAME = "RuleOperations-test"


@pytest.fixture
def aws_credentials():
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"


@pytest.fixture
def repositories(aws_credentials):
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
        dynamodb.create_table(
            TableName=RULE_OPERATIONS_TABLE_NAME,
            KeySchema=[{"AttributeName": "operationId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "operationId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        rules_repository = RulesRepository(
            table_name=RULES_TABLE_NAME, dynamodb_resource=dynamodb
        )
        rule_operations_repository = RuleOperationsRepository(
            table_name=RULE_OPERATIONS_TABLE_NAME, dynamodb_resource=dynamodb
        )
        yield rules_repository, rule_operations_repository


@pytest.fixture
def module(aws_credentials, monkeypatch):
    """Import `rule_operation_processor` fresh per test with env vars set."""
    monkeypatch.setenv("RULES_TABLE_NAME", RULES_TABLE_NAME)
    monkeypatch.setenv("RULE_OPERATIONS_TABLE_NAME", RULE_OPERATIONS_TABLE_NAME)

    sys.modules.pop("rule_operation_processor", None)
    import rule_operation_processor as mod

    return mod


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


def _sample_rule(**overrides) -> RedactionRule:
    defaults = dict(
        ruleName="已存在规则",
        pattern=r"bar\d+",
        enabled=True,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return RedactionRule(**defaults)


def _sqs_event(payload: dict) -> dict:
    return {"Records": [{"body": json.dumps(payload)}]}


def _invoke(module, rules_repository, rule_operations_repository, payload):
    """Invoke `handler` with `rules_repository`/`rule_operations_repository`
    patched in as the module's lazily-initialized singletons, then return the
    resulting `RuleOperation` record for convenience.
    """
    module._rules_repository = rules_repository
    module._rule_operations_repository = rule_operations_repository

    result = module.handler(_sqs_event(payload), None)

    return result, rule_operations_repository.get_by_id(payload["operationId"])


def test_valid_create_is_applied_and_rule_is_persisted(module, repositories):
    rules_repository, rule_operations_repository = repositories
    operation = _sample_operation(
        operationId="op-create-ok",
        type="create",
        targetRuleName="新规则",
        pattern=r"\d{4}",
        status="queued",
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-create-ok",
            "type": "create",
            "targetRuleName": "新规则",
            "pattern": r"\d{4}",
        },
    )

    assert updated_operation.status == "applied"
    assert updated_operation.errorReason is None

    created_rule = rules_repository.get_by_name("新规则")
    assert created_rule is not None
    assert created_rule.pattern == r"\d{4}"
    assert created_rule.enabled is True
    assert created_rule.isBuiltIn is False


def test_duplicate_name_create_is_rejected_and_rules_unchanged(module, repositories):
    rules_repository, rule_operations_repository = repositories
    existing_rule = _sample_rule(ruleName="重复名称")
    rules_repository.put(existing_rule)

    operation = _sample_operation(
        operationId="op-create-dup",
        type="create",
        targetRuleName="重复名称",
        pattern=r"\d+",
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-create-dup",
            "type": "create",
            "targetRuleName": "重复名称",
            "pattern": r"\d+",
        },
    )

    assert updated_operation.status == "rejected"
    assert updated_operation.errorReason == "DUPLICATE_NAME"

    unchanged_rule = rules_repository.get_by_name("重复名称")
    assert unchanged_rule == existing_rule


def test_invalid_pattern_create_is_rejected(module, repositories):
    rules_repository, rule_operations_repository = repositories
    operation = _sample_operation(
        operationId="op-create-bad-pattern",
        type="create",
        targetRuleName="坏正则规则",
        pattern="[",
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-create-bad-pattern",
            "type": "create",
            "targetRuleName": "坏正则规则",
            "pattern": "[",
        },
    )

    assert updated_operation.status == "rejected"
    assert updated_operation.errorReason == "INVALID_PATTERN"
    assert rules_repository.get_by_name("坏正则规则") is None


def test_valid_enable_is_applied_and_rule_enabled_flips(module, repositories):
    rules_repository, rule_operations_repository = repositories
    rules_repository.put(_sample_rule(ruleName="待启用规则", enabled=False))

    operation = _sample_operation(
        operationId="op-enable-ok",
        type="enable",
        targetRuleName="待启用规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-enable-ok",
            "type": "enable",
            "targetRuleName": "待启用规则",
        },
    )

    assert updated_operation.status == "applied"
    assert rules_repository.get_by_name("待启用规则").enabled is True


def test_valid_disable_is_applied_and_rule_enabled_flips(module, repositories):
    rules_repository, rule_operations_repository = repositories
    rules_repository.put(_sample_rule(ruleName="待禁用规则", enabled=True))

    operation = _sample_operation(
        operationId="op-disable-ok",
        type="disable",
        targetRuleName="待禁用规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-disable-ok",
            "type": "disable",
            "targetRuleName": "待禁用规则",
        },
    )

    assert updated_operation.status == "applied"
    assert rules_repository.get_by_name("待禁用规则").enabled is False


def test_unknown_name_enable_disable_is_rejected(module, repositories):
    rules_repository, rule_operations_repository = repositories

    operation = _sample_operation(
        operationId="op-enable-unknown",
        type="enable",
        targetRuleName="不存在的规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-enable-unknown",
            "type": "enable",
            "targetRuleName": "不存在的规则",
        },
    )

    assert updated_operation.status == "rejected"
    assert updated_operation.errorReason == "UNKNOWN_RULE_NAME"


def test_valid_delete_of_custom_rule_is_applied_and_removed(module, repositories):
    rules_repository, rule_operations_repository = repositories
    rules_repository.put(_sample_rule(ruleName="待删除规则", isBuiltIn=False))

    operation = _sample_operation(
        operationId="op-delete-ok",
        type="delete",
        targetRuleName="待删除规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-delete-ok",
            "type": "delete",
            "targetRuleName": "待删除规则",
        },
    )

    assert updated_operation.status == "applied"
    assert rules_repository.get_by_name("待删除规则") is None


def test_delete_of_builtin_rule_is_rejected(module, repositories):
    rules_repository, rule_operations_repository = repositories
    rules_repository.put(_sample_rule(ruleName="内置规则", isBuiltIn=True))

    operation = _sample_operation(
        operationId="op-delete-builtin",
        type="delete",
        targetRuleName="内置规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-delete-builtin",
            "type": "delete",
            "targetRuleName": "内置规则",
        },
    )

    assert updated_operation.status == "rejected"
    assert updated_operation.errorReason == "BUILT_IN_NOT_DELETABLE"
    assert rules_repository.get_by_name("内置规则") is not None


def test_delete_of_unknown_name_is_rejected(module, repositories):
    rules_repository, rule_operations_repository = repositories

    operation = _sample_operation(
        operationId="op-delete-unknown",
        type="delete",
        targetRuleName="不存在的规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _, updated_operation = _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-delete-unknown",
            "type": "delete",
            "targetRuleName": "不存在的规则",
        },
    )

    assert updated_operation.status == "rejected"
    assert updated_operation.errorReason == "UNKNOWN_RULE_NAME"


def test_status_moves_through_processing_before_terminal_state(module, repositories):
    """The operation's status must pass through `processing` before landing
    on its terminal state, not jump straight there."""
    rules_repository, rule_operations_repository = repositories
    observed_statuses = []
    original_update_status = rule_operations_repository.update_status

    def _spying_update_status(operation_id, status, error_reason=None):
        observed_statuses.append(status)
        return original_update_status(operation_id, status, error_reason=error_reason)

    rule_operations_repository.update_status = _spying_update_status

    rules_repository.put(_sample_rule(ruleName="观察规则", enabled=False))
    operation = _sample_operation(
        operationId="op-status-sequence",
        type="enable",
        targetRuleName="观察规则",
        pattern=None,
    )
    rule_operations_repository.create(operation)

    _invoke(
        module,
        rules_repository,
        rule_operations_repository,
        {
            "operationId": "op-status-sequence",
            "type": "enable",
            "targetRuleName": "观察规则",
        },
    )

    assert observed_statuses == ["processing", "applied"]


# ---------------------------------------------------------------------------
# Failure-path log content
#
# The failure branch used to log the entire SQS record, whose `body` carries
# the user-authored rule `pattern` -- a Sensitive_Value. Lambda logs do reach
# CloudWatch, so these assert on a leak that was live rather than a
# hypothetical one. Both tests drive the branch without touching DynamoDB:
# the payload fails before any repository call, so the module's lazily
# initialized singletons are stubbed with sentinels.
# ---------------------------------------------------------------------------

MARKER_PATTERN = r"MARKERPATTERN-110101199003077777"


def _stub_repositories(module):
    module._rules_repository = object()
    module._rule_operations_repository = object()


def test_failed_record_log_excludes_rule_pattern_and_names_the_message(
    module, caplog
):
    """A record that fails to process is identified by Safe_Reference only."""
    _stub_repositories(module)
    body = json.dumps(
        {
            "operationId": "op-log-safe",
            "targetRuleName": "标记规则",
            "pattern": MARKER_PATTERN,
        }
    )  # no "type" key -> `process_operation` raises KeyError

    with caplog.at_level("ERROR"):
        result = module.handler(
            {"Records": [{"messageId": "msg-log-safe", "body": body}]}, None
        )

    assert result == {"processedCount": 0}
    assert MARKER_PATTERN not in caplog.text
    assert "msg-log-safe" in caplog.text
    assert "op-log-safe" in caplog.text


def test_failed_record_log_survives_a_malformed_body(module, caplog):
    """`json.loads(record["body"])` may be exactly what failed, so reading
    `operationId` for the log line must not raise inside the handler."""
    _stub_repositories(module)
    malformed_body = '{"operationId": "op-broken", "pattern": "' + MARKER_PATTERN

    with caplog.at_level("ERROR"):
        result = module.handler(
            {"Records": [{"messageId": "msg-broken", "body": malformed_body}]}, None
        )

    assert result == {"processedCount": 0}
    assert MARKER_PATTERN not in caplog.text
    assert "msg-broken" in caplog.text
    assert "operationId None" in caplog.text
