# Requirements Document

## Introduction

Chinese PII in this POC currently depends on a single detection source, and that source can go blind.

The measured situation, per detection source:

| Source | Chinese support | Measured on `chi.pdf` / `chi.docx` |
|---|---|---|
| Amazon Textract `DetectDocumentText` | Not supported. AWS's Textract FAQ scopes detection to "the Standard English alphabet and ASCII symbols" and text extraction to English, German, French, Spanish, Italian and Portuguese. | Page 0 returned **zero CJK characters**. Every CJK run collapsed to Latin garbage (`'1'`, `'E'`, `'ETE'`, `'SKA'`); only ASCII content came back. |
| Bedrock Data Automation | Not supported. `bda-limits.html` lists the same six document languages and states BDA "does not support vertical text … as is common in languages like Japanese and Chinese". | 54 `text_words` with excellent, correctly-placed normalized bounding boxes and **zero CJK characters**. Right boxes, wrong text. Evidence in `D:\Users\szipan\Downloads\bda-chinese-eval\`. |
| Bedrock Guardrails Sensitive Information Filters | Supported ("Optimized and supported"), and operates on text, so it works whenever the text layer is intact. | Still misses values: the VIN `LSVAU2A89CN123456` is never detected in either `chi.pdf` or `chi.docx`, despite `VEHICLE_IDENTIFICATION_NUMBER` being in the enabled entity list. |

So for a Chinese document, CJK-only PII — personal names, addresses, licence plates — reaches the redactor through exactly one route: the PDF/DOCX text layer feeding Guardrails. The `pdf-dual-path-pii-detection` feature added a second path for PDFs, but that second path is Textract, which cannot read Chinese. A Chinese PDF that *also* carries a damaged ToUnicode CMap — the precise defect class the dual-path feature was built for — loses both routes to CJK PII simultaneously, and the job still reports `completed`.

This feature adds a third detection source: send each extracted text unit to a Bedrock foundation model, have the Model return PII spans **as text**, and locate those spans with the same occurrence-scan machinery `guardrails_pii_detect.py` already uses (commit `790de95`). The Model never returns coordinates.

Four constraints are settled and are not open for redesign:

1. **Additive only.** The LLM may only *add* redactions. There is deliberately no "adjudicate whether an existing detection is a false positive and unredact it" capability. An LLM false negative is a silent, invisible leak — the exact failure class this codebase has spent significant effort eliminating. An LLM false positive is a visible black box, which a reviewer can see and recover from. That asymmetry is the entire safety argument for admitting a probabilistic source into a compliance pipeline.
2. **Never-raise, non-fatal.** The tool mirrors `detect_pii_with_guardrails` exactly: it swallows every failure, returns partial or empty results, and can never drive a Redaction Job to `failed`. It supplements; it is not a primary path. This is the opposite of the OCR path, which is primary for PDF and deliberately does fail the job.
3. **Determinism and auditability are constrained, not achieved.** The pipeline is explicitly "固定顺序的确定性流水线（不依赖 LLM 自由规划工具调用顺序，避免非确定性影响可测试性）" (`agent/runtime.py`). A generative model makes *output* non-reproducible even though the *control flow* stays fixed. For a compliance artifact at a listed company that is a real cost, so the feature pins what can be pinned — temperature 0, a pinned model id rather than a floating alias, a versioned prompt — and records what produced each span so a run can be explained to an auditor even though it cannot be replayed byte-for-byte.
4. **A plain tool, not MCP, not a Skill, not a sub-agent.** A module under `backend/agent/tools/` exposing a plain function, called from `handle_job` at a fixed position, structurally identical to `detect_pii_with_guardrails`.

An **explicit dependency**: the only operational control over this feature's over-redaction is observing its behaviour, and application logs currently never reach CloudWatch. `agent/runtime.py` sets a level on its module logger but never installs a handler or calls `basicConfig`, so application records go to a handler-less root logger and AgentCore ingests nothing — the runtime log group holds only `bedrock_agentcore.app`'s own structured events. That is a pre-existing defect outside this feature's scope and probably warrants its own small spec, but every observability criterion below is inert until it is fixed. The design document records it as a dependency and risk.

`unitRef` namespaces do not change. `text:{i}` / `image:{i}` / `ocr:{i}` already exist, and `merge_spans_within_unit` / `_regroup_and_merge` group on the raw `unitRef` string. The LLM source reads the same units, so it emits spans in those same namespaces: no fourth namespace, no merge-logic change.

## Glossary

- **Redaction_Agent**: The AgentCore-hosted deterministic pipeline (`backend/agent/orchestration.py`, `handle_job`) that extracts, matches, redacts, verifies, stores, and updates the Redaction Job.
- **Orchestration_Pipeline**: The fixed-order tool sequence inside `handle_job`: extract → embedded-image OCR → page OCR (`.pdf` only) → regex matching → Guardrails detection → LLM detection (new) → merge → redact → verify → store → update.
- **Text_Unit**: An `ExtractedTextUnit` from the document's text layer, an `ImageRegion` whose `recognizedText` is populated, or an OCR_Text_Unit from `ocr_pdf_pages` — i.e. any of the three unit collections `detect_pii_with_guardrails` already accepts.
- **Unit_Ref**: The namespaced `MatchSpan.unitRef` string identifying which unit a span's offsets belong to — `"text:{index}"`, `"image:{index}"`, or `"ocr:{index}"`.
- **Model**: The Amazon Bedrock foundation model, addressed by Model_Id, that classifies PII in a Text_Unit's text.
- **Model_Id**: The pinned Bedrock model identifier or inference profile identifier used to address the Model — a specific, versioned identifier, never a floating alias.
- **LLM_Detection_Tool**: The new plain Python tool (`backend/agent/tools/llm_pii_detect.py`) that sends Text_Unit text to the Model and converts the returned detections into LLM_MatchSpans.
- **LLM_Request**: One Bedrock model invocation carrying the Prompt_Template and one Text_Unit's text.
- **PII_Detection**: One entry of the Model's response, comprising a verbatim matched-text string and one PII_Entity_Type.
- **PII_Entity_Type**: A PII category label drawn from the Shared_Entity_Vocabulary.
- **Shared_Entity_Vocabulary**: The fixed set of PII category labels the Model is instructed to choose from. It is the union of the Bedrock Guardrails Sensitive Information Filter entity type names already configured in `CDK_Stack`'s `PiiGuardrail` — so both ML sources describe shared categories with the same words — and the CJK_Extension_Types.
- **CJK_Extension_Types**: The PII category labels added to the Shared_Entity_Vocabulary for identifier classes that Guardrails' entity list, whose jurisdiction-specific entries cover only the US, Canada, and the UK, cannot name at all — most importantly the mainland Chinese resident identity number.
- **LLM_MatchSpan**: A `MatchSpan` produced from a PII_Detection, carrying `ruleName` `"llm:{PII_Entity_Type}"`.
- **Guardrails_Detection_Tool**: The existing `detect_pii_with_guardrails` (`backend/agent/tools/guardrails_pii_detect.py`).
- **Occurrence_Scan**: The offset-recovery mechanism already implemented in `guardrails_pii_detect.py` — locate every non-overlapping occurrence of a matched-text string within the exact string that was sent to the service, then apply the Digit_Boundary_Rule.
- **Digit_Boundary_Rule**: The filter applied by Occurrence_Scan when a matched text consists entirely of ASCII digits: reject any occurrence immediately preceded **or** immediately followed by an ASCII digit; if that rejects every occurrence, emit all of them anyway.
- **Span_Merging_Logic**: `merge_spans_within_unit` (`backend/agent/tools/matching.py`) together with `_regroup_and_merge` (`backend/agent/orchestration.py`), which group spans by Unit_Ref before merging.
- **Prompt_Template**: The module-level instruction text sent with every LLM_Request, carrying a Prompt_Version identifier.
- **Prompt_Version**: A string identifying one exact revision of the Prompt_Template.
- **Audit_Record**: A log record emitted per LLM_Request describing what produced the resulting spans — Model_Id, Prompt_Version, a digest and length of the input text, and the returned PII_Entity_Types with the offsets emitted for each.
- **Detection_Summary**: The diagnostic record attached to a Redaction Job (`RedactionJob.detectionSummary`), already carrying per-path region counts and the per-page text-layer health signal, and served by `GET /jobs/{jobId}` and rendered by Job_Status_View.
- **LLM_Only_Span**: A merged `MatchSpan` whose contributing rule names — `ruleName` together with `mergedFromRuleNames` — are all `"llm:"`-prefixed, i.e. a region no other detection source found.
- **JobStatusHandler**: The `GET /jobs/{jobId}` Lambda (`backend/lambdas/job_status_handler.py`).
- **Job_Status_View**: The frontend job status and download component (`frontend/src/views/JobStatusView.tsx`).
- **CDK_Stack**: `RedactionStack` (`backend/infra/redaction_stack.py`).

## Requirements

### Requirement 1: LLM PII Detection Tool

**User Story:** As a compliance reviewer, I want a Bedrock foundation model to read the extracted document text and report the PII it finds, so that Chinese names, addresses, and licence plates are detected even though Textract and Bedrock Data Automation cannot read them.

#### Acceptance Criteria

1. WHEN the LLM_Detection_Tool receives a collection of Text_Units, THE LLM_Detection_Tool SHALL issue exactly one LLM_Request per Text_Unit whose text contains at least one character.
2. WHEN a Text_Unit's text contains zero characters, THE LLM_Detection_Tool SHALL skip that Text_Unit and issue zero LLM_Requests for it.
3. THE LLM_Detection_Tool SHALL accept the same three unit collections the Guardrails_Detection_Tool accepts — text-layer units, image regions with `recognizedText` populated, and OCR_Text_Units — with the OCR collection optional and last in the signature.
4. THE LLM_Detection_Tool SHALL assign each LLM_MatchSpan the Unit_Ref of the collection and index the span's text came from, using the existing `"text:{index}"`, `"image:{index}"`, and `"ocr:{index}"` namespaces.
5. THE LLM_Detection_Tool SHALL assign each LLM_MatchSpan the `zone` value the corresponding Guardrails_Detection_Tool branch assigns — the unit's own `zone` for text-layer and OCR units, and `"image"` for image regions.
6. THE Prompt_Template SHALL instruct the Model to return a JSON array in which every element carries the verbatim matched text exactly as it appears in the supplied text and one PII_Entity_Type from the Shared_Entity_Vocabulary.
7. THE Shared_Entity_Vocabulary SHALL contain every entity type name configured on `CDK_Stack`'s `PiiGuardrail`, together with the CJK_Extension_Types.
8. THE CJK_Extension_Types SHALL contain a label for the mainland Chinese resident identity number.
9. WHEN the Model returns a PII_Detection whose PII_Entity_Type belongs to the Shared_Entity_Vocabulary, THE LLM_Detection_Tool SHALL set the resulting LLM_MatchSpan's `ruleName` to `"llm:{PII_Entity_Type}"`.
10. WHERE a PII_Detection's PII_Entity_Type is absent from the Shared_Entity_Vocabulary, THE LLM_Detection_Tool SHALL emit the span with `ruleName` `"llm:UNSPECIFIED"` and log the returned label at debug level.
11. IF the Model's response cannot be parsed into a JSON array of PII_Detections, THEN THE LLM_Detection_Tool SHALL log a warning and produce zero LLM_MatchSpans for that Text_Unit.
12. THE LLM_Detection_Tool SHALL be a module under `backend/agent/tools/` exposing a module-level function, and SHALL invoke no MCP server, package no Agent Skill, and invoke no sub-agent.
13. THE LLM_Detection_Tool SHALL reuse the lazy client construction pattern used by the Guardrails_Detection_Tool, so one Bedrock client is shared across invocations within an Agent Runtime process.

### Requirement 2: Offset Recovery Through the Existing Occurrence Scan

**User Story:** As a developer, I want the LLM's text spans located by the same mechanism the Guardrails path already uses, so that a second offset-recovery implementation cannot drift from the first and reintroduce mis-placed redactions.

#### Acceptance Criteria

1. WHEN the LLM_Detection_Tool resolves a PII_Detection to offsets, THE Occurrence_Scan SHALL locate every non-overlapping occurrence of the matched text within the exact string that was sent to the Model.
2. WHERE a PII_Detection's matched text consists entirely of ASCII digits, THE Digit_Boundary_Rule SHALL reject every occurrence that is immediately preceded by an ASCII digit or immediately followed by an ASCII digit.
3. IF the Digit_Boundary_Rule rejects every occurrence of a matched text, THEN THE Occurrence_Scan SHALL emit spans for all of that matched text's occurrences and log a warning.
4. IF a PII_Detection's matched text occurs zero times in the string sent to the Model, THEN THE LLM_Detection_Tool SHALL skip that PII_Detection and log at debug level.
5. THE Occurrence_Scan SHALL produce identical offset ranges for the LLM_Detection_Tool and for the Guardrails_Detection_Tool when given the same scanned string and the same matched text.
6. THE LLM_Detection_Tool SHALL emit only LLM_MatchSpans satisfying `0 <= startOffset < endOffset <= len(text)`, where `text` is the string sent to the Model.
7. WHEN the number of LLM_MatchSpans emitted for a single PII_Detection exceeds the occurrence warning threshold, THE LLM_Detection_Tool SHALL log a warning naming the PII_Entity_Type, the Unit_Ref, and the emitted count.
8. THE LLM_Detection_Tool SHALL cap neither the number of occurrences scanned nor the number of LLM_MatchSpans emitted for a PII_Detection.

### Requirement 3: The LLM Source Is Additive Only

**User Story:** As a compliance reviewer, I want the language model to be able to add redactions and nothing else, so that a model mistake can only ever produce a visible black box rather than an invisible leak.

#### Acceptance Criteria

1. THE Orchestration_Pipeline SHALL pass to the redactor every `MatchSpan` produced by regex matching and by the Guardrails_Detection_Tool, whatever the LLM_Detection_Tool returned.
2. THE LLM_Detection_Tool SHALL expose no parameter, return value, or side effect that marks, suppresses, shrinks, or deletes a `MatchSpan` produced by another detection source.
3. WHEN the Orchestration_Pipeline merges the three sources' spans, THE Span_Merging_Logic SHALL produce, for each Unit_Ref, a set of covered character offsets equal to the union of the offsets covered by that Unit_Ref's input spans.
4. WHEN the LLM_Detection_Tool returns zero LLM_MatchSpans, THE Orchestration_Pipeline SHALL produce a merged span collection identical to the collection it produced before this feature existed.
5. THE Orchestration_Pipeline SHALL send no `MatchSpan` produced by another detection source to the Model for review, confirmation, or scoring.

### Requirement 4: Never-Raise, Non-Fatal Error Handling

**User Story:** As a system operator, I want a language model failure to cost recall and nothing else, so that a supplementary detection source can never block document redaction.

#### Acceptance Criteria

1. IF an LLM_Request returns a throttling error or a server-side error, THEN THE LLM_Detection_Tool SHALL retry that request with exponential backoff for up to 3 total attempts.
2. IF an LLM_Request fails after the retries defined in Criterion 1, THEN THE LLM_Detection_Tool SHALL log a warning, skip that Text_Unit, and continue with the remaining Text_Units.
3. IF the Model_Id environment variable is absent or blank, THEN THE LLM_Detection_Tool SHALL log a warning and return zero LLM_MatchSpans without issuing any LLM_Request.
4. IF the Bedrock client cannot be constructed, THEN THE LLM_Detection_Tool SHALL log a warning and return zero LLM_MatchSpans.
5. WHEN any unexpected error arises while processing a Text_Unit, THE LLM_Detection_Tool SHALL return the LLM_MatchSpans it has already collected rather than discarding them.
6. THE LLM_Detection_Tool SHALL return a list of LLM_MatchSpans for every input, and SHALL raise no exception to its caller.
7. THE Orchestration_Pipeline SHALL wrap the LLM_Detection_Tool call in its own error handler that degrades to zero LLM_MatchSpans, mirroring the handler already wrapping the Guardrails_Detection_Tool call.
8. THE Redaction_Agent SHALL change a Redaction Job's status to `failed` only for the failure conditions that already cause `failed` — extraction failure, OCR_Path failure, redaction failure, structure verification failure, storage failure — and SHALL NOT change it to `failed` because of an LLM detection failure.

### Requirement 5: Determinism Constraints and Auditability

**User Story:** As a compliance auditor, I want to know which model, which prompt, and which input produced a given redaction, so that a probabilistic detection source can still be explained during a review.

#### Acceptance Criteria

1. THE LLM_Detection_Tool SHALL set the Model's temperature to 0 on every LLM_Request.
2. WHERE the Model's inference configuration accepts a nucleus-sampling parameter, THE LLM_Detection_Tool SHALL set that parameter to its most deterministic supported value on every LLM_Request.
3. THE LLM_Detection_Tool SHALL address the Model by a Model_Id read from an environment variable, and THE CDK_Stack SHALL supply a pinned, versioned identifier for that variable.
4. THE LLM_Detection_Tool SHALL reject a Model_Id whose value is a floating alias — an identifier ending in `latest` or carrying no version segment — by logging a warning and returning zero LLM_MatchSpans.
5. THE Prompt_Template SHALL be a module-level constant carrying a Prompt_Version string.
6. WHEN the LLM_Detection_Tool completes an LLM_Request, THE LLM_Detection_Tool SHALL emit an Audit_Record containing the Unit_Ref, the Model_Id, the Prompt_Version, the SHA-256 digest of the text sent, the character length of the text sent, and each returned PII_Entity_Type paired with the offset ranges emitted for it.
7. THE Audit_Record SHALL contain neither the text sent to the Model nor any PII_Detection's matched text, so that recording the audit trail does not copy PII into the log stream.
8. THE Orchestration_Pipeline SHALL invoke the LLM_Detection_Tool at one fixed position in its sequence, and SHALL NOT let the Model select, reorder, or skip any pipeline step.

### Requirement 6: Orchestration Integration

**User Story:** As a developer, I want the LLM source wired in exactly like the Guardrails source, so that adding a third detection source changes no shared machinery.

#### Acceptance Criteria

1. WHEN the Redaction_Agent processes a Redaction Job, THE Orchestration_Pipeline SHALL invoke the LLM_Detection_Tool after the Guardrails_Detection_Tool and before span merging.
2. THE Orchestration_Pipeline SHALL pass the LLM_Detection_Tool the same text-layer units, image regions, and OCR_Text_Units it passes to the Guardrails_Detection_Tool.
3. THE Orchestration_Pipeline SHALL concatenate the LLM_MatchSpans with the regex and Guardrails spans and pass the single combined collection to the existing `_regroup_and_merge`.
4. THE Orchestration_Pipeline SHALL introduce no change to `merge_spans_within_unit` and no change to `_regroup_and_merge`.
5. THE LLM_Detection_Tool SHALL emit LLM_MatchSpans only in the `text:`, `image:`, and `ocr:` Unit_Ref namespaces, and SHALL introduce no additional namespace.
6. WHEN a merged span combines an LLM_MatchSpan with a span from another source, THE Span_Merging_Logic SHALL retain every contributing rule name in `mergedFromRuleNames`, including the `"llm:"`-prefixed one.
7. THE Redaction_Agent SHALL invoke the LLM_Detection_Tool for every Redaction Job whose `sourceExtension` is `.pdf`.
8. THE Redaction_Agent SHALL invoke the LLM_Detection_Tool for every Redaction Job whose `sourceExtension` is `.docx`.
9. THE Redaction_Agent SHALL require no frontend interaction and no user configuration to activate LLM detection.

### Requirement 7: Over-Redaction Observability

**User Story:** As a compliance reviewer, I want to see how many redacted regions only the language model found, so that I can judge whether the model is contributing recall or just painting black boxes.

#### Acceptance Criteria

1. WHEN the Redaction_Agent records a Detection_Summary for a Redaction Job, THE Detection_Summary SHALL contain the number of LLM_MatchSpans the LLM_Detection_Tool produced before merging.
2. WHEN the Redaction_Agent records a Detection_Summary, THE Detection_Summary SHALL contain the number of LLM_Only_Spans in the merged span collection.
3. THE Redaction_Agent SHALL record a Detection_Summary for every Redaction Job it transitions to `completed`, whatever that Job's `sourceExtension`.
4. WHEN JobStatusHandler serves `GET /jobs/{jobId}` for a Job whose record carries a Detection_Summary, THE JobStatusHandler SHALL include the LLM counts within the `detectionSummary` value of the response body.
5. WHILE a Redaction Job's displayed status is `completed` and its `detectionSummary` is present, THE Job_Status_View SHALL display the number of LLM_Only_Spans alongside the per-path counts it already displays.
6. WHILE a Redaction Job's `detectionSummary` is absent, THE Job_Status_View SHALL display its status indicator and download action unchanged from their behavior before this feature existed.
7. WHEN the number of LLM_Only_Spans in a Redaction Job exceeds the over-redaction warning threshold, THE Redaction_Agent SHALL log a warning naming the Job's `jobId` and that count.
8. WHEN the Redaction_Agent records the LLM counts in a Detection_Summary, THE Redaction_Agent SHALL leave the Job's `jobId`, `sourceKey`, `sourceExtension`, `fileName`, and `createdAt` attributes unchanged.
9. THE Redaction_Agent SHALL select detection sources and resolve `MatchSpan`s independently of every value recorded in a Detection_Summary.

### Requirement 8: Infrastructure Provisioning and Operational Control

**User Story:** As a system administrator, I want the model identifier and its invocation permission provisioned through CDK with a documented off switch, so that the feature can be enabled, pinned, and disabled without a code change.

#### Acceptance Criteria

1. THE CDK_Stack SHALL grant the AgentCore Runtime execution role permission to invoke the Model, scoped to the provisioned Model_Id's resource, and SHALL grant no broader Bedrock model permission.
2. THE CDK_Stack SHALL pass the Model_Id to the AgentCore Runtime as an environment variable, alongside the existing `GUARDRAIL_ID`, `GUARDRAIL_VERSION`, and `PDF_OCR_DPI` variables.
3. THE CDK_Stack SHALL set the Model_Id environment variable to a pinned, versioned Bedrock model or inference profile identifier.
4. WHEN an operator clears the Model_Id environment variable, THE LLM_Detection_Tool SHALL become dormant and THE Redaction_Agent SHALL continue completing Redaction Jobs using the regex, Guardrails, and OCR sources.
5. THE LLM_Detection_Tool SHALL emit every warning and Audit_Record through a module-level `logging` logger, so that the records reach CloudWatch once log handler configuration is fixed in the Agent Runtime.
