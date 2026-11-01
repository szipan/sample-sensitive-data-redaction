# Implementation Plan: Bedrock Guardrails PII Detection Integration

## Overview

This plan implements the Bedrock Guardrails PII detection integration in four stages: CDK infrastructure provisioning, the new detection tool, orchestration integration with span merging, and testing. All implementation uses Python, consistent with the existing codebase.

## Tasks

- [x] 1. Provision Bedrock Guardrail CDK infrastructure
  - [x] 1.1 Add CfnGuardrail and CfnGuardrailVersion resources to redaction_stack.py
    - Import `aws_bedrock` from `aws_cdk`
    - Create `CfnGuardrail` with `sensitive_information_policy_config` containing all PII entity types with `BLOCK` action
    - Create `CfnGuardrailVersion` referencing the guardrail's `attr_guardrail_id`
    - Add `bedrock:ApplyGuardrail` IAM permission to `agent_execution_role` scoped to the guardrail ARN
    - Add `GUARDRAIL_ID` and `GUARDRAIL_VERSION` environment variables to the `agent_runtime` resource
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

- [x] 2. Implement Guardrails PII detection tool
  - [x] 2.1 Create `backend/agent/tools/guardrails_pii_detect.py` with core detection logic
    - Implement `detect_pii_with_guardrails(text_units, image_regions) -> List[MatchSpan]`
    - Read `GUARDRAIL_ID` and `GUARDRAIL_VERSION` from `os.environ`; return `[]` if missing
    - Implement `_call_apply_guardrail(client, guardrail_id, version, text)` with retry logic (3 attempts, exponential backoff)
    - For each non-empty text unit, call `apply_guardrail` and parse `assessments[].sensitiveInformationPolicy.piiEntities[]`
    - Convert each entity to `MatchSpan` using `str.find()` for offset calculation
    - Set `ruleName` to `f"guardrails:{entity_type}"`
    - Process image regions with `recognizedText` using `unitRef="image:{index}"`
    - Wrap all errors — never raise exceptions, return empty list on failure
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x]* 2.2 Write property tests for detect_pii_with_guardrails offset calculation
    - **Property 2: Offset validity for all guardrails spans**
    - **Validates: Requirement 2.2**

  - [x]* 2.3 Write property tests for ruleName format
    - **Property 1: Guardrails spans use correct ruleName format**
    - **Validates: Requirement 2.3**

  - [x]* 2.4 Write property tests for empty text unit handling
    - **Property 6: Empty text units produce no spans**
    - **Validates: Requirement 2.5**

- [x] 3. Implement orchestration integration and span merging
  - [x] 3.1 Add `_regroup_and_merge` helper function to orchestration.py
    - Implement function that groups spans by `unitRef` and calls `_merge_spans_within_unit` per group
    - Import `_merge_spans_within_unit` from `agent.tools.matching`
    - Maintain first-seen `unitRef` ordering in the output
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 3.2 Integrate Guardrails detection into `handle_job` pipeline
    - Import `detect_pii_with_guardrails` from `agent.tools.guardrails_pii_detect`
    - After `match_rules()` call, add `detect_pii_with_guardrails()` call wrapped in try/except
    - Concatenate regex matches and guardrails matches
    - Pass concatenated list through `_regroup_and_merge`
    - Pass merged result to `_redact_document`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.1, 5.4_

  - [x]* 3.3 Write property tests for merge backward compatibility
    - **Property 3: Merge backward compatibility**
    - **Validates: Requirement 3.5**

  - [x]* 3.4 Write property tests for non-overlapping merge output
    - **Property 4: Merge produces non-overlapping spans per unitRef**
    - **Validates: Requirements 3.1, 3.2**

  - [x]* 3.5 Write property tests for graceful degradation
    - **Property 5: Graceful degradation preserves regex results**
    - **Validates: Requirements 4.3, 5.1**

  - [x]* 3.6 Write property tests for mergedFromRuleNames tracking
    - **Property 7: mergedFromRuleNames tracks all contributing sources**
    - **Validates: Requirement 3.3**

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Export `_merge_spans_within_unit` for external use
  - [x] 5.1 Update `backend/agent/tools/matching.py` to make `_merge_spans_within_unit` importable
    - Rename to `merge_spans_within_unit` (remove leading underscore) or add to `__all__` / import in orchestration
    - Ensure no breaking changes to existing callers
    - _Requirements: 3.1, 3.5_

- [x] 6. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- The detection tool is designed for graceful degradation — any failure returns empty list
- `_merge_spans_within_unit` already handles the core merge logic; we only need to regroup concatenated spans by unitRef before calling it
- The CDK changes use `CfnGuardrail` (L1 construct) since no L2 construct exists for Bedrock Guardrails yet
- No new third-party dependencies are required — only boto3 (already bundled) and aws-cdk-lib (already used)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "3.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4"] },
    { "id": 4, "tasks": ["3.5", "3.6"] }
  ]
}
```
