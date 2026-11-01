"""Idempotent Redaction Rule collection initialization.

Initialization logic (an idempotent script that runs once at application
startup / first deployment, not on every request): a Built-in Rule's default
record (`enabled=true`) is written only when the table has no record for its
`ruleName`; an existing record is never modified (whatever its `enabled`
value), apart from the pattern upgrade described in
`initialize_rule_collection`.

This module operates purely on an in-memory representation of the rule
collection (a `dict[str, RedactionRule]` keyed by `ruleName`). It has no
DynamoDB/AWS SDK dependency; wiring this function to the real
`RedactionRules` table (via `RulesRepository`) is done by
`lambdas/rules_init_handler.py`.
"""
from datetime import datetime, timezone
from typing import Dict

from shared.builtin_rules import BUILT_IN_RULES, LEGACY_BUILT_IN_PATTERNS
from shared.models import RedactionRule

RuleCollection = Dict[str, RedactionRule]
"""An in-memory Redaction Rule collection, keyed by `ruleName`."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_rule_collection(existing_rules: RuleCollection) -> RuleCollection:
    """Return a rule collection with every Built-in Rule present, without
    disturbing any already-stored record.

    For each Built-in Rule defined in `BUILT_IN_RULES`:
    - If its `ruleName` is NOT already a key in `existing_rules`, add it to
      the result with `enabled=True` (no existing
      Redaction Rule records -> every Built-in Rule ends up enabled).
    - If its `ruleName` IS already a key in `existing_rules`, leave that
      existing entry completely unchanged, regardless of its current
      `enabled` value (a prior stored state, enabled or
      disabled, is retained and never reset).

    - Exception: if the existing entry is a Built-in Rule whose stored
      `pattern` is one of that rule's `LEGACY_BUILT_IN_PATTERNS`, its
      `pattern` (and `updatedAt`) is upgraded to the current one. `enabled`
      and `createdAt` are kept, so a prior enabled/disabled choice is still retained. A pattern
      that matches no legacy value is left untouched.

    Any other (non-built-in / custom) entries already present in
    `existing_rules` are also left unchanged and are included in the
    result as-is.

    This function does not mutate `existing_rules` in place — it returns a
    new dict, leaving the caller's input collection untouched.
    """
    result: RuleCollection = dict(existing_rules)

    for built_in in BUILT_IN_RULES:
        rule_name = built_in["ruleName"]
        if rule_name in result:
            existing = result[rule_name]
            if (
                existing.isBuiltIn
                and existing.pattern != built_in["pattern"]
                and existing.pattern in LEGACY_BUILT_IN_PATTERNS.get(rule_name, ())
            ):
                result[rule_name] = RedactionRule(
                    ruleName=rule_name,
                    pattern=built_in["pattern"],
                    enabled=existing.enabled,
                    isBuiltIn=existing.isBuiltIn,
                    createdAt=existing.createdAt,
                    updatedAt=_now_iso(),
                )
            continue
        timestamp = _now_iso()
        result[rule_name] = RedactionRule(
            ruleName=rule_name,
            pattern=built_in["pattern"],
            enabled=True,
            isBuiltIn=built_in["isBuiltIn"],
            createdAt=timestamp,
            updatedAt=timestamp,
        )

    return result
