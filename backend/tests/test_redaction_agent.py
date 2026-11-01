"""Unit tests for `agent/redaction_agent.py` — the approach-(b) Strands
assembly of the AgentCore Redaction Agent.

These tests are the executable statement of the approach-(b) decision: the
redaction agent is a real `strands.Agent` that owns the pipeline's full tool
set, while the tool modules stay plain and strands-free. They run fully
offline — building the agent constructs `Agent(tools=[...])` with no model,
no credentials and no network — so they need neither AWS nor a live model.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# NOTE: strands / agent.redaction_agent are imported lazily INSIDE each test,
# not at module top level. A top-level import would load `strands` into
# `sys.modules` at pytest collection time, which would trip the session-global
# defensive check in tests/test_llm_pii_detect.py::test_2_16 (which asserts
# `strands` is absent from sys.modules). Deferring the import keeps strands out
# of the process until this file's tests actually run.

#: The exact set of tool names the redaction agent must register — the
#: `__name__` of each of the 12 plain pipeline tool functions.
EXPECTED_TOOL_NAMES = {
    "extract_docx_content",
    "extract_pdf_content",
    "recognize_image_text",
    "ocr_pdf_pages",
    "match_rules",
    "detect_pii_with_guardrails",
    "detect_pii_with_llm",
    "redact_docx_document",
    "redact_pdf_document_with_report",
    "verify_structure",
    "store_redacted_document",
    "update_job_status",
}


def test_build_redaction_agent_returns_strands_agent():
    """`build_redaction_agent()` constructs a real `strands.Agent` offline
    (no model/credentials/network)."""
    from strands import Agent

    from agent.redaction_agent import build_redaction_agent

    agent = build_redaction_agent()
    assert isinstance(agent, Agent)


def test_build_redaction_agent_registers_exactly_the_twelve_tools():
    """The agent registers exactly the 12 expected pipeline tools, by name.

    Strands exposes the registered tool names on `Agent.tool_names`, which
    defaults each name to the wrapped function's `__name__`.
    """
    from agent.redaction_agent import build_redaction_agent

    agent = build_redaction_agent()
    assert set(agent.tool_names) == EXPECTED_TOOL_NAMES
    assert len(agent.tool_names) == 12


def test_get_redaction_agent_returns_cached_instance():
    """`get_redaction_agent()` returns the same cached agent on repeat
    calls (built at most once per process)."""
    from strands import Agent

    from agent.redaction_agent import get_redaction_agent

    first = get_redaction_agent()
    second = get_redaction_agent()
    assert first is second
    assert isinstance(first, Agent)


def test_llm_tool_module_is_strands_free_yet_registered_on_agent():
    """Approach (b) in one assertion: the LLM PII tool's
    MODULE imports no strands, yet `detect_pii_with_llm` is nonetheless
    registered as a tool on the assembled Strands agent.

    That pairing — SDK coupling confined to `redaction_agent.py`, the tool
    source left strands-free — is the whole point of wrapping with
    `tool(fn)` at assembly time instead of decorating the source functions.
    """
    from agent.redaction_agent import build_redaction_agent
    from agent.tools import llm_pii_detect

    source = Path(llm_pii_detect.__file__).read_text(encoding="utf-8")
    assert "import strands" not in source
    assert "from strands" not in source

    agent = build_redaction_agent()
    assert "detect_pii_with_llm" in agent.tool_names
