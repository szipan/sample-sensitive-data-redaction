r"""End-to-end `Patti.pdf` regression test (pdf-dual-path-pii-detection) — the
ONLY test that verifies this feature's premise.

The defect
----------

`Patti.pdf` renders `Patti、Martin、Christina` but carries a broken font
ToUnicode CMap, so its text layer reads `'Pa#、Mar&n、Chris&na '` — the
`tt`/`ti` ligatures decode to `#`/`&`. Measured through the single-path
pipeline, Bedrock Guardrails saw only `NAME='Chris'` in that mangled string,
one redaction rectangle reached the page, and coverage of the three names was
`Patti` 0%, `Martin` 0%, `Christina` 56.6% — while the Redaction Job reported
`completed` with no error. PII leaked and nothing in the record said so.

This test asserts that the dual path closes that hole: the OCR_Path reads the
rendered glyphs, Guardrails finds all three names there, and the redactor
turns the resulting `ocr:` spans into rectangles that black the names out.

Why the coverage check is geometric, not textual
------------------------------------------------

`"Patti" not in extracted_text(redacted)` is trivially true of the SOURCE
too — the source's text layer never contained `Patti` in the first place.
A text-absence assertion would therefore pass on the unfixed pipeline and
prove nothing. So coverage is measured on pixels:

1. Locate each name in the SOURCE by rasterizing page 0 and running Textract
   `DetectDocumentText` over it (it returns exactly `['Patti,', 'Martin,',
   'Christina']`), giving one normalized bounding box per name. Textract is
   the instrument here precisely because the text layer cannot be trusted.
2. Render the same page of the REDACTED document at the same dpi and measure
   the fraction of near-black pixels inside each box.

The discriminator: ordinary ink density for black text on white paper reads
15–18% of the box's pixels as near-black, while a Solid_Redaction_Block reads
~100%. So the same measurement is taken on the source as a control — an
assertion that only checked the redacted output could pass because of a
broken measurement rather than because of a redaction.

Running it deliberately
-----------------------

This test calls REAL Amazon Textract and REAL Bedrock Guardrails, so it costs
money and needs credentials, and it reads a file that exists only on the
machine where the document was collected. It is therefore guarded by an
environment variable and skips (never fails) when anything it needs is
missing, so `python -m pytest tests -q` stays offline and free.

There is no pytest configuration file in this repository — no `pytest.ini`,
`pyproject.toml`, or `setup.cfg` — so a custom marker could not be registered
without introducing one (and would emit `PytestUnknownMarkWarning` if left
unregistered). An environment-variable guard needs no configuration, works
with the existing `python -m pytest tests` invocation unchanged, and keeps the
skip *reason* visible in `-rs` output. From `backend/`:

    $env:RUN_PATTI_REGRESSION = "1"
    $env:GUARDRAIL_ID = "sbw1o5pg4f92"      # the deployed pii-redaction-guardrail
    $env:GUARDRAIL_VERSION = "1"
    $env:AWS_DEFAULT_REGION = "us-west-2"   # where that Guardrail lives
    .venv\Scripts\python.exe -m pytest tests\test_patti_regression.py -q -s

`PATTI_PDF_PATH` overrides the document location. The test prints every number
it measured, so a run doubles as the audit record the manual verification was.

Invoke it as its own pytest run, not as part of `pytest tests`: sibling modules
install moto's fake AWS credentials into `os.environ` at module scope for the
rest of the process, so in a whole-suite run the credential probe below finds
unusable credentials and skips.

Requirements: 1.1, 1.9, 2.2, 2.4, 5.4.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

import fitz  # PyMuPDF
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent import orchestration  # noqa: E402
from agent.tools.job_status_update import STATUS_COMPLETED  # noqa: E402
from shared.models import RedactionJob  # noqa: E402

#: Set this to run the test. Absent -> skipped, so the default suite stays
#: offline and free.
RUN_FLAG_ENV_VAR = "RUN_PATTI_REGRESSION"

#: Overrides the document location; the default only exists on the machine
#: the document was collected on.
PDF_PATH_ENV_VAR = "PATTI_PDF_PATH"
DEFAULT_PDF_PATH = Path(r"D:\Users\szipan\Downloads\Patti.pdf")

JOB_ID = "patti-regression-0000-0000-000000000000"
FILE_NAME = "Patti.pdf"

#: The three values the single-path pipeline covered at 0%, 0%, and 56.6%.
EXPECTED_NAMES = ("Patti", "Martin", "Christina")

#: Both rasterizations (source, for locating; redacted, for measuring) use
#: this dpi, so one normalized box indexes both images identically.
MEASURE_DPI = 200

#: An 8-bit RGB sample counts as "ink" below this on every channel. Well
#: above JPEG-ish noise around glyph edges, well below mid-grey.
BLACK_SAMPLE_MAX = 64

#: A Solid_Redaction_Block measures ~1.00; anti-aliasing at the box's own
#: border is the only thing keeping it off exactly 1.0.
REDACTED_MIN_BLACK_FRACTION = 0.99

#: Black text on white paper measured 0.15-0.18 in the manual verification.
#: The control assertion; also what an unredacted name would read.
SOURCE_MAX_BLACK_FRACTION = 0.50

#: Text layer versus OCR non-whitespace counts for page 0. The design
#: predicted 19 vs 23 and the measurement is 18 vs 22 — hence a tolerance on
#: the absolute values and an exact assertion on the GAP, which is what the
#: diagnostic exists to expose and is structurally grounded: `Patti` loses
#: two characters to `Pa#`, `Martin` and `Christina` one each to `Mar&n` and
#: `Chris&na`, so the text layer is short by exactly 4 regardless of how each
#: path counts the three delimiters.
PREDICTED_TEXT_LAYER_CHARS = 19
PREDICTED_OCR_CHARS = 23
CHAR_COUNT_TOLERANCE = 2
EXPECTED_CHAR_GAP = 4

#: Measured `detectionSummary`: 1 text-layer region (Guardrails' `NAME='Chris'`
#: in the mangled string), 3 OCR regions (one per name), 1 agreed. The two
#: unagreed OCR regions are `Patti` and `Martin` — exactly the values the text
#: layer lost, and the number this whole feature exists to move off zero.
MIN_OCR_REGIONS = len(EXPECTED_NAMES)
MIN_OCR_ONLY_REGIONS = 2


# ---------------------------------------------------------------------------
# Guards: everything missing is a skip, never a failure
# ---------------------------------------------------------------------------


def _source_pdf_path() -> Path:
    return Path(os.environ.get(PDF_PATH_ENV_VAR) or DEFAULT_PDF_PATH)


def _skip_unless_runnable() -> Path:
    """Return the source PDF path, or skip with the reason it cannot run."""
    if not (os.environ.get(RUN_FLAG_ENV_VAR) or "").strip():
        pytest.skip(
            "{}, calls real Textract + Bedrock Guardrails; set {}=1 to run "
            "(see this module's docstring)".format(__name__, RUN_FLAG_ENV_VAR)
        )

    pdf_path = _source_pdf_path()
    if not pdf_path.is_file():
        pytest.skip(
            "source document not found at {}; set {} to its location".format(
                pdf_path, PDF_PATH_ENV_VAR
            )
        )

    missing = [
        name
        for name in ("GUARDRAIL_ID", "GUARDRAIL_VERSION")
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        # Without these `detect_pii_with_guardrails` stays dormant by design
        # and returns no spans at all, so the test would measure nothing.
        pytest.skip("Guardrails not configured; {} unset".format(", ".join(missing)))

    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 is a project dependency
        pytest.skip("boto3 is not installed")

    if boto3.session.Session().get_credentials() is None:
        pytest.skip("no AWS credentials available for Textract/Bedrock")

    # Credentials *existing* is not enough: several sibling test modules
    # (`test_job_status_update.py`, `test_download_handler.py`, ...) set
    # moto's fake keys into `os.environ` at module scope for the rest of the
    # process, so in a whole-suite run this test would inherit credentials
    # that look present and are rejected by Textract. Probe them once, and
    # skip rather than fail if they are not usable - a fake-credential run
    # says nothing about whether the PII was redacted.
    try:
        boto3.client("sts").get_caller_identity()
    except Exception as error:  # noqa: BLE001 - any failure means "cannot run"
        pytest.skip(
            "AWS credentials are not usable ({}); if this was a whole-suite "
            "run, other modules replaced them with moto's fake keys - invoke "
            "this file on its own".format(type(error).__name__)
        )

    return pdf_path


# ---------------------------------------------------------------------------
# The pipeline run
# ---------------------------------------------------------------------------


def _make_job() -> RedactionJob:
    return RedactionJob(
        jobId=JOB_ID,
        status="processing",
        sourceKey="{}/{}".format(JOB_ID, FILE_NAME),
        sourceExtension=".pdf",
        fileName=FILE_NAME,
        createdAt="2024-01-01T00:00:00+00:00",
        updatedAt="2024-01-01T00:00:00+00:00",
    )


def _run_dual_path_pipeline(pdf_bytes: bytes) -> dict:
    """Run the real `handle_job` over `pdf_bytes` and capture what it stored.

    Only the four *infrastructure* boundaries are replaced — the Job record
    read, the source-bytes read, the enabled-rules read, and the two writes
    (S3 object, DynamoDB record). Everything the feature consists of runs for
    real: `ocr_pdf_pages` against Textract, `detect_pii_with_guardrails`
    against Bedrock, `redact_pdf_document_with_report`, `verify_structure`,
    and `_build_detection_summary`. So the `pageHealth` asserted below is
    literally the summary the pipeline would have recorded on the Job.

    Enabled rules are empty on purpose: no regex rule in this project matches
    a personal name, so all three detections must come from Guardrails, and
    the result then does not depend on whatever rules happen to be in the
    deployed table.
    """
    captured: dict = {"status": None, "error": None, "summary": None, "redacted": None}

    def _capture_store(redacted_bytes: bytes, job_id: str, file_name: str) -> str:
        captured["redacted"] = redacted_bytes
        return "{}/{}".format(job_id, file_name)

    def _capture_update(
        job_id: str,
        status: str,
        redacted_key: Optional[str] = None,
        error_message: Optional[str] = None,
        detection_summary: Optional[dict] = None,
        error_code: Optional[str] = None,
        error_params: Optional[dict] = None,
    ) -> None:
        captured["status"] = status
        captured["error"] = error_message
        captured["summary"] = detection_summary

    with patch.object(orchestration, "_load_job", return_value=_make_job()), patch.object(
        orchestration, "_read_source_bytes", return_value=pdf_bytes
    ), patch.object(
        orchestration, "_load_enabled_rules", return_value=[]
    ), patch.object(
        orchestration, "store_redacted_document", side_effect=_capture_store
    ), patch.object(
        orchestration, "update_job_status", side_effect=_capture_update
    ):
        orchestration.handle_job(JOB_ID)

    return captured


# ---------------------------------------------------------------------------
# The measurement instrument
# ---------------------------------------------------------------------------


def _render_page_png(pdf_bytes: bytes, page_index: int, dpi: int) -> bytes:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return document.load_page(page_index).get_pixmap(dpi=dpi).tobytes("png")
    finally:
        document.close()


def _locate_names(
    pdf_bytes: bytes, page_index: int, names: Tuple[str, ...]
) -> Dict[str, Tuple[float, float, float, float]]:
    """Find each name on the SOURCE page and return its normalized box.

    Deliberately built from `boto3` and `fitz` directly rather than from
    `ocr_pdf_pages`: the instrument that says where a name *is* must not be
    the same code path whose output the test is judging.

    Returns `{name: (left, top, width, height)}` in Textract's `[0, 1]`
    fractions of the page.
    """
    import boto3

    png_bytes = _render_page_png(pdf_bytes, page_index, MEASURE_DPI)
    response = boto3.client("textract").detect_document_text(
        Document={"Bytes": png_bytes}
    )

    words: List[Tuple[str, Tuple[float, float, float, float]]] = []
    for block in response.get("Blocks") or []:
        if block.get("BlockType") != "WORD":
            continue
        box = (block.get("Geometry") or {}).get("BoundingBox") or {}
        if not box:
            continue
        words.append(
            (
                block.get("Text") or "",
                (
                    float(box["Left"]),
                    float(box["Top"]),
                    float(box["Width"]),
                    float(box["Height"]),
                ),
            )
        )

    print("\n[instrument] Textract read the source page as: {}".format(
        [text for text, _ in words]
    ))

    located: Dict[str, Tuple[float, float, float, float]] = {}
    for name in names:
        for text, box in words:
            if name in text:
                located[name] = box
                break

    unlocated = [name for name in names if name not in located]
    if unlocated:
        # An instrument failure, not a redaction failure - say so plainly
        # rather than letting it read as a coverage regression.
        pytest.fail(
            "measurement instrument failed: Textract did not read {} on the "
            "source page (it read {}), so coverage cannot be measured".format(
                unlocated, [text for text, _ in words]
            )
        )
    return located


def _black_fraction(
    pdf_bytes: bytes,
    page_index: int,
    normalized_box: Tuple[float, float, float, float],
    dpi: int = MEASURE_DPI,
) -> float:
    """Fraction of near-black pixels inside `normalized_box` on a rendered page.

    The box's fractions are of the whole page, and `get_pixmap` rasterizes the
    whole page, so they scale onto the pixmap by its own width/height with no
    dpi arithmetic — the same reason `_normalized_bbox_to_page_rect` takes no
    dpi parameter.
    """
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pixmap = document.load_page(page_index).get_pixmap(dpi=dpi)
        left, top, width, height = normalized_box
        x0 = max(0, int(left * pixmap.width))
        y0 = max(0, int(top * pixmap.height))
        x1 = min(pixmap.width, max(x0 + 1, int((left + width) * pixmap.width)))
        y1 = min(pixmap.height, max(y0 + 1, int((top + height) * pixmap.height)))

        samples = pixmap.samples
        components = pixmap.n
        stride = pixmap.stride
        black = 0
        total = 0
        for y in range(y0, y1):
            row = y * stride
            for x in range(x0, x1):
                base = row + x * components
                total += 1
                if all(
                    samples[base + channel] <= BLACK_SAMPLE_MAX
                    for channel in range(min(3, components))
                ):
                    black += 1
        return black / total if total else 0.0
    finally:
        document.close()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_patti_pdf_dual_path_covers_every_name_and_records_the_asymmetry():
    """Redacting `Patti.pdf` through the dual path blacks out `Patti`,
    `Martin`, and `Christina`, and records the text-layer/OCR character
    asymmetry that explains why the single path could not
    ."""
    pdf_path = _skip_unless_runnable()
    source_bytes = pdf_path.read_bytes()

    captured = _run_dual_path_pipeline(source_bytes)

    assert captured["status"] == STATUS_COMPLETED, (
        "job did not complete ({}): {}".format(captured["status"], captured["error"])
    )
    redacted_bytes = captured["redacted"]
    assert redacted_bytes, "no redacted document was stored"

    summary = captured["summary"]
    assert summary is not None, "a completed .pdf job must record a detectionSummary"
    print(
        "\n[measured] detectionSummary: textLayerRegions={} ocrRegions={} "
        "agreedRegions={}".format(
            summary["textLayerRegions"],
            summary["ocrRegions"],
            summary["agreedRegions"],
        )
    )
    print("[measured] pageHealth: {}".format(summary["pageHealth"]))

    # --- the text layer's damage is on the record ---------
    page_health = summary["pageHealth"]
    assert len(page_health) == 1, "Patti.pdf is a one-page document"
    health = page_health[0]
    assert health["pageIndex"] == 0
    # The signal is the asymmetry, so that is what is asserted exactly; the
    # absolute counts get a tolerance because the design predicted 19/23 and
    # the measurement is 18/22 (see PREDICTED_* above).
    assert health["ocrChars"] - health["textLayerChars"] == EXPECTED_CHAR_GAP, (
        "expected the OCR path to read exactly {} more non-whitespace "
        "characters than the text layer (the ligature characters `Pa#`, "
        "`Mar&n`, `Chris&na` lost); got textLayerChars={} ocrChars={}".format(
            EXPECTED_CHAR_GAP, health["textLayerChars"], health["ocrChars"]
        )
    )
    assert health["textLayerChars"] == pytest.approx(
        PREDICTED_TEXT_LAYER_CHARS, abs=CHAR_COUNT_TOLERANCE
    )
    assert health["ocrChars"] == pytest.approx(
        PREDICTED_OCR_CHARS, abs=CHAR_COUNT_TOLERANCE
    )

    # --- the OCR path found what the text layer lost -
    assert summary["ocrRegions"] >= MIN_OCR_REGIONS, (
        "expected at least one OCR-derived region per name ({}), got {}".format(
            MIN_OCR_REGIONS, summary["ocrRegions"]
        )
    )
    ocr_only_regions = summary["ocrRegions"] - summary["agreedRegions"]
    assert ocr_only_regions >= MIN_OCR_ONLY_REGIONS, (
        "expected at least {} OCR regions with no agreeing text-layer region "
        "(`Patti` and `Martin`, which the text layer lost entirely), got "
        "{} of {}".format(
            MIN_OCR_ONLY_REGIONS, ocr_only_regions, summary["ocrRegions"]
        )
    )

    # --- the rectangles actually cover the names ----
    boxes = _locate_names(source_bytes, 0, EXPECTED_NAMES)
    coverage: Dict[str, Tuple[float, float]] = {}
    for name in EXPECTED_NAMES:
        coverage[name] = (
            _black_fraction(source_bytes, 0, boxes[name]),
            _black_fraction(redacted_bytes, 0, boxes[name]),
        )
    for name, (source_fraction, redacted_fraction) in coverage.items():
        print(
            "[measured] {:<10} black coverage: source {:.1%} -> redacted "
            "{:.1%}".format(name, source_fraction, redacted_fraction)
        )

    for name, (source_fraction, _redacted) in coverage.items():
        # Control: the instrument must read plain text as sparse ink, or a
        # "100% covered" reading below would mean nothing.
        assert source_fraction < SOURCE_MAX_BLACK_FRACTION, (
            "measurement instrument is not discriminating: {!r} reads {:.1%} "
            "black in the SOURCE, where ordinary text ink is 15-18%".format(
                name, source_fraction
            )
        )

    uncovered = {
        name: redacted_fraction
        for name, (_source, redacted_fraction) in coverage.items()
        if redacted_fraction < REDACTED_MIN_BLACK_FRACTION
    }
    assert not uncovered, (
        "REGRESSION: the dual path left PII visible. Black coverage in the "
        "redacted document, by name: {}. Expected >= {:.0%} for every name; "
        "the single-path pipeline this feature replaced measured Patti 0%, "
        "Martin 0%, Christina 56.6%.".format(
            {name: "{:.1%}".format(value) for name, value in uncovered.items()},
            REDACTED_MIN_BLACK_FRACTION,
        )
    )
