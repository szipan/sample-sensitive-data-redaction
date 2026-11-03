"""Public interface compatibility tests for the PDF tools.

Compares against literal expected values (not against the modules
themselves). Requirements: 10.1, 10.2.
"""

import dataclasses
import inspect

import pytest

from agent.tools import extraction_pdf, ocr_pdf, redaction_pdf

_EMPTY = inspect.Parameter.empty

# (function, [(param name, default or _EMPTY), ...])
_EXPECTED_SIGNATURES = [
    (extraction_pdf.extract_pdf_content, [("file_bytes", _EMPTY)]),
    (ocr_pdf.ocr_pdf_pages, [("file_bytes", _EMPTY)]),
    (
        redaction_pdf.redact_pdf_document,
        [
            ("file_bytes", _EMPTY),
            ("text_units", _EMPTY),
            ("match_spans", _EMPTY),
            ("image_regions", None),
        ],
    ),
    (
        redaction_pdf.redact_pdf_document_with_report,
        [
            ("file_bytes", _EMPTY),
            ("text_units", _EMPTY),
            ("match_spans", _EMPTY),
            ("ocr_text_units", None),
            ("image_regions", None),
        ],
    ),
]


@pytest.mark.parametrize(
    "func,expected", _EXPECTED_SIGNATURES, ids=lambda v: getattr(v, "__name__", "")
)
def test_function_signature_matches(func, expected):
    params = list(inspect.signature(func).parameters.values())
    assert [(p.name, p.default) for p in params] == expected
    assert all(p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD for p in params)


@pytest.mark.parametrize(
    "cls,module,name",
    [
        (extraction_pdf.ExtractionError, "agent.tools.extraction_pdf", "ExtractionError"),
        (ocr_pdf.OcrPathError, "agent.tools.ocr_pdf", "OcrPathError"),
        (redaction_pdf.RedactionError, "agent.tools.redaction_pdf", "RedactionError"),
    ],
)
def test_exception_class_identity(cls, module, name):
    assert cls.__module__ == module
    assert cls.__name__ == name
    assert issubclass(cls, Exception)


def test_ocr_path_error_defaults_to_no_page_index():
    err = ocr_pdf.OcrPathError("boom")
    assert err.pageIndex is None


def test_ocr_path_error_carries_page_index_in_attribute_and_message():
    err = ocr_pdf.OcrPathError("boom", 7)
    assert err.pageIndex == 7
    assert isinstance(err.pageIndex, int)
    assert "7" in str(err)
    assert "boom" in str(err)


def test_ocr_path_error_page_index_keyword():
    err = ocr_pdf.OcrPathError("boom", page_index=3)
    assert err.pageIndex == 3
    assert "3" in str(err)


def test_redaction_report_fields():
    assert dataclasses.is_dataclass(redaction_pdf.PdfRedactionReport)
    names = [f.name for f in dataclasses.fields(redaction_pdf.PdfRedactionReport)]
    assert names == ["textLayerRegions", "ocrRegions", "agreedRegions"]


def test_redaction_result_fields():
    assert dataclasses.is_dataclass(redaction_pdf.PdfRedactionResult)
    names = [f.name for f in dataclasses.fields(redaction_pdf.PdfRedactionResult)]
    assert names == ["documentBytes", "report"]
