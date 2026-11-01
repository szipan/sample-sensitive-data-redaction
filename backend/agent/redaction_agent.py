"""Strands assembly for the AgentCore Redaction Agent — the ONE module that
couples this pipeline to the Strands Agents SDK.

Approach (b): the redaction agent is a real ``strands.Agent`` that owns the
pipeline's full tool set, assembled HERE by importing the plain tool
functions from ``agent/tools/*`` and wrapping each with ``strands.tool(fn)``
at assembly time. The tool modules themselves stay plain and strands-free ON
PURPOSE — the ``@tool`` decoration is applied here, never in the source
modules. Two properties fall out of that decision and are the reason for it:

- ``agent/tools/llm_pii_detect.py`` stays free of the SDK: its
  module imports no ``strands`` / MCP / sub-agent machinery. The LLM PII tool
  still participates fully in the Strands agent (it is registered below via
  ``tool(detect_pii_with_llm)``), yet its source never imports the SDK. That
  pairing — registered on the agent, strands-free at the source — is the
  executable statement of this whole approach.
- Every tool module stays importable and unit-testable without the Strands
  SDK installed. Nothing under ``agent/tools/`` grows an SDK dependency, so
  the existing per-tool unit tests keep running with no SDK present.

Execution stays deterministic. ``agent.orchestration.handle_job`` drives the
fixed tool sequence (extract → OCR → match → redact → verify → store →
update) by calling the plain functions DIRECTLY. The Agent's LLM planning
loop is intentionally NOT run: the pipeline is a fixed-order, deterministic
one (it does not depend on LLM-driven tool planning), which forbids handing
tool ordering to a model. This Agent exists so the pipeline is a
first-class Strands agent with a registered tool set — an identity and a
forward-preparation for future model-driven features — and constructing it
does NOT change today's control flow.

``Agent(tools=[...])`` constructs with no model object, no credentials and no
network access (verified against the pinned ``strands-agents`` in
``backend/.venv``); the reasoning loop is only entered when the agent is
*called*, which this project never does. Building the agent is therefore
safe to do offline and at process start.
"""
from __future__ import annotations

from strands import Agent, tool

from agent.tools.extraction_docx import extract_docx_content
from agent.tools.extraction_pdf import extract_pdf_content
from agent.tools.guardrails_pii_detect import detect_pii_with_guardrails
from agent.tools.job_status_update import update_job_status
from agent.tools.llm_pii_detect import detect_pii_with_llm
from agent.tools.matching import match_rules
from agent.tools.ocr import recognize_image_text
from agent.tools.ocr_pdf import ocr_pdf_pages
from agent.tools.redaction_docx import redact_docx_document
from agent.tools.redaction_pdf import redact_pdf_document_with_report
from agent.tools.storage import store_redacted_document
from agent.tools.structure_verification import verify_structure

#: The plain pipeline tool functions the redaction agent owns, in pipeline
#: order (extract → OCR → match → detect → redact → verify → store →
#: update). These are the SAME callables `agent.orchestration.handle_job`
#: invokes directly; here they are only *registered* on a Strands agent.
REDACTION_AGENT_TOOLS = [
    extract_docx_content,
    extract_pdf_content,
    recognize_image_text,
    ocr_pdf_pages,
    match_rules,
    detect_pii_with_guardrails,
    detect_pii_with_llm,
    redact_docx_document,
    redact_pdf_document_with_report,
    verify_structure,
    store_redacted_document,
    update_job_status,
]

REDACTION_AGENT_SYSTEM_PROMPT = (
    "You are the AgentCore Redaction Agent. You own the tools of a sensitive-"
    "data redaction pipeline for .docx and .pdf documents: content "
    "extraction, OCR, rule/PII matching, redaction, structure verification, "
    "storage, and job-status update. The pipeline runs as a FIXED, "
    "deterministic sequence and does not rely on you to plan or reorder tool "
    "calls; your registered tool set defines the pipeline's capabilities."
)


def build_redaction_agent() -> Agent:
    """Construct the Strands ``Agent`` that owns the redaction pipeline's
    tools.

    Each plain tool function is wrapped with ``tool(fn)`` at assembly time —
    the source modules are never decorated — and registered on the agent.
    Construction uses no model/credentials/network; the agent's reasoning
    loop is not run here (see module docstring).
    """
    return Agent(
        tools=[tool(fn) for fn in REDACTION_AGENT_TOOLS],
        system_prompt=REDACTION_AGENT_SYSTEM_PROMPT,
    )


_agent: Agent | None = None


def get_redaction_agent() -> Agent:
    """Return a process-wide cached redaction ``Agent``, building it at most
    once per process on first call."""
    global _agent
    if _agent is None:
        _agent = build_redaction_agent()
    return _agent
