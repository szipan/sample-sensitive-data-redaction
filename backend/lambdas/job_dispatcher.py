"""`JobDispatcher` Lambda: DynamoDB Streams trigger on `RedactionJobs`.

Responsibilities:
1. Consume `RedactionJobs` table Streams records (`NEW_IMAGE` view type).
   Only newly *created* Jobs matter here: filter to
   `eventName == "INSERT"` records whose new image has `status ==
   "processing"` (a Job is only ever created with that status by
   `UploadHandler`; `MODIFY`/`REMOVE` records -- e.g. the Agent
   later updating the Job to `completed`/`failed` -- must NOT be
   re-dispatched).
2. For each qualifying record, extract `jobId` and call AgentCore's
   `InvokeAgentRuntime` API (boto3 client `bedrock-agentcore`, operation
   `invoke_agent_runtime`), passing `jobId` in the request payload.
3. Do NOT wait for the AgentCore Redaction Agent to finish processing the
   Job. `invoke_agent_runtime` returns once the Runtime's entrypoint
   returns, and the entrypoint (`agent/main.py`) acknowledges immediately
   and runs the pipeline in a background thread, which later updates
   `RedactionJobs` directly via `update_job_status`. This handler never
   reads or waits on the response body; it fires the invocation and moves
   on to the next record.

The AgentCore Runtime ARN is injected via the `AGENT_RUNTIME_ARN`
environment variable rather than hardcoded, and the IAM permission granted
to this Lambda (`infra/redaction_stack.py`) is scoped to
`bedrock-agentcore:InvokeAgentRuntime` on that Runtime.
"""
import json
import logging
import os
import uuid
from typing import Any, Dict, List

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DISPATCHED_STATUS = "processing"
INSERT_EVENT_NAME = "INSERT"

# Lazily-initialized AWS client so it is created once per Lambda execution
# environment (warm-start reuse) rather than per invocation, mirroring the
# pattern used in `lambdas/upload_handler.py`.
_agentcore_client = None


def _get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore")
    return _agentcore_client


def _is_new_processing_job(record: Dict[str, Any]) -> bool:
    """Return True if `record` is an INSERT Streams record for a Job newly
    created with `status == "processing"`.

    Uses the low-level DynamoDB Streams attribute-value shape (e.g.
    `{"S": "processing"}`), since Streams records are not unmarshalled by
    the Lambda service.
    """
    if record.get("eventName") != INSERT_EVENT_NAME:
        return False
    new_image = record.get("dynamodb", {}).get("NewImage") or {}
    status_attr = new_image.get("status") or {}
    return status_attr.get("S") == DISPATCHED_STATUS


def _extract_job_id(record: Dict[str, Any]) -> str:
    new_image = record["dynamodb"]["NewImage"]
    return new_image["jobId"]["S"]


def _invoke_agent_runtime(job_id: str) -> None:
    """Fire-and-forget invocation of the AgentCore Redaction Agent for
    `job_id`. Does not wait for the Agent to finish processing the Job --
    only for the Runtime to accept this single invocation request.
    """
    agent_runtime_arn = os.environ["AGENT_RUNTIME_ARN"]
    payload = json.dumps({"jobId": job_id}).encode("utf-8")

    response = _get_agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=str(uuid.uuid4()),
        payload=payload,
        contentType="application/json",
    )
    # Close the streaming response immediately without reading it.
    # The agent processes asynchronously; we only need confirmation that
    # the Runtime accepted the invocation (no exception raised above).
    # Without this, the Lambda blocks until the agent completes its entire
    # pipeline — which in review mode can be 30+ minutes, far exceeding
    # any Lambda timeout.
    event_stream = response.get("completion") or response.get("body")
    if hasattr(event_stream, 'close'):
        event_stream.close()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = event.get("Records") or []

    dispatched_count = 0
    for record in records:
        if not _is_new_processing_job(record):
            continue

        job_id = _extract_job_id(record)
        try:
            _invoke_agent_runtime(job_id)
            dispatched_count += 1
        except Exception:
            # Do not let one bad record fail the whole batch; log and move
            # on. The POC has no dead-letter/retry policy for dispatch
            # failures (Job would remain stuck in `processing`, which is an
            # accepted POC-scope limitation -- there is no reconciliation
            # sweep in this design).
            logger.exception(
                "Failed to invoke AgentCore Runtime for job %s", job_id
            )

    return {"dispatchedCount": dispatched_count}
