"""Unit tests for `shared/rule_collection.py`.

Covers the initialization semantics described in design.md's "RedactionRule"
Data Model section:

- Empty existing collection -> both Built-in Rules end up present and
  `enabled=True`.
- A Built-in Rule already present with a stored `enabled=False` state keeps
  that state after re-initialization, while a still-absent Built-in Rule is
  added as `enabled=True`.
- A custom (non-built-in) rule already present in the collection is left
  completely untouched.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.builtin_rules import IDENTITY_CARD_RULE_NAME, MOBILE_NUMBER_RULE_NAME
from shared.models import RedactionRule
from shared.rule_collection import initialize_rule_collection


def test_empty_collection_results_in_both_builtin_rules_enabled():
    result = initialize_rule_collection({})

    assert set(result.keys()) == {IDENTITY_CARD_RULE_NAME, MOBILE_NUMBER_RULE_NAME}
    for rule in result.values():
        assert rule.enabled is True
        assert rule.isBuiltIn is True


def test_existing_disabled_builtin_rule_is_not_reset_to_enabled():
    existing_identity_card_rule = RedactionRule(
        ruleName=IDENTITY_CARD_RULE_NAME,
        pattern=r"^\d{17}[\dX]$",
        enabled=False,
        isBuiltIn=True,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )
    existing_rules = {IDENTITY_CARD_RULE_NAME: existing_identity_card_rule}

    result = initialize_rule_collection(existing_rules)

    # The already-present Built-in Rule keeps its stored disabled state,
    # completely unchanged (same object/values), rather than being reset.
    assert result[IDENTITY_CARD_RULE_NAME] is existing_identity_card_rule
    assert result[IDENTITY_CARD_RULE_NAME].enabled is False

    # The previously-absent Built-in Rule is added and enabled by default.
    assert MOBILE_NUMBER_RULE_NAME in result
    assert result[MOBILE_NUMBER_RULE_NAME].enabled is True
    assert result[MOBILE_NUMBER_RULE_NAME].isBuiltIn is True


def test_custom_rule_is_left_untouched():
    custom_rule = RedactionRule(
        ruleName="自定义规则",
        pattern=r"foo\d+",
        enabled=True,
        isBuiltIn=False,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )
    existing_rules = {"自定义规则": custom_rule}

    result = initialize_rule_collection(existing_rules)

    assert result["自定义规则"] is custom_rule
    assert IDENTITY_CARD_RULE_NAME in result
    assert MOBILE_NUMBER_RULE_NAME in result


def test_does_not_mutate_the_input_collection():
    existing_rules: dict = {}

    result = initialize_rule_collection(existing_rules)

    assert existing_rules == {}
    assert result != existing_rules
