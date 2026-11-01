"""Pure Rule Operation validation/application functions.

Implements the per-operation-type validation rules described in design.md's
"RuleOperationProcessor" processing logic section:

    create:         the name must be unique (case-sensitive exact comparison),
                    1-100 characters long, and the pattern 1-500 characters
                    and compilable; on success it is written to
                    `RedactionRules` (`enabled=true`, `isBuiltIn=false`).
    enable/disable: the target rule name must exist; its `enabled` field is
                    then updated.
    delete:         the target rule name must exist and have
                    `isBuiltIn=false`; the rule is then removed from the
                    table.

This module operates purely on an in-memory representation of the rule
collection (`RuleCollection = dict[str, RedactionRule]`, the same
abstraction `shared/rule_collection.initialize_rule_collection` uses) and
has no DynamoDB/AWS SDK dependency. Wiring these functions to the real
`RedactionRules` table (via `RulesRepository`) is done by the
`RuleOperationProcessor` Lambda; this module intentionally does not import or
reference `RulesRepository`.

Design choice: this module provides
BOTH the three pure validation functions
(`validate_create`, `validate_enable_disable`, `validate_delete` — no
mutation, safe to call speculatively) AND three small paired application
functions (`apply_create`, `apply_enable_disable`, `apply_delete`) that run
the corresponding validation first and only mutate a *returned copy* of the
collection when validation succeeds. Pairing validation with application
this way is natural here (each application is a one-line dict update once
validated) and directly supports the "valid mutations are reflected" /
"invalid operations leave the collection unchanged" property tests:
every `apply_*` function returns the *original*
`existing_rules` object, untouched, whenever validation fails.

Error-reason priority for `validate_create` when a submission violates more
than one rule simultaneously: length constraints are checked first, then
name-uniqueness, then pattern compilability. This ordering is not specified
by requirements.md/design.md (each Acceptance Criterion addresses a single
violation in isolation); it is an implementation choice documented here for
consistency, and does not affect any single-violation case.
"""
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional, Tuple

from shared.models import (
    RedactionRule,
    RuleOperationErrorReason,
    is_valid_name_length,
    is_valid_pattern_length,
)
from shared.rule_collection import RuleCollection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of validating a single Rule Operation.

    `ok=True` implies `errorReason is None` (the operation may proceed).
    `ok=False` always carries one of the `RuleOperationErrorReason` values
    from `shared/models.py`, matching the terminal `rejected` state's
    `errorReason` field on the `RuleOperation` data model.
    """

    ok: bool
    errorReason: Optional[RuleOperationErrorReason] = None

    @staticmethod
    def success() -> "ValidationResult":
        return ValidationResult(ok=True, errorReason=None)

    @staticmethod
    def failure(reason: RuleOperationErrorReason) -> "ValidationResult":
        return ValidationResult(ok=False, errorReason=reason)


# ---------------------------------------------------------------------------
# Pure validation functions (no mutation)
# ---------------------------------------------------------------------------


def validate_create(
    existing_rules: RuleCollection, rule_name: str, pattern: str
) -> ValidationResult:
    """Validate a `create` Rule Operation against `existing_rules`.

    Checks, in priority order when more than one
    applies:
    1. `rule_name` length is 1-100 chars AND `pattern` length is 1-500
       chars -> otherwise `INVALID_LENGTH`.
    2. `rule_name` does not already exist in `existing_rules`, compared
       case-sensitively and exactly -> otherwise `DUPLICATE_NAME`.
    3. `pattern` compiles under Python's `re` module (the same regex engine
       the AgentCore Redaction Agent uses for matching) -> otherwise
       `INVALID_PATTERN`.
    """
    if not is_valid_name_length(rule_name) or not is_valid_pattern_length(pattern):
        return ValidationResult.failure("INVALID_LENGTH")

    if rule_name in existing_rules:
        return ValidationResult.failure("DUPLICATE_NAME")

    try:
        re.compile(pattern)
    except re.error:
        return ValidationResult.failure("INVALID_PATTERN")

    return ValidationResult.success()


def validate_enable_disable(
    existing_rules: RuleCollection, rule_name: str
) -> ValidationResult:
    """Validate an `enable`/`disable` Rule Operation against `existing_rules`.

    Checks: `rule_name` must exactly match, case
    sensitively, an existing Redaction Rule name -> otherwise
    `UNKNOWN_RULE_NAME`.
    """
    if rule_name not in existing_rules:
        return ValidationResult.failure("UNKNOWN_RULE_NAME")

    return ValidationResult.success()


def validate_delete(
    existing_rules: RuleCollection, rule_name: str
) -> ValidationResult:
    """Validate a `delete` Rule Operation against `existing_rules`.

    Checks:
    1. `rule_name` must exactly match, case sensitively, an existing
       Redaction Rule name -> otherwise `UNKNOWN_RULE_NAME`.
    2. That Redaction Rule must not be a Built-in Rule -> otherwise
       `BUILT_IN_NOT_DELETABLE`.
    """
    rule = existing_rules.get(rule_name)
    if rule is None:
        return ValidationResult.failure("UNKNOWN_RULE_NAME")

    if rule.isBuiltIn:
        return ValidationResult.failure("BUILT_IN_NOT_DELETABLE")

    return ValidationResult.success()


# ---------------------------------------------------------------------------
# Paired application functions (validate, then mutate a returned copy)
# ---------------------------------------------------------------------------


def apply_create(
    existing_rules: RuleCollection, rule_name: str, pattern: str
) -> Tuple[RuleCollection, ValidationResult]:
    """Validate and, if valid, apply a `create` Rule Operation.

    Returns `(new_collection, ValidationResult.success())` on success, where
    `new_collection` is `existing_rules` plus the new enabled, non-built-in
    Redaction Rule. On failure, returns
    `(existing_rules, ValidationResult.failure(reason))` — the exact same
    `existing_rules` object, left completely unmutated.
    """
    result = validate_create(existing_rules, rule_name, pattern)
    if not result.ok:
        return existing_rules, result

    timestamp = _now_iso()
    new_rules: RuleCollection = dict(existing_rules)
    new_rules[rule_name] = RedactionRule(
        ruleName=rule_name,
        pattern=pattern,
        enabled=True,
        isBuiltIn=False,
        createdAt=timestamp,
        updatedAt=timestamp,
    )
    return new_rules, result


def apply_enable_disable(
    existing_rules: RuleCollection, rule_name: str, enabled: bool
) -> Tuple[RuleCollection, ValidationResult]:
    """Validate and, if valid, apply an `enable`/`disable` Rule Operation.

    Returns `(new_collection, ValidationResult.success())` on success, where
    `new_collection` has `rule_name`'s `enabled` field set to `enabled`.
    On failure, returns `(existing_rules,
    ValidationResult.failure(reason))` — the exact same `existing_rules`
    object, left completely unmutated.
    """
    result = validate_enable_disable(existing_rules, rule_name)
    if not result.ok:
        return existing_rules, result

    new_rules: RuleCollection = dict(existing_rules)
    current = new_rules[rule_name]
    new_rules[rule_name] = replace(current, enabled=enabled, updatedAt=_now_iso())
    return new_rules, result


def apply_delete(
    existing_rules: RuleCollection, rule_name: str
) -> Tuple[RuleCollection, ValidationResult]:
    """Validate and, if valid, apply a `delete` Rule Operation.

    Returns `(new_collection, ValidationResult.success())` on success, where
    `new_collection` no longer contains `rule_name`. On
    failure, returns `(existing_rules, ValidationResult.failure(reason))` —
    the exact same `existing_rules` object, left completely unmutated.
    """
    result = validate_delete(existing_rules, rule_name)
    if not result.ok:
        return existing_rules, result

    new_rules: RuleCollection = dict(existing_rules)
    del new_rules[rule_name]
    return new_rules, result
