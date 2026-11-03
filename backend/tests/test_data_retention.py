"""Retention behaviour: job TTL on upload and source deletion on completion."""
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[1]
for p in (BACKEND_ROOT, BACKEND_ROOT / "lambdas"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent import orchestration  # noqa: E402
from lambdas import upload_handler  # noqa: E402


def test_put_job_item_sets_expires_at(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(upload_handler, "_get_jobs_table", lambda: table)
    monkeypatch.setenv("JOB_RETENTION_DAYS", "3")
    upload_handler._put_job_item({"jobId": "j"})
    item = table.put_item.call_args.kwargs["Item"]
    assert abs(item["expiresAt"] - (time.time() + 3 * 86400)) < 60


def test_put_job_item_defaults_to_one_day_on_bad_env(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(upload_handler, "_get_jobs_table", lambda: table)
    monkeypatch.setenv("JOB_RETENTION_DAYS", "abc")
    upload_handler._put_job_item({"jobId": "j"})
    item = table.put_item.call_args.kwargs["Item"]
    assert abs(item["expiresAt"] - (time.time() + 1 * 86400)) < 60


def test_delete_source_document_removes_object(monkeypatch):
    s3 = MagicMock()
    monkeypatch.setattr(orchestration, "_get_s3_client", lambda: s3)
    monkeypatch.setenv("SOURCE_BUCKET_NAME", "src")
    orchestration._delete_source_document("j/a.pdf")
    s3.delete_object.assert_called_once_with(Bucket="src", Key="j/a.pdf")


def test_delete_source_document_never_raises(monkeypatch):
    s3 = MagicMock()
    s3.delete_object.side_effect = RuntimeError("denied")
    monkeypatch.setattr(orchestration, "_get_s3_client", lambda: s3)
    monkeypatch.setenv("SOURCE_BUCKET_NAME", "src")
    orchestration._delete_source_document("j/a.pdf")


def test_failure_message_never_contains_exception_text():
    from agent.tools.extraction_pdf import ExtractionError

    msg = orchestration._failure_message(ExtractionError("John Smith 123-45-6789"))
    assert msg == "Processing failed (ExtractionError)"


def test_failure_message_keeps_orchestration_error_text():
    exc = orchestration.OrchestrationError("Unsupported source extension: '.txt'")
    assert orchestration._failure_message(exc) == str(exc)


def test_delete_detections_removes_object(monkeypatch):
    s3 = MagicMock()
    monkeypatch.setattr(orchestration, "_get_s3_client", lambda: s3)
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", "out")
    orchestration._delete_detections("j")
    s3.delete_object.assert_called_once_with(Bucket="out", Key="j/detections.json")


def test_delete_detections_never_raises(monkeypatch):
    s3 = MagicMock()
    s3.delete_object.side_effect = RuntimeError("denied")
    monkeypatch.setattr(orchestration, "_get_s3_client", lambda: s3)
    monkeypatch.setenv("OUTPUT_BUCKET_NAME", "out")
    orchestration._delete_detections("j")
