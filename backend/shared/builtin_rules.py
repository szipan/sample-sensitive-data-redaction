"""Built-in Redaction Rule constants.

Defines the regular-expression patterns and metadata for the two Built-in
Rules described in design.md's "RedactionRule" Data Model section:

- Chinese resident identity card number (身份证号): 17 digits followed by one
  checksum character that is either a digit (0-9) or an uppercase "X".
- Mainland China mobile phone number (大陆手机号): 11 digits, starting with
  "1", with a second digit between 3 and 9.

This module is intentionally dependency-free (no boto3/AWS SDK imports), and
does not implement rule-collection initialization or the
DynamoDB repository — only the pattern constants, built-in rule
metadata, and a reference matching helper used exclusively by tests
to validate the production matching logic
against a naive/direct implementation.
"""
import re

# ---------------------------------------------------------------------------
# Built-in rule regex patterns
# ---------------------------------------------------------------------------

IDENTITY_CARD_PATTERN = r"(?<!\d)\d{17}[\dX](?!\d)"
"""Matches an 18-character Chinese resident identity card number: 17 numeric
digits followed by one checksum character that is either a digit (0-9) or an
uppercase "X". The digit boundaries stop it from matching 18 digits inside a
longer number (e.g. a 19-digit bank card), which mislabelled the card as an
identity number."""

MOBILE_NUMBER_PATTERN = r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"
"""Matches an 11-digit mainland China mobile phone number: starts with "1",
second digit is 3-9, followed by 9 more digits. An optional +86 / 86 country
prefix is included in the match. The digit boundaries stop it from matching
11 digits inside an identity-card or bank-card number."""

#: Patterns earlier releases stored for each Built-in Rule. A stored Built-in
#: Rule whose pattern is still one of these is upgraded to the current pattern
#: at deploy time (see `rule_collection.initialize_rule_collection`); a pattern
#: a user changed to anything else is left alone.
LEGACY_BUILT_IN_PATTERNS = {
    "身份证号": (r"\d{17}[\dX]",),
    "大陆手机号": (r"1[3-9]\d{9}",),
}


# ---------------------------------------------------------------------------
# Built-in rule names / metadata (design.md Data Models: RedactionRule)
# ---------------------------------------------------------------------------

IDENTITY_CARD_RULE_NAME = "身份证号"
MOBILE_NUMBER_RULE_NAME = "大陆手机号"

BUILT_IN_RULES = (
    {"ruleName": IDENTITY_CARD_RULE_NAME, "pattern": IDENTITY_CARD_PATTERN, "isBuiltIn": True},
    {"ruleName": MOBILE_NUMBER_RULE_NAME, "pattern": MOBILE_NUMBER_PATTERN, "isBuiltIn": True},
)
"""Metadata for the Built-in Rules. Each entry's keys mirror the
`RedactionRule` dataclass fields (`shared/models.py`) that are known ahead of
persistence (`ruleName`, `pattern`, `isBuiltIn`); `enabled`/`createdAt`/
`updatedAt` are assigned by the rule-collection initialization logic, not here.
"""


# ---------------------------------------------------------------------------
# Reference (naive/direct) matching functions — TEST USE ONLY.
#
# These exist solely so property tests can
# compare the Agent's production rule-matching tool against a
# straightforward, obviously-correct application of `re` on the same
# pattern. Production code must NOT depend on this module's matching
# functions.
# ---------------------------------------------------------------------------


def reference_match(pattern: str, text: str) -> bool:
    """Return True if `pattern` fully matches `text`, via a direct `re.match`
    call with no caching, precompilation, or optimization.

    This is the generic reference (naive) matcher: a thin, deliberately
    simple wrapper around `re.match` used only by tests as the "obviously
    correct" oracle to compare production matching logic against.
    """
    return re.match(pattern, text) is not None


def reference_match_identity_card(text: str) -> bool:
    """Reference (naive) matcher for the identity-card Built-in Rule.
    TEST USE ONLY — see module docstring."""
    return reference_match(IDENTITY_CARD_PATTERN, text)


def reference_match_mobile_number(text: str) -> bool:
    """Reference (naive) matcher for the mobile-number Built-in Rule.
    TEST USE ONLY — see module docstring."""
    return reference_match(MOBILE_NUMBER_PATTERN, text)
