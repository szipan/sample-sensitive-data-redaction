"""SDK-independent invocation-handling core of the AgentCore Redaction Agent

This module owns NO SDK wiring. `backend/agent/main.py` is the tracked
AgentCore Runtime entrypoint: it constructs the `BedrockAgentCoreApp`,
configures logging, and delegates each invocation to `process_invocation`
here. This module depends only on the standard library and
`agent.orchestration`, so it stays importable and unit-testable without the
Bedrock AgentCore Runtime SDK or the Strands Agents SDK installed.

What this module does is take whatever `InvokeAgentRuntime` payload arrived,
resolve a `jobId` from it, and drive the fixed-order deterministic pipeline
`handle_job(job_id)` (`agent/orchestration.py`) exactly once, converting
every failure mode into an invocation-level response rather than an
exception.

Design decisions
----------------
1. Deterministic, NOT LLM-planned. The orchestration is a fixed-order,
   deterministic pipeline (it does not depend on LLM-driven tool planning,
   which would make it non-deterministic and hard to test). `handle_job`
   already sequences every tool
   (extract → OCR → match → redact → verify → store → update) in a fixed
   order internally, and `process_invocation` calls it DIRECTLY -- control is
   never handed to an LLM planner to choose tool order.

   The redaction agent is now assembled as a real `strands.Agent` in
   `agent/redaction_agent.py` (approach (b): each plain pipeline tool is
   registered via `strands.tool(fn)` at assembly time, so the tool modules
   under `agent/tools/` stay strands-free, which keeps
   `llm_pii_detect.py` free of any `strands` import). `strands-agents` is pinned in
   `requirements.txt` precisely because that assembly gives it a genuine
   in-repo caller.

   That Strands agent is referenced at the `main.py` AgentCore entrypoint,
   NOT here. `runtime.py` -- and `process_invocation` in particular --
   deliberately remain SDK-independent: they import no `strands` and no
   `bedrock_agentcore`, so they stay importable and unit-testable without
   either SDK installed. That is exactly why the agent reference lives in
   `main.py` (which already imports the AgentCore SDK) rather than on this
   path.

   Execution stays deterministic regardless: `process_invocation` calls
   `handle_job` DIRECTLY, and the assembled agent's reasoning loop is never
   run, so the fixed-order, deterministic premise -- and the determinism
   that depends on it -- is preserved exactly.

2. Fire-and-forget ack. `process_invocation` returns a small success/ack
   response. The actual Redaction Job result is written to DynamoDB by the
   pipeline (`update_job_status`), not returned synchronously -- consistent
   with the fire-and-forget dispatch design (`JobDispatcher` does not wait on
   the Agent).

Payload shape
-------------
`JobDispatcher` sends `json.dumps({"jobId": job_id}).encode("utf-8")` with
`contentType="application/json"` (see `lambdas/job_dispatcher.py`
`_invoke_agent_runtime`). Depending on how AgentCore Runtime hands the
payload to the entrypoint, it may arrive already-decoded as a `dict`, or as
raw JSON `bytes`/`str`. `extract_job_id` accepts all three shapes so the
entrypoint is robust to that detail.

Logging
-------
This module takes a logger and sets no level on it. The Effective_Log_Level
is `agent.logging_config.configure_logging`'s to own.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Union

from agent.orchestration import handle_job

logger = logging.getLogger(__name__)

# Key carrying the Redaction Job id in the invocation payload -- must match
# the shape `JobDispatcher._invoke_agent_runtime` sends (`{"jobId": ...}`).
JOB_ID_KEY = "jobId"

STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
INVALID_PAYLOAD_ERROR_CODE = "INVALID_INVOCATION_PAYLOAD"
JOB_PROCESSING_ERROR_CODE = "JOB_PROCESSING_ERROR"

# A raw invocation payload may be a decoded dict, or raw JSON bytes/str.
InvocationPayload = Union[Dict[str, Any], bytes, bytearray, str]


class InvalidInvocationPayloadError(Exception):
    """Raised when an invocation payload cannot be parsed into a usable
    `jobId` (not JSON, not an object, or missing/blank `jobId`).

    Handled internally by `process_invocation`, which converts it into a
    graceful error response rather than letting it crash the Runtime.
    """


def extract_job_id(payload: InvocationPayload) -> str:
    """Extract the `jobId` from an `InvokeAgentRuntime` payload.

    Accepts the payload as a decoded ``dict`` or as raw JSON ``bytes`` /
    ``bytearray`` / ``str`` (matching the `{"jobId": ...}` shape that
    `JobDispatcher` sends).

    Raises:
        InvalidInvocationPayloadError: if the payload is not valid JSON, is
            not a JSON object, or has a missing/blank/non-string `jobId`.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidInvocationPayloadError(
                "Invocation payload is not valid UTF-8"
            ) from exc

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise InvalidInvocationPayloadError(
                "Invocation payload is not valid JSON"
            ) from exc

    if not isinstance(payload, dict):
        raise InvalidInvocationPayloadError(
            "Invocation payload must be a JSON object with a {!r} field".format(
                JOB_ID_KEY
            )
        )

    job_id = payload.get(JOB_ID_KEY)
    if not isinstance(job_id, str) or not job_id:
        raise InvalidInvocationPayloadError(
            "Invocation payload is missing a non-empty {!r} string".format(JOB_ID_KEY)
        )
    return job_id


def process_invocation(payload: InvocationPayload) -> Dict[str, Any]:
    """SDK-independent core of the AgentCore Runtime entrypoint.

    Extracts the `jobId` from the invocation `payload` and drives the
    fixed-order redaction pipeline (`handle_job`) exactly once, returning a
    small ack/error response. This function NEVER raises: a malformed payload
    yields a `rejected` error response (not an unhandled crash), and any
    unexpected error surfacing from `handle_job` (e.g. the Job record cannot
    be read at all) is also converted into an error response so the Runtime
    process stays alive for subsequent invocations.

    The Job's real outcome (`completed`/`failed`) is written to DynamoDB by
    the pipeline itself; this response is only an invocation-level
    acknowledgement, consistent with the fire-and-forget dispatch design.
    """
    try:
        job_id = extract_job_id(payload)
    except InvalidInvocationPayloadError as exc:
        logger.warning("Rejecting AgentCore invocation: %s", exc)
        return {
            "status": STATUS_REJECTED,
            "error": {"code": INVALID_PAYLOAD_ERROR_CODE, "message": str(exc)},
        }

    try:
        # Deterministic, direct invocation of the fixed-order pipeline tool
        # (NOT via an LLM planner). `handle_job` itself converts any
        # in-pipeline failure into a `failed` Job status; the only thing that
        # can propagate out is a pre-pipeline failure such as the Job record
        # not existing (see `orchestration._load_job`).
        handle_job(job_id)
    except Exception as exc:  # noqa: BLE001 - keep the Runtime process alive
        logger.exception("handle_job raised for jobId %s", job_id)
        return {
            "status": STATUS_REJECTED,
            "jobId": job_id,
            "error": {"code": JOB_PROCESSING_ERROR_CODE, "message": str(exc)},
        }

    return {"status": STATUS_ACCEPTED, "jobId": job_id}
