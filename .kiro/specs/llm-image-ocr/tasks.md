# Implementation Plan: Multimodal LLM Embedded-Image OCR

## Overview

Implemented in Python (`backend/agent`, `backend/infra`), matching the existing
codebase. This feature is mostly a re-skin of `recognize_image_text` in
`backend/agent/tools/ocr.py`: its downstream edge to Amazon Textract is severed
and replaced with an edge to Bedrock `Converse` carrying an image content
block. The public signature and contract are unchanged, so nothing downstream
(`match_rules`, `detect_pii_with_guardrails`, `detect_pii_with_llm`, the
redactor) changes.

The order is: update the shared model docstrings first (comment-only, no shape
change), reimplement `ocr.py` in isolation — reusing the patterns already
shipped by `llm_pii_detect.py` (pinned/versioned model-id predicate,
environment-driven off switch, lazy `bedrock-runtime` client,
`Converse`-with-retry at `temperature=0`/no `topP`, never-raise posture,
PII-safe `sha256`-digest audit record) — then wire the best-effort call site
into `handle_job`, then provision the model id and its scoped invoke grant in
CDK, and finally rewrite `tests/test_ocr.py` around the new engine.

Three things are deliberately **not** changed, and tasks exist to state that
rather than to do it: `backend/agent/tools/ocr_pdf.py` (the full-page Textract
path, which stays fatal-on-failure and keeps its word-level geometry — no task
touches it), `_regroup_and_merge` / `merge_spans_within_unit` (the merge logic,
untouched because the `image:{index}` namespace is unchanged), and
`tests/test_guardrails_pii_detect.py`, which must keep passing **unmodified** —
this feature imports `_is_retryable` from `guardrails_pii_detect` but does not
touch it.

The single behavioural change visible outside `ocr.py`: an embedded-image OCR
failure no longer drives the Job to `failed` (Requirement 4.7). The reimplemented
tool never raises, and the call site is wrapped belt-and-braces so even a *bug*
in the tool cannot flip the Job to `failed`.

## Tasks

- [x] 1. Update the shared model docstrings
  - [x] 1.1 Record the Converse engine on `ImageRegion` in `shared/models.py`
    - Edit `backend/shared/models.py` and mirror the identical changes into `backend/lambdas/shared/models.py`
    - `ImageRegion`'s docstring: change the sentence saying `recognize_image_text` uses Amazon Textract's `DetectDocumentText` to say it uses a multimodal Bedrock model via the `Converse` API
    - `imageBytes`' docstring: note the bytes now feed a `Converse` image content block rather than Textract's `Document.Bytes`
    - Comment-only edits: add, remove, or retype no field, so every existing constructor call site and persisted record is unaffected
    - _Requirements: 2.3_

- [x] 2. Reimplement `recognize_image_text` on Bedrock Converse
  - [x] 2.1 Rewrite `backend/agent/tools/ocr.py` configuration, prompt, model-id predicate, and lazy client
    - Define module constants: `IMAGE_OCR_MODEL_ID_ENV_VAR = "IMAGE_OCR_MODEL_ID"`, `OCR_PROMPT_VERSION = "llm-image-ocr/v1"`, `MAX_RETRIES = 3`, `RETRY_BASE_DELAY_SECONDS = 1.0`, `MAX_OUTPUT_TOKENS = 4096`, `IMAGE_SIZE_LIMIT_BYTES = int(3.75 * 1024 * 1024)`
    - Write `OCR_PROMPT` as a module-level constant carrying `OCR_PROMPT_VERSION`, sent as the `system` block: instruct the model to act as an OCR engine and transcribe only the characters visible in the image, in natural reading order; state that the text is often Chinese and may be Japanese or Korean and may mix Latin script, so recognize CJK characters as well as Latin-script text; forbid translation, transliteration/pinyin, summarization, and commentary; forbid markdown, code fences, and labels; return nothing when there is no legible text
    - Implement `_is_pinned_model_id(model_id) -> bool` accepting a trailing `-v\d+:\d+` segment (so `us.anthropic.claude-haiku-4-5-20251001-v1:0` and `amazon.nova-lite-v1:0` pass) and rejecting blank, unversioned, and `*-latest` values; family-independent — validate the id's shape, not the vendor
    - Implement `_read_model_config()` reading `IMAGE_OCR_MODEL_ID_ENV_VAR`, returning the pinned id or `None` (with a warning) when the variable is absent, blank, or not pinned — the dormant/off-switch path
    - Implement the lazy `_get_bedrock_runtime_client()` with `import boto3` **inside** the getter, constructing `bedrock-runtime`, caching in a module global; import no Strands symbol
    - _Requirements: 1.5, 1.6, 1.7, 2.5, 2.6, 4.3, 5.3, 6.1, 6.2, 6.3, 6.4_

  - [x]* 2.2 Write property test for model-identifier gating
    - **Feature: llm-image-ocr, Property 8: Requests are issued only for a pinned model identifier**
    - Generators must produce pinned ids of several families plus blank, unversioned, and `*-latest` near-misses
    - **Validates: Requirements 4.3, 5.3, 6.1, 6.2; Property 8**

  - [x] 2.3 Implement image-format detection from magic bytes
    - Implement `_detect_image_format(image_bytes) -> Optional[str]` returning the `Converse` format `"png"`/`"jpeg"`/`"gif"`/`"webp"` matching the bytes' leading magic number, or `None` when the bytes match none of the four or are too short to classify
    - PNG `89 50 4E 47 0D 0A 1A 0A`; JPEG `FF D8 FF`; GIF `47 49 46 38`; WEBP `52 49 46 46` at offset 0 **and** `57 45 42 50` at offset 8
    - Detect from the actual bytes rather than any caller hint (`ImageRegion` carries only `imageBytes`), so the declared `format` stays honest and never triggers a `ValidationException` for a format mismatch
    - _Requirements: 3.1, 3.3, 3.4_

  - [x]* 2.4 Write property test for format declaration
    - **Feature: llm-image-ocr, Property 4: The declared image format matches the bytes**
    - Generators must produce valid PNG/JPEG/GIF/WEBP magic-byte prefixes with random trailing bytes, plus near-misses: a `RIFF...` prefix that is not `...WEBP`, a BMP `BM` prefix, and byte strings shorter than the shortest magic number
    - **Validates: Requirement 3.3; Property 4**

  - [x] 2.5 Implement the Converse call, response read, size guard, audit record, and the public entrypoint
    - Implement a converse-with-retry helper calling `client.converse(modelId, system=[{"text": OCR_PROMPT}], messages=[{"role": "user", "content": [{"image": {"format": image_format, "source": {"bytes": image_bytes}}}]}], inferenceConfig={"temperature": 0.0, "maxTokens": MAX_OUTPUT_TOKENS})` — pass raw bytes (the SDK base64-encodes), send `temperature=0.0`, and **omit** `topP`
    - Reuse the shared retry decision: `from agent.tools.guardrails_pii_detect import _is_retryable`; retry throttling/5xx with exponential backoff (`RETRY_BASE_DELAY_SECONDS * 2 ** attempt`), `MAX_RETRIES` total attempts; on terminal failure (retries exhausted or a non-retryable error such as `ValidationException`/`AccessDeniedException`) log a warning via `describe_exception(error)` from `agent.logging_config` — never `str(error)`, never `exc_info=True` — and return `None`
    - Read the recognized text with an `_assistant_text`-style defensive walk: concatenate the `text` of every block in `output.message.content`, tolerating any level being absent or the wrong type, yielding `""` for a malformed response; write the `.strip()`ed result to `region.recognizedText`, and `""` when the model returned no readable text
    - Size guard: skip a region whose `len(imageBytes)` exceeds `IMAGE_SIZE_LIMIT_BYTES` with a warning, `recognizedText` left unchanged (pixel dimensions are not pre-checked — an over-dimension image falls into the per-request error path)
    - Emit one audit `logger.info` per **completed** request (even when the recognized text is empty; never for a skipped/failed request) carrying `sourceLocation`, the model id, `OCR_PROMPT_VERSION`, the SHA-256 digest and byte length of the exact image sent, and the character length of the recognized text — and recording neither the image bytes nor the recognized text
    - Implement `recognize_image_text(image_regions) -> List[ImageRegion]`: return an empty input unchanged with zero requests (checked at the top, before the model id is read); read the model config and return unchanged when dormant; construct the client (return unchanged on construction failure); for each region with present/non-empty `imageBytes`, detect format, apply the size guard, issue exactly one request (never batched), and write `recognizedText`; wrap the per-region body so any unexpected error skips that region and the rest still process; return the same instances in the same order for every input and raise nothing
    - Emit every warning and audit record through a module-level `logging` logger (`agent.tools.ocr`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.8, 2.1, 2.2, 2.3, 2.4, 3.2, 4.1, 4.2, 4.4, 4.5, 4.6, 5.1, 5.2, 5.4, 5.5, 5.6_

  - [x]* 2.6 Write property test for per-region request accounting
    - **Feature: llm-image-ocr, Property 1: One request per eligible region, none for ineligible**
    - Exactly one `converse` call per region whose bytes are present, non-empty, supported, and within the size limit; zero for absent/empty bytes; never two regions in one request
    - **Validates: Requirements 1.1, 1.8; Property 1**

  - [x]* 2.7 Write property test for recognized-text assignment
    - **Feature: llm-image-ocr, Property 2: Recognized text is the model's returned text, empty when none**
    - `recognizedText` is set to the returned text, and to `""` when the response carries no readable text
    - **Validates: Requirements 1.2, 1.3; Property 2**

  - [x]* 2.8 Write property test for the instance-preservation contract
    - **Feature: llm-image-ocr, Property 3: The same instances are returned, in order, with only recognizedText touched**
    - Same `ImageRegion` instances, same order; only `recognizedText`, only on eligible regions; `boundingBox`/`zone`/`sourceLocation`/`imageBytes` unchanged; empty input returned unchanged with zero requests
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4; Property 3**

  - [x]* 2.9 Write property test for skipping ineligible images
    - **Feature: llm-image-ocr, Property 5: Unsupported, undeterminable, and oversized images are skipped without side effects**
    - No request, `recognizedText` unchanged, a warning logged, remaining regions still processed; the size generator must straddle `IMAGE_SIZE_LIMIT_BYTES` on both sides
    - **Validates: Requirements 3.1, 3.2, 3.4; Property 5**

  - [x]* 2.10 Write property test for retry accounting
    - **Feature: llm-image-ocr, Property 6: Transient failures are retried up to three attempts**
    - Exactly `min(failures + 1, MAX_RETRIES)` attempts per region, text recovered iff fewer than `MAX_RETRIES` attempts were needed; a non-retryable error stops after one attempt
    - Fake errors carry duck-typed `ClientError`-shaped `response` dicts so the imported `_is_retryable` classifies them as in production
    - **Validates: Requirement 4.1; Property 6**

  - [x]* 2.11 Write property test for the never-raise and partial-results contract
    - **Feature: llm-image-ocr, Property 7: The tool never raises and never discards work already done**
    - For any per-region client behaviour (raising, malformed response, valid text) the tool returns a list, raises nothing, and every region processed before a failing one keeps its text
    - **Validates: Requirements 4.2, 4.5, 4.6; Property 7**

  - [x]* 2.12 Write property test for the pinned inference configuration
    - **Feature: llm-image-ocr, Property 10: Every request pins the inference configuration**
    - Every issued request carries `temperature == 0` and omits `topP`
    - **Validates: Requirements 5.1, 5.2; Property 10**

  - [x]* 2.13 Write property test for the audit record
    - **Feature: llm-image-ocr, Property 11: The audit record identifies the run without recording image bytes or text**
    - The per-region audit record contains `sourceLocation`, model id, prompt version, the SHA-256 digest and byte length of exactly the image sent, and the recognized-text character length — and contains neither the image bytes nor the recognized text
    - **Validates: Requirements 5.4, 5.5; Property 11**

  - [x]* 2.14 Write the example, edge-case, and structural unit tests for `ocr.py`
    - Signature is `list[ImageRegion] -> list[ImageRegion]` (_Requirements: 2.1_)
    - The request block is `image={format, source={bytes}}` carrying raw (un-base64'd) bytes and addressed via `converse` (_Requirements: 1.4, 6.3_)
    - `OCR_PROMPT` is module-level, carries `OCR_PROMPT_VERSION`, and instructs CJK recognition plus only-visible-characters with no translation/transliteration/commentary (_Requirements: 1.5, 1.6, 1.7_)
    - The lazy Bedrock client is constructed once across two invocations (_Requirements: 2.5_)
    - The module imports with no `boto3`/`strands`/`bedrock_agentcore` installed and imports no Strands symbol (_Requirements: 2.6_)
    - A client-construction failure yields the regions unchanged and no exception (_Requirements: 4.4_)
    - `IMAGE_OCR_MODEL_ID_ENV_VAR` is distinct from `LLM_PII_MODEL_ID` (_Requirements: 6.4_)
    - The audit record and warnings are captured via `caplog` on the `agent.tools.ocr` logger (_Requirements: 5.6_)
    - Retarget the two prior Textract-shaped tests and **remove** the Textract-block-joining test: keep the empty-input-no-client intent (P3's empty clause), rewrite one-call-per-region against `converse`, broaden the bounding-box test into P3, and delete `test_line_blocks_are_joined_and_non_line_blocks_are_ignored`
    - Mock Bedrock with `unittest.mock`, not moto
    - _Requirements: 1.4, 1.5, 1.6, 1.7, 2.1, 2.5, 2.6, 4.4, 6.3, 6.4, 5.6_

- [x] 3. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - `tests/test_guardrails_pii_detect.py` must be passing **unmodified** at this point (this feature imports `_is_retryable` from it)

- [x] 4. Wire the best-effort call site into orchestration
  - [x] 4.1 Wrap `recognize_image_text` belt-and-braces in `handle_job`
    - Edit `backend/agent/orchestration.py`: keep the tool invoked from the existing `if content.imageRegions:` block, at the same pipeline position, for both `.docx` and `.pdf`, before matching
    - Wrap the call in `try/except Exception`, falling back to the un-OCR'd regions (which keep their placeholder `recognizedText`) and logging a warning with `exc_info=False` — the same fail-open bracket `handle_job` already uses for `detect_pii_with_guardrails` and `detect_pii_with_llm`; log a warning rather than a silent `pass`, since reaching the handler means a bug broke the tool's never-raise promise
    - Change no pipeline ordering and no other block; the recognized text still flows to `match_rules`, `detect_pii_with_guardrails`, and `detect_pii_with_llm` under the `image:{index}` namespace, with no frontend interaction or user configuration
    - _Requirements: 4.7, 8.1, 8.2, 8.3, 8.4_

  - [x]* 4.2 Write property test for job survival under image-OCR failure
    - **Feature: llm-image-ocr, Property 9: An embedded-image OCR failure never fails the Job**
    - For any failure mode — dormant configuration and, with the wrap, an unexpected exception — a Job whose other steps succeed reaches `completed` with a redacted document stored
    - **Validates: Requirements 4.7, 9.4; Property 9**

- [x] 5. Provision the model and its permission in CDK
  - [x] 5.1 Add the image-OCR model id, env var, and scoped invoke grant
    - Edit `backend/infra/redaction_stack.py`: add module-level constants `IMAGE_OCR_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"` and `IMAGE_OCR_FOUNDATION_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"` beside the LLM PII constants
    - Inject `"IMAGE_OCR_MODEL_ID": IMAGE_OCR_MODEL_ID` into the AgentCore Runtime environment alongside `GUARDRAIL_ID` / `GUARDRAIL_VERSION` / `PDF_OCR_DPI` / `LLM_PII_MODEL_ID`
    - Add a `bedrock:InvokeModel` statement scoped to **both** the inference-profile ARN in this account/Region and the underlying `arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0` (a Converse call through a cross-Region profile authorizes against both); add no wildcard model resource
    - Comment that clearing `IMAGE_OCR_MODEL_ID` is the documented off switch
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

  - [x]* 5.2 Write CDK template smoke tests in `tests/test_redaction_stack.py`
    - Assert the synthesized agent execution role policy contains `bedrock:InvokeModel` on the inference-profile and foundation-model ARNs and on no wildcard model resource (_Requirements: 9.1_)
    - Assert `IMAGE_OCR_MODEL_ID` is present in the Runtime's environment variables (_Requirements: 9.2_)
    - Assert the stack's literal satisfies `ocr._is_pinned_model_id`, so the stack and the runtime agree by construction (_Requirements: 9.3_)
    - _Requirements: 9.1, 9.2, 9.3_

- [x] 6. Prove the CJK recall gap is closed
  - [x]* 6.1 Write the Chinese-PII image recall integration test
    - Run `recognize_image_text` against a fixture image containing Chinese PII with a **real** model call, env-guarded exactly as `tests/test_llm_cjk_recall.py` guards its real-model calls
    - Assert a **recall floor**, not an equality: the recognized text contains the Chinese resident identity number and a Chinese name that Textract's `DetectDocumentText` returns as Latin garbage
    - Marked optional because it costs a real model invocation; it is nevertheless the only test that verifies the feature's premise
    - _Requirements: 1.1, 1.2, 1.5_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Task 6.1 is the one worth not skipping: without it, the claim that this feature closes the CJK embedded-image recall gap is untested.
- `backend/agent/tools/ocr_pdf.py` is **not modified** by any task (Requirement 7). The full-page Textract path keeps its word-level geometry and stays fatal-on-failure; the two OCR paths are deliberately asymmetric now (best-effort embedded images, fatal full-page).
- No changes to `merge_spans_within_unit` or `_regroup_and_merge` are required — the `image:{index}` namespace is unchanged, so downstream detection and merging are untouched.
- No changes to `job_status_update.py`, `job_status_handler.py`, or the frontend are required — this feature changes only how `recognizedText` is produced, adding no new `detectionSummary` key, API field, or frontend control.
- The only shared-model change is a docstring edit (task 1.1); no field is added, removed, or retyped.
- Every property test uses Hypothesis (already a project dependency — see `backend/.hypothesis/`), runs at least **100 iterations**, and is tagged `Feature: llm-image-ocr, Property {number}: {property title}`. No property-based framework is written from scratch.
- Bedrock is mocked with `unittest.mock`, not moto, following `tests/test_llm_pii_detect.py` and `tests/test_guardrails_pii_detect.py`. A fake `converse` client returns configurable responses and raises configurable errors carrying duck-typed `ClientError`-shaped `response` dicts so the imported `_is_retryable` classifies them as in production. Real model calls appear only in task 6.1.
- `tests/test_guardrails_pii_detect.py` must keep passing **unmodified**: this feature imports `_is_retryable` from `guardrails_pii_detect` but does not touch it.
- No new third-party dependencies: `boto3` and Hypothesis are already in use.
- Backend tests run as **two separate processes** — the MagicMock `sys.modules` PDF-library/`docx` stubs registered by the first command must not contaminate the real-PDF-library tests in the second:
  ```
  .venv\Scripts\python.exe -m pytest tests\test_ocr.py tests\test_orchestration.py tests\test_redaction_stack.py tests\test_guardrails_pii_detect.py tests\test_llm_pii_detect.py -q
  .venv\Scripts\python.exe -m pytest tests\test_extraction_pdf.py tests\test_redaction_pdf.py -q
  ```

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "5.1"] },
    { "id": 1, "tasks": ["2.3", "5.2"] },
    { "id": 2, "tasks": ["2.5"] },
    { "id": 3, "tasks": ["4.1"] },
    { "id": 4, "tasks": ["2.2", "2.4", "2.6", "2.7", "2.8", "2.9", "2.10", "2.11", "2.12", "2.13", "2.14", "4.2"] },
    { "id": 5, "tasks": ["6.1"] }
  ]
}
```
