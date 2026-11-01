"""Unit tests for `shared/rule_validation.py`.

Covers the per-operation-type validation rules described in design.md's
"RuleOperationProcessor" processing logic section and Requirements
2.6-2.12:

- `validate_create`: duplicate name (case-sensitive exact match) rejected as
  `DUPLICATE_NAME`; invalid regex rejected as `INVALID_PATTERN`; name/pattern
  length violations rejected as `INVALID_LENGTH`; a valid new rule is
  accepted.
- `validate_enable_disable`: an existing name is accepted; an unknown name
  is rejected as `UNKNOWN_RULE_NAME`.
- `validate_delete`: an existing custom rule is accepted; an unknown name is
  rejected as `UNKNOWN_RULE_NAME`; an existing built-in rule is rejected as
  `BUILT_IN_NOT_DELETABLE`.

Also exercises the paired `apply_*` functions to confirm invalid operations
leave the input collection completely unchanged (same object, untouched),
which the later property tests depend on.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.models import NAME_MAX_LENGTH, PATTERN_MAX_LENGTH, RedactionRule
from shared.rule_validation import (
    apply_create,
    apply_delete,
    apply_enable_disable,
    validate_create,
    validate_delete,
    validate_enable_disable,
)

EXISTING_CUSTOM_RULE = RedactionRule(
    ruleName="Existing Custom",
    pattern=r"\d+",
    enabled=True,
    isBuiltIn=False,
    createdAt="2024-01-01T00:00:00+00:00",
    updatedAt="2024-01-01T00:00:00+00:00",
)

EXISTING_BUILT_IN_RULE = RedactionRule(
    ruleName="身份证号",
    pattern=r"^\d{17}[\dX]$",
    enabled=True,
    isBuiltIn=True,
    createdAt="2024-01-01T00:00:00+00:00",
    updatedAt="2024-01-01T00:00:00+00:00",
)


def _existing_rules():
    return {
        EXISTING_CUSTOM_RULE.ruleName: EXISTING_CUSTOM_RULE,
        EXISTING_BUILT_IN_RULE.ruleName: EXISTING_BUILT_IN_RULE,
    }


# ---------------------------------------------------------------------------
# validate_create
# ---------------------------------------------------------------------------


def test_validate_create_accepts_a_valid_new_rule():
    result = validate_create(_existing_rules(), "New Rule", r"foo\d+")

    assert result.ok is True
    assert result.errorReason is None


def test_validate_create_rejects_duplicate_name_case_sensitive_exact_match():
    result = validate_create(_existing_rules(), "Existing Custom", r"foo\d+")

    assert result.ok is False
    assert result.errorReason == "DUPLICATE_NAME"


def test_validate_create_accepts_name_differing_only_by_case():
    # Case-sensitive exact match means a different-case name is NOT a
    # Duplicate.
    result = validate_create(_existing_rules(), "existing custom", r"foo\d+")

    assert result.ok is True


def test_validate_create_rejects_invalid_regex():
    result = validate_create(_existing_rules(), "New Rule", r"(unbalanced")

    assert result.ok is False
    assert result.errorReason == "INVALID_PATTERN"


def test_validate_create_rejects_name_too_short():
    result = validate_create(_existing_rules(), "", r"foo\d+")

    assert result.ok is False
    assert result.errorReason == "INVALID_LENGTH"


def test_validate_create_rejects_name_too_long():
    result = validate_create(_existing_rules(), "a" * (NAME_MAX_LENGTH + 1), r"foo\d+")

    assert result.ok is False
    assert result.errorReason == "INVALID_LENGTH"


def test_validate_create_rejects_pattern_too_short():
    result = validate_create(_existing_rules(), "New Rule", "")

    assert result.ok is False
    assert result.errorReason == "INVALID_LENGTH"


def test_validate_create_rejects_pattern_too_long():
    result = validate_create(
        _existing_rules(), "New Rule", "a" * (PATTERN_MAX_LENGTH + 1)
    )

    assert result.ok is False
    assert result.errorReason == "INVALID_LENGTH"


def test_apply_create_adds_rule_on_success():
    existing = _existing_rules()

    new_rules, result = apply_create(existing, "New Rule", r"foo\d+")

    assert result.ok is True
    assert "New Rule" in new_rules
    assert new_rules["New Rule"].enabled is True
    assert new_rules["New Rule"].isBuiltIn is False
    # Original collection is untouched.
    assert "New Rule" not in existing


def test_apply_create_leaves_collection_unchanged_on_failure():
    existing = _existing_rules()

    new_rules, result = apply_create(existing, "Existing Custom", r"foo\d+")

    assert result.ok is False
    assert new_rules is existing


# ---------------------------------------------------------------------------
# validate_enable_disable
# ---------------------------------------------------------------------------


def test_validate_enable_disable_accepts_existing_name():
    result = validate_enable_disable(_existing_rules(), "Existing Custom")

    assert result.ok is True
    assert result.errorReason is None


def test_validate_enable_disable_rejects_unknown_name():
    result = validate_enable_disable(_existing_rules(), "Does Not Exist")

    assert result.ok is False
    assert result.errorReason == "UNKNOWN_RULE_NAME"


def test_apply_enable_disable_updates_enabled_field_on_success():
    existing = _existing_rules()

    new_rules, result = apply_enable_disable(existing, "Existing Custom", False)

    assert result.ok is True
    assert new_rules["Existing Custom"].enabled is False
    # Original collection is untouched.
    assert existing["Existing Custom"].enabled is True


def test_apply_enable_disable_leaves_collection_unchanged_on_failure():
    existing = _existing_rules()

    new_rules, result = apply_enable_disable(existing, "Does Not Exist", True)

    assert result.ok is False
    assert new_rules is existing


# ---------------------------------------------------------------------------
# validate_delete
# ---------------------------------------------------------------------------


def test_validate_delete_accepts_existing_custom_rule():
    result = validate_delete(_existing_rules(), "Existing Custom")

    assert result.ok is True
    assert result.errorReason is None


def test_validate_delete_rejects_unknown_name():
    result = validate_delete(_existing_rules(), "Does Not Exist")

    assert result.ok is False
    assert result.errorReason == "UNKNOWN_RULE_NAME"


def test_validate_delete_rejects_built_in_rule():
    result = validate_delete(_existing_rules(), "身份证号")

    assert result.ok is False
    assert result.errorReason == "BUILT_IN_NOT_DELETABLE"


def test_apply_delete_removes_rule_on_success():
    existing = _existing_rules()

    new_rules, result = apply_delete(existing, "Existing Custom")

    assert result.ok is True
    assert "Existing Custom" not in new_rules
    # Original collection is untouched.
    assert "Existing Custom" in existing


def test_apply_delete_leaves_collection_unchanged_on_failure():
    existing = _existing_rules()

    new_rules, result = apply_delete(existing, "身份证号")

    assert result.ok is False
    assert new_rules is existing
