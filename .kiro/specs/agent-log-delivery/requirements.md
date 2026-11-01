# Requirements Document

## Introduction

The AgentCore Redaction Agent has no application telemetry. Every log statement it executes is discarded before it leaves the process.

`agent/runtime.py` does this:

```python
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
```

It sets a level on its own module logger and never installs a handler, never calls `logging.basicConfig`. Records therefore propagate up to a handler-less root logger and are dropped by the "no handlers could be found" fallback. Every module under `backend/agent/` follows the same `logging.getLogger(__name__)` pattern, so the whole agent is affected.

**Measured evidence.** The runtime log group `/aws/bedrock-agentcore/runtimes/pii_redaction_agent-gGlsFL4Jwi-DEFAULT` received **4 events across 4 redaction jobs**. All 4 came from the SDK's own `bedrock_agentcore.app` logger ("Invocation completed successfully"). Zero events from any application logger exist in the group over a wider window.

What is being lost, verified by reading the code — 15 call sites under `backend/agent/`:

| Module | Sites | What is discarded |
|---|---|---|
| `agent/tools/guardrails_pii_detect.py` | 9 | `ApplyGuardrail` retry/failure warnings, the response-parse warning, the match-not-found debug line, the digit-boundary filter's "kept N of M" debug line, the "rejected all N, emitting all N as a fallback" warning, the `OCCURRENCE_WARN_THRESHOLD` over-redaction warning, the missing-Guardrail-config warning, the client-construction warning, the partial-results warning |
| `agent/tools/ocr_pdf.py` | 4 | `PDF_OCR_DPI` misconfiguration warnings, the Textract `DetectDocumentText` retry warning, the oversized-image re-render fallback warning |
| `agent/runtime.py` | 2 | the rejected-payload warning, the `handle_job` top-level exception traceback |

`backend/agent/orchestration.py` and every module under `backend/shared/` contain **no logging at all** — `handle_job`'s graceful-degradation branches (the Guardrails `except Exception: guardrails_matches = []`, the `verify_structure` failure path, the top-level catch-all that converts any pipeline error into a `failed` Job) are silent by omission, not by delivery failure. That is a related gap and this feature makes fixing it possible, but adding those statements is not in scope here.

The only surviving signal today is `RedactionJob.detectionSummary`, and only because it goes to DynamoDB rather than to a log. This is a compliance tool for a listed company, running in production, with no application telemetry.

**Two facts found while investigating, beyond the original defect report:**

1. **`agent/runtime.py` is not the deployed entrypoint.** `CDK_Stack` deploys `agent_bundle/package_clean` with `entrypoint=["main.py"]`. That `main.py` is a separate, hand-written `BedrockAgentCoreApp` entrypoint that lazily imports `agent.orchestration.handle_job` directly and never imports `agent/runtime.py`. `runtime.py`'s `build_agentcore_app()` and `main()` are dead code in the deployed system. Worse, `agent_bundle/package_clean/` is listed in `backend/.gitignore` and `main.py` exists in no other tracked location, so the process that actually runs in production is not under version control. A `basicConfig` call added to `agent/runtime.py` would change nothing in production.

2. **The SDK already emits structured JSON, on its own named logger.** `bedrock_agentcore/runtime/app.py` attaches a `StreamHandler` with a `RequestContextFormatter` to the logger named `bedrock_agentcore.app`, emitting one JSON object per record with `timestamp`, `level`, `message`, `logger`, and — where available — `requestId` and `sessionId`. That logger propagates to root by default, so installing a root handler would emit each SDK record twice. It also means a JSON format costs no new dependency and matches what is already in the stream.

**The fix can itself create a PII leak, and that is the larger half of this work.** Because nothing has ever been delivered, nobody has audited what these statements contain. Installing a handler makes all of them start landing in CloudWatch Logs. An audit of all 15 agent call sites was performed for this document, and the deliberate messages are clean — not one logs document text or a matched value; they log entity types, `unitRef` strings, counts, page indexes, byte sizes and environment variable names. The exposure is entirely in three indirect channels:

- **Third-party loggers at DEBUG.** `botocore` logs full HTTP request bodies at DEBUG level. The `ApplyGuardrail` request body *is* the document text (`content[0].text.text`); the `DetectDocumentText` request body *is* the rasterized page image. A root handler with a DEBUG level would stream the entire document to CloudWatch. This is by far the largest hazard, and it is created by the fix, not by any existing statement.
- **Exception messages and tracebacks.** Nine sites pass a caught `error` into the message or set `exc_info=True`. A `botocore` error string or a pipeline exception can carry an S3 key or a file name (`runtime.py`'s `logger.exception("handle_job raised for jobId %s")` renders whatever `handle_job` raised).
- **Future statements.** The `llm-cjk-pii-recall` spec's Audit_Record already sets the precedent to generalise: entity types, offsets, a SHA-256 digest and the character length of the input — never the input text, never a matched value.

One finding outside the agent: `lambdas/rule_operation_processor.py:209` logs the entire SQS record (`logger.exception("Failed to process Rule Operation record: %s", record)`), which carries the user-authored rule pattern. Lambda logs do work, so this one is already live.

**Why Lambda is unaffected.** All eight Lambda handlers use the identical `getLogger(__name__)` + `setLevel(INFO)` pattern — verified by reading them — yet their records do reach CloudWatch, because the AWS Lambda Python runtime pre-configures a handler on the root logger before the handler module is imported. The AgentCore Runtime container has no such convention: it is a long-lived process this project starts itself via `BedrockAgentCoreApp.run()`, and nothing installs a root handler. The asymmetry is environmental, not a difference in the code.

**Scope.** Application log records reach CloudWatch, they contain no sensitive value, and their volume and retention are bounded. No metrics, no tracing, no dashboards, no alarms — those are a separate spec if wanted.

### Decisions this document defaults, pending confirmation

Five choices are written below as concrete criteria so they can be argued with rather than deferred. Each is a default, not a settled fact:

1. **Standard library only, no Aws Lambda Powertools.** Requirement 3.5. Powertools' `Logger` is the conventional choice, but it is a third-party dependency in a product where licensing has been called out as mattering, and a JSON formatter matching the SDK's existing field set is roughly fifteen lines of `logging.Formatter`.
2. **Structured JSON, not plain text.** Requirement 3. The questions this project actually asks a log are counting questions ("how many occurrences did the digit filter narrow", "how many detections crossed the threshold"), and the SDK already emits JSON into the same stream.
3. **`LOG_LEVEL` defaults to `INFO`.** Requirement 4.2. This does discard the digit-boundary "kept N of M" line, which sits at DEBUG. The alternative — DEBUG by default — turns on per-detection records for every unit of every document, which is a volume and content risk in exchange for a diagnostic nobody reads on a healthy run.
4. **`sourceKey` and `fileName` are treated as sensitive.** Requirement 2.4. A filename in this product is routinely `张三-劳动合同.pdf`; an S3 key contains it. Treating them as loggable would put personal names in the log stream through the back door.
5. **Rule patterns are treated as sensitive.** Requirement 2.5. They are user-authored regexes that can encode both business logic and a literal PII value (a pattern written to catch one specific person's ID number contains that number).

## Glossary

- **Agent_Runtime_Process**: The long-lived container process AgentCore Runtime starts for the deployed agent, entered through `BedrockAgentCoreApp.run()`.
- **Agent_Entrypoint_Module**: The module `CDK_Stack` names as the AgentCore Runtime entrypoint — currently `agent_bundle/package_clean/main.py`.
- **Agent_Bundle**: The deployment artifact directory `agent_bundle/package_clean`, referenced by `CDK_Stack`'s `AgentRuntimeArtifact.from_code_asset`.
- **Logging_Configurator**: The new module that installs and configures log delivery for the Agent_Runtime_Process.
- **Application_Logger**: Any `logging.Logger` obtained by `logging.getLogger(__name__)` inside `backend/agent/` or `backend/shared/`.
- **SDK_Logger**: The logger named `bedrock_agentcore.app`, created and given its own handler by the AgentCore Runtime SDK.
- **Third_Party_Logger**: Any logger owned by an installed distribution rather than by this project — `botocore`, `boto3`, `urllib3`, `httpx`, `opentelemetry`, `strands`, and their descendants.
- **Log_Handler**: A `logging.Handler` that writes formatted Log_Records to a destination.
- **Log_Record**: One formatted record written to the Agent_Runtime_Process's standard output stream.
- **Effective_Log_Level**: The severity threshold the Logging_Configurator applies to Application_Loggers.
- **Default_Log_Level**: The Effective_Log_Level used when Log_Level_Variable supplies no usable value.
- **Log_Level_Variable**: The environment variable `LOG_LEVEL`, read by the Logging_Configurator, set by `CDK_Stack` alongside `GUARDRAIL_ID` and `PDF_OCR_DPI`.
- **Log_Content_Rule**: The rule governing which values a Log_Record may carry, defined by Requirement 2.
- **Sensitive_Value**: Any of: Document_Text, Matched_Value, Source_Object_Identifier, Rule_Pattern.
- **Document_Text**: Any substring of an `ExtractedTextUnit.text`, an `ImageRegion.recognizedText`, an OCR text unit's text, or the raw bytes of a source or redacted document.
- **Matched_Value**: The text a detection matched — a Bedrock Guardrails `piiEntities[].match` value, a regex match, or an LLM-returned match string.
- **Source_Object_Identifier**: A `RedactionJob.sourceKey`, `RedactionJob.redactedKey`, or `RedactionJob.fileName` value, or any substring of one.
- **Rule_Pattern**: A `RedactionRule.pattern` value.
- **Safe_Reference**: A value that identifies what a Log_Record concerns without carrying a Sensitive_Value — a `jobId`, a Unit_Ref, a character offset, a character count, a byte count, a page index, a PII entity type, a rule name, an environment variable name, or a SHA-256 digest of Document_Text.
- **Unit_Ref**: The namespaced `MatchSpan.unitRef` string — `"text:{index}"`, `"image:{index}"`, or `"ocr:{index}"`.
- **Orchestration_Pipeline**: The fixed-order sequence inside `handle_job` (`backend/agent/orchestration.py`).
- **Lambda_Handlers**: The eight handler modules under `backend/lambdas/`.
- **Runtime_Log_Group**: The CloudWatch Logs group the AgentCore service creates for the deployed runtime, currently `/aws/bedrock-agentcore/runtimes/pii_redaction_agent-gGlsFL4Jwi-DEFAULT`.
- **Retention_Period**: The finite number of days after which CloudWatch Logs deletes events from the Runtime_Log_Group.
- **CDK_Stack**: `RedactionStack` (`backend/infra/redaction_stack.py`).

## Requirements

### Requirement 1: Application log records reach CloudWatch from the deployed runtime

**User Story:** As an operator of a compliance tool, I want the agent's log records to arrive in CloudWatch Logs, so that the retry, fallback and over-redaction warnings the code already writes can actually be read.

#### Acceptance Criteria

1. THE Logging_Configurator SHALL be a version-controlled module under `backend/agent/`.
2. WHEN the Logging_Configurator runs, THE Logging_Configurator SHALL install one Log_Handler that writes each Log_Record to the Agent_Runtime_Process's standard output stream.
3. WHEN the Logging_Configurator runs a second time within one Agent_Runtime_Process, THE Logging_Configurator SHALL leave the set of installed Log_Handlers unchanged.
4. WHEN an Application_Logger emits a record at or above the Effective_Log_Level, THE Agent_Runtime_Process SHALL write exactly one Log_Record for it to standard output.
5. THE Logging_Configurator SHALL deliver records from every Application_Logger under `backend/agent/` and `backend/shared/` without any of those modules installing a Log_Handler of its own.
6. WHILE the SDK_Logger carries its own Log_Handler, THE Agent_Runtime_Process SHALL write exactly one Log_Record per record the SDK_Logger emits.
7. THE Agent_Entrypoint_Module SHALL invoke the Logging_Configurator before it dispatches an invocation to the Orchestration_Pipeline.
8. THE Agent_Bundle's Agent_Entrypoint_Module SHALL be produced from a version-controlled source file.
9. WHERE a module sets a level directly on its own Application_Logger, THE Logging_Configurator SHALL still apply the Effective_Log_Level to that logger.

### Requirement 2: No sensitive value appears in a log record

**User Story:** As a compliance officer, I want a stated rule about what a log record may contain and every existing statement checked against it, so that turning on log delivery in a redaction product does not create the exact leak the product exists to prevent.

#### Acceptance Criteria

1. THE Log_Records the Agent_Runtime_Process writes SHALL exclude Document_Text.
2. THE Log_Records the Agent_Runtime_Process writes SHALL exclude Matched_Value.
3. WHERE a Log_Record identifies the text a decision concerned, THE Application_Logger SHALL identify it by Safe_Reference.
4. THE Log_Records the Agent_Runtime_Process writes SHALL exclude Source_Object_Identifier.
5. THE Log_Records the Agent_Runtime_Process writes SHALL exclude Rule_Pattern.
6. WHILE the Effective_Log_Level is more verbose than `INFO`, THE Logging_Configurator SHALL hold every Third_Party_Logger at a level no more verbose than `INFO`.
7. WHEN an Application_Logger records a caught exception, THE Log_Record SHALL carry the exception's type name, the exception's message, and the traceback.
8. IF an exception's message would carry a Sensitive_Value into a Log_Record, THEN THE Application_Logger SHALL replace that value with a Safe_Reference before emitting the record.
9. THE Log_Records the Lambda_Handlers write SHALL comply with criteria 2.1 through 2.5.

### Requirement 3: Records are structured and dependency-free

**User Story:** As a developer diagnosing a job, I want each record to be a single JSON object with the same shape the SDK already emits, so that counting questions can be answered with a CloudWatch Logs Insights query instead of a regex over prose.

#### Acceptance Criteria

1. THE Log_Record SHALL be a single-line JSON object.
2. THE Log_Record SHALL carry a UTC timestamp, a level name, the emitting logger's name, and the formatted message.
3. WHERE a record carries exception information, THE Log_Record SHALL carry the exception type name, the exception message, and the traceback as distinct JSON fields.
4. THE Log_Record's field names SHALL match the field names the SDK_Logger's formatter emits for the same concepts.
5. THE Logging_Configurator SHALL import only Python standard library modules.
6. IF a value in a Log_Record cannot be serialized to JSON, THEN THE Log_Handler SHALL write the record with that value rendered as its string representation.

### Requirement 4: The level is configurable and its default is deliberate

**User Story:** As an operator, I want to raise the agent's log verbosity through configuration the way `PDF_OCR_DPI` and `GUARDRAIL_ID` are configured, so that a diagnostic run does not need a code change and a normal run does not pay for per-detection records.

#### Acceptance Criteria

1. THE Logging_Configurator SHALL read the Effective_Log_Level from the Log_Level_Variable.
2. THE Default_Log_Level SHALL be `INFO`.
3. IF the Log_Level_Variable is absent or holds only whitespace, THEN THE Logging_Configurator SHALL apply the Default_Log_Level.
4. IF the Log_Level_Variable holds a value that names no Python logging level, THEN THE Logging_Configurator SHALL apply the Default_Log_Level and emit one Log_Record naming the variable and reporting the level applied.
5. THE Logging_Configurator SHALL accept level names irrespective of letter case.
6. THE CDK_Stack SHALL set the Log_Level_Variable in the AgentCore Runtime's environment variables.
7. WHILE the Effective_Log_Level is `DEBUG`, THE Agent_Runtime_Process SHALL write a Log_Record for the digit-boundary filter's narrowed-occurrence decision.
8. WHILE the Effective_Log_Level is `INFO`, THE Agent_Runtime_Process SHALL write a Log_Record for the digit-boundary filter's reject-all fallback and for the `OCCURRENCE_WARN_THRESHOLD` over-redaction warning.

### Requirement 5: Volume and retention are bounded

**User Story:** As the owner of the AWS bill and of a data-retention policy, I want log volume proportional to what happened and a finite retention window, so that enabling delivery does not create an unbounded, indefinitely-retained store of records about redaction jobs.

#### Acceptance Criteria

1. WHILE the Effective_Log_Level is `INFO`, THE Application_Loggers SHALL write no Log_Record per individual PII detection.
2. WHILE the Effective_Log_Level is `INFO`, THE Application_Loggers SHALL write no Log_Record per text unit that produced no warning.
3. THE Runtime_Log_Group SHALL have a finite Retention_Period.
4. WHERE the AgentCore service creates the Runtime_Log_Group rather than the CDK_Stack, THE CDK_Stack SHALL set the Retention_Period on that group.
5. IF the Retention_Period cannot be set from the CDK_Stack, THEN THE design document SHALL record the mechanism used instead and the reason.

### Requirement 6: The Lambda handlers keep their existing configuration

**User Story:** As a reviewer of this change, I want the spec to say plainly whether the Lambda handlers need touching, so that the change stays as small as the defect and nobody reconfigures logging in eight working handlers on the assumption that it is broken there too.

#### Acceptance Criteria

1. THE Lambda_Handlers SHALL retain their existing `logging.getLogger(__name__)` plus `setLevel` configuration.
2. THE Lambda_Handlers SHALL rely on the AWS Lambda runtime's own root Log_Handler for delivery, installing no Log_Handler of their own.
3. THE Lambda_Handlers SHALL obtain their loggers without importing the Logging_Configurator.
4. WHEN a Lambda_Handler's log statement carries a Sensitive_Value, THE Lambda_Handler SHALL replace that value with a Safe_Reference.
