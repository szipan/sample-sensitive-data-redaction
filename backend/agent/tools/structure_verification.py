"""`verify_structure` Agent tool.

Contract of `verify_structure`:

    Input:  the source document and the Redacted Document
    Output: a verification result (boolean + failure reason)
    Notes:  checks that the extension, page count, page order and every
            table's row/column counts are unchanged; any mismatch counts as a
            processing failure

Scope note: this module implements ONLY the standalone
`verify_structure` tool function. The Agent orchestration that calls it as
part of `handle_job` (`verification = verify_structure(job.sourceBytes,
redacted)`) is explicitly out of scope here.

--------------------------------------------------------------------------
Interpretation of the extension check
--------------------------------------------------------------------------

The orchestration calls `verify_structure(job.sourceBytes, redacted)` — i.e.
with no separate extension arguments for the source vs. the Redacted
Document. This module's `verify_structure(source_bytes,
redacted_bytes, extension)` signature mirrors that: there is exactly ONE
`extension` value, not two, because by construction of the pipeline
(`redact_document`/`redact_docx_document`/`redact_pdf_document`, and the
`handle_job` orchestration) the Redacted Document is always produced from —
and stored/returned with — the same extension as the source
`RedactionJob.sourceExtension`. There is no
step anywhere in this pipeline that could change a Redacted Document's
extension independently of its source, so a literal string-vs-string
extension comparison would be comparing `extension` to itself and could
never fail.

Given that, this tool's actual job for the extension check is to confirm
that `redacted_bytes` is genuinely parseable *as a valid document of that
extension* — i.e. that whatever `redact_document` produced is not corrupted
in a way that makes it stop actually being a valid `.docx`/`.pdf` file. This
module implements that interpretation: both `source_bytes` and
`redacted_bytes` must parse without raising `ExtractionError` when
extracted via `extract_docx_content`/`extract_pdf_content` for the given
`extension`. If either fails to parse, `verify_structure` returns
`ok=False` with a parsing-related reason rather than letting the exception
propagate, so the orchestration can set the Job to `failed` instead of
storing a document it cannot verify.

That the Redacted Document *retains the source file extension* is
ultimately a property of a different layer than this function's
byte-content comparison: the extension is part of the S3 object key / file
name the orchestration writes via `store_redacted_document` (key format
`{jobId}/{fileName}`), constructed directly from `job.sourceExtension` —
not something `verify_structure` can independently observe from
`source_bytes`/`redacted_bytes` alone (raw bytes carry no filename). This
module's parseability check is the closest byte-content-level proxy for
"the Redacted Document is still validly of the source's extension" that
`verify_structure`'s documented input shape (bytes + a single shared
`extension`) supports.

--------------------------------------------------------------------------
Scope decision: PDF table-structure verification
--------------------------------------------------------------------------

The `verify_structure` contract calls for matching table row/column counts
unconditionally, but `extraction_pdf.py`
does not currently produce any table-structure metadata
for `.pdf` — and the PDF text layer is obtained by walking the page content
stream character by character (the glyph traversal in `pdf_glyphs`), which
yields positioned characters and has no structural "table" concept at all:
a PDF page is just a sequence of positioned glyphs, with no reliable,
general way to detect table boundaries/rows/columns for arbitrary PDFs
(unlike `.docx`, where `python-docx` exposes explicit `<w:tbl>` table XML
elements with well-defined rows/columns). Building ad hoc PDF table
detection (e.g. from block bounding-box grid alignment) would be a
nontrivial, unreliable heuristic — explicitly the option this task's
instructions call out as NOT low-effort/reliable.

This module therefore takes option (b): table-structure verification is
performed for `.docx` only (using `extraction_docx.py`'s existing
`DocumentStructureMetadata.tables`), and is explicitly OUT OF SCOPE / not
verifiable at this extraction layer for `.pdf` in this POC. For `.pdf`,
`verify_structure` instead checks page count and page order (the checks
the `.pdf` extraction layer *does* support). This mirrors `extraction_docx.py`'s own documented
choice to leave `pageCount=None` for `.docx` (no page concept at that
extraction layer) — both are the same kind of documented, per-format
extraction-layer limitation, not a silent gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agent.tools.extraction_docx import ExtractionError as DocxExtractionError
from agent.tools.extraction_docx import extract_docx_content
from agent.tools.extraction_pdf import ExtractionError as PdfExtractionError
from agent.tools.extraction_pdf import extract_pdf_content

StructureVerificationReason = str
"""A short machine-readable failure reason (e.g. `"page_count_mismatch"`).

Kept as a plain `str` (rather than a `Literal`/enum) since, unlike
`RuleOperationErrorReason` in `shared/models.py`, this reason is not
persisted anywhere and has no schema contract with a downstream consumer
yet — the Agent orchestration is only expected to log/attach it
to `update_job_status(failed, ...)`'s free-text error message.
"""


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of `verify_structure`, mirroring the `ValidationResult`
    pattern used by `shared/rule_validation.py`: `ok=True`
    implies `reason is None`, and `ok=False` always carries a specific
    machine-readable `reason` string.
    """

    ok: bool
    reason: Optional[StructureVerificationReason] = None

    @staticmethod
    def success() -> "VerificationResult":
        return VerificationResult(ok=True, reason=None)

    @staticmethod
    def failure(reason: StructureVerificationReason) -> "VerificationResult":
        return VerificationResult(ok=False, reason=reason)


def verify_structure(source_bytes: bytes, redacted_bytes: bytes, extension: str) -> VerificationResult:
    """Compare a source Supported Document against its Redacted Document
    counterpart for structural preservation.

    Dispatches on `extension`:

    - `.docx`: both byte strings must parse via `extract_docx_content`
      (the extension/parseability check — see module docstring). Then
      compares table counts and, per matching table index, `rowCount`/
      `colCount`. `.docx` has no page concept at this extraction layer
      (`extraction_docx.py`'s `DocumentStructureMetadata.pageCount` is
      always `None`), so page-count/page-order checks are skipped for
      `.docx` — this mirrors that module's own documented choice.
    - `.pdf`: both byte strings must parse via `extract_pdf_content`. Then
      compares `pageCount` and `pageOrder`. Table
      structure verification is out of scope for `.pdf` in this POC (see
      module docstring's "Scope decision" section) and is intentionally
      NOT checked here.
    - Any other `extension` value: returns `ok=False` with reason
      `"unsupported_extension"` rather than raising, since `verify_structure`
      is documented to always
      resolve to a `VerificationResult` rather than let an exception
      propagate out of the tool.

    Any mismatch in the applicable checks, or a parse failure on either
    document, results in `ok=False` with a specific `reason`. All
    applicable checks passing results in `ok=True, reason=None`.

    This function never raises: parsing failures (corrupted/unparseable
    bytes for the given `extension`) are caught and converted into a
    `VerificationResult.failure("parse_error_...")`, so the orchestration can mark
    the Job `failed`.
    """
    if extension == ".docx":
        return _verify_docx_structure(source_bytes, redacted_bytes)
    if extension == ".pdf":
        return _verify_pdf_structure(source_bytes, redacted_bytes)
    return VerificationResult.failure("unsupported_extension")


def _verify_docx_structure(source_bytes: bytes, redacted_bytes: bytes) -> VerificationResult:
    try:
        source_result = extract_docx_content(source_bytes)
    except DocxExtractionError:
        return VerificationResult.failure("parse_error_source")

    try:
        redacted_result = extract_docx_content(redacted_bytes)
    except DocxExtractionError:
        return VerificationResult.failure("parse_error_redacted")

    source_tables = source_result.structureMetadata.tables
    redacted_tables = redacted_result.structureMetadata.tables

    if len(source_tables) != len(redacted_tables):
        return VerificationResult.failure("table_count_mismatch")

    for source_table, redacted_table in zip(source_tables, redacted_tables):
        if (
            source_table.rowCount != redacted_table.rowCount
            or source_table.colCount != redacted_table.colCount
        ):
            return VerificationResult.failure("table_row_col_mismatch")

    return VerificationResult.success()


def _verify_pdf_structure(source_bytes: bytes, redacted_bytes: bytes) -> VerificationResult:
    try:
        source_result = extract_pdf_content(source_bytes)
    except PdfExtractionError:
        return VerificationResult.failure("parse_error_source")

    try:
        redacted_result = extract_pdf_content(redacted_bytes)
    except PdfExtractionError:
        return VerificationResult.failure("parse_error_redacted")

    if source_result.pageCount != redacted_result.pageCount:
        return VerificationResult.failure("page_count_mismatch")

    if source_result.pageOrder != redacted_result.pageOrder:
        return VerificationResult.failure("page_order_mismatch")

    return VerificationResult.success()
