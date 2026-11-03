# Requirements Document

## Introduction

PDF PII detection in this POC currently trusts a single source of truth: the PDF's text layer, read via `pdf_glyphs.collect_page_glyphs`. That text layer is not trustworthy. Both test PDFs in this repository carry a broken font ToUnicode CMap, so ligature glyphs decode to unrelated codepoints — the page renders `Patti`, `Martin`, `Christina`, `https://`, `routing`, while the text layer reads `Pa#`, `Mar&n`, `Chris&na`, `hbps://`, `rou:ng`.

Measured end-to-end through the deployed pipeline on `Patti.pdf` (visible content `Patti、Martin、Christina`):

- Bedrock Guardrails, given the mangled string `'Pa#、Mar&n、Chris&na '`, returned **1** entity (`NAME='Chris'`). Given the clean string it returns 3.
- The output PDF page carried **1** redaction rectangle. Coverage: `Patti` 0%, `Martin` 0%, `Christina` 56.6%.
- The Redaction Job reported `completed` with no error.
- The same content as `.docx` redacted perfectly, because `word/document.xml` stores real characters.

So PII leaks while the job reports success, and nothing in the job record or in `verify_structure` distinguishes "no PII present" from "PII present but undecodable". For a listed-enterprise compliance use case that is the wrong risk profile.

The redaction *geometry* is not the defect here — extraction and redaction were just aligned onto a shared glyph traversal (commit `c010490`) so offsets map 1:1 onto character rectangles. This feature addresses the fact that the text layer's *content* cannot be trusted.

This feature introduces a second, independent detection path for PDFs: rasterize each page and run Amazon Textract `DetectDocumentText` over it, then detect PII in the OCR text with the same rules and Guardrails, and union both paths' results before redaction. The two paths have complementary failure modes — a ToUnicode defect breaks the text layer but not OCR; an OCR misread breaks OCR but not a clean text layer; a scanned page has no text layer at all. To leak a PII value, both paths must fail on that same value.

The feature also makes the outcome auditable: per-source detection counts and a text-layer health diagnostic are recorded on the Redaction Job, returned by `GET /jobs/{jobId}`, and displayed in the frontend job status view, so a reviewer can distinguish a trustworthy result from an untrustworthy one instead of seeing a bare `completed`.

`.docx` is deliberately out of scope for the OCR path: `word/document.xml` stores real characters, so the ToUnicode class of defect does not exist there and there is no reason to pay OCR cost.

## Glossary

- **Redaction_Agent**: The AgentCore-hosted deterministic pipeline (`backend/agent/orchestration.py`, `handle_job`) that extracts, matches, redacts, verifies, stores, and updates the Redaction Job.
- **Text_Layer_Path**: The existing PDF detection path — `pdf_glyphs.collect_page_glyphs` produces one `ExtractedTextUnit` per page, whose character offsets map 1:1 onto on-page character rectangles.
- **OCR_Path**: The new PDF detection path — each page is rasterized to an image, Textract_DetectDocumentText recognizes its text, and one OCR_Text_Unit per page is produced with Word_Block geometry.
- **OCR_Text_Unit**: An `ExtractedTextUnit` produced by the OCR_Path for one page, carrying the OCR-recognized page text plus the Word_Block list that locates each recognized word.
- **Word_Block**: A single Textract `WORD` block, comprising the recognized word text, its character offset range within the OCR_Text_Unit's text, and its normalized (0–1) bounding box relative to the rasterized page image.
- **Textract_DetectDocumentText**: The Amazon Textract synchronous `DetectDocumentText` API.
- **Unit_Ref**: The namespaced `MatchSpan.unitRef` string identifying which unit a span's offsets belong to — `"text:{index}"` for Text_Layer_Path units, `"image:{index}"` for embedded-image regions, and `"ocr:{pageIndex}"` for OCR_Text_Units.
- **Span_Merging_Logic**: `merge_spans_within_unit` (`backend/agent/tools/matching.py`) together with `_regroup_and_merge` (`backend/agent/orchestration.py`), which group spans by Unit_Ref before merging.
- **PDF_Redactor**: `redact_pdf_document` (`backend/agent/tools/redaction_pdf.py`), which resolves each MatchSpan to on-page rectangles and permanently erases them.
- **Solid_Redaction_Block**: An opaque black rectangle applied via `pdf_content_edit` (content-stream glyph removal plus a black fill box).
- **Structure_Verification_Tool**: `verify_structure` (`backend/agent/tools/structure_verification.py`), which compares source and redacted documents for page count, page order, and (for `.docx`) table structure.
- **Detection_Summary**: A record attached to a Redaction Job describing how many redacted regions came from each detection path, how many regions the two paths agreed on, and the per-page Text_Layer_Health_Signal.
- **Agreed_Region**: A pair of resolved rectangles on the same page, one from the Text_Layer_Path and one from the OCR_Path, whose areas intersect.
- **Text_Layer_Health_Signal**: A per-page diagnostic pair — the count of non-whitespace characters in the page's Text_Layer_Path text, and the count of non-whitespace characters in the same page's OCR_Path text.
- **Raster_Resolution**: The dots-per-inch at which a PDF page is rasterized before OCR.
- **JobStatusHandler**: The `GET /jobs/{jobId}` Lambda (`backend/lambdas/job_status_handler.py`).
- **Job_Status_View**: The frontend job status and download component (`frontend/src/views/JobStatusView.tsx`).
- **CDK_Stack**: `RedactionStack` (`backend/infra/redaction_stack.py`).

## Requirements

### Requirement 1: OCR Detection Path for PDF Documents

**User Story:** As a compliance reviewer, I want PII in a PDF to be detected from the rendered page as well as from the text layer, so that a broken font encoding cannot hide PII from the redaction pipeline.

#### Acceptance Criteria

1. WHEN the Redaction_Agent processes a Redaction Job whose `sourceExtension` is `.pdf`, THE OCR_Path SHALL produce exactly one OCR_Text_Unit for each page of the source document.
2. WHEN the OCR_Path processes a page, THE OCR_Path SHALL rasterize the page's full rectangle to a PNG image at the Raster_Resolution.
3. WHEN the OCR_Path has rasterized a page, THE OCR_Path SHALL call Textract_DetectDocumentText exactly once for that page image.
4. WHEN Textract_DetectDocumentText returns blocks for a page, THE OCR_Path SHALL build that page's OCR_Text_Unit text by joining Word_Block texts with a single space within each recognized line and a single newline character between recognized lines.
5. WHEN the OCR_Path builds a page's OCR_Text_Unit text, THE OCR_Path SHALL record, for every Word_Block, the character offset range that Word_Block occupies within that text together with the Word_Block's normalized bounding box.
6. THE OCR_Path SHALL assign each OCR_Text_Unit the Unit_Ref `ocr:{pageIndex}`, where `pageIndex` is the 0-based source page index.
7. THE Redaction_Agent SHALL evaluate every enabled Redaction Rule against every OCR_Text_Unit using the same regex matching logic applied to Text_Layer_Path units.
8. THE Redaction_Agent SHALL evaluate Bedrock Guardrails PII detection against every OCR_Text_Unit whose text contains at least one character.
9. THE Redaction_Agent SHALL pass the MatchSpans produced from the Text_Layer_Path and the MatchSpans produced from the OCR_Path together, as one collection, to the PDF_Redactor.
10. WHILE a Redaction Job's `sourceExtension` is `.docx`, THE Redaction_Agent SHALL detect PII from the document's stored text only, and SHALL make zero Textract_DetectDocumentText calls for page rasterization.

### Requirement 2: Resolving OCR Matches to Page Rectangles

**User Story:** As a compliance reviewer, I want PII found by the OCR path to be blacked out on the correct part of the page, so that OCR detections translate into real redactions rather than reports.

#### Acceptance Criteria

1. WHEN the PDF_Redactor receives a MatchSpan whose Unit_Ref begins with `text:`, THE PDF_Redactor SHALL resolve that span to rectangles from the page's glyph character rectangles.
2. WHEN the PDF_Redactor receives a MatchSpan whose Unit_Ref begins with `ocr:`, THE PDF_Redactor SHALL resolve that span to the bounding boxes of every Word_Block whose character offset range intersects the span's offset range.
3. WHEN the PDF_Redactor converts a Word_Block's normalized bounding box to PDF user space, THE PDF_Redactor SHALL map it onto the page rectangle by scaling each normalized coordinate by the corresponding page dimension and offsetting by the page rectangle's origin.
4. THE PDF_Redactor SHALL apply a Solid_Redaction_Block to every rectangle resolved from the Text_Layer_Path and to every rectangle resolved from the OCR_Path.
5. WHERE a MatchSpan resolved from the OCR_Path covers part of a Word_Block, THE PDF_Redactor SHALL apply a Solid_Redaction_Block covering that entire Word_Block.
6. WHERE rectangles resolved from the Text_Layer_Path and the OCR_Path overlap on the same page, THE PDF_Redactor SHALL apply a Solid_Redaction_Block for each rectangle.
7. WHEN a Redaction Job's PDF produces zero MatchSpans from both paths, THE PDF_Redactor SHALL return a document whose visible content is unchanged from the source.

### Requirement 3: OCR Path Failures Are Surfaced, Not Swallowed

**User Story:** As a compliance reviewer, I want a PDF job whose OCR path did not run to completion to be reported as failed, so that a degraded detection run is never mistaken for a clean one.

#### Acceptance Criteria

1. IF Textract_DetectDocumentText returns a throttling error or a server-side error for a page, THEN THE OCR_Path SHALL retry that page with exponential backoff for up to 3 total attempts.
2. IF the rasterized page image exceeds Textract_DetectDocumentText's synchronous input size limit, THEN THE OCR_Path SHALL re-rasterize that page at 150 dots per inch and call Textract_DetectDocumentText once more for that page.
3. IF Textract_DetectDocumentText fails for a page after the retries defined in Criteria 1 and 2, THEN THE Redaction_Agent SHALL change the Redaction Job status to `failed` and record the failing 0-based page index and the failure reason in the Job's `errorMessage`.
4. IF a page cannot be rasterized, THEN THE Redaction_Agent SHALL change the Redaction Job status to `failed` and record the failing 0-based page index in the Job's `errorMessage`.
5. WHEN the Redaction_Agent changes a Redaction Job status to `failed` because of an OCR_Path failure, THE Redaction_Agent SHALL leave the Redacted Document unstored.
6. THE Redaction_Agent SHALL change a `.pdf` Redaction Job's status to `completed` only when the OCR_Path produced an OCR_Text_Unit for every page of that document.
7. WHERE Bedrock Guardrails PII detection fails, THE Redaction_Agent SHALL continue the Redaction Job using the detections collected from the remaining sources.

### Requirement 4: Preservation of Existing Pipeline Invariants

**User Story:** As a developer, I want the new OCR path to leave the existing extraction, merging, and verification behavior untouched, so that adding it cannot regress documents that already redact correctly.

#### Acceptance Criteria

1. THE Span_Merging_Logic SHALL merge a MatchSpan only with MatchSpans carrying an identical Unit_Ref.
2. WHEN the Span_Merging_Logic receives a collection containing no `ocr:`-referenced MatchSpans, THE Span_Merging_Logic SHALL produce the same result it produced before this feature existed.
3. THE Text_Layer_Path SHALL record each character exactly as `pdf_glyphs.collect_page_glyphs` reports it, so that offset `i` of a page's text unit remains the character at index `i` of the same page's glyph traversal.
4. WHEN the Structure_Verification_Tool compares a source PDF against a Redacted Document produced with OCR_Path redactions applied, THE Structure_Verification_Tool SHALL report `ok` for a Redacted Document whose page count and page order match the source.
5. THE Redaction_Agent SHALL continue to invoke embedded-image OCR for `ImageRegion`s of both `.docx` and `.pdf` documents, using the `image:{index}` Unit_Ref namespace.
6. THE CDK_Stack SHALL grant the AgentCore Runtime execution role permission to call `textract:DetectDocumentText`.
7. THE OCR_Path SHALL read the Raster_Resolution from an environment variable, and SHALL use 200 dots per inch when that variable is absent or holds a value outside the range 72 to 400.

### Requirement 5: Auditable Detection Provenance

**User Story:** As a compliance reviewer, I want a completed job to tell me how many PII regions each detection path found and whether the two paths agreed, so that I can judge whether a result is trustworthy instead of reading a bare `completed`.

#### Acceptance Criteria

1. WHEN the Redaction_Agent completes a Redaction Job, THE Redaction_Agent SHALL record a Detection_Summary on that Job's `RedactionJobs` record.
2. THE Detection_Summary SHALL contain the number of redacted regions resolved from `text:`-referenced MatchSpans and the number of redacted regions resolved from `ocr:`-referenced MatchSpans.
3. THE Detection_Summary SHALL contain the number of Agreed_Regions found across all pages of the document.
4. THE Detection_Summary SHALL contain, for each page of a `.pdf` document, that page's Text_Layer_Health_Signal.
5. THE Redaction_Agent SHALL select detection paths and resolve MatchSpans independently of every Text_Layer_Health_Signal value.
6. WHEN JobStatusHandler serves `GET /jobs/{jobId}` for a Job whose record carries a Detection_Summary, THE JobStatusHandler SHALL include that Detection_Summary in the response body under the key `detectionSummary`.
7. WHERE a Job's record carries no Detection_Summary, THE JobStatusHandler SHALL return `null` for `detectionSummary`.
8. WHILE a Redaction Job's displayed status is `completed` and its `detectionSummary` is present, THE Job_Status_View SHALL display the total number of redacted regions together with the per-path counts.
9. WHILE a Redaction Job's `detectionSummary` is absent, THE Job_Status_View SHALL display its status indicator and download action unchanged from their behavior before this feature existed.
10. WHEN the Redaction_Agent records a Detection_Summary, THE Redaction_Agent SHALL leave the Job's `jobId`, `sourceKey`, `sourceExtension`, `fileName`, and `createdAt` attributes unchanged.
