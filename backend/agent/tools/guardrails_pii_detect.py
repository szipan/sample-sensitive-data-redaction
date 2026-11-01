"""`detect_pii_with_guardrails` Agent tool.

Supplements the regex-based `match_rules` tool (`matching.py`) with ML-based
PII detection from Amazon Bedrock Guardrails' Sensitive Information Filters,
via the `bedrock-runtime` `ApplyGuardrail` API. Both detection sources
produce `MatchSpan`s in the same shape and `unitRef` namespace, so the
orchestration layer can simply concatenate them and re-merge overlaps with
`merge_spans_within_unit`.

Requirements covered:
    - 2.1: `ApplyGuardrail` is called once per non-empty text unit /
      OCR-recognized image region.
    - 2.2: each returned PII detection becomes a `MatchSpan` with correct
      `startOffset`, `endOffset`, `unitRef` and `zone`.
    - 2.3: `ruleName` is `f"guardrails:{PII_TYPE}"` (e.g.
      `"guardrails:NAME"`), so a merged span's `mergedFromRuleNames` makes
      it obvious which contributions came from Guardrails vs. from a
      user-defined regex rule.
    - 2.4: any API error (after retries) is swallowed — the tool logs and
      returns whatever spans it managed to collect, never raising into the
      pipeline.
    - 2.5: empty text units are skipped entirely (no API call, no span).
    - 2.6: both `ExtractedTextUnit` and `ImageRegion.recognizedText` are
      processed, reusing `matching.py`'s `unitRef` scheme (`"text:{index}"`
      / `"image:{index}"`).
    - 5.2: throttling / transient service errors are retried with
      exponential backoff, up to `MAX_RETRIES` attempts per text unit.
    - 5.3: missing `GUARDRAIL_ID` / `GUARDRAIL_VERSION` env vars log a
      warning and yield an empty result (the feature simply stays dormant
      until the CDK stack provisions and injects them).

Requirements covered from pdf-dual-path-pii-detection design.md
("Component 3: `guardrails_pii_detect.py` — accept OCR units"):
    - 1.8: `ApplyGuardrail` is called once per OCR_Text_Unit whose text
      contains at least one character (and not at all for empty ones),
      using the third `unitRef` namespace `"ocr:{index}"`.
    - 3.7: a Guardrails failure never fails the Redaction Job — the
      never-raise / partial-results posture below extends unchanged to the
      OCR collection.

Interface decisions (documented here since they affect callers/tests):

1. Configuration by environment, not by argument: the Guardrail identifier
   and version are provisioned by CDK and injected into the AgentCore
   Runtime as `GUARDRAIL_ID` / `GUARDRAIL_VERSION`, so
   `detect_pii_with_guardrails` reads them itself rather than making every
   caller thread them through. This keeps the call site in
   `orchestration.py` symmetric with `match_rules(...)` and means the
   feature is entirely backend-configured.

2. Offsets recovered by scanning for every occurrence, with a digit-boundary
   filter: `ApplyGuardrail` reports each detection as the *matched text*
   (`{"type": ..., "match": ...}`), not as character offsets into the input.
   Offsets are therefore recovered by locating the match text inside the
   exact string that was sent to the API. A single `str.find()` is not
   enough: it returns only the first occurrence, and for short match text
   that first occurrence is frequently the *wrong* one. Observed failure
   (`chi.docx`): Guardrails detects the CVV `456`, but the bank card number
   `6222021234567890123` earlier in the same text unit contains `456` as a
   substring, so the span was drawn inside the card number (invisibly, since
   the card number is separately redacted) and the real CVV survived
   unredacted. `_spans_for_text` therefore scans for *every non-overlapping*
   occurrence of the match text rather than just the first.

   Emitting all of them unconditionally, however, over-redacts visibly: a
   detected AGE `34` in `chi.docx` blacked out every `34` in the unit,
   including the ones inside the card number `6222021234567890123` and the
   licence plate `京A·12345`. Both problems are solved by one rule —
   **when the match text is entirely ASCII digits, an occurrence is rejected
   if it is flanked by an ASCII digit on *either* side** (the character
   immediately before it is a digit, or the one immediately after it is).
   Either side, not both: `456` inside `6222021234567890123` is only
   preceded by a digit (`...3|456|789...` — both, in fact), while `34` inside
   `京A·12345` is preceded by `2`, and the real values (`CVV码456` at
   end-of-string, `今年34 岁`) are flanked by non-digits on both sides. The
   filter is scoped to all-digit match text because that is the only shape
   where "substring of a longer number" is a plausible false positive;
   emails, names, CJK text and anything containing separators (`123-45-6789`)
   keep the unconditional all-occurrences behavior.

   If the filter rejects *every* occurrence, all of them are emitted anyway
   (`_spans_for_text`'s fallback): a genuine PII value can legitimately sit
   flush against digits, and dropping the detection entirely would recreate
   the silent under-redaction this whole mechanism exists to eliminate.
   Failing toward over-redaction is the safe direction — a reviewer can see
   and reason about an unexpected black box, but cannot see PII that was
   never covered. Occurrences are likewise never capped, but an unusually
   high *emitted* count is logged at warning level
   (`OCCURRENCE_WARN_THRESHOLD`) so broad over-redaction stays observable,
   and the fallback path logs distinguishably from a filter that merely
   narrowed the set. Downstream `merge_spans_within_unit` collapses
   overlapping/adjacent spans sharing a `unitRef`, so redundant occurrences
   cost nothing. If the match text cannot be found at all (`find()` returns
   -1 on the first attempt), the detection is skipped defensively rather
   than turned into a bogus span.

3. Never raising: this is a *supplementary* detection layer, so a
   Guardrails failure must never fail a Redaction Job. Every failure mode —
   missing env vars, client construction
   failure, per-unit API failure, malformed response — is caught here and
   degrades to fewer (or zero) spans. `orchestration.py` still wraps the
   call in its own try/except as belt-and-braces, per design.md.

4. Partial results are kept: a failure while processing one text unit does
   not discard spans already collected from earlier units. Discarding them
   would only reduce redaction coverage for no benefit.

5. Three unit collections, one loop shape: `ocr_text_units` is an optional
   third collection appended *last* in the signature, so every existing
   call site keeps working unchanged (`.docx` jobs simply never pass it).
   It is iterated after `text_units` and `image_regions` and inside the
   same `try`, so decision 4's partial-results behavior covers it too, and
   it emits `unitRef=f"ocr:{index}"` — a namespace `merge_spans_within_unit`
   and `_regroup_and_merge` already handle, since they group on raw
   `unitRef` equality. Unlike the image-region branch, which hardcodes
   `zone="image"` because `ImageRegion.zone` is a fixed `Literal`, OCR
   spans take `zone=unit.zone` (i.e. `"ocr"`), which `ocr_pdf_pages` sets.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from agent.logging_config import describe_exception
from agent.tools.pii_offsets import (
    ASCII_DIGITS,
    find_occurrences,
    is_all_ascii_digits,
    is_digit_flanked,
    resolve_match_occurrences,
)
from shared.models import ExtractedTextUnit, ImageRegion, MatchSpan

logger = logging.getLogger(__name__)

#: Max concurrent per-unit ApplyGuardrail calls.
GUARDRAILS_MAX_WORKERS = int(os.environ.get("GUARDRAILS_MAX_WORKERS", "8"))

__all__ = ["detect_pii_with_guardrails"]

GUARDRAIL_ID_ENV_VAR = "GUARDRAIL_ID"
GUARDRAIL_VERSION_ENV_VAR = "GUARDRAIL_VERSION"

#: Total attempts per `ApplyGuardrail` call (1 initial + 2 retries).
MAX_RETRIES = 3

#: Backoff before retry attempt *n* is ``RETRY_BASE_DELAY_SECONDS * 2 ** n``,
#: i.e. 1s, 2s, 4s...
RETRY_BASE_DELAY_SECONDS = 1.0

#: `botocore` error codes treated as transient and therefore retryable.
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "InternalFailure",
        "ModelTimeoutException",
    }
)

_RULE_NAME_PREFIX = "guardrails:"
_IMAGE_ZONE = "image"

#: Emitted-span count above which a single detection resolving to that many
#: places in one text unit is logged at warning level. The count compared
#: against this threshold is the number of spans actually emitted (i.e. after
#: the digit-boundary filter of `_spans_for_text`, or the full set when the
#: fallback fires), not the raw occurrence count — the warning exists to
#: surface *realized* over-redaction. Nothing is ever capped: capping would
#: silently reintroduce the under-redaction this scan exists to fix. The
#: count is still worth surfacing, because a genuine PII value repeats a
#: handful of times at most within one paragraph/table cell, so a much larger
#: number means the match text is short enough to be hitting incidental
#: substrings that the boundary rule could not exclude. 20 sits comfortably
#: above normal repetition and far below the hundreds of hits a pathological
#: one-character match would produce. Boundary filtering should make counts
#: this high rare, which is exactly why they are worth a warning.
OCCURRENCE_WARN_THRESHOLD = 20

#: The Occurrence_Scan primitives now live in `agent.tools.pii_offsets`, a
#: pure, log-free helper shared with `detect_pii_with_llm` so both detection
#: sources recover offsets through *identical* code rather than two copies
#: that could silently drift apart. The old private names are re-exported as
#: module-level aliases below so any existing importer keeps resolving, and
#: `_spans_for_text` drives its logging from the returned
#: `OccurrenceResolution` instead of recomputing the scan here. See
#: `pii_offsets` for the full rationale on the all-digit scoping, the
#: either-side rejection, and the mandatory emit-all fallback.
_ASCII_DIGITS = ASCII_DIGITS
_is_all_ascii_digits = is_all_ascii_digits
_find_occurrences = find_occurrences
_is_digit_flanked = is_digit_flanked

# Lazily-initialized so a single client is reused across invocations within
# the same Agent Runtime process, consistent with the lazy-client pattern
# used elsewhere in this backend (e.g. `agent/tools/ocr.py`,
# `lambdas/job_dispatcher.py`).
_bedrock_runtime_client = None


def _get_bedrock_runtime_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        import boto3

        _bedrock_runtime_client = boto3.client("bedrock-runtime")
    return _bedrock_runtime_client


def _text_unit_ref(index: int) -> str:
    return f"text:{index}"


def _image_region_ref(index: int) -> str:
    return f"image:{index}"


def _ocr_unit_ref(index: int) -> str:
    return f"ocr:{index}"


def _is_retryable(error: Exception) -> bool:
    """Return True for throttling and 5xx service errors.

    Reads `botocore`'s `ClientError.response` shape duck-typed (via
    `getattr`) rather than importing `botocore.exceptions`, so this module
    stays importable — and unit-testable with a plain fake client — without
    boto3/botocore installed.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False

    error_code = (response.get("Error") or {}).get("Code")
    if error_code in _RETRYABLE_ERROR_CODES:
        return True

    status_code = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return isinstance(status_code, int) and status_code >= 500


def _extract_pii_entities(response: dict) -> List[dict]:
    """Collect every `piiEntities[]` entry across all `assessments[]` of an
    `ApplyGuardrail` response, keeping only entries that carry both a
    `"type"` and a non-empty `"match"` (anything else cannot be turned into
    a span)."""
    entities: List[dict] = []
    for assessment in response.get("assessments") or []:
        sensitive_policy = assessment.get("sensitiveInformationPolicy") or {}
        for entity in sensitive_policy.get("piiEntities") or []:
            if entity.get("type") and entity.get("match"):
                entities.append(entity)
    return entities


def _call_apply_guardrail(
    client, guardrail_id: str, guardrail_version: str, text_content: str
) -> List[dict]:
    """Call `ApplyGuardrail` for `text_content` and return its detected PII
    entity dicts (each with `"type"` and `"match"`).

    Retries transient failures (throttling, 5xx) up to `MAX_RETRIES` total
    attempts with exponential backoff. Returns an empty list — never
    raises — when every attempt fails, when the error is not retryable, or
    when the response simply contains no PII (design.md "Error Scenario 5":
    no PII is normal operation, not an error).
    """
    for attempt in range(MAX_RETRIES):
        try:
            response = client.apply_guardrail(
                guardrailIdentifier=guardrail_id,
                guardrailVersion=guardrail_version,
                source="INPUT",
                content=[{"text": {"text": text_content}}],
            )
        except Exception as error:  # noqa: BLE001 - see module docstring, decision 3
            is_last_attempt = attempt == MAX_RETRIES - 1
            if not _is_retryable(error) or is_last_attempt:
                # `describe_exception`, not `error` — do not unwrap this as
                # noise. This call's request body *is* the document text
                # (`content[0].text.text`), so a `botocore`
                # `ParamValidationError` quoting the offending parameter
                # echoes the whole document back through `str(exc)`, and
                # interpolating the raw error would put it in the record
                # (see `describe_exception`). The sanitizer keeps
                # the diagnostically load-bearing part — a
                # `ThrottlingException` still reads as one — and discards the
                # free text around it.
                logger.warning(
                    "ApplyGuardrail failed on attempt %d/%d; skipping this text unit: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    describe_exception(error),
                )
                return []
            time.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** attempt))
            continue

        try:
            return _extract_pii_entities(response)
        except Exception:  # noqa: BLE001 - malformed/unexpected response shape
            logger.warning(
                "Could not parse ApplyGuardrail response; skipping this text unit",
                exc_info=True,
            )
            return []

    return []


def _spans_for_text(
    client,
    guardrail_id: str,
    guardrail_version: str,
    zone,
    unit_ref: str,
    text: str,
) -> List[MatchSpan]:
    """Detect PII in a single `text` and convert each detection into
    `MatchSpan`s for `unit_ref`.

    `ApplyGuardrail` reports matched *text*, not offsets, so each detection
    is resolved by scanning `text` for **every** non-overlapping occurrence
    of the match text — not just the first (module docstring, decision 2).
    Resolving only the first occurrence silently mis-places short matches: a
    detected 3-digit CVV `456` resolves inside an earlier bank card number
    `6222021234567890123`, leaving the real CVV unredacted in the output.

    Occurrences are then filtered by a **digit-boundary rule, applied only
    when the match text is entirely ASCII digits** (`_is_all_ascii_digits`):
    an occurrence is rejected when an ASCII digit sits immediately before it
    **or** immediately after it (either side, not both — see
    `_is_digit_flanked`). That single rule removes both failure modes: the
    `456` decoy inside the card number is rejected while the real `CVV码456`
    is kept, and a detected AGE `34` no longer blacks out the `34` inside
    `6222021234567890123` or inside the plate `京A·12345` while still
    covering `今年34 岁`. Match text that is *not* all ASCII digits (emails,
    names, CJK, anything with separators like `123-45-6789`) keeps the
    unfiltered all-occurrences behavior: the "substring of a longer number"
    false-positive mechanism does not apply to it.

    If the filter rejects **every** occurrence, all of them are emitted
    instead. A genuine PII value can legitimately sit flush against digits,
    and dropping the detection entirely would recreate the silent
    under-redaction this scan exists to eliminate; failing toward
    over-redaction is the safe direction for this product — a reviewer can
    see and reason about an unexpected black box, but cannot see PII that was
    never covered. The fallback is logged distinguishably from an ordinary
    narrowing, so an operator can tell "kept 1 of 3" from "rejected all 3,
    emitting all 3".

    Nothing is ever capped; an unusually high *emitted* count is only logged
    (`OCCURRENCE_WARN_THRESHOLD`).

    Detections whose `"match"` text cannot be located in `text` at all are
    skipped (logged at debug). Together with the scan, this guarantees every
    returned span satisfies ``0 <= startOffset < endOffset <= len(text)``.
    """
    spans: List[MatchSpan] = []
    for entity in _call_apply_guardrail(client, guardrail_id, guardrail_version, text):
        match_text = entity["match"]
        if not match_text:
            # `_extract_pii_entities` already filters these out; belt-and-braces
            # so the scan below can never make zero progress.
            continue

        pii_type = entity["type"]
        # Offsets are recovered by the shared, log-free Occurrence_Scan
        # (`resolve_match_occurrences`), so this tool and `detect_pii_with_llm`
        # locate a match identically. The logging below is
        # driven from the returned resolution rather than recomputing the scan.
        resolution = resolve_match_occurrences(text, match_text)
        if not resolution.occurrences:
            logger.debug(
                "ApplyGuardrail returned a %s match not present in %s; skipping",
                pii_type,
                unit_ref,
            )
            continue

        emitted = resolution.occurrences
        if resolution.fallback_applied:
            # Mandatory safety fallback: a genuine value can sit flush
            # against digits, and emitting nothing would silently leave
            # PII in the output. Distinct wording from the branch below so
            # logs distinguish "kept some" from "kept none, emitting all".
            logger.warning(
                "Digit-boundary filter rejected all %d occurrences of the "
                "%s match in %s; emitting all %d as a fallback rather than "
                "risk leaving the value unredacted",
                resolution.total_found,
                pii_type,
                unit_ref,
                resolution.total_found,
            )
        elif len(emitted) < resolution.total_found:
            logger.debug(
                "Digit-boundary filter kept %d of %d occurrences of the "
                "%s match in %s (rejected ones are flanked by a digit)",
                len(emitted),
                resolution.total_found,
                pii_type,
                unit_ref,
            )

        rule_name = f"{_RULE_NAME_PREFIX}{pii_type}"
        for start, end in emitted:
            spans.append(
                MatchSpan(
                    zone=zone,
                    unitRef=unit_ref,
                    startOffset=start,
                    endOffset=end,
                    ruleName=rule_name,
                )
            )

        if len(emitted) > OCCURRENCE_WARN_THRESHOLD:
            logger.warning(
                "ApplyGuardrail %s match resolved to %d emitted occurrences in "
                "%s; redacting all of them (short match text may be "
                "over-redacting)",
                pii_type,
                len(emitted),
                unit_ref,
            )
    return spans


def _read_guardrail_config() -> Optional[tuple]:
    """Return `(guardrail_id, guardrail_version)` from the environment, or
    `None` (having logged a warning) when either is missing or blank
    ."""
    guardrail_id = (os.environ.get(GUARDRAIL_ID_ENV_VAR) or "").strip()
    guardrail_version = (os.environ.get(GUARDRAIL_VERSION_ENV_VAR) or "").strip()

    if not guardrail_id or not guardrail_version:
        logger.warning(
            "%s/%s not set; skipping Bedrock Guardrails PII detection",
            GUARDRAIL_ID_ENV_VAR,
            GUARDRAIL_VERSION_ENV_VAR,
        )
        return None

    return guardrail_id, guardrail_version


def detect_pii_with_guardrails(
    text_units: List[ExtractedTextUnit],
    image_regions: List[ImageRegion],
    ocr_text_units: Optional[List[ExtractedTextUnit]] = None,
) -> List[MatchSpan]:
    """Detect PII entities in extracted document text via Bedrock Guardrails'
    `ApplyGuardrail` API.

    Args:
        text_units: body/table/header/footer text, as produced by
            `extract_document_content`. Units whose
            `text` is empty are skipped without an API call.
        image_regions: image regions as produced by
            `extract_document_content` and populated by
            `recognize_image_text`. Regions whose
            `recognizedText` is `None` or empty are skipped.
        ocr_text_units: OCR_Text_Units as produced by `ocr_pdf_pages` — one
            `zone="ocr"` unit per PDF page. Optional and last, so `.docx`
            jobs and every pre-existing call site can omit it; `None` is
            treated as an empty collection. Units whose `text` is empty are
            skipped without an API call, exactly as for `text_units`.

    Returns:
        One `MatchSpan` per non-overlapping occurrence of each PII
        detection's match text that survives the digit-boundary filter (see
        `_spans_for_text`), unmerged, using the
        same `unitRef` scheme as `match_rules` (`"text:{index}"` /
        `"image:{index}"` / `"ocr:{index}"`) so all sources can be
        concatenated and merged together by the orchestration layer. Every
        span's `ruleName` is `f"guardrails:{PII_TYPE}"`.

        Returns an empty list when the Guardrail environment variables are
        missing, when the `bedrock-runtime` client cannot be created, or
        when no PII was detected. This function never raises: per-unit API
        failures only cost that unit's spans, and any unexpected error
        degrades to the spans collected so far.
    """
    config = _read_guardrail_config()
    if config is None:
        return []
    guardrail_id, guardrail_version = config

    try:
        client = _get_bedrock_runtime_client()
    except Exception:  # noqa: BLE001 - e.g. no credentials/region configured
        logger.warning(
            "Could not create bedrock-runtime client; "
            "skipping Bedrock Guardrails PII detection",
            exc_info=True,
        )
        return []

    spans: List[MatchSpan] = []
    try:
        # Independent ApplyGuardrail calls run concurrently; `pool.map` keeps
        # the original (text, image, ocr) ordering of the results.
        work = []
        for index, unit in enumerate(text_units):
            if unit.text:
                work.append((unit.zone, _text_unit_ref(index), unit.text))
        for index, region in enumerate(image_regions):
            if region.recognizedText:
                work.append((_IMAGE_ZONE, _image_region_ref(index), region.recognizedText))
        for index, unit in enumerate(ocr_text_units or []):
            if unit.text:
                work.append((unit.zone, _ocr_unit_ref(index), unit.text))

        def _one(item):
            zone, unit_ref, text = item
            try:
                return _spans_for_text(
                    client,
                    guardrail_id,
                    guardrail_version,
                    zone=zone,
                    unit_ref=unit_ref,
                    text=text,
                )
            except Exception:  # noqa: BLE001 - a unit failure costs only its spans
                logger.warning(
                    "Guardrails PII detection failed for %s; skipping unit",
                    unit_ref,
                    exc_info=True,
                )
                return []

        with ThreadPoolExecutor(max_workers=GUARDRAILS_MAX_WORKERS) as pool:
            for unit_spans in pool.map(_one, work):
                spans.extend(unit_spans)
    except Exception:  # noqa: BLE001 - see module docstring, decisions 3 and 4
        logger.warning(
            "Unexpected error during Bedrock Guardrails PII detection; "
            "returning %d span(s) collected so far",
            len(spans),
            exc_info=True,
        )

    return spans
