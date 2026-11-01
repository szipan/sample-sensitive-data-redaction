"""Unit tests for `agent/runtime.py` (AgentCore Runtime entrypoint).

These tests exercise the SDK-independent core of the entrypoint
(`process_invocation` / `extract_job_id`) in isolation, without importing the
Strands Agents SDK or the Bedrock AgentCore Runtime SDK (both of which
`agent/runtime.py` imports lazily inside `build_redaction_agent` /
`build_agentcore_app`, so importing the module here does not require them).

Covered behaviors:
- A well-formed invocation payload carrying a `jobId` -- in each shape
  AgentCore Runtime might deliver it (a decoded ``dict``, raw JSON ``bytes``
  exactly as `JobDispatcher` sends, or a JSON ``str``) -- causes `handle_job`
  to be invoked exactly once with that `jobId`, and yields an `accepted` ack.
- A malformed payload (missing/blank/non-string `jobId`, non-JSON bytes,
  non-object JSON) is handled gracefully: `handle_job` is NOT called and a
  `rejected` error response is returned rather than an unhandled crash.
- If `handle_job` itself raises (e.g. the Job record cannot be read), the
  entrypoint still returns a `rejected` error response instead of crashing
  the Runtime process.

`handle_job` is mocked via `unittest.mock.patch` so these tests neither run
the real redaction pipeline nor touch AWS.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agent import runtime  # noqa: E402

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _dispatcher_payload_bytes(job_id: str) -> bytes:
    """Reproduce the exact payload shape `JobDispatcher._invoke_agent_runtime`
    sends: `json.dumps({"jobId": job_id}).encode("utf-8")`."""
    return json.dumps({"jobId": job_id}).encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        {"jobId": JOB_ID},
        _dispatcher_payload_bytes(JOB_ID),
        json.dumps({"jobId": JOB_ID}),
        bytearray(_dispatcher_payload_bytes(JOB_ID)),
    ],
    ids=["dict", "json-bytes", "json-str", "json-bytearray"],
)
def test_valid_payload_invokes_handle_job_once(payload):
    with patch.object(runtime, "handle_job") as mock_handle_job:
        result = runtime.process_invocation(payload)

    mock_handle_job.assert_called_once_with(JOB_ID)
    assert result == {"status": runtime.STATUS_ACCEPTED, "jobId": JOB_ID}


def test_extra_payload_fields_are_ignored():
    with patch.object(runtime, "handle_job") as mock_handle_job:
        result = runtime.process_invocation({"jobId": JOB_ID, "extra": "ignored"})

    mock_handle_job.assert_called_once_with(JOB_ID)
    assert result["status"] == runtime.STATUS_ACCEPTED


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jobId": ""},
        {"jobId": None},
        {"jobId": 123},
        {"job_id": JOB_ID},
        b"not-json",
        b"\xff\xfe",
        json.dumps(["not", "an", "object"]),
        "42",
    ],
    ids=[
        "missing-key",
        "empty-string",
        "none",
        "non-string",
        "wrong-key",
        "non-json-bytes",
        "invalid-utf8",
        "json-array",
        "json-scalar",
    ],
)
def test_malformed_payload_is_rejected_without_calling_handle_job(payload):
    with patch.object(runtime, "handle_job") as mock_handle_job:
        result = runtime.process_invocation(payload)

    mock_handle_job.assert_not_called()
    assert result["status"] == runtime.STATUS_REJECTED
    assert result["error"]["code"] == runtime.INVALID_PAYLOAD_ERROR_CODE
    assert result["error"]["message"]  # a human-readable reason is present


def test_handle_job_exception_is_converted_to_error_response():
    with patch.object(
        runtime, "handle_job", side_effect=RuntimeError("job not found")
    ) as mock_handle_job:
        result = runtime.process_invocation({"jobId": JOB_ID})

    mock_handle_job.assert_called_once_with(JOB_ID)
    assert result["status"] == runtime.STATUS_REJECTED
    assert result["jobId"] == JOB_ID
    assert result["error"]["code"] == runtime.JOB_PROCESSING_ERROR_CODE
    assert "job not found" in result["error"]["message"]


def test_extract_job_id_accepts_dispatcher_byte_payload():
    assert runtime.extract_job_id(_dispatcher_payload_bytes(JOB_ID)) == JOB_ID


@pytest.mark.parametrize(
    "payload",
    [b"not-json", {"jobId": ""}, {}, json.dumps(42)],
    ids=["non-json", "blank", "missing", "scalar"],
)
def test_extract_job_id_raises_on_malformed_payload(payload):
    with pytest.raises(runtime.InvalidInvocationPayloadError):
        runtime.extract_job_id(payload)
