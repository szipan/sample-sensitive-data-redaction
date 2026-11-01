"""Plausibility filter for model-produced PII spans (precision layer).

Bedrock Guardrails and the LLM source both label free text with an entity
type, and both over-trigger on technical Chinese/English documents: a cloud
spec ``4c, 16G`` comes back as ``MAC_ADDRESS``, an instance count ``43`` as
``AGE``, a public documentation link as ``URL``, and the LLM once returned a
whole paragraph as one ``URL`` match. Every such span becomes a row the
reviewer must untick, or a black box in an auto-mode document.

`filter_implausible_spans` drops a span when its matched text cannot be the
entity type it is labelled with. It only runs on the two model sources
(`guardrails:*` / `llm:*`); user-defined regex rules are explicit intent and
are never filtered. Types without a check pass through unchanged.

Checks (applied to the matched text, i.e. `text[startOffset:endOffset]`):

- Types in `NON_PII_ENTITY_TYPES` (``URL``) are dropped: in enterprise
  documents URLs are overwhelmingly public sites, and a URL that embeds a
  person's name still has that name caught by the NAME detection.
- A length cap per type (`MAX_SPAN_CHARS`), so a run-on match cannot black
  out a paragraph.
- Format checks for structured types (EMAIL, IP/MAC address, PHONE, card and
  account numbers, AWS keys, SWIFT, IBAN), and for USERNAME / PASSWORD,
  which must be a single token (a USERNAME also ASCII-only), so a company
  name like ``固德威 Goodwe`` is not reported as a USERNAME.
- AGE must be a date/age expression, or a bare number next to an age marker
  (``35 岁`` / ``年龄 35`` / ``age 35``). A bare ``43`` is dropped.

Fail-open: an unexpected error keeps every span (recall over precision for
bugs in this layer). Pure and log-free apart from one debug count, so it is
safe to unit test without AWS.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Callable, Dict, List, Optional

from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan

logger = logging.getLogger(__name__)

#: Rule-name prefixes of the model sources this filter applies to.
MODEL_SOURCE_PREFIXES = ("guardrails:", "llm:")

#: Entity types that are configured upstream but are not PII for this
#: product, so their spans are always dropped.
NON_PII_ENTITY_TYPES = frozenset({"URL"})

#: Upper bound on matched-text length per entity type; `None` key = default.
MAX_SPAN_CHARS: Dict[Optional[str], int] = {
    "NAME": 30,
    "ADDRESS": 120,
    None: 64,
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_MAC_RE = re.compile(
    r"^(?:[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}"
    r"|[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4})$"
)
_PHONE_CHARS_RE = re.compile(r"^[\d\s+\-().]+$")
_DIGIT_GROUP_RE = re.compile(r"^[\d\s\-]+$")
_AWS_ACCESS_KEY_RE = re.compile(r"^(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}$")
_AWS_SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9/+=]{40}$")
_SWIFT_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9 ]{10,32}$")
_BARE_NUMBER_RE = re.compile(r"^\d{1,3}$")
#: A login name is one token of ASCII letters, digits, and . _ - @ +, with at
#: least one letter or digit. Guardrails labels Chinese company names such as
#: `固德威 Goodwe` as USERNAME; a CJK or space-separated value is never one.
_USERNAME_RE = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9._@+\-]{2,64}$")
#: A password is one token (no whitespace), 4-64 characters.
_PASSWORD_RE = re.compile(r"^\S{4,64}$")
_AGE_AFTER_RE = re.compile(r"^\s{0,2}(?:周岁|岁|years?\s+old|yrs?\b|y/o)", re.IGNORECASE)
_AGE_BEFORE_RE = re.compile(r"(?:年龄|年纪|age[ds]?|Age)\s*[:：]?\s*$", re.IGNORECASE)


def _digit_count(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


def _is_ip(value: str, _text: str, _start: int, _end: int) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _is_phone(value: str, _text: str, _start: int, _end: int) -> bool:
    return bool(_PHONE_CHARS_RE.match(value)) and 7 <= _digit_count(value) <= 15


def _digits_between(low: int, high: int) -> Callable[[str, str, int, int], bool]:
    def check(value: str, _text: str, _start: int, _end: int) -> bool:
        return bool(_DIGIT_GROUP_RE.match(value)) and low <= _digit_count(value) <= high

    return check


def _regex(pattern: "re.Pattern[str]") -> Callable[[str, str, int, int], bool]:
    def check(value: str, _text: str, _start: int, _end: int) -> bool:
        return bool(pattern.match(value.strip()))

    return check


def _is_age(value: str, text: str, start: int, end: int) -> bool:
    if not _BARE_NUMBER_RE.match(value.strip()):
        # Dates (1990年03月07日, 1990-03-07) and phrases (35岁, 35 years old)
        # carry their own marker; any non-digit content is accepted.
        return any(not ch.isdigit() and not ch.isspace() for ch in value)
    after = text[end : end + 12]
    before = text[max(0, start - 8) : start]
    return bool(_AGE_AFTER_RE.match(after) or _AGE_BEFORE_RE.search(before))


#: Format check per entity type: (value, unit_text, start, end) -> plausible.
_FORMAT_CHECKS: Dict[str, Callable[[str, str, int, int], bool]] = {
    "EMAIL": _regex(_EMAIL_RE),
    "IP_ADDRESS": _is_ip,
    "MAC_ADDRESS": _regex(_MAC_RE),
    "PHONE": _is_phone,
    "CREDIT_DEBIT_CARD_NUMBER": _digits_between(13, 19),
    "CREDIT_DEBIT_CARD_CVV": _digits_between(3, 4),
    "PIN": _digits_between(4, 12),
    "US_BANK_ACCOUNT_NUMBER": _digits_between(6, 17),
    "US_BANK_ROUTING_NUMBER": _digits_between(9, 9),
    "US_SOCIAL_SECURITY_NUMBER": _digits_between(9, 9),
    "AWS_ACCESS_KEY": _regex(_AWS_ACCESS_KEY_RE),
    "AWS_SECRET_KEY": _regex(_AWS_SECRET_KEY_RE),
    "SWIFT_CODE": _regex(_SWIFT_RE),
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER": _regex(_IBAN_RE),
    "AGE": _is_age,
    "USERNAME": _regex(_USERNAME_RE),
    "PASSWORD": _regex(_PASSWORD_RE),
}


def _entity_type(rule_name: str) -> Optional[str]:
    for prefix in MODEL_SOURCE_PREFIXES:
        if rule_name.startswith(prefix):
            return rule_name[len(prefix) :]
    return None


def is_plausible(entity_type: str, text: str, start: int, end: int) -> bool:
    """Whether `text[start:end]` can be an `entity_type` value."""
    value = text[start:end]
    if not value.strip():
        return False
    if entity_type in NON_PII_ENTITY_TYPES:
        return False
    max_chars = MAX_SPAN_CHARS.get(entity_type, MAX_SPAN_CHARS[None])
    if len(value) > max_chars:
        return False
    check = _FORMAT_CHECKS.get(entity_type)
    return check is None or check(value, text, start, end)


def _source_text(
    unit_ref: str,
    text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
    ocr_text_units: List[ExtractedTextUnit],
) -> Optional[str]:
    namespace, _, index_text = unit_ref.partition(":")
    try:
        index = int(index_text)
    except ValueError:
        return None
    collection = {"text": text_units, "ocr": ocr_text_units}.get(namespace)
    if collection is not None:
        return collection[index].text if 0 <= index < len(collection) else None
    if namespace == "image" and 0 <= index < len(image_regions):
        return image_regions[index].recognizedText
    return None


def filter_implausible_spans(
    spans: List[MatchSpan],
    text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
    ocr_text_units: Optional[List[ExtractedTextUnit]] = None,
) -> List[MatchSpan]:
    """Return `spans` without the model-source spans that fail `is_plausible`.

    Spans from any other source, and spans whose unit text cannot be
    resolved, are kept unchanged. Never raises: on an unexpected error the
    input list is returned as is.
    """
    try:
        kept: List[MatchSpan] = []
        for span in spans:
            entity_type = _entity_type(span.ruleName)
            if entity_type is None:
                kept.append(span)
                continue
            text = _source_text(span.unitRef, text_units, image_regions, ocr_text_units or [])
            if text is None or is_plausible(entity_type, text, span.startOffset, span.endOffset):
                kept.append(span)
        if len(kept) != len(spans):
            logger.debug("Dropped %d implausible model span(s)", len(spans) - len(kept))
        return kept
    except Exception:  # noqa: BLE001 - fail open, see module docstring
        logger.warning("Span plausibility filter failed; keeping all spans", exc_info=False)
        return spans
