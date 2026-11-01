"""Shared data models for the PII Document Redaction POC backend.

These dataclasses define the shapes exchanged between `infra/` (implicitly,
via the DynamoDB table schemas), `lambdas/`, and `agent/`. This module is
intentionally dependency-free (no boto3/AWS SDK imports): it is a pure data
model + validation module, safe to import from any backend package without
pulling in AWS clients.

Persisted models (map 1:1 to DynamoDB tables introduced in later tasks):
- `RedactionJob`      -> `RedactionJobs` table
- `RedactionRule`     -> `RedactionRules` table
- `RuleOperation`     -> `RuleOperations` table

In-memory-only models (never persisted; passed between Agent tools during a
single Redaction Job's processing):
- `ExtractedTextUnit`
- `OcrWord`
- `ImageRegion`
- `MatchSpan`
"""
from dataclasses import dataclass
from typing import List, Literal, Optional

# ---------------------------------------------------------------------------
# Length constraints
# ---------------------------------------------------------------------------

NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 100
PATTERN_MIN_LENGTH = 1
PATTERN_MAX_LENGTH = 500


def is_valid_name_length(name: str) -> bool:
    """Return True if `name` satisfies the Redaction Rule name length bound.

    A valid name has between `NAME_MIN_LENGTH` and `NAME_MAX_LENGTH`
    characters, inclusive.
    """
    return NAME_MIN_LENGTH <= len(name) <= NAME_MAX_LENGTH


def is_valid_pattern_length(pattern: str) -> bool:
    """Return True if `pattern` satisfies the Redaction Rule pattern length bound.

    A valid pattern has between `PATTERN_MIN_LENGTH` and `PATTERN_MAX_LENGTH`
    characters, inclusive.
    """
    return PATTERN_MIN_LENGTH <= len(pattern) <= PATTERN_MAX_LENGTH


# ---------------------------------------------------------------------------
# RedactionJob
# ---------------------------------------------------------------------------

JobStatus = Literal["processing", "detected", "completed", "failed"]

#: The processing workflow mode for a Redaction Job. `"auto"` runs the full
#: pipeline without user interaction (the default and pre-feature behavior);
#: `"review"` pauses after PII detection so the user can choose which values
#: to redact before proceeding.
JobMode = Literal["auto", "review"]
JOB_MODE_DEFAULT: JobMode = "auto"
SourceExtension = Literal[".docx", ".pdf"]

#: Which detection source(s) are allowed to contribute a given entity type's
#: spans for a Job (detection-source-switch feature). `"both"` keeps every
#: source's spans (the default and pre-feature behavior); `"llm"` keeps only
#: the LLM foundation-model source's spans and drops Guardrails' spans;
#: `"guardrails"` keeps only Guardrails' spans. This is the shared policy type
#: for the per-entity switches below (ADDRESS and NAME); each switch is scoped
#: to its ONE entity type only — every other PII type is unaffected.
DetectionSource = Literal["both", "llm", "guardrails"]
DETECTION_SOURCE_DEFAULT: DetectionSource = "both"
DETECTION_SOURCE_VALUES = ("both", "llm", "guardrails")

#: Back-compat aliases (the switch first shipped for ADDRESS only).
AddressDetectionSource = DetectionSource
ADDRESS_DETECTION_SOURCE_DEFAULT: DetectionSource = DETECTION_SOURCE_DEFAULT
ADDRESS_DETECTION_SOURCE_VALUES = DETECTION_SOURCE_VALUES


@dataclass
class RedactionJob:
    """A single document redaction job (DynamoDB table `RedactionJobs`).

    State machine: `processing -> completed` or `processing -> failed`.
    `completed` and `failed` are terminal; no other transitions occur.

    `detectionSummary` (dual-path PDF detection additive extension): the
    diagnostic counts recorded on the `completed` transition for `.pdf` jobs
    processed through both detection paths — how many redaction rectangles
    came from the text layer, how many from the OCR path, how many the two
    paths agreed on, plus a per-page character-count health signal. It exists
    so a reviewer can see *whether the second path mattered* for a given
    document (an OCR-only region is PII the text layer lost) without
    re-running the job. The health signal is written here and deliberately
    never read by the pipeline: routing is unconditional, because a
    heuristic false negative would silently reintroduce the blind spot this
    feature removes.

    The llm-cjk-pii-recall feature adds a third detection source (a Bedrock
    foundation model) and two further keys: `llmSpans` (the number of LLM
    spans produced before merging) and `llmOnlySpans` (the number of merged
    spans no other source contributed to). These two LLM keys are *always
    present* whenever a summary is recorded, for both `.pdf` and `.docx`
    jobs, because the LLM source runs for both extensions. The four
    dual-path keys above, by contrast, are *absent rather than zero* for
    `.docx`: a `.docx` has no text-layer-versus-OCR distinction, so a `0`
    there would falsely read as "both paths ran and found nothing" — the
    same false-zero trap that kept the dual-path summary off `.docx`
    originally. A consumer must therefore treat the four dual-path keys as
    optional (present only for `.pdf`) while relying on the two LLM keys
    being present for either extension. A summary is now recorded on the
    `completed` transition for both `.pdf` and `.docx`; it is still left
    `None` for `failed` jobs and for any job recorded before this field
    existed. Stored as a plain `dict`
    (JSON-shaped: only `int`/`str`/`list`/`dict`) rather than a dataclass
    because it is written straight into DynamoDB and served straight out of
    `GET /jobs/{jobId}`, with no code branching on its contents. Optional and
    defaulted, so this is backward-compatible with every existing caller that
    constructs a `RedactionJob` without it.
    """

    jobId: str
    status: JobStatus
    sourceKey: str
    sourceExtension: SourceExtension
    fileName: str
    createdAt: str
    updatedAt: str
    redactedKey: Optional[str] = None
    errorMessage: Optional[str] = None
    detectionSummary: Optional[dict] = None
    #: The caller-selected ADDRESS detection source policy for this Job
    #: (see `DetectionSource`). Defaults to `"both"` so a job record written
    #: before this field existed, or by a client that does not send the field,
    #: behaves exactly as before (every source's ADDRESS spans are kept).
    addressDetectionSource: DetectionSource = DETECTION_SOURCE_DEFAULT
    #: The caller-selected NAME detection source policy for this Job
    #: (see `DetectionSource`). Defaults to `"both"`. Useful because Guardrails
    #: can over-trigger NAME on short CJK words (e.g. “照片”/photo on an ID
    #: card); selecting `"llm"` drops Guardrails' NAME spans while keeping the
    #: LLM's. Scoped to the NAME entity only.
    nameDetectionSource: DetectionSource = DETECTION_SOURCE_DEFAULT
    #: The processing workflow mode for this Job (see `JobMode`). Defaults to
    #: `"auto"` so a job record written before this field existed, or by a
    #: client that does not send the field, behaves exactly as before (full
    #: auto pipeline). `"review"` pauses after detection so the user can
    #: confirm which PII values to redact.
    mode: JobMode = JOB_MODE_DEFAULT


# ---------------------------------------------------------------------------
# RedactionRule
# ---------------------------------------------------------------------------


@dataclass
class RedactionRule:
    """A global PII matching rule (DynamoDB table `RedactionRules`).

    `ruleName` is the table's primary key and is compared case-sensitively
    for uniqueness. `isBuiltIn` rules cannot be deleted.
    """

    ruleName: str
    pattern: str
    enabled: bool
    isBuiltIn: bool
    createdAt: str
    updatedAt: str


# ---------------------------------------------------------------------------
# RuleOperation
# ---------------------------------------------------------------------------

RuleOperationType = Literal["create", "enable", "disable", "delete"]
RuleOperationStatus = Literal["queued", "processing", "applied", "rejected"]
RuleOperationErrorReason = Literal[
    "DUPLICATE_NAME",
    "INVALID_PATTERN",
    "UNKNOWN_RULE_NAME",
    "BUILT_IN_NOT_DELETABLE",
    "INVALID_LENGTH",
]


@dataclass
class RuleOperation:
    """A requested change to the Redaction Rule collection.

    (DynamoDB table `RuleOperations`.) `pattern` is only populated for
    `type == "create"`. `errorReason` is only populated once `status`
    reaches the terminal `rejected` state; `queued`/`processing` are
    non-terminal, `applied`/`rejected` are terminal.
    """

    operationId: str
    type: RuleOperationType
    targetRuleName: str
    status: RuleOperationStatus
    createdAt: str
    updatedAt: str
    pattern: Optional[str] = None
    errorReason: Optional[RuleOperationErrorReason] = None


# ---------------------------------------------------------------------------
# In-memory-only models used during Agent processing (never persisted)
# ---------------------------------------------------------------------------

# `"ocr"` (dual-path PDF detection additive extension) marks text that came
# from rasterizing a whole PDF page and running Amazon Textract over it,
# rather than from the document's own text layer. It is neither a structural
# document zone (`body`/`table`/`header`/`footer`, which describe *where in
# the document* text was found) nor an embedded image region (`image`, which
# describes one picture inside a document): it describes *how the text was
# obtained*. Keeping it a distinct zone value is what lets the redactor tell
# the two coordinate spaces apart — see `OcrWord.boundingBox`.
TextUnitZone = Literal["body", "table", "header", "footer", "ocr"]
MatchZone = Literal["body", "table", "header", "footer", "image", "ocr"]


@dataclass
class TextPosition:
    """Position of an `ExtractedTextUnit` within the source document.

    For `zone == "table"`, `tableIndex`/`rowIndex`/`colIndex` identify the
    cell the text came from. For other zones (`body`/`header`/`footer`),
    only `index` (e.g. paragraph index within that zone) is populated.

    `pageIndex` (additive extension): the 0-based source-document
    page the text unit came from, when the source format has a fixed page
    concept. Populated by the `.pdf` extractor (`extract_pdf_content`)
    so the PDF redaction tool can look up which
    `fitz.Page` a matched text unit lives on without re-parsing the
    document. Left `None` for `.docx` extraction, which has no
    fixed page concept at the XML level (see `extraction_docx.py`'s
    `DocumentStructureMetadata.pageCount` docstring). Optional and
    defaulted, so this is backward-compatible with every existing caller
    that constructs a `TextPosition` without it.
    """

    index: Optional[int] = None
    tableIndex: Optional[int] = None
    rowIndex: Optional[int] = None
    colIndex: Optional[int] = None
    pageIndex: Optional[int] = None


@dataclass
class BoundingBox:
    """A rectangular region within a page or image, in the source document's coordinate space."""

    left: float
    top: float
    width: float
    height: float


@dataclass
class OcrWord:
    """One Amazon Textract `WORD` block, located both in the OCR text and on the page.

    `startOffset`/`endOffset` are character offsets into the owning
    `ExtractedTextUnit.text` (half-open, so `text[startOffset:endOffset]`
    is exactly `text`), which is how a `MatchSpan` found in OCR text is
    resolved back to a rectangle: every word whose offset range intersects
    the span's range contributes its bounding box.

    WARNING - `boundingBox` is the one `BoundingBox` in this module that is
    NOT in source-document coordinate space. Textract reports geometry
    NORMALIZED to `[0, 1]` relative to the rasterized page image it was
    given, so these are fractions of the page's width/height, not points.
    Every other `BoundingBox` here (`ExtractedTextUnit.boundingBox`,
    `ImageRegion.boundingBox`) is already in the source document's own
    coordinate space and can be handed to PyMuPDF directly; this one cannot.
    The PDF redactor scales it onto `page.rect` at redaction time
    (`_normalized_bbox_to_page_rect`). The inconsistency is Textract's, not
    a choice — storing the raw response values keeps the conversion in one
    place instead of spreading a page-size dependency into the OCR tool —
    but it is a trap worth stating loudly, since a normalized box used as
    if it were points redacts a sliver in the page's top-left corner and
    looks plausible while covering nothing.

    Word granularity is Textract's smallest geometric unit, so OCR-derived
    redaction is word-granular: a span touching part of a word blacks out
    the whole word. Over-redaction is the safe direction.
    """

    text: str
    startOffset: int
    endOffset: int
    boundingBox: BoundingBox


@dataclass
class ExtractedTextUnit:
    """A unit of extracted document text, as produced by `extract_document_content`.

    `boundingBox` (additive extension): the text unit's bounding
    rectangle within its source page, in the same page-coordinate space
    `fitz`/`PyMuPDF` uses (points, origin top-left). Populated by the
    `.pdf` extractor (`extract_pdf_content`) alongside `position.pageIndex`
    so the PDF redaction tool (`redact_pdf_document`) can call
    PyMuPDF's `page.add_redact_annot(rect, ...)` for the correct page and
    rectangle without needing to re-extract or re-search the document.
    Left `None` for `.docx` extraction, which has no per-unit
    pixel/point rectangle concept. Optional and defaulted, so this is
    backward-compatible with every existing caller that constructs an
    `ExtractedTextUnit` without it.

    `ocrWords` (dual-path PDF detection additive extension): the per-word
    geometry of a unit whose text came from OCR rather than from the
    document's text layer, populated only by `ocr_pdf_pages` (which emits
    one `zone="ocr"` unit per PDF page). It exists because OCR text has no
    per-character geometry the way `rawdict` extraction does, so the
    redactor cannot resolve an offset range to a rectangle by walking
    characters; the word list is the only bridge from an offset in
    `text` back to a place on the page. Left `None` for every text-layer
    unit (`.pdf` and `.docx` alike), whose spans are resolved through the
    existing character-level path instead. Optional and defaulted, so this
    is backward-compatible with every existing caller that constructs an
    `ExtractedTextUnit` without it.
    """

    zone: TextUnitZone
    text: str
    position: TextPosition
    boundingBox: Optional[BoundingBox] = None
    ocrWords: Optional[List[OcrWord]] = None


@dataclass
class RecognizedElement:
    """One recognized text element of an embedded image, the in-image analogue of `OcrWord`.

    Produced by `recognize_image_text` (in-image-redaction feature) from a
    Sonnet-class multimodal model's localization response: one element per
    recognized line of in-image text, carrying both the transcribed
    characters and where they sit on the image.

    `startOffset`/`endOffset` are half-open character offsets into the owning
    `ImageRegion.recognizedText` (so `recognizedText[startOffset:endOffset]`
    is exactly `text`), which is how an `image:` `MatchSpan` found in the
    recognized text is resolved back to a rectangle: every element whose
    offset range intersects the span's range contributes its bounding box.

    `boundingBox` is NORMALIZED to `[0, 1]` of the embedded image's OWN pixel
    dimensions — fractions of the image's width/height, not points — exactly
    as `OcrWord.boundingBox` is normalized to its rasterized page rather than
    the source document's coordinate space. The redactor scales it onto the
    decoded image's actual pixel size at redaction time.
    """

    text: str
    startOffset: int
    endOffset: int
    boundingBox: BoundingBox


@dataclass
class ImageRegion:
    """An embedded image or scanned-page region, as produced by `extract_document_content`
    and later filled in by `recognize_image_text` (Image Text Recognition / OCR).

    `recognizedText` and `boundingBox` are `None` until `recognize_image_text`
    populates them; `extract_document_content` only produces a placeholder
    with `sourceLocation` (and, per the `imageBytes` note below, `imageBytes`)
    set.

    `imageBytes` (interface decision): a multimodal Amazon Bedrock model,
    invoked via the `Converse` API, needs
    each region's raw image bytes (fed as a `Converse` image content block
    rather than Textract's `Document.Bytes`), and `recognize_image_text` takes
    a single `list[ImageRegion]`
    argument (no parallel bytes list/dict to keep in sync by position or
    id), so the raw encoded image bytes (e.g. PNG/JPEG, as read from the
    source document) travel on the `ImageRegion` itself. `extract_document_content`
    is expected to populate `imageBytes` alongside the
    `sourceLocation` placeholder; `recognize_image_text` reads
    it to call the multimodal Bedrock model via `Converse` and does not modify it.

    `recognizedElements` (in-image-redaction additive extension):
    `recognize_image_text` now ALSO attaches per-element geometry here,
    produced by a Sonnet-class multimodal model in the same `Converse` call
    that yields `recognizedText`. `None` — the default and the placeholder —
    means "no trustworthy per-element geometry": either localization was not
    performed, or it was performed and is untrustworthy (unparseable/empty
    response, or any implausible box). Any region carrying `image:` spans
    whose `recognizedElements is None` is covered by the whole-image fallback
    at redaction time. A non-empty list means reliable geometry the redactor
    uses for targeted, per-region redaction. Optional and defaulted, so this
    is backward-compatible with every existing caller that constructs an
    `ImageRegion` without it.
    """

    zone: Literal["image"]
    sourceLocation: str
    recognizedText: Optional[str] = None
    boundingBox: Optional[BoundingBox] = None
    imageBytes: Optional[bytes] = None
    recognizedElements: Optional[List[RecognizedElement]] = None


@dataclass
class MatchSpan:
    """A (possibly merged) span of text matched by a Redaction Rule.

    `unitRef` identifies the `ExtractedTextUnit` or `ImageRegion` the span
    was found in (e.g. an index or generated id into the extracted content
    collection); `startOffset`/`endOffset` are character offsets into that
    unit's text (or `recognizedText` for `zone == "image"`).

    `mergedFromRuleNames` (interface decision): `match_rules`
    merges overlapping/adjacent raw regex matches *within the same text
    unit/image region* into a single `MatchSpan`. When every raw match that fed
    into a merged span came
    from the same rule, `ruleName` alone still fully describes the span and
    this field is left `None`. When a merged span combines matches from
    two or more *different* rules, `ruleName` keeps the first contributing
    rule's name (so existing callers that only read `ruleName` still get a
    single valid rule name) and `mergedFromRuleNames` is populated with the
    full, de-duplicated, order-of-first-appearance list of every
    contributing rule name (including the one already in `ruleName`, at
    index 0), so callers that care about all contributing rules can recover
    them. This is an additive field only; it never changes the meaning of
    `ruleName` for existing callers.

    Because merging can run more than once over the same spans (the
    orchestration layer re-merges `match_rules`' already-merged output
    together with Bedrock Guardrails PII spans and, since the
    llm-cjk-pii-recall feature, LLM PII spans), this field is *input* to
    merging as well as output: a merged span's contributing-name list is
    the union of every input span's `mergedFromRuleNames` (falling back to
    its `ruleName` when that field is `None`), so provenance survives
    repeated merge passes instead of collapsing back to a single name. A
    contributing name therefore comes from one of three sources: a regex
    rule name, a `guardrails:*` name, or an `llm:*` name. A span found by
    more than one source keeps every such name here, retained across both
    merge passes, so a merged span can carry rule names from all three
    sources at once.
    """

    zone: MatchZone
    unitRef: str
    startOffset: int
    endOffset: int
    ruleName: str
    mergedFromRuleNames: Optional[list[str]] = None
