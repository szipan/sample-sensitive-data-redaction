"""`RulesInitHandler` Lambda: deploy-time Redaction Rule collection init
(wiring `initialize_rule_collection` to the real `RedactionRules` table via
`RulesRepository`).

This is an idempotent initialization script that runs at deployment, not on
every request. This module is that script: it is NOT wired to any API
Gateway route. Instead, `infra/redaction_stack.py` invokes it once per
`cdk deploy` (stack create/update) via a CDK Custom Resource backed by
this Lambda (`aws_cdk.custom_resources.Provider`).

Behavior:
1. Read every currently-stored `RedactionRule` via `RulesRepository.get_all()`.
2. Convert that list to the `dict[ruleName, RedactionRule]` shape that
   `initialize_rule_collection` (`shared/rule_collection.py`)
   expects, and call it.
3. For each Built-in Rule name that appears in the result but was NOT in
   the original existing set (i.e. newly added by `initialize_rule_collection`
   because no record for it existed yet), persist it via
   `RulesRepository.put()`.
4. Rules that already existed (built-in or custom, enabled or disabled) are
   left alone, with one exception: a Built-in Rule whose stored pattern is
   a known legacy pattern is upgraded to the current one (its `enabled`
   state is kept). `put()` is not called for any other existing rule, which
   avoids unnecessary writes.

`handler` doubles as both the CDK Custom Resource entry point (receiving a
CloudFormation custom-resource event via the `Provider` framework, whose
event `RequestType` -- `Create`/`Update`/`Delete` -- is inspected so the
init logic only runs on `Create`/`Update`, never `Delete`) and a plain
callable usable directly from tests without needing to fabricate a
CloudFormation event shape.
"""
import logging
from typing import Any, Dict

from shared.rule_collection import initialize_rule_collection
from shared.rules_repository import RulesRepository

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# CloudFormation custom-resource `RequestType`s for which the initialization
# logic should actually run. On stack deletion (`Delete`) the initialized
# Built-in Rules are intentionally left in place (this is a one-way,
# additive bootstrap step, not something to undo on teardown).
_RUN_ON_REQUEST_TYPES = ("Create", "Update")


def initialize_rules(repository: RulesRepository) -> None:
    """Run the idempotent Built-in Rule initialization against `repository`.

    Reads all existing rules, computes the initialized collection via
    `initialize_rule_collection`, and persists only the Built-in Rules that
    were newly added, plus Built-in Rules whose legacy pattern was upgraded
    (their `enabled` state is kept). Every other rule is left as stored.
    """
    existing_rules = repository.get_all()
    existing_by_name = {rule.ruleName: rule for rule in existing_rules}

    initialized_by_name = initialize_rule_collection(existing_by_name)

    newly_added_names = set(initialized_by_name) - set(existing_by_name)
    for rule_name in newly_added_names:
        repository.put(initialized_by_name[rule_name])
        logger.info("Initialized Built-in Rule %s", rule_name)
    # Built-in Rules whose stored legacy pattern was upgraded (`enabled` kept).
    for rule_name, existing in existing_by_name.items():
        updated = initialized_by_name.get(rule_name)
        if updated is not None and updated.pattern != existing.pattern:
            repository.put(updated)
            logger.info("Upgraded Built-in Rule %s to the current pattern", rule_name)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """CDK Custom Resource entry point.

    `event` follows the shape produced by `aws_cdk.custom_resources.Provider`
    (a wrapped CloudFormation custom-resource request). Only `Create` and
    `Update` request types trigger initialization; `Delete` is a no-op
    (nothing to roll back). The return value is the `Provider` framework's
    expected response shape (`PhysicalResourceId` + `Data`); no custom
    outputs are needed by this resource.
    """
    request_type = event.get("RequestType")
    if request_type in _RUN_ON_REQUEST_TYPES:
        initialize_rules(RulesRepository())
    else:
        logger.info("Skipping rule initialization for RequestType=%s", request_type)

    return {
        "PhysicalResourceId": "RulesInitCustomResource",
        "Data": {},
    }
