"""Unit tests for `lambdas/job_dispatcher.py`.

Covers the two main behaviors described in design.md's "JobDispatcher"
component:

- A DynamoDB Streams `INSERT` record whose new image has `status ==
  "processing"` triggers exactly one `invoke_agent_runtime` call with the
  correct `jobId` in the payload.
- A record with a status other than `"processing"`, or a non-`INSERT`
  event (`MODIFY`/`REMOVE`), triggers no call.

The AgentCore Runtime is not available in unit tests and
`boto3` does not necessarily model the real `bedrock-agentcore` service in
every environment, so the boto3 client itself is mocked via
`unittest.mock.patch` rather than exercised against `moto` (which does not
support AgentCore).
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "lambdas"))

AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test"


@pytest.fixture
def job_dispatcher_module(monkeypatch):
    """Import `job_dispatcher` fresh per test with env vars set, and reset
    its lazily-initialized AWS client global so each test starts clean."""
    monkeypatch.setenv("AGENT_RUNTIME_ARN", AGENT_RUNTIME_ARN)

    sys.modules.pop("job_dispatcher", None)
    import job_dispatcher as module

    module._agentcore_client = None
    return module


def _insert_record(job_id: str, status: str) -> dict:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "jobId": {"S": job_id},
                "status": {"S": status},
            }
        },
    }


def _modify_record(job_id: str, status: str) -> dict:
    record = _insert_record(job_id, status)
    record["eventName"] = "MODIFY"
    return record


def _remove_record(job_id: str) -> dict:
    return {
        "eventName": "REMOVE",
        "dynamodb": {
            "OldImage": {
                "jobId": {"S": job_id},
                "status": {"S": "completed"},
            }
        },
    }


def _mock_agentcore_client(job_dispatcher_module) -> MagicMock:
    mock_client = MagicMock()
    patcher = patch.object(job_dispatcher_module, "_get_agentcore_client", return_value=mock_client)
    patcher.start()
    return mock_client, patcher


def test_insert_processing_record_invokes_agent_runtime_once(job_dispatcher_module):
    mock_client, patcher = _mock_agentcore_client(job_dispatcher_module)
    try:
        job_id = "11111111-1111-1111-1111-111111111111"
        event = {"Records": [_insert_record(job_id, "processing")]}

        result = job_dispatcher_module.handler(event, None)

        assert result == {"dispatchedCount": 1}
        mock_client.invoke_agent_runtime.assert_called_once()
        _, kwargs = mock_client.invoke_agent_runtime.call_args
        assert kwargs["agentRuntimeArn"] == AGENT_RUNTIME_ARN
        payload = json.loads(kwargs["payload"])
        assert payload == {"jobId": job_id}
    finally:
        patcher.stop()


@pytest.mark.parametrize(
    "build_record",
    [
        lambda job_id: _insert_record(job_id, "completed"),
        lambda job_id: _insert_record(job_id, "failed"),
        lambda job_id: _modify_record(job_id, "processing"),
        lambda job_id: _remove_record(job_id),
    ],
    ids=["insert-completed", "insert-failed", "modify-processing", "remove"],
)
def test_non_qualifying_records_do_not_invoke_agent_runtime(
    job_dispatcher_module, build_record
):
    mock_client, patcher = _mock_agentcore_client(job_dispatcher_module)
    try:
        job_id = "22222222-2222-2222-2222-222222222222"
        event = {"Records": [build_record(job_id)]}

        result = job_dispatcher_module.handler(event, None)

        assert result == {"dispatchedCount": 0}
        mock_client.invoke_agent_runtime.assert_not_called()
    finally:
        patcher.stop()


def test_mixed_batch_dispatches_only_qualifying_records(job_dispatcher_module):
    mock_client, patcher = _mock_agentcore_client(job_dispatcher_module)
    try:
        qualifying_job_id = "33333333-3333-3333-3333-333333333333"
        event = {
            "Records": [
                _insert_record(qualifying_job_id, "processing"),
                _insert_record("other-job", "completed"),
                _modify_record("another-job", "processing"),
                _remove_record("removed-job"),
            ]
        }

        result = job_dispatcher_module.handler(event, None)

        assert result == {"dispatchedCount": 1}
        mock_client.invoke_agent_runtime.assert_called_once()
        _, kwargs = mock_client.invoke_agent_runtime.call_args
        payload = json.loads(kwargs["payload"])
        assert payload == {"jobId": qualifying_job_id}
    finally:
        patcher.stop()
