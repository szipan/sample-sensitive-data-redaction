"""Shared Occurrence_Scan helper for locating detected PII match text inside
the exact string a detection source was given
(llm-cjk-pii-recall design.md "Component 1: `backend/agent/tools/pii_offsets.py`").

Both ML-based detection sources — Bedrock Guardrails
(`detect_pii_with_guardrails`) and the Bedrock foundation model
(`detect_pii_with_llm`) — report each detection as *matched text*, never as
character offsets. Each therefore recovers offsets by scanning the exact
string it sent to the service for every non-overlapping occurrence of that
matched text, then applying the digit-boundary rule below. The two tools must
produce **identical** offset ranges for the same
scanned string and matched text; that is only structurally guaranteed if the
logic is shared code, not two copies that a future edit could silently drift
apart. This module is that shared code.

Why the subtleties matter — each was arrived at from a specific observed
failure, and a divergent copy would reintroduce exactly one of these bugs in
exactly one source:

- **Scan for every occurrence, not just the first.** A single `str.find()`
  returns only the first hit, and for short match text that first hit is
  frequently the *wrong* one. Observed (`chi.docx`): Guardrails detects the
  CVV `456`, but the bank card number `6222021234567890123` earlier in the
  same text unit contains `456` as a substring, so a first-occurrence span
  was drawn inside the card number (invisibly, since the card number is
  separately redacted) and the real CVV survived unredacted.

- **The digit-boundary filter is scoped to all-ASCII-digit match text.**
  Emitting every occurrence unconditionally over-redacts visibly: a detected
  AGE `34` blacked out every `34` in the unit, including the ones inside the
  card number `6222021234567890123` and the licence plate `京A·12345`. The
  filter is scoped to all-digit match text because that is the only shape
  where "substring of a longer number" is a plausible false positive; emails,
  names, CJK text and anything containing separators (`123-45-6789`) keep the
  unconditional all-occurrences behaviour.

- **An occurrence is rejected when flanked by an ASCII digit on *either*
  side, not both.** `456` inside `6222021234567890123` is preceded by a digit
  (`...3|456|789...`), while `34` inside `京A·12345` is preceded by `2`; the
  real values (`CVV码456` at end-of-string, `今年34 岁`) are flanked by
  non-digits on both sides. Requiring digits on both sides would fail to
  reject the plate-embedded `34`.

- **When the filter rejects *every* occurrence, all of them are emitted
  anyway** (`fallback_applied=True`). A genuine PII value can legitimately sit
  flush against digits, and dropping the detection entirely would recreate the
  silent under-redaction this whole mechanism exists to eliminate. Failing
  toward over-redaction is the safe direction — a reviewer can see and reason
  about an unexpected black box, but cannot see PII that was never covered.

This module emits **no log records**. It is a pure helper so that it is
trivially property-testable and so each caller can log in its own vocabulary:
the Guardrails caller narrates `ApplyGuardrail`, the LLM caller narrates its
own model call. Coupling the logic to one caller's log wording is precisely
why it is being extracted rather than shared by importing the
logging-bearing function.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

__all__ = [
    "ASCII_DIGITS",
    "OccurrenceResolution",
    "find_occurrences",
    "is_all_ascii_digits",
    "is_digit_flanked",
    "resolve_match_occurrences",
]

#: ASCII digits, used for *both* halves of the digit-boundary rule (deciding
#: whether a match text is "entirely digits", and testing the characters
#: flanking an occurrence). ASCII-only is deliberate rather than
#: `str.isdigit()`: `str.isdigit()` is True for characters that are not
#: positional decimal digits at all (superscripts like "²") and for
#: full-width / other-script digit forms ("１", "٣"). The false-positive
#: mechanism being defended against is "a short run of digits happens to sit
#: inside a longer *number*", and the numbers in scope here (card numbers,
#: CVVs, phone numbers, IDs, licence plates) are written in ASCII digits. A
#: full-width digit run therefore falls outside the rule and keeps the
#: unconditional all-occurrences behavior, which errs toward over-redaction —
#: the safe direction.
ASCII_DIGITS = frozenset("0123456789")


@dataclass(frozen=True)
class OccurrenceResolution:
    """Where a detected match text sits in the text that was sent out.

    Attributes:
        occurrences: the `(start, end)` ranges to emit, ascending and
            non-overlapping. Each range slices back to the match text.
        total_found: the number of occurrences found *before* the
            digit-boundary filter was applied. Equal to `len(occurrences)`
            when the filter kept everything (or did not apply).
        fallback_applied: True only when the digit-boundary rule rejected
            *every* occurrence and all of them were emitted anyway. When True,
            `occurrences` holds all `total_found` ranges.
    """

    occurrences: List[Tuple[int, int]]
    total_found: int
    fallback_applied: bool


def is_all_ascii_digits(value: str) -> bool:
    """True when `value` is non-empty and made only of ASCII digits.

    Not `value.isdigit()`: see `ASCII_DIGITS` for why ASCII semantics are
    used here deliberately.
    """
    return bool(value) and all(char in ASCII_DIGITS for char in value)


def find_occurrences(text: str, match_text: str) -> List[Tuple[int, int]]:
    """Every `(start, end)` non-overlapping occurrence of `match_text` in
    `text`, in ascending order. Each hit advances the cursor past its own end
    so returned ranges never overlap (match `"aa"` in `"aaaa"` yields
    `(0, 2), (2, 4)`)."""
    occurrences: List[Tuple[int, int]] = []
    position = 0
    while True:
        start = text.find(match_text, position)
        if start == -1:
            return occurrences
        end = start + len(match_text)
        occurrences.append((start, end))
        position = end


def is_digit_flanked(text: str, start: int, end: int) -> bool:
    """True when the occurrence `text[start:end]` has an ASCII digit
    immediately before it **or** immediately after it.

    Either side is enough: a short digit run sitting inside a longer number
    is a false positive regardless of which end it touches. `456` at index 9
    of `6222021234567890123` is preceded by `3`; `34` at index 2 of the
    `12345` in `京A·12345` is preceded by `2`; the real `456` in `CVV码456`
    ends the string and is preceded by `码`, and the real `34` in `今年34 岁`
    is preceded by `年` and followed by a space — so only the decoys are
    rejected. Positions outside the string count as non-digits, which is what
    makes an end-of-string value acceptable.
    """
    if start > 0 and text[start - 1] in ASCII_DIGITS:
        return True
    return end < len(text) and text[end] in ASCII_DIGITS


def resolve_match_occurrences(text: str, match_text: str) -> OccurrenceResolution:
    """Every non-overlapping occurrence of `match_text` in `text`, filtered by
    the digit-boundary rule when `match_text` is entirely ASCII digits, with
    the emit-all fallback when that rule rejects every occurrence.

    Behaviour, mirroring `guardrails_pii_detect._spans_for_text` exactly:

    - Scan `text` for every non-overlapping occurrence of `match_text`
      (`find_occurrences`). `total_found` records this count.
    - When `match_text` is entirely ASCII digits (`is_all_ascii_digits`),
      reject any occurrence flanked by an ASCII digit on *either* side
      (`is_digit_flanked`). Non-all-digit match text keeps every occurrence.
    - If that filter leaves at least one occurrence, emit the kept set with
      `fallback_applied=False`.
    - If that filter rejects *every* occurrence, emit all of them anyway with
      `fallback_applied=True` — the mandatory safety fallback, so a value
      sitting flush against digits is never silently dropped.

    When `match_text` does not occur in `text` at all, returns an empty
    resolution (`occurrences=[]`, `total_found=0`, `fallback_applied=False`);
    the caller is responsible for skipping such a detection.

    Nothing is ever capped: every surviving occurrence is emitted. This
    guarantees every emitted range satisfies
    ``0 <= start < end <= len(text)`` and ``text[start:end] == match_text``.
    """
    occurrences = find_occurrences(text, match_text)
    total_found = len(occurrences)

    if not occurrences:
        return OccurrenceResolution(
            occurrences=[], total_found=0, fallback_applied=False
        )

    if is_all_ascii_digits(match_text):
        kept = [
            (start, end)
            for start, end in occurrences
            if not is_digit_flanked(text, start, end)
        ]
        if kept:
            return OccurrenceResolution(
                occurrences=kept,
                total_found=total_found,
                fallback_applied=False,
            )
        # Mandatory safety fallback: a genuine value can sit flush against
        # digits, and emitting nothing would silently leave PII in the output.
        return OccurrenceResolution(
            occurrences=occurrences,
            total_found=total_found,
            fallback_applied=True,
        )

    return OccurrenceResolution(
        occurrences=occurrences,
        total_found=total_found,
        fallback_applied=False,
    )
