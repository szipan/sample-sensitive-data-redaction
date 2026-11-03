"""AgentCore Runtime entrypoint — the tracked Agent_Entrypoint_Source
(agent-log-delivery design.md "Component 2: The entrypoint becomes
version-controlled").

Synced to the Agent_Bundle root by `scripts/sync_agent_entrypoint.py`; the
bundle copy (`agent_bundle/package_clean/main.py`) is gitignored, **this file
is the source of record**. Before this file existed, the
process that actually ran in production was hand-written, tracked nowhere,
unreviewable and unrebuildable — and it never imported `agent/runtime.py`,
which is how a second divergent copy of the app wiring came to exist.

Keep this file an exact copy of what the bundle root must contain: the sync
script copies it byte-for-byte and does not rewrite anything.

Heavy modules (boto3, pypdfium2/pypdf, python-docx) stay behind lazy imports
inside `invoke`, so container initialisation stays well inside AgentCore's
30-second limit.
"""
import logging
import os
import sys
import threading

# Put the directory holding this file at the front of `sys.path`.
#
# This line is written for the location that matters — the Agent_Bundle root,
# where the deployed copy lives. There, `dirname(abspath(__file__))` is the
# bundle root itself, which is the directory holding `agent/`, `shared/` and
# every vendored dependency, so this is exactly what makes
# `from agent.logging_config import ...` resolve. `entrypoint=["main.py"]` in
# `infra/redaction_stack.py` runs the bundle copy from that root and is
# deliberately left alone: a subdirectory entrypoint path has undocumented
# working-directory and `sys.path` semantics for
# `AgentRuntimeArtifact.from_code_asset`, and that is a deployment-only
# failure mode.
#
# At *this* tracked location the same expression resolves to `backend/agent/`
# — the package interior, not the package's parent — so it does not enable the
# `agent.` imports below. Those work here for a different reason: `backend/`
# is already on `sys.path` when the suite or a dev shell runs from there. The
# line is therefore inert rather than wrong when this file is imported in
# place, and harmless: no module name under `agent/` shadows a standard
# library module. It is retained verbatim because the bundle copy needs it and
# the two files must not diverge.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.logging_config import configure_logging  # stdlib-only, cheap

# Configure log delivery at import, before any invocation is dispatched.
# Safe to do at import precisely because
# `agent.logging_config` imports only the standard library — the lazy-import
# discipline in `invoke` exists to protect AgentCore's 30-second
# initialisation limit, and this import costs nothing against it. Idempotent,
# so this and `agent/__init__.py`'s import-time call together install one
# handler, not two.
configure_logging()

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context=None):
    """Handle one `InvokeAgentRuntime` call from JobDispatcher.

    References the assembled Strands redaction agent (its identity and
    registered tool set) and then delegates to
    `agent.runtime.process_invocation`, the deterministic executor. The agent
    is obtained here — the true entrypoint AgentCore calls, which already
    imports the AgentCore SDK — rather than in `runtime.py`, so
    `runtime.py`/`process_invocation` stay SDK-independent and unit-testable
    without `strands` (see `agent/redaction_agent.py` and `agent/runtime.py`).
    The agent is referenced, not handed control: `process_invocation` →
    `handle_job` remains the fixed-order deterministic pipeline; the agent's
    reasoning loop is never run. The reference is best-effort and guarded — it
    never gates job handling, so `process_invocation(payload)` always runs.

    Delegating to `process_invocation` rather than parsing the payload here is
    the point of this file, not tidying: `process_invocation` is the
    unit-tested version (20 tests in `tests/test_agent_runtime.py`) — it
    rejects a non-string `jobId`, returns typed error codes, and **logs**. The
    hand-rolled version this replaces returned `{"status": "error"}` from a
    bare `except Exception` with no log statement anywhere in it, so
    installing a handler would still have produced nothing at all when a job
    failed at the invocation level.
    """
    # Lazy import to preserve the 30-second init discipline: strands is pulled
    # only when an invocation actually arrives, never at container init. The
    # agent reference is decorative (its reasoning loop is never run), so it is
    # best-effort and guarded: it must never gate job handling.
    try:
        from agent.redaction_agent import get_redaction_agent  # lazy: pulls strands

        agent = get_redaction_agent()
        logger.info(
            "Redaction Strands agent (%d registered tools) handling invocation",
            len(agent.tool_names),
        )
    except Exception:  # noqa: BLE001 - decorative reference must never fail a job
        logger.warning(
            "Could not build the Strands redaction agent reference; "
            "continuing with the deterministic pipeline",
            exc_info=True,
        )

    from agent.runtime import (  # lazy: pulls boto3, pdf libs, docx
        STATUS_ACCEPTED,
        InvalidInvocationPayloadError,
        extract_job_id,
        process_invocation,
    )

    # A malformed payload is rejected synchronously: `process_invocation`
    # returns (and logs) the typed rejection without starting any work.
    try:
        job_id = extract_job_id(payload)
    except InvalidInvocationPayloadError:
        return process_invocation(payload)

    # Run the job in a background thread and acknowledge right away.
    #
    # `InvokeAgentRuntime` does not return to the caller until this handler
    # returns. A review-mode job waits up to 30 minutes for the user to
    # confirm, and a large document takes minutes, so running the pipeline
    # inline kept JobDispatcher blocked past its 300 s Lambda timeout. The
    # DynamoDB stream then retried the whole batch (re-running jobs that were
    # already running) and held every later upload behind it, which showed up
    # as jobs stuck in `processing` with no runtime logs at all.
    #
    # `add_async_task` keeps the session's ping at `HealthyBusy` so AgentCore
    # does not reclaim it at the 15-minute idle timeout while the thread runs.
    task_id = app.add_async_task("redaction_job", {"jobId": job_id})

    def _run() -> None:
        try:
            process_invocation(payload)  # never raises; logs its own errors
        finally:
            app.complete_async_task(task_id)

    threading.Thread(target=_run, name=f"redaction-job-{job_id}", daemon=True).start()
    return {"status": STATUS_ACCEPTED, "jobId": job_id}


if __name__ == "__main__":
    app.run()
