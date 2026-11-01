# Requirements Document

## Introduction

Embedded-image text in this POC currently reaches the redactor through exactly one route — Amazon Textract's `DetectDocumentText`, wrapped by `recognize_image_text` in `backend/agent/tools/ocr.py` — and that route is blind to CJK. AWS scopes Textract detection to the Standard English alphabet and ASCII symbols; measured against the repository's Chinese fixtures, every CJK run collapsed to Latin garbage and zero Chinese characters came back. So when a Supported Document carries text only inside an embedded image or graphic, and that text is Chinese (or Japanese/Korean), the recognized text handed to every downstream detection source (regex `match_rules`, Bedrock Guardrails, and the LLM text source) is empty or corrupt, and the PII in that image is never redacted — a silent, invisible leak.

This feature replaces the OCR engine on the **embedded-image path only** with a multimodal Amazon Bedrock foundation model (starting with Claude Haiku). A multimodal model reads the image directly and returns the recognized characters as text, with strong CJK support. The model is addressed through the Bedrock `Converse` API by a pinned, versioned Model_Id read from a new environment variable, exactly as `backend/agent/tools/llm_pii_detect.py` addresses its own model — so the model can be retargeted (Haiku → Sonnet → a Nova family model) by configuration alone, with no code change.

Scope boundaries are settled and are not open for redesign:

1. **Embedded-image path only.** This feature reimplements `recognize_image_text(list[ImageRegion]) -> list[ImageRegion]`, which fills each region's `recognizedText`. Its public signature and contract do not change, so nothing downstream (`match_rules`, `detect_pii_with_guardrails`, `detect_pii_with_llm`, the redactor) changes.
2. **The full-page rasterization path stays on Textract.** `backend/agent/tools/ocr_pdf.py` (`ocr_pdf_pages`) is untouched. That path needs word-level bounding-box geometry to place page-level redactions, and a multimodal LLM does not return coordinates. The embedded-image path needs no geometry — embedded-image redaction blacks out or replaces the whole region — which is precisely why the LLM is a viable engine here and not there.
3. **Geometry is not touched.** `recognize_image_text` already leaves `ImageRegion.boundingBox` as `extract_document_content` set it (the image's position within the document). That stays true.

Embedded-image OCR is now a **best-effort, supplementary** detection input (see Requirement 4): an OCR failure lets the job continue — that image simply contributes no text — mirroring the supplementary posture of `detect_pii_with_guardrails` and `detect_pii_with_llm`, rather than propagating and driving the job to `failed` as the current Textract implementation does. This is a settled, deliberate change from the current behavior. The accepted tradeoff: for a document whose PII lives *only* inside an embedded image, a silent OCR failure means that PII is not redacted while the job still reports `completed`; this is accepted because embedded-image OCR is one of several detection inputs and best-effort by nature.

## Glossary

- **Embedded_Image_OCR_Tool**: The reimplemented `recognize_image_text` module-level function in `backend/agent/tools/ocr.py`, which fills `ImageRegion.recognizedText` using the Multimodal_Model. Its signature is `list[ImageRegion] -> list[ImageRegion]`.
- **Image_Region**: An `ImageRegion` (`backend/shared/models.py`) representing an embedded image or graphic, carrying the raw encoded image bytes in `imageBytes` and an empty `recognizedText` placeholder produced by `extract_document_content`.
- **Recognized_Text**: The text characters the Multimodal_Model reads from an Image_Region's bytes, written to that region's `recognizedText`.
- **Multimodal_Model**: The Amazon Bedrock foundation model, addressed by Image_OCR_Model_Id through the Bedrock `Converse` API's image content block, that performs Image Text Recognition on an Image_Region's bytes.
- **Image_OCR_Model_Id**: The pinned, versioned Bedrock model identifier or inference profile identifier used to address the Multimodal_Model — a specific, versioned identifier, never a floating alias — read from the Image_OCR_Model_Id_Variable.
- **Image_OCR_Model_Id_Variable**: The environment variable carrying the Image_OCR_Model_Id, distinct from the `LLM_PII_MODEL_ID` variable used by the LLM text source.
- **OCR_Request**: One Bedrock `Converse` invocation carrying the OCR_Prompt and one Image_Region's bytes as an image content block.
- **OCR_Prompt**: The module-level instruction text sent with every OCR_Request, instructing the Multimodal_Model to return only the characters visible in the image with no translation, transliteration, or commentary, and carrying an OCR_Prompt_Version.
- **OCR_Prompt_Version**: A string identifying one exact revision of the OCR_Prompt.
- **Supported_Image_Format**: An image encoding the Bedrock `Converse` image content block accepts — PNG, JPEG, GIF, and WEBP.
- **Image_Size_Limit**: The maximum encoded image byte size the Bedrock `Converse` API accepts for a single image content block.
- **Full_Page_OCR_Path**: The existing `ocr_pdf_pages` tool (`backend/agent/tools/ocr_pdf.py`), which rasterizes every PDF page and calls Textract for word-level geometry, and which this feature does not modify.
- **Redaction_Agent**: The AgentCore-hosted deterministic pipeline (`backend/agent/orchestration.py`, `handle_job`) that extracts, OCRs, matches, redacts, verifies, stores, and updates the Redaction Job.
- **CDK_Stack**: `RedactionStack` (`backend/infra/redaction_stack.py`), which provisions the AgentCore Runtime, its execution role, and its environment variables.

## Requirements

### Requirement 1: Multimodal LLM Image Text Recognition

**User Story:** As a compliance reviewer, I want a multimodal Bedrock model to read the text inside embedded images, so that Chinese, Japanese, and Korean characters in an image are recognized and can be redacted, which Textract cannot do.

#### Acceptance Criteria

1. WHEN the Embedded_Image_OCR_Tool receives a non-empty collection of Image_Regions, THE Embedded_Image_OCR_Tool SHALL issue exactly one OCR_Request per Image_Region whose `imageBytes` is present and non-empty.
2. WHEN an OCR_Request completes, THE Embedded_Image_OCR_Tool SHALL set the corresponding Image_Region's `recognizedText` to the text the Multimodal_Model returned.
3. WHEN the Multimodal_Model returns no readable text for an Image_Region, THE Embedded_Image_OCR_Tool SHALL set that Image_Region's `recognizedText` to an empty string.
4. THE Embedded_Image_OCR_Tool SHALL address the Multimodal_Model through the Bedrock `Converse` API, sending each Image_Region's bytes as an image content block.
5. THE Embedded_Image_OCR_Tool SHALL send the Multimodal_Model text that instructs recognition of Chinese, Japanese, and Korean characters in addition to Latin-script text.
6. THE OCR_Prompt SHALL instruct the Multimodal_Model to return only the characters visible in the image, in reading order, with no translation, no transliteration, no summarization, and no commentary.
7. THE OCR_Prompt SHALL be a module-level constant carrying an OCR_Prompt_Version string.
8. THE Embedded_Image_OCR_Tool SHALL issue exactly one OCR_Request per eligible Image_Region and SHALL NOT batch multiple Image_Regions into a single OCR_Request.

### Requirement 2: Unchanged Public Interface

**User Story:** As a developer, I want the OCR tool's signature and contract to stay identical, so that swapping the OCR engine changes no downstream detection, merging, or redaction code.

#### Acceptance Criteria

1. THE Embedded_Image_OCR_Tool SHALL accept a single `list[ImageRegion]` argument and SHALL return a `list[ImageRegion]`.
2. THE Embedded_Image_OCR_Tool SHALL return the same Image_Region instances it received, in the same order, with `recognizedText` populated on the eligible ones.
3. THE Embedded_Image_OCR_Tool SHALL leave each Image_Region's `boundingBox`, `zone`, `sourceLocation`, and `imageBytes` unchanged.
4. WHEN the Embedded_Image_OCR_Tool receives an empty collection of Image_Regions, THE Embedded_Image_OCR_Tool SHALL return that collection unchanged and issue zero OCR_Requests.
5. THE Embedded_Image_OCR_Tool SHALL reuse the lazy client construction pattern used elsewhere in the backend, so one Bedrock client is shared across invocations within an Agent Runtime process.
6. THE Embedded_Image_OCR_Tool SHALL import no Strands Agents SDK symbol and SHALL remain importable for unit testing without the `boto3`, `strands`, or `bedrock_agentcore` packages installed.

### Requirement 3: Image Format and Size Handling

**User Story:** As a system operator, I want the tool to handle images the model cannot accept, so that an unsupported or oversized image degrades recall for that one image rather than crashing the run.

#### Acceptance Criteria

1. WHERE an Image_Region's bytes are not in a Supported_Image_Format, THE Embedded_Image_OCR_Tool SHALL skip that Image_Region, leave its `recognizedText` unchanged, log a warning, and continue with the remaining Image_Regions.
2. WHERE an Image_Region's byte size exceeds the Image_Size_Limit, THE Embedded_Image_OCR_Tool SHALL skip that Image_Region, leave its `recognizedText` unchanged, log a warning, and continue with the remaining Image_Regions.
3. WHEN the Embedded_Image_OCR_Tool builds the image content block for an OCR_Request, THE Embedded_Image_OCR_Tool SHALL declare the Supported_Image_Format that matches the Image_Region's bytes.
4. IF an Image_Region's bytes are present but their format cannot be determined, THEN THE Embedded_Image_OCR_Tool SHALL skip that Image_Region, leave its `recognizedText` unchanged, and log a warning.

### Requirement 4: Never-Raise, Non-Fatal Error Handling

**User Story:** As a system operator, I want an image-OCR failure to cost recall on that image and nothing else, so that a supplementary recognition step can never block document redaction.

> **Settled decision.** This requirement specifies graceful degradation: an embedded-image OCR failure no longer fails the Redaction Job. This is a deliberate, accepted change from the current Textract implementation, whose exceptions propagate to the top-level handler and drive the job to `failed`. The rationale is that embedded-image OCR is one of several detection inputs (regex, Guardrails, LLM text, page OCR for PDFs) and is best-effort by nature, mirroring the supplementary posture of `detect_pii_with_guardrails` and `detect_pii_with_llm`. The accepted tradeoff: for a document whose PII lives *only* inside an embedded image, a silent OCR failure means that PII is not redacted while the job still reports `completed`.

#### Acceptance Criteria

1. IF an OCR_Request returns a throttling error or a server-side error, THEN THE Embedded_Image_OCR_Tool SHALL retry that request with exponential backoff for up to 3 total attempts.
2. IF an OCR_Request fails after the retries defined in Criterion 1, THEN THE Embedded_Image_OCR_Tool SHALL log a warning, leave that Image_Region's `recognizedText` unchanged, and continue with the remaining Image_Regions.
3. IF the Image_OCR_Model_Id_Variable is absent or blank, THEN THE Embedded_Image_OCR_Tool SHALL log a warning and return the Image_Regions unchanged without issuing any OCR_Request.
4. IF the Bedrock client cannot be constructed, THEN THE Embedded_Image_OCR_Tool SHALL log a warning and return the Image_Regions unchanged.
5. WHEN any unexpected error arises while processing one Image_Region, THE Embedded_Image_OCR_Tool SHALL continue processing the remaining Image_Regions rather than aborting the collection.
6. THE Embedded_Image_OCR_Tool SHALL return a `list[ImageRegion]` for every input and SHALL raise no exception to its caller.
7. THE Redaction_Agent SHALL NOT change a Redaction Job's status to `failed` because of an embedded-image OCR failure.

### Requirement 5: Determinism Constraints and Auditability

**User Story:** As a compliance auditor, I want to know which model and which prompt produced a given image's recognized text, so that a probabilistic recognition source can still be explained during a review.

#### Acceptance Criteria

1. THE Embedded_Image_OCR_Tool SHALL set the Multimodal_Model's temperature to 0 on every OCR_Request.
2. WHERE the Multimodal_Model's inference configuration rejects specifying a nucleus-sampling parameter together with temperature, THE Embedded_Image_OCR_Tool SHALL send temperature alone and omit the nucleus-sampling parameter.
3. THE Embedded_Image_OCR_Tool SHALL reject an Image_OCR_Model_Id whose value is a floating alias — an identifier ending in `latest` or carrying no version segment — by logging a warning and returning the Image_Regions unchanged.
4. WHEN the Embedded_Image_OCR_Tool completes an OCR_Request, THE Embedded_Image_OCR_Tool SHALL emit an audit log record containing the Image_OCR_Model_Id, the OCR_Prompt_Version, a digest of the image bytes sent, and the character length of the recognized text.
5. THE audit log record SHALL contain neither the image bytes nor the Recognized_Text, so that recording the audit trail does not copy image content or PII into the log stream.
6. THE Embedded_Image_OCR_Tool SHALL emit every warning and audit log record through a module-level `logging` logger, so that the records reach CloudWatch through the Agent Runtime's log configuration.

### Requirement 6: Pluggable Model Configuration

**User Story:** As a system administrator, I want the OCR model chosen by configuration, so that I can start on Claude Haiku and later switch to a stronger model with no code change.

#### Acceptance Criteria

1. THE Embedded_Image_OCR_Tool SHALL read the Image_OCR_Model_Id from the Image_OCR_Model_Id_Variable at invocation time.
2. THE Embedded_Image_OCR_Tool SHALL accept any pinned, versioned Bedrock model identifier or inference profile identifier as the Image_OCR_Model_Id, independent of the model family it names.
3. THE Embedded_Image_OCR_Tool SHALL address the Multimodal_Model through the Bedrock `Converse` API, so that retargeting the Image_OCR_Model_Id across model families requires no code change.
4. THE Image_OCR_Model_Id_Variable SHALL be distinct from the `LLM_PII_MODEL_ID` variable, so that the image-OCR model and the LLM text-detection model can be configured independently.

### Requirement 7: Full-Page OCR Path Unchanged

**User Story:** As a developer, I want the Textract page-OCR path left exactly as it is, so that word-level redaction geometry for PDFs is preserved.

#### Acceptance Criteria

1. THE Full_Page_OCR_Path SHALL continue to use Amazon Textract and SHALL continue to return word-level bounding-box geometry for each recognized page.
2. THE Redaction_Agent SHALL continue to invoke the Full_Page_OCR_Path for every Redaction Job whose `sourceExtension` is `.pdf`, unchanged by this feature.
3. THE Redaction_Agent SHALL continue to treat a Full_Page_OCR_Path failure as a primary-path failure that drives the Redaction Job to `failed`.
4. THIS feature SHALL introduce no change to `backend/agent/tools/ocr_pdf.py`.

### Requirement 8: Orchestration Integration

**User Story:** As a developer, I want the new OCR engine wired in at the same point in the pipeline, so that replacing the engine changes no orchestration structure.

#### Acceptance Criteria

1. THE Redaction_Agent SHALL invoke the Embedded_Image_OCR_Tool only when a Supported Document contains at least one Image_Region, at the same pipeline position the current OCR call occupies.
2. THE Redaction_Agent SHALL pass the Recognized_Text populated by the Embedded_Image_OCR_Tool to the regex, Guardrails, and LLM text detection sources exactly as it does today.
3. THE Redaction_Agent SHALL require no frontend interaction and no user configuration to activate embedded-image LLM OCR.
4. WHEN the Embedded_Image_OCR_Tool has run, THE downstream detection sources SHALL read each Image_Region's text from `recognizedText` using the existing `"image:{index}"` Unit_Ref namespace, unchanged.

### Requirement 9: Infrastructure Provisioning and Operational Control

**User Story:** As a system administrator, I want the OCR model identifier and its invocation permission provisioned through CDK with a documented off switch, so that the feature can be enabled, pinned, and disabled without a code change.

#### Acceptance Criteria

1. THE CDK_Stack SHALL grant the AgentCore Runtime execution role permission to invoke the Multimodal_Model, scoped to the provisioned Image_OCR_Model_Id's resource (and, where the Image_OCR_Model_Id is an inference profile, the underlying foundation model), and SHALL grant no broader Bedrock model permission.
2. THE CDK_Stack SHALL pass the Image_OCR_Model_Id to the AgentCore Runtime as the Image_OCR_Model_Id_Variable, alongside the existing `GUARDRAIL_ID`, `GUARDRAIL_VERSION`, `PDF_OCR_DPI`, and `LLM_PII_MODEL_ID` variables.
3. THE CDK_Stack SHALL set the Image_OCR_Model_Id_Variable to a pinned, versioned Bedrock model or inference profile identifier.
4. WHEN an operator clears the Image_OCR_Model_Id_Variable, THE Embedded_Image_OCR_Tool SHALL become dormant and leave Image_Region `recognizedText` unpopulated, and THE Redaction_Agent SHALL continue completing Redaction Jobs using the remaining detection sources.
