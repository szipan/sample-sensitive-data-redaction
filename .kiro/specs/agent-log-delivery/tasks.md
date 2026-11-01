# Implementation Plan: Agent Log Delivery

## Overview

Implemented in Python (`backend/agent`, `backend/lambdas`, `backend/infra`, `backend/scripts`), matching the existing codebase. The order follows the real dependencies: the Logging_Configurator exists before anything invokes it, the tracked entrypoint and its sync script arrive together with the import-time call, the three call-site sanitizations are independent of each other and of everything else, and the CDK changes touch nothing the tests exercise.

Most of the length here is content, not plumbing. The handler is one module; the reason this spec has eleven tasks is that 15 statements which have never left the process start landing in CloudWatch the moment it exists, and two indirect channels can carry document text into them.

Two tasks carry more risk than their size suggests:

- **Task 2** builds the two fixtures. Six of the eight properties assert through them, and both have a specific way of being wrong: a capture fixture that asserts on `caplog.records` tests neither the filter nor the formatter, and a fake-pipeline fixture whose raising mode is treated as optional declares the exception-message channel clean without ever exercising it.
- **Task 7.3** is a leak happening in production right now. Lambda logs already work, so `rule_operation_processor.py:209` is writing the user-authored rule pattern to CloudWatch today. It is a task, not a note.

Nothing in `shared/models.py` changes. Nothing in the eight Lambda handlers' logging configuration changes — task 7.5 exists to prove that rather than to do it.

## Tasks

- [x] 1. Build the Logging_Configurator
  - [x] 1.1 Create `backend/agent/logging_config.py` with its constants, `resolve_log_level`, and `describe_exception`
    - Import only `json`, `logging`, `os`, `sys`, `traceback`, `datetime`, `typing` — no third party, no AWS Lambda Powertools
    - Define `OWNED_LOGGER_ROOTS = frozenset({"agent", "shared"})`, `LOG_LEVEL_ENV_VAR = "LOG_LEVEL"`, `DEFAULT_LOG_LEVEL = logging.INFO`, `_HANDLER_MARKER`
    - Define `_LEVEL_NAMES` as an explicit `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG` mapping, not `logging.getLevelNamesMapping()`, with a comment recording that the explicit form refuses `NOTSET` and numeric strings — both misconfigurations rather than intentions
    - Implement `resolve_log_level(raw) -> (level, warning_or_None)`: `raw.strip().upper()` against `_LEVEL_NAMES`, default on absent/blank/unrecognised, and **return** rather than emit the warning, since no handler exists at the point the level is resolved
    - Implement `describe_exception(error)`: `str(error)` only when the exception's own module root is in `OWNED_LOGGER_ROOTS`; a duck-typed `response["Error"]["Code"]` branch rendering `"AWS error code {code}"`; `"<message omitted: unaudited exception type>"` otherwise
    - Document why the default is omission: an exception type nobody has audited should be silent rather than leak, and `errorType` always survives to search on
    - _Requirements: 2.8, 3.5, 4.1, 4.3, 4.4, 4.5_

  - [ ]* 1.2 Write property test for level resolution
    - **Property 6: Level resolution is total and case-insensitive**
    - **Validates: Requirements 4.1, 4.3, 4.4, 4.5**
    - Bias the generator toward the adversarial near-misses — `" info "`, `"Debug"`, `"warn"`, `"10"`, `"NOTSET"`, `""`, `"   "`; `"10"` and `"NOTSET"` are the two an implementation built on `logging.getLevelName` gets wrong in opposite directions

  - [x] 1.3 Implement `OwnedLoggerFilter` and `JsonLogFormatter`
    - `OwnedLoggerFilter.filter` is one expression — `record.name.split(".", 1)[0] in OWNED_LOGGER_ROOTS or record.levelno >= logging.INFO` — unconditional, consulting no environment variable, so no configuration turns it off
    - Enumerate no third-party logger names anywhere: this is an allow-list precisely so it stays closed when a dependency is added
    - `JsonLogFormatter.format` emits one line of `json.dumps(entry, ensure_ascii=False, default=str)` carrying `timestamp`, `level`, `message`, `logger`, `location`, plus `errorType` / `errorMessage` / `stackTrace` when `record.exc_info` is set
    - `timestamp` from `datetime.now(timezone.utc)` formatted `%Y-%m-%dT%H:%M:%S.%f`[:-3] + `"Z"`, matching the SDK's rendering without its deprecated `utcnow()`
    - `stackTrace` uses `traceback.format_tb(exc_info[2])` — frames only, so neither `str(exc)` nor the `__cause__` / `__context__` chain is rendered; `errorMessage` uses `describe_exception`, the only place `str(exc)` is consulted
    - Guard message interpolation: on failure emit `str(record.msg)` plus a `formatError` field carrying only an exception type name, never `record.args`
    - The formatter never re-raises — `logging` swallows formatter exceptions and drops the event, which is the worst outcome available to a feature about not dropping events
    - _Requirements: 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 3.6_

  - [ ]* 1.4 Write property test for record serialization
    - **Property 5: Every record serializes to exactly one parseable line**
    - **Validates: Requirements 2.7, 3.1, 3.2, 3.3, 3.6**
    - Generators must include arguments carrying newlines, CJK characters and control characters, and objects that cannot be JSON-serialized; a newline-bearing argument is the specific hazard, because a record spanning two lines becomes two CloudWatch events and any query counting events over-counts

  - [x] 1.5 Implement `configure_logging`
    - In order: resolve the level; find-or-create a `StreamHandler(sys.stdout)` tagged with `_HANDLER_MARKER` and reuse it if root already carries one; `root.setLevel(max(level, logging.INFO))`; set the level on each of `OWNED_LOGGER_ROOTS`; sweep `logging.root.manager.loggerDict` setting the level on every already-created logger with an owned root component; set `propagate = False` on `bedrock_agentcore.app`; emit the deferred warning; return the handler
    - Use the handler attribute marker rather than a module-level `_configured` flag, so a second call re-applies levels without adding a second handler
    - Comment that `root.setLevel(max(level, INFO))` is a floor, not the configured level, and is the primary enforcement of Requirement 2.6 — `botocore.endpoint`'s `logger.debug("Request body: %s", body)` is never constructed, and `LOG_LEVEL=DEBUG` cannot widen it
    - Return the handler so tests can drive the filter and formatter directly
    - _Requirements: 1.1, 1.2, 1.3, 1.6, 1.9, 2.6, 4.1, 4.2_

- [x] 2. Build the two fixtures the property tests depend on
  - [x] 2.1 Add the handler-output capture fixture
    - Add to `backend/tests/conftest.py`: call `configure_logging()`, redirect the returned handler's stream to an in-memory buffer, yield the buffer, and restore root's handlers and levels afterwards
    - Every output assertion in tasks 3 and 9 goes through this buffer, **not** `caplog.records` — `caplog` attaches its own unfiltered, unformatted handler, so a `caplog`-based assertion would bypass both the filter and the formatter, which are the two things under test
    - Leave `caplog` working: it stays the right tool for the existing tests that assert a statement fired
    - _Requirements: 1.2, 1.4_

  - [x] 2.2 Add the fake-pipeline fixture with a mandatory raising mode
    - Stand up fakes for `bedrock-runtime`, `textract`, S3 and DynamoDB and drive `handle_job` end to end on generated bytes
    - The raising mode is not an extra case: on roughly half the draws `apply_guardrail` or `detect_document_text` must raise with a marker value embedded in the exception's own message, because that is the channel `exc_info=True` and the two interpolating sites open, and a fixture that only returns detections would declare it clean
    - Expose a knob for planting marker values as document text, matched values, S3 keys, file names and rule patterns, so Property 1 can assert over all five Sensitive_Value classes at once
    - _Requirements: 2.1, 2.2, 2.4, 2.5_

- [ ] 3. Property tests and unit tests for the Configurator's behaviour
  - [ ]* 3.1 Write property test for configuration idempotence
    - **Property 3: Configuration is idempotent**
    - **Validates: Requirements 1.3**
    - `f(x) = f(f(x))` over the handler set — this is what makes task 5.3's import-time call safe alongside the entrypoint's explicit one

  - [ ]* 3.2 Write property test for owned-logger levels and single delivery
    - **Property 4: Owned loggers carry the Effective_Log_Level and deliver exactly once**
    - **Validates: Requirements 1.4, 1.5, 1.9**
    - Pre-create each generated `agent.*` / `shared.*` logger with an arbitrary explicit level before configuring, which is the shape `runtime.py`'s `setLevel(logging.INFO)` had

  - [ ]* 3.3 Write property test for the third-party INFO floor
    - **Property 2: Third_Party_Loggers stay at the INFO floor for every LOG_LEVEL**
    - **Validates: Requirements 2.6**
    - Generate the logger name rather than naming `botocore`: a test that names `botocore` also passes against a deny-list, which fails open for the next dependency added
    - Cover both clauses — the inherited floor, and the filter rejecting a sub-INFO record from a logger that gave itself an explicit DEBUG level

  - [ ]* 3.4 Write unit tests for the example-classified criteria
    - `configure_logging` installs exactly one marked `StreamHandler` on `sys.stdout` (_Requirements: 1.2_)
    - `bedrock_agentcore.app` has `propagate = False`, such that a simulated SDK record yields exactly one line across both handlers (_Requirements: 1.6_)
    - The formatter's key set equals the SDK's `RequestContextFormatter` key set, with and without exception info — this is where the SDK's field names are pinned, since prose in a design document does not prevent a rename (_Requirements: 3.4_)
    - `logging_config.py` imports only names in `sys.stdlib_module_names`, asserted by parsing the module with `ast` (_Requirements: 3.5_)
    - `DEFAULT_LOG_LEVEL is logging.INFO` (_Requirements: 4.2_)
    - _Requirements: 1.2, 1.6, 3.4, 3.5, 4.2_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Put the entrypoint under version control and configure on import
  - [x] 5.1 Create `backend/agent/main.py` as the tracked Agent_Entrypoint_Source
    - An exact copy of what the Agent_Bundle root must contain: `sys.path` insert, `from agent.logging_config import configure_logging`, `configure_logging()` at import, then the `BedrockAgentCoreApp` wiring
    - The `@app.entrypoint` coroutine lazily imports `agent.runtime.process_invocation` and returns its result — replacing the current bundle copy's hand-rolled payload parsing and silent `except Exception`, which returns `{"status": "error"}` with no record at all
    - Configuring at import is safe here precisely because `agent.logging_config` imports only the standard library, so it costs nothing against AgentCore's 30-second initialisation limit that the lazy-import discipline exists to protect
    - Keep `entrypoint=["main.py"]` in the stack — moving the entrypoint into the package is attractive but the working-directory and `sys.path` semantics of a subdirectory path are undocumented for `AgentRuntimeArtifact.from_code_asset`, and a deployment-only failure mode is a bad trade for deleting the sync script
    - _Requirements: 1.7, 1.8_

  - [x] 5.2 Create `backend/scripts/sync_agent_entrypoint.py`
    - Copy `backend/agent/main.py` to `backend/agent_bundle/package_clean/main.py`, failing loudly with a non-zero exit when the bundle directory is absent
    - Without this, "produced from a version-controlled source file" is a convention in a docstring — and the bundle copy on disk today already differs from what task 5.1 tracks
    - _Requirements: 1.8_

  - [x] 5.3 Call `configure_logging()` from `backend/agent/__init__.py`
    - Currently empty; add the import-time call so delivery holds regardless of what the untracked bundle copy contains, since every path to `handle_job` imports the `agent` package first
    - Record in a comment why an import-time side effect is acceptable here: `backend/agent/` is not a library, it has one consumer, the call is idempotent, additive, stdlib-only, and does not disturb `pytest`'s capture or `caplog`
    - _Requirements: 1.3, 1.5, 1.7_

  - [ ]* 5.4 Write unit tests for the entrypoint and the static no-handler guarantee
    - Root carries a marked handler immediately after importing `agent/main.py`, before any invocation, with `bedrock_agentcore.runtime` stubbed (_Requirements: 1.7_)
    - `sync_agent_entrypoint.py` produces a byte-identical copy, and exits non-zero when the bundle directory is absent (_Requirements: 1.8_)
    - No module under `backend/agent/` or `backend/shared/` calls `addHandler` or `basicConfig`, parsed with `ast` (_Requirements: 1.5_)
    - _Requirements: 1.5, 1.7, 1.8_

- [x] 6. Remove the dead code from `agent/runtime.py`
  - [x] 6.1 Delete the four unreachable definitions, the stray level call, and rewrite the docstring
    - Delete `build_agentcore_app()`, `main()`, the `__main__` block, and `build_redaction_agent()` — all four provably dead, since the deployed entrypoint never imports this module
    - Delete `logger.setLevel(logging.INFO)` at line 67: the level is the Configurator's to own, and leaving it makes the `loggerDict` sweep load-bearing for no reason
    - Rewrite the module docstring to say what the module now is — the SDK-independent invocation-handling core, with `backend/agent/main.py` owning the `BedrockAgentCoreApp` wiring
    - Keep `extract_job_id`, `process_invocation`, the status/error-code constants and `InvalidInvocationPayloadError` untouched; no test calls any of the four deleted names, so this is test-neutral
    - Deleting `build_redaction_agent()` is **not** a withdrawal of Strands support. It registers the whole fixed-order pipeline as a *single* tool with no model-driven planning — a vestige of the deterministic-pipeline decision that made Strands unnecessary here, and not a useful reference for real Strands work, which will have actual tool sequences and actual planning. `strands-agents` stays in `requirements.txt`; see task 11.1
    - _Requirements: 1.9_

- [x] 7. Sanitize the three call sites that can carry a Sensitive_Value
  - [x] 7.1 Sanitize `guardrails_pii_detect.py:277`
    - `..., error` → `..., describe_exception(error)`
    - The `ApplyGuardrail` request body *is* the document text, so a `botocore` `ParamValidationError` can echo the document back through `str(exc)`
    - _Requirements: 2.8_

  - [x] 7.2 Sanitize `ocr_pdf.py:329`
    - `..., error` → `..., describe_exception(error)`
    - Same channel; the `DetectDocumentText` request body is the rasterized page image
    - _Requirements: 2.8_

  - [x] 7.3 Stop `rule_operation_processor.py:209` logging the whole SQS record
    - `"...: %s", record` → `"... (messageId %s, operationId %s)", record.get("messageId"), <operationId>`, reading `operationId` defensively, since `json.loads(record["body"])` may be what failed
    - The record's `body` carries the user-authored `pattern`, and **Lambda logs already work, so this is leaking in production today** — this is the one change here that fixes a live defect rather than preventing a new one
    - Leave the handler's `getLogger(__name__)` + `setLevel(INFO)` configuration alone and do not import the Configurator
    - _Requirements: 2.5, 2.9, 6.4_

  - [ ]* 7.4 Write property test for the Rule Operation failure record
    - **Property 8: A failed Rule Operation record excludes the Rule_Pattern**
    - **Validates: Requirements 2.9, 6.4**
    - Kept out of Property 1 because it exercises a Lambda relying on the AWS runtime's own root handler, with no Configurator involved; folding it in would force one fixture to stand up two incompatible logging setups

  - [ ]* 7.5 Write the negative-space test for the eight Lambda handlers
    - Parse every module under `backend/lambdas/` with `ast` and assert each calls `getLogger` and `setLevel`, calls neither `addHandler` nor `basicConfig`, and does not import `agent.logging_config`
    - Worth having precisely because the instinct on reading this spec is to "fix" logging in eight handlers that already work — the asymmetry is environmental, not a code difference
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 8. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Content and volume properties over the whole pipeline
  - [x]* 9.1 Write property test for Sensitive_Value exclusion
    - **Property 1: No Sensitive_Value reaches an emitted record**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.8**
    - Assert over the handler's **serialized output**, not `caplog.records`: a record's `args` are innocent until `getMessage()` and the formatter render them, and the leak lives in the rendering
    - Assert substring, not equality — a marker embedded in a longer string still counts
    - Drive the fake-pipeline fixture's raising mode on at least half the draws

  - [ ]* 9.2 Write property test for record-volume independence
    - **Property 7: Record volume is independent of detection and unit counts**
    - **Validates: Requirements 5.1, 5.2**
    - Stated as independence rather than an absolute bound, so it keeps working if a future change adds one record per *job*

  - [ ]* 9.3 Write unit tests for the level gating of the digit-boundary statements
    - The narrowed-occurrence record appears under `LOG_LEVEL=DEBUG` and not under `INFO` (_Requirements: 4.7_)
    - The reject-all fallback warning and the `OCCURRENCE_WARN_THRESHOLD` over-redaction warning appear under both (_Requirements: 4.8_)
    - This pins the accepted consequence of defaulting to `INFO`, so a future edit cannot quietly demote the two warnings that have to survive it
    - _Requirements: 4.7, 4.8_

- [x] 10. Configure `LOG_LEVEL` and retention in the stack
  - [x] 10.1 Add `LOG_LEVEL` and the log-group retention to `redaction_stack.py`
    - Add `"LOG_LEVEL": "INFO"` to the AgentCore Runtime's environment variables alongside `GUARDRAIL_ID` / `GUARDRAIL_VERSION` / `PDF_OCR_DPI` — the default made explicit, so raising verbosity means editing a visible value rather than discovering an absent variable
    - Add `logs.LogRetention(self, "AgentRuntimeLogRetention", log_group_name=self.agent_runtime.application_log_group.log_group_name, retention=logs.RetentionDays.THREE_MONTHS)`
    - Leave `removal_policy` at its `RETAIN` default: `cdk destroy` should not take the last quarter's operational record with it, unlike the source and output buckets which are `DESTROY` on purpose
    - Comment that the group name embeds `AgentRuntimeId`, so replacing the runtime produces a new group and leaves the old one at whatever retention it had — acceptable for a POC, a job for a future cleanup task
    - _Requirements: 4.6, 5.3, 5.4_

  - [ ]* 10.2 Write CDK template assertions for the variable and the retention resource
    - The AgentCore Runtime's environment variables include `LOG_LEVEL` (_Requirements: 4.6_)
    - A `Custom::LogRetention` resource exists with a finite `RetentionInDays` and a `LogGroupName` referencing the runtime's `AgentRuntimeId` (_Requirements: 5.3, 5.4_)
    - Deterministic against one synthesized template, so one execution each
    - _Requirements: 4.6, 5.3, 5.4_

- [x] 11. Convert the two runtime SDK pins from wheel-verified to import-verified
  - [x] 11.1 Install `strands-agents==1.50.2` and `bedrock-agentcore==1.20.0` into `backend/.venv`
    - Both pins currently carry the caveat "NOT import-verified locally" because both SDKs are absent from the venv; installing them replaces wheel-availability evidence with an actual import
    - Expect a substantial transitive tail — `mcp`, `opentelemetry`, `pydantic`, `pydantic_settings`, `docstring_parser`, `sse_starlette`
    - Update the `requirements.txt` comment block: drop the "deliberately NOT installed in backend/.venv" and "NOT import-verified locally" notes, and drop the claim that `strands` is used by `build_redaction_agent`, which task 6.1 deletes. `strands-agents` **stays** — future agent development on this project will use the Strands Agents SDK, so it is a deliberate forward-looking dependency, not dead weight
    - Re-run the full suite afterwards and confirm the baseline is unchanged; a transitive tail this size is the realistic way this task regresses something
    - Verify `import strands` and `from bedrock_agentcore.runtime import BedrockAgentCoreApp` both succeed, and that the APIs the code uses still exist

- [x] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Task 9.1 is the one worth not skipping: it is the only test that checks the claim this whole spec rests on, that turning delivery on does not create the leak the product exists to prevent.
- Task 7.3 is not optional and is not a cleanup. It fixes a live production leak; the other two sanitizations prevent one that does not exist yet.
- Task 2 comes before every property test on purpose. Six of the eight properties assert through those two fixtures, and both have a specific failure mode — see the task bodies.
- No changes to `shared/models.py` or `backend/lambdas/shared/models.py`. Nothing here adds, removes or reshapes a dataclass, which is worth stating because every previous detection-source feature in this project needed one.
- No changes to the eight Lambda handlers' logging configuration. Task 7.5 asserts that statically, because Requirement 6 exists to stop someone reconfiguring logging in handlers that work.
- No new third-party dependencies. The Configurator is standard library only, and Hypothesis is already in `requirements-dev.txt`.
- Every property test uses Hypothesis, runs at least 100 iterations, and is tagged `Feature: agent-log-delivery, Property {number}: {property title}`.
- Bedrock Runtime, Textract, S3 and DynamoDB are faked in all property tests. No task here makes a real service call.
- Backend tests run as a single, order-independent process:
  ```
  cd backend; .venv\Scripts\python.exe -m pytest tests -q
  ```
  Current baseline is **258 passed**. The old two-command, `test_regroup_and_merge.py`-first instruction from earlier specs no longer applies: that file now attempts the real `fitz` / `docx` import and stubs only on `ImportError`, and `python-docx==1.1.2` is installed, so collection order no longer changes behaviour.
- One known pre-existing flake: `test_property_7_provenance_merged_span_lists_every_contributing_name` has intermittently failed with Hypothesis `DeadlineExceeded` (285ms against a 200ms deadline) on a cold suite and passed on re-run. If a task run hits it, it is a timing artifact — the fix is `deadline=None` on that property, not a logic change.
- **The one thing no coding task here can verify: that records actually arrive in the CloudWatch group.** That is a manual post-deploy check — run `sync_agent_entrypoint.py`, deploy, invoke a job, and confirm `/aws/bedrock-agentcore/runtimes/{id}-DEFAULT` holds events from `agent.*` loggers where it previously held only the 4 `bedrock_agentcore.app` events. Until that check is done, this feature is verified in the test suite and unverified in production.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "10.1", "11.1"] },
    { "id": 1, "tasks": ["1.3", "10.2"] },
    { "id": 2, "tasks": ["1.2", "1.4", "1.5"] },
    { "id": 3, "tasks": ["2.1", "5.1", "6.1", "7.1", "7.2", "7.3"] },
    { "id": 4, "tasks": ["2.2", "5.2", "5.3"] },
    { "id": 5, "tasks": ["3.1", "3.2", "3.3", "3.4", "5.4", "7.4", "7.5", "9.3"] },
    { "id": 6, "tasks": ["9.1", "9.2"] }
  ]
}
```
