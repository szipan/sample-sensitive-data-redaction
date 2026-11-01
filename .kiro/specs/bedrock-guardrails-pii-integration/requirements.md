# Requirements Document

## Introduction

This feature integrates Amazon Bedrock Guardrails Sensitive Information Filters as a supplementary PII detection layer alongside the existing regex-based matching in the PII Document Redaction POC. Bedrock Guardrails provides ML-based detection of 30+ PII entity types (names, addresses, emails, credit card numbers, etc.) in both Chinese and English text, eliminating the need to write custom regex patterns for each PII category.

The Guardrails detection runs in parallel with existing regex rules and their results are merged into a unified set of `MatchSpan` objects before redaction proceeds. The feature is backend-fixed and frontend-transparent — always-on with no user-facing configuration.

## Glossary

- **Guardrail**: An Amazon Bedrock Guardrails resource configured with Sensitive Information Filters to detect PII entities in text.
- **ApplyGuardrail_API**: The `bedrock-runtime` `ApplyGuardrail` API that evaluates text content against a configured Guardrail and returns detected PII entities with character offsets.
- **PII_Entity_Type**: One of the 30+ sensitive information categories supported by Bedrock Guardrails (e.g., NAME, ADDRESS, EMAIL, CREDIT_DEBIT_CARD_NUMBER, PHONE, etc.).
- **Guardrails_MatchSpan**: A `MatchSpan` produced by converting a Bedrock Guardrails PII detection result (with character offsets) into the system's standard `MatchSpan` format.
- **Merged_Spans**: The unified collection of `MatchSpan` objects produced by combining regex-based matches and Guardrails-based matches, with overlapping spans merged into single spans.
- **Redaction_Agent**: The AgentCore-hosted deterministic pipeline that processes documents through extraction, matching, redaction, verification, and storage.
- **Text_Unit**: An `ExtractedTextUnit` or the `recognizedText` of an `ImageRegion`, representing a discrete piece of text extracted from a document.

## Requirements

### Requirement 1: Guardrail Infrastructure Provisioning

**User Story:** As a system administrator, I want the Bedrock Guardrail resource to be provisioned automatically via CDK, so that the PII detection service is available without manual setup.

#### Acceptance Criteria

1. THE CDK_Stack SHALL provision an Amazon Bedrock Guardrail resource with Sensitive Information Filters enabled for all supported PII entity types.
2. THE CDK_Stack SHALL configure the Guardrail with a `BLOCK` action for all PII entity types so that detection results include character offsets.
3. THE CDK_Stack SHALL create a Guardrail version after provisioning, so that the ApplyGuardrail_API can reference a stable version identifier.
4. THE CDK_Stack SHALL grant the Redaction_Agent execution role `bedrock:ApplyGuardrail` permission scoped to the provisioned Guardrail resource ARN.
5. THE CDK_Stack SHALL pass the Guardrail identifier and version as environment variables to the Redaction_Agent runtime.

### Requirement 2: Guardrails PII Detection Tool

**User Story:** As a developer, I want a dedicated tool that calls the Bedrock Guardrails ApplyGuardrail API to detect PII in text, so that I can obtain ML-based PII detections in a format compatible with the existing matching pipeline.

#### Acceptance Criteria

1. WHEN the Guardrails detection tool receives a list of Text_Units, THE Guardrails_Detection_Tool SHALL call the ApplyGuardrail_API for each text unit's content.
2. WHEN the ApplyGuardrail_API returns PII detections with character offsets, THE Guardrails_Detection_Tool SHALL convert each detection into a Guardrails_MatchSpan with correct `startOffset`, `endOffset`, `unitRef`, and `zone` fields.
3. THE Guardrails_Detection_Tool SHALL set the `ruleName` field of each Guardrails_MatchSpan to a descriptive name incorporating the detected PII_Entity_Type (e.g., `"guardrails:NAME"`, `"guardrails:EMAIL"`).
4. WHEN the ApplyGuardrail_API returns an error or times out, THE Guardrails_Detection_Tool SHALL log the error and return an empty list of matches without interrupting the overall pipeline.
5. WHEN a text unit's content is empty, THE Guardrails_Detection_Tool SHALL skip it and produce no Guardrails_MatchSpan for that unit.
6. THE Guardrails_Detection_Tool SHALL process both `ExtractedTextUnit` objects and `ImageRegion` objects with `recognizedText` populated, using the same `unitRef` naming scheme as the regex matching tool (`text:{index}` and `image:{index}`).

### Requirement 3: Span Merging with Guardrails Results

**User Story:** As a developer, I want Guardrails PII detections to be merged with regex-based matches into a unified set of spans, so that overlapping detections from different sources produce clean, non-overlapping redaction regions.

#### Acceptance Criteria

1. WHEN both regex matches and Guardrails detections exist for the same text unit, THE Merging_Logic SHALL combine them into a single list and merge overlapping or adjacent spans into unified spans.
2. WHEN a Guardrails_MatchSpan overlaps with a regex-produced MatchSpan within the same text unit, THE Merging_Logic SHALL merge them into a single span covering the union of both ranges.
3. THE Merging_Logic SHALL preserve the `mergedFromRuleNames` field to track all contributing rule names (both regex rule names and Guardrails PII type identifiers) when spans from different sources are merged.
4. WHEN Guardrails detections exist for a text unit that has no regex matches, THE Merging_Logic SHALL include the Guardrails_MatchSpans in the final result without alteration.
5. THE Merging_Logic SHALL produce the same output as the existing `_merge_spans_within_unit` function when given only regex matches (backward compatibility).

### Requirement 4: Orchestration Pipeline Integration

**User Story:** As a system operator, I want the Guardrails PII detection to execute as part of the existing redaction pipeline, so that every document processed benefits from ML-based PII detection automatically.

#### Acceptance Criteria

1. WHEN the Redaction_Agent processes a document, THE Orchestration_Pipeline SHALL invoke Guardrails PII detection on the same extracted text units and image regions used for regex matching.
2. THE Orchestration_Pipeline SHALL combine regex match results and Guardrails detection results before passing the merged spans to the redaction step.
3. WHEN the Guardrails detection tool returns an error, THE Orchestration_Pipeline SHALL continue processing with only regex match results (graceful degradation).
4. THE Orchestration_Pipeline SHALL pass the Guardrail identifier and version to the detection tool from environment variables.
5. THE Orchestration_Pipeline SHALL NOT require any frontend interaction or user configuration to activate Guardrails detection.

### Requirement 5: Error Handling and Resilience

**User Story:** As a system operator, I want the system to remain functional even when Guardrails detection fails, so that document redaction is never blocked by a supplementary detection layer.

#### Acceptance Criteria

1. IF the ApplyGuardrail_API is unavailable or returns a service error, THEN THE Redaction_Agent SHALL complete the job using only regex-based matches.
2. IF the ApplyGuardrail_API returns a throttling error, THEN THE Guardrails_Detection_Tool SHALL retry with exponential backoff up to 3 attempts before falling back to empty results.
3. IF the Guardrail resource identifier or version environment variables are missing, THEN THE Guardrails_Detection_Tool SHALL log a warning and return an empty list of matches.
4. THE Redaction_Agent SHALL NOT transition to `failed` status due to Guardrails detection errors alone; only pre-existing failure conditions (extraction failure, structure verification failure, etc.) cause `failed` status.
