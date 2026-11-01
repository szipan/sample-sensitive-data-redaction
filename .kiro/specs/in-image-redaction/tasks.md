# Implementation Plan: In-Image Redaction

## Overview

Implemented in Python (`backend/agent`, `backend/infra`, `backend/shared`,
`backend/lambdas/shared`), matching the existing codebase and building directly
on the shipped `llm-image-ocr` feature. This feature closes the *redaction* half
of the embedded-image gap: `image:`-namespaced spans that the redactors today
skip are turned into black rectangles drawn on the image itself and re-embedded
into the output `.docx`/`.pdf`.

The work proceeds root-cause first, then along the pipeline:

1. **Data models** grow an additive `RecognizedElement` type and an
   `ImageRegion.recognizedElements` field (the in-image analogue of `OcrWord`),
   both optional/defaulted so every existing caller and persisted record is
   unaffected.
2. **Extraction** is made to actually supply image bytes — the root cause that
   today leaves in-image PII undetected and unredacted: the `.pdf` extractor
   populates `imageBytes` via `extract_image(xref)`, and the `.docx` extractor
   (which emits no image regions at all today) begins emitting one `ImageRegion`
   per media part with a stable `docx:part:{partname}` re-embedding handle.
3. **`ocr.py` is extended** (not rewritten) to retarget to Sonnet and return
   text *and* per-element boxes in the same single `Converse` call: the prompt
   asks for a JSON array of `{text, box}` line entries, a defensive parser and a
   robust pixel-vs-fractional coordinate normalizer turn that into
   `RecognizedElement`s, and the whole list collapses to `None` (untrustworthy)
   whenever any box is implausible.
4. **A new `redaction_image.py` module** holds all Pillow interaction: decode,
   decide targeted-vs-whole-image, draw filled black rectangles, re-encode in
   the same format.
5. **Both redactors** stop skipping `image:` spans, gain an `image_regions`
   parameter, and re-embed the modified image (`page.replace_image` for `.pdf`,
   image-part blob replacement for `.docx`).
6. **Orchestration** threads `content.imageRegions` into the redactor. Pipeline
   order is unchanged.
7. **CDK** retargets `IMAGE_OCR_MODEL_ID` to a Sonnet-class id; the env var and
   scoped grant follow the constants automatically.
8. **Pillow becomes a real backend dependency** and is vendored into the agent
   bundle.

The correctness spine (Requirement 6) governs every failure mode: **detected
in-image PII must never remain visible in the output.** Every localization
failure — dormant model, unparseable response, no usable boxes, an implausible
box, a request that fails after retries — funnels into the
**Whole_Image_Fallback** (black out the entire image). Only an unrecoverable
*image decode/draw/encode/re-embed* failure fails the Job (`RedactionError`);
localization failure degrades to safe over-redaction and the Job still
completes.

Three things are deliberately **not** changed, and are called out rather than
touched: `backend/agent/tools/ocr_pdf.py` (the full-page Textract path stays
byte-for-byte — Requirement 10.1, no task touches it), `_regroup_and_merge` and
the regex/Guardrails/LLM detection over `recognizedText` (Requirement 9.1, the
`image:{index}` namespace is unchanged), and
`tests/test_guardrails_pii_detect.py`, which must keep passing **unmodified** —
this feature imports `_is_retryable` from `guardrails_pii_detect` but does not
touch it.

## Tasks

- [x] 1. Add per-element geometry to the shared data models
  - [x] 1.1 Add `RecognizedElement` and `ImageRegion.recognizedElements`
    - Edit `backend/shared/models.py` and mirror the identical changes into `backend/lambdas/shared/models.py`
    - Add a `RecognizedElement` dataclass with fields `text: str`, `startOffset: int`, `endOffset: int`, `boundingBox: BoundingBox`; docstring: it is the in-image analogue of `OcrWord`, its offsets are half-open indices into `ImageRegion.recognizedText`, and its `boundingBox` is normalized to `[0,1]` of the image's own pixel dimensions
    - Add `recognizedElements: Optional[List[RecognizedElement]] = None` to `ImageRegion`; docstring: `None` (the default and placeholder) means "no trustworthy per-element geometry" (localization not performed, or performed and untrustworthy) and drives the Whole_Image_Fallback for any region that carries `image:` spans; a non-empty list means reliable geometry for targeted redaction
    - Update `ImageRegion`'s docstring to note `recognize_image_text` now also attaches `recognizedElements`, and that a Sonnet-class model returns the geometry
    - Additive only: add no required field and retype nothing, so every existing constructor call site and persisted record is unaffected; both new shapes are in-memory-only and never serialized
    - _Requirements: 2.5, 9.2_

- [x] 2. Supply image bytes from extraction (the redaction prerequisite)
  - [x] 2.1 Populate `imageBytes` in the `.pdf` extractor
    - Edit `backend/agent/tools/extraction_pdf.py`: in `_extract_image_regions`, call `document.extract_image(xref)` and set `imageBytes = extracted["image"]` when present
    - Keep the existing `sourceLocation` format `"page:{pageIndex}:xref:{xref}"` and `boundingBox` unchanged — the xref remains the re-embed handle
    - IF `extract_image` fails or returns no/undecodable stream, THEN leave `imageBytes=None`; the region is then skipped by OCR, carries no spans, and the redactor never touches it (best-effort posture)
    - Thread the `document` (or a bound closure) into `_extract_image_regions` so it can call the document-level `extract_image`
    - _Requirements: 1.1_

  - [x]* 2.2 Update `.pdf` extraction unit tests
    - Edit `tests/test_extraction_pdf.py`: assert `imageBytes` is now populated from `extract_image`, and that `sourceLocation`/`boundingBox` are unchanged; add an extract-failure case asserting `imageBytes=None`
    - _Requirements: 1.1_

  - [x] 2.3 Emit `ImageRegion`s from the `.docx` extractor
    - Edit `backend/agent/tools/extraction_docx.py`: enumerate `document.part.package.iter_parts()` and, for each part whose `content_type` starts with `image/` (or whose `partname` starts with `/word/media/`), append one `ImageRegion(zone="image", sourceLocation=f"docx:part:{part.partname}", imageBytes=part.blob, boundingBox=None)`
    - Each distinct media part becomes exactly one `ImageRegion` (a part reused by several `blip` references is enumerated once); `boundingBox` stays `None` because in-image redaction uses the image's own pixel dimensions, not document geometry
    - `sourceLocation` (`docx:part:{partname}`) is the stable re-embed handle the redactor uses
    - _Requirements: 1.1_

  - [x]* 2.4 Update `.docx` extraction unit tests
    - Edit `tests/test_extraction_docx.py`: assert one `ImageRegion` per media part, each with populated `imageBytes` and a `docx:part:` handle (previously the list was empty)
    - _Requirements: 1.1_

- [x] 3. Extend `recognize_image_text` to Sonnet OCR-with-geometry
  - [x] 3.1 Rewrite the prompt and add the defensive localization-response parser
    - Edit `backend/agent/tools/ocr.py`: rewrite `OCR_PROMPT` to instruct the model to return **only** a JSON array whose every element is `{"text": "<characters on one line>", "box": [x0, y0, x1, y1]}` (line-granular, tight box around that line's glyphs); transcribe CJK exactly; forbid translation, transliteration/pinyin, summarization, commentary, markdown, and code fences
    - Bump `OCR_PROMPT_VERSION` to `"in-image-redaction/v1"`
    - Add `_parse_localization_response(assistant_text) -> Optional[list[tuple[str, list]]]`: locate the first `[` and last `]` to tolerate stray prose/fences, `json.loads` the slice, and return the list of `(text, raw_box)` pairs, skipping any entry not shaped `{"text": str, "box": [4 items]}`; it MUST never raise — unparseable input returns `None`
    - Keep the `Converse` request structurally identical to today (same image content block; `system=[{"text": OCR_PROMPT}]`; `inferenceConfig` `temperature=0.0`, `maxTokens`, `topP` omitted); only the prompt and response parsing change
    - _Requirements: 3.1, 3.6, 3.7, 8.1, 8.2_

  - [x]* 3.2 Write property test for the pinned-model gating (JSON-shaped responses)
    - **Feature: in-image-redaction, Property 8: Requests are issued only for a pinned model identifier**
    - Generators produce pinned ids of several families (Sonnet, Haiku, Nova) plus blank, unversioned, and `*-latest` near-misses; assert a request is issued (and geometry attached) iff the id is pinned/versioned, else zero requests and no geometry
    - **Validates: Requirements 3.2, 3.4, 3.5; Property 8**

  - [x] 3.3 Add the coordinate normalizer and lazy pixel-dimension helper
    - Add `_image_pixel_dimensions(image_bytes) -> tuple[int, int]` with `import PIL`/`from PIL import Image` **inside** the function, returning `Image.open(BytesIO(image_bytes)).size`; keep it lazy so pure-normalizer tests need no Pillow
    - Add `_normalize_box(raw_box, image_width_px, image_height_px) -> Optional[BoundingBox]`: return `None` on any non-numeric coordinate; if **any** coordinate `> 1.0` treat the box as pixels and divide x by width / y by height, otherwise treat it as already-fractional; compute `left=min(x0,x1)` etc.; return `None` for an inverted box (`x1<=x0` or `y1<=y0` after scaling); clamp all edges into `[0,1]`; return `None` if clamping collapses to zero/negative area; otherwise return the normalized `BoundingBox`
    - Image pixel dimensions MUST come from the bytes (via `_image_pixel_dimensions`), never from any model-reported dimension
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x]* 3.4 Write property test for coordinate-normalization totality
    - **Feature: in-image-redaction, Property 5: Coordinate normalization is total and never yields a bad box**
    - Generators produce four coordinate values (numeric and non-numeric) and positive image dimensions; assert `_normalize_box` returns either a positive-area `BoundingBox` with edges in `[0,1]` or `None`, never raising and never returning a degenerate/inverted/out-of-bounds box
    - **Validates: Requirements 4.1, 4.3, 4.4; Property 5**

  - [x]* 3.5 Write property test for pixel-vs-fractional interpretation
    - **Feature: in-image-redaction, Property 6: Pixel-vs-fractional interpretation is correct and deterministic**
    - Assert boxes with every coordinate `<= 1.0` are treated as fractional (returned clamped, unchanged interpretation) and any coordinate `> 1.0` triggers pixel scaling by the actual dimensions; the same input always yields the same interpretation
    - **Validates: Requirements 4.1, 4.2, 4.5; Property 6**

  - [x] 3.6 Assemble recognized text from elements and attach the per-region outcome
    - Rename `_recognize_one_region` to `_recognize_and_localize_one_region`; after a completed request: (1) parse JSON — on `None`/empty set `recognizedText=""`, `recognizedElements=None`, emit the audit record, return; (2) assemble `recognizedText` by joining entry texts with `"\n"`, tracking each entry's `[startOffset, endOffset)` during assembly (mirroring `ocr_pdf._build_page_unit`); (3) normalize each entry's box
    - If **every** entry produced a plausible box and there is ≥1 element, set `region.recognizedElements = [RecognizedElement(text, start, end, box), ...]` (reliable); if **any** entry's box is implausible, set `region.recognizedElements = None` (untrustworthy) while leaving `recognizedText` set so downstream detection still produces spans and the redactor whole-images the region
    - Keep the public `recognize_image_text(image_regions) -> List[ImageRegion]` signature, per-region single-request accounting, size/format guards, never-raise loop, lazy `bedrock-runtime` client, pinned-id gating, and strands-free import all exactly as shipped; the tool now mutates `recognizedText` **and** `recognizedElements` (additive) and nothing else
    - `recognizedText` remains a plain string so downstream detection is untouched
    - _Requirements: 2.1, 2.2, 3.1, 9.1, 9.2, 9.3, 9.4_

  - [x] 3.7 Extend the audit record with rectangle count / fallback flag
    - Extend the existing per-completed-request `logger.info` audit line (on the `agent.tools.ocr` logger) to add the number of rectangles that will be redacted for the image and whether the Whole_Image_Fallback will apply
    - It continues to carry `sourceLocation`, model id, `OCR_PROMPT_VERSION`, the image SHA-256 digest, and byte length, and continues to record **neither** the image bytes, the recognized text, **nor** any box coordinates or content
    - _Requirements: 8.3, 8.4, 8.5_

  - [x]* 3.8 Write property test for recognized-text assembly and offset comparability
    - **Feature: in-image-redaction, Property 1: Recognized text is assembled from the elements, so offsets are comparable**
    - For any parsed list of entries, assert `recognizedText[startOffset:endOffset]` equals each element's text and the elements' offset ranges are non-overlapping and in reading order
    - **Validates: Requirements 2.1, 2.2; Property 1**

  - [x]* 3.9 Write property test for the inference-configuration pinning
    - **Feature: in-image-redaction, Property 12: Every localization request pins the inference configuration**
    - Assert every issued `Converse` request carries `temperature == 0` and omits `topP`
    - **Validates: Requirements 8.1, 8.2; Property 12**

  - [x]* 3.10 Write property test for the PII-safe audit record
    - **Feature: in-image-redaction, Property 13: The audit record identifies the run without recording PII**
    - For any eligible region and any (JSON) model response, assert the audit record contains `sourceLocation`, model id, prompt version, the image SHA-256 digest and byte length, and the rectangle count, and contains neither the image bytes, the recognized text, nor any box coordinates
    - **Validates: Requirements 8.3, 8.4; Property 13**

  - [x]* 3.11 Write property test for the instance-preservation contract
    - **Feature: in-image-redaction, Property 14: The same instances are returned, with only text and geometry touched**
    - Assert `recognize_image_text` returns the same `ImageRegion` instances in the same order, mutates only `recognizedText` and `recognizedElements` and only on eligible regions, and leaves `boundingBox`/`zone`/`sourceLocation`/`imageBytes` unchanged
    - **Validates: Requirement 9.2; Property 14**

  - [x]* 3.12 Update and retarget the example/edge/structural unit tests for `ocr.py`
    - Convert the existing fake `converse` responses to return **JSON** assistant text (`[{"text": ..., "box": [...]}, ...]`) using the `FakeConverseClient`/`SequencedConverseClient`/`BehaviorConverseClient` fakes already in `tests/test_ocr.py`; mock Bedrock with `unittest.mock`, not moto
    - Assert `OCR_PROMPT` is module-level, carries `OCR_PROMPT_VERSION == "in-image-redaction/v1"`, and asks for text+box with no translation/transliteration/commentary; assert the localization prompt version constant (_Requirements: 3.6, 3.7_)
    - Add example tests for `_parse_localization_response` (valid array, prose-wrapped array, malformed entries skipped, garbage → `None`) and for a single `Converse` call yielding both text and boxes (_Requirements: 3.1_)
    - Retire the shipped `llm-image-ocr` "recognizedText is the returned text" test into the new P1 assembly test; broaden the "only recognizedText touched" test into P14
    - _Requirements: 3.1, 3.6, 3.7, 9.3, 9.4_

- [x] 4. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - `tests/test_guardrails_pii_detect.py` must be passing **unmodified** at this point (this feature imports `_is_retryable` from it)

- [x] 5. Build the image-manipulation module `redaction_image.py`
  - [x] 5.1 Implement `redaction_image.py` decode/decision/draw/encode core
    - Create `backend/agent/tools/redaction_image.py` with: `class ImageRedactionError(Exception)` (unrecoverable decode/draw/encode failure, Requirement 7.3); `DEFAULT_BOX_PADDING_PX = 4`; `BOX_PADDING_ENV_VAR = "IMAGE_REDACTION_BOX_PADDING_PX"` read at use-time with the default as fallback
    - Implement `redact_image_bytes(image_bytes, region, spans_for_region) -> bytes`: decode with Pillow (`Image.open`), remember the original format and actual `(W, H)`; a decode failure raises `ImageRedactionError`
    - Decide **targeted** iff `region.recognizedElements is not None` **and** every span resolves to ≥1 rectangle; otherwise **Whole_Image_Fallback** (a single full-image rectangle `(0,0,W,H)`)
    - Convert palettized/`P`-mode images to `RGB` so black is exact; draw filled opaque black rectangles via `ImageDraw.Draw(img).rectangle`; re-encode to the **same format** the image arrived in and return the bytes; an encode failure raises `ImageRedactionError`
    - Import Pillow lazily inside `redact_image_bytes` so the module imports without Pillow installed
    - _Requirements: 1.1, 6.2, 7.3_

  - [x] 5.2 Implement the pure offset→box mapping and padding helpers
    - Add `_rects_for_image_span(recognized_elements, span, W, H) -> list[rect]`: include an element's box iff `element.startOffset < span.endOffset and span.startOffset < element.endOffset` (half-open overlap), scaling each included normalized `[0,1]` box to pixels (mirroring `redaction_pdf._rects_for_ocr_span` + `_normalized_bbox_to_page_rect`)
    - Add a padding helper that expands each pixel rectangle by Box_Padding and clamps it to `[0, W] × [0, H]`
    - Keep these helpers pure (no Pillow) so they are unit-tested directly
    - _Requirements: 2.3, 2.4, 5.1, 5.2, 5.3_

  - [x]* 5.3 Write property test for span-coverage under reliable geometry
    - **Feature: in-image-redaction, Property 2: Every detected in-image PII span is covered by a rectangle**
    - For any region with reliable geometry and any `image:` span, assert the targeted rectangle set covers the union of the boxes of every element the span overlaps, and that a span overlapping no element forces the whole-image fallback (span PII never left uncovered)
    - **Validates: Requirements 1.1, 1.2, 6.3; Property 2**

  - [x]* 5.4 Write property test for exact offset-overlap box inclusion
    - **Feature: in-image-redaction, Property 3: Offset overlap decides box inclusion, exactly**
    - For any elements and span, assert `_rects_for_image_span` includes an element's box iff the half-open ranges overlap, and never includes a non-overlapping element's box
    - **Validates: Requirements 2.3, 2.4; Property 3**

  - [x]* 5.5 Write property test for padding expand-then-clamp
    - **Feature: in-image-redaction, Property 7: Padding expands the rectangle and then clamps within bounds**
    - For any pixel rectangle, image dimensions, and non-negative Box_Padding, assert the padded rectangle contains the original and lies entirely within `[0, W] × [0, H]`
    - **Validates: Requirements 5.1, 5.2; Property 7**

  - [x]* 5.6 Write property test for the whole-image fallback triggers
    - **Feature: in-image-redaction, Property 9: Every untrustworthy-localization mode covers the whole image**
    - For any region with ≥1 `image:` span whose localization is unavailable/unparseable/empty or produced any implausible box (`recognizedElements is None`), assert `redact_image_bytes` fills the entire image (assert decoded corner pixels are black); generate small in-memory PNGs with Pillow
    - **Validates: Requirements 6.1, 6.2, 6.3, 7.2; Property 9**

  - [x]* 5.7 Write unit tests for decode/encode failure and drawing
    - New module `tests/test_redaction_image.py`: assert a targeted redaction blacks out the expected region on a generated PNG (decoded pixels), a `P`-mode image is handled, the output format matches the input, and an undecodable byte string raises `ImageRedactionError`
    - Uses real Pillow (decoding real PNGs) — belongs in the real-`fitz`/`docx` test process (see Notes)
    - _Requirements: 1.1, 7.3_

- [x] 6. Teach the PDF redactor to act on `image:` spans
  - [x] 6.1 Add the `image:` handler and `image_regions` parameter to `redaction_pdf.py`
    - Edit `backend/agent/tools/redaction_pdf.py`: add an optional `image_regions=None` parameter to both `redact_pdf_document_with_report` and the `bytes`-returning wrapper `redact_pdf_document`
    - Replace the bare `continue` that drops non-`text:`/`ocr:` spans with an `image:` handler: group `image:` spans by region index, resolve `image:{index}` → `ImageRegion`, and for each referenced region call `redaction_image.redact_image_bytes(region.imageBytes, region, spans)`, wrapping `ImageRedactionError` into `RedactionError`
    - Re-embed via `page.replace_image(xref, stream=new_bytes)`, deriving `pageIndex`/`xref` from `sourceLocation` (`"page:{pageIndex}:xref:{xref}"`); run image replacement after `apply_redactions`, then serialize once with `document.tobytes()` as today
    - Defensively raise `RedactionError` if a span references a region with no `imageBytes` (a wiring bug, matching the existing out-of-range guards)
    - Leave the `text:`/`ocr:` redaction path and the `PdfRedactionReport` counts byte-for-byte unchanged; import `fitz` lazily as today
    - _Requirements: 1.1, 1.2, 1.4, 7.3, 10.2_

  - [x]* 6.2 Write property test that `text:`/`ocr:` redaction is unchanged (PDF)
    - **Feature: in-image-redaction, Property 11: `text:` and `ocr:` spans are redacted unchanged**
    - For any set of `text:`/`ocr:` spans and no `image:` spans, assert the `.pdf` output is identical to the pre-feature output
    - **Validates: Requirements 1.4, 10.2; Property 11**

  - [x]* 6.3 Write unit tests for PDF `image:` redaction and manipulation-failure semantics
    - Edit `tests/test_redaction_pdf.py`: add targeted-redaction and whole-image-fallback cases with a stubbed `Page.replace_image`; assert an image decode failure raises `RedactionError`; assert existing `text:`/`ocr:` behavior unchanged
    - Opens real PDFs — belongs in the real-`fitz`/`docx` test process (see Notes)
    - _Requirements: 1.1, 1.2, 7.3_

- [x] 7. Teach the DOCX redactor to act on `image:` spans
  - [x] 7.1 Add the `image:` handler and `image_regions` parameter to `redaction_docx.py`
    - Edit `backend/agent/tools/redaction_docx.py`: add an optional `image_regions=None` parameter to `redact_docx_document`
    - After the existing text-run redaction on the reopened `Document` and **before** the single `document.save()`, group `image:` spans by region index and for each referenced region compute `new_bytes = redaction_image.redact_image_bytes(region.imageBytes, region, spans)` (wrapping `ImageRedactionError` into `RedactionError`)
    - Re-embed by locating the image part by the `docx:part:{partname}` handle in `sourceLocation` among `document.part.package.iter_parts()` and replacing its blob (`image_part._blob = new_bytes`, mirroring this module's existing use of python-docx internals); replacing the media part updates every `<a:blip>` that references it (safe over-redaction if reused)
    - Leave the existing text-run redaction and the "runs with no match are untouched" guarantee unchanged; import `docx` lazily as today
    - _Requirements: 1.1, 1.2, 1.4, 7.3_

  - [x]* 7.2 Write unit tests for DOCX `image:` redaction
    - Edit `tests/test_redaction_docx.py`: add image-part blob replacement by the `docx:part:` handle for both targeted and fallback cases; assert existing text redaction is unchanged
    - Opens real `.docx` — belongs in the real-`fitz`/`docx` test process (see Notes)
    - _Requirements: 1.1, 1.2, 1.4_

- [x] 8. Thread `image_regions` through orchestration
  - [x] 8.1 Pass `content.imageRegions` into the redactor
    - Edit `backend/agent/orchestration.py`: pass `content.imageRegions` into `_redact_document`, which forwards it to `redact_pdf_document_with_report` (`.pdf`) and `redact_docx_document` (`.docx`)
    - Change no detection order, add no new supplementary source, and keep the never-raise posture of the three detection sources and the primary-path redaction failure semantics unchanged
    - _Requirements: 1.5, 7.5, 9.1_

  - [x]* 8.2 Write property test for the localization-degrades / manipulation-fails split
    - **Feature: in-image-redaction, Property 10: Localization failures degrade; manipulation failures fail the Job**
    - For any localization failure (dormant, retries exhausted, unparseable, implausible), assert a Job whose other steps succeed reaches `completed` with the image safely over-redacted; and for any image decode/draw/encode/re-embed failure on an image carrying spans, assert the redactor raises `RedactionError` and the Job reaches `failed` with nothing stored
    - **Validates: Requirements 7.2, 7.3, 7.4; Property 10**

  - [x]* 8.3 Write property test that a no-span image is left byte-identical
    - **Feature: in-image-redaction, Property 4: An image with no spans is left byte-identical**
    - For any set of match spans and image regions, assert an `ImageRegion` referenced by no `image:` span is neither decoded, drawn on, nor re-embedded, and its output bytes equal its source bytes
    - **Validates: Requirements 1.3, 6.5; Property 4**

  - [x]* 8.4 Update orchestration wiring tests
    - Edit `tests/test_orchestration.py`: assert `image_regions` is threaded into `_redact_document`; assert pipeline order and posture are unchanged
    - _Requirements: 1.5, 7.5_

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Retarget the localization model in CDK
  - [x] 10.1 Point `IMAGE_OCR_MODEL_ID` / `IMAGE_OCR_FOUNDATION_MODEL` at Sonnet
    - Edit `backend/infra/redaction_stack.py`: set `IMAGE_OCR_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"` and `IMAGE_OCR_FOUNDATION_MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"`
    - The `IMAGE_OCR_MODEL_ID` runtime environment variable and the scoped `bedrock:InvokeModel` grant already reference these constants, so retargeting moves both the env var (Requirement 11.2) and the IAM grant (Requirements 11.1, 11.3) to Sonnet in one edit; the prior Haiku image-OCR grant is thereby replaced; the separate `LLM_PII_MODEL_ID` grant is untouched
    - Keep the comment that clearing `IMAGE_OCR_MODEL_ID` is the documented off switch (Requirement 11.4); add no new environment variable and no wildcard model resource
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [x]* 10.2 Update the CDK template smoke tests
    - Edit `tests/test_redaction_stack.py`: assert `IMAGE_OCR_MODEL_ID` is now the Sonnet-class id and is pinned per `ocr._is_pinned_model_id`; assert the scoped `bedrock:InvokeModel` grant names the Sonnet inference-profile and foundation-model ARNs and no wildcard model resource; assert the env var is present on the Runtime
    - _Requirements: 3.3, 11.1, 11.2, 11.3_

- [x] 11. Vendor Pillow into the backend and the agent bundle
  - [x] 11.1 Add Pillow as a real backend dependency
    - Add `Pillow==11.0.0` (verify the pin publishes `cp312` aarch64 manylinux wheels — `manylinux_2_28_aarch64` / `manylinux2014_aarch64` — matching the platforms `build_agent_bundle.py` already downloads for PyMuPDF) to **both** `backend/requirements.txt` and `backend/agent-runtime-requirements.txt`
    - The runtime imports Pillow as `PIL`
    - _Requirements: 1.1_

  - [x] 11.2 Extend the bundle builder's key-package check
    - Edit `backend/scripts/build_agent_bundle.py`: add `PIL`/`pillow` to the summary's key-package list so a missing Pillow wheel is caught during the build
    - _Requirements: 1.1_

  - [x] 11.3 Rebuild the agent bundle so the aarch64 Pillow wheel is vendored
    - Run `backend/scripts/build_agent_bundle.py` so the aarch64 cp312 Pillow wheel is downloaded (via pip's `--platform`/`--python-version` flags, the same pattern used for PyMuPDF) and unpacked into `agent_bundle/package_clean/`
    - The build must run on a Linux host (per the script's KNOWN LIMITATION); confirm the summary reports `PIL`/`pillow` present
    - Deployment (`cdk deploy`) is left to the user and is not part of this task
    - _Requirements: 1.1_

- [x] 12. Prove the in-image leak is closed end to end (env-guarded, real Sonnet)
  - [x]* 12.1 Write the env-guarded real-Sonnet recall + redaction integration test
    - Add an env-guarded integration test (mirroring `tests/test_image_ocr_recall.py`), guarded by `RUN_IN_IMAGE_REDACTION`, that against the real Chinese driving-license fixture runs `recognize_image_text` (real Sonnet `Converse`) **and** the redactor end to end
    - Assert (a) the recall floor — `recognizedText` contains the Chinese resident id and name — and (b) that the redacted output image's PII-region pixels are black (a coarse "is this area covered" check) or that the Whole_Image_Fallback fired
    - Skips (never fails) when credentials, model access, region, Pillow, or a CJK font are missing, so `pytest tests` stays offline and free; it costs a real model invocation but is the only test that verifies the feature's premise
    - _Requirements: 1.1, 1.2, 6.2_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Task 12 is the one worth not skipping: without it, the claim that this feature closes the demonstrated in-image leak is untested end to end.
- **`backend/agent/tools/ocr_pdf.py` is not modified by any task** (Requirement 10.1). The full-page Textract path keeps its word-level geometry and stays fatal-on-failure; the two OCR paths remain deliberately asymmetric (best-effort embedded images, fatal full-page).
- **Downstream detection and merging are untouched** (Requirement 9.1): the regex/Guardrails/LLM detection over `recognizedText` and `_regroup_and_merge` are unchanged because the `image:{index}` namespace and `recognizedText`'s string shape are unchanged. This feature only *adds* `recognizedElements` and teaches the redactor to act on `image:` spans.
- **`tests/test_guardrails_pii_detect.py` must keep passing unmodified**: this feature imports `_is_retryable` from `guardrails_pii_detect` but does not touch it. `tests/test_ocr_pdf.py`-equivalent paths must likewise keep passing unmodified (Requirement 10.1).
- The correctness spine is Requirement 6 (Whole_Image_Fallback), captured by Properties 2 and 9: every untrustworthy-localization mode covers the whole image, so detected in-image PII is never left visible. A *localization* failure never fails the Job; only an unrecoverable *image manipulation* failure does (`RedactionError` → Job `failed`).
- Every property test uses **Hypothesis** (already a project dependency — see `backend/.hypothesis/`), runs at least **100 iterations**, and is tagged `Feature: in-image-redaction, Property {number}: {property title}`. No property-based framework is written from scratch.
- Bedrock is mocked with `unittest.mock`, **not moto**, reusing the `FakeConverseClient`/`SequencedConverseClient`/`BehaviorConverseClient` fakes in `tests/test_ocr.py`; the fake `converse` now returns a **JSON** assistant text (`[{"text": ..., "box": [...]}, ...]`) so the parser and normalizer are exercised. Fake errors carry duck-typed `ClientError`-shaped `response` dicts so the imported `_is_retryable` classifies them as in production. Real model calls appear only in task 12.
- **Pillow in tests**: the normalizer and offset→box property tests call the pure functions with explicit dimensions and need no Pillow. Tests for `redact_image_bytes` and the re-embed paths generate small in-memory PNGs with Pillow and assert on decoded pixels; these need Pillow, now a real dev dependency.
- No new third-party dependencies beyond **Pillow**: `boto3`, PyMuPDF (`fitz`), python-docx, and Hypothesis are already in use.
- Backend tests run as **two separate processes** — the `MagicMock` `sys.modules` `fitz`/`docx` stubs registered by the first command must not contaminate the real-`fitz`/`docx` tests in the second. Image-redaction tests that decode real PNGs and open real `.pdf`/`.docx` belong in the **second** (real-`fitz`/`docx`) process group:
  ```
  .venv\Scripts\python.exe -m pytest tests\test_ocr.py tests\test_orchestration.py tests\test_redaction_stack.py tests\test_guardrails_pii_detect.py tests\test_llm_pii_detect.py -q
  .venv\Scripts\python.exe -m pytest tests\test_extraction_pdf.py tests\test_extraction_docx.py tests\test_redaction_pdf.py tests\test_redaction_docx.py tests\test_redaction_image.py -q
  ```

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "10.1", "11.1", "11.2"] },
    { "id": 1, "tasks": ["2.1", "2.3", "3.1", "3.3", "10.2", "11.3"] },
    { "id": 2, "tasks": ["2.2", "2.4", "3.2", "3.4", "3.5", "3.6", "5.1", "5.2"] },
    { "id": 3, "tasks": ["3.7", "3.8", "3.9", "3.10", "3.11", "3.12", "5.3", "5.4", "5.5", "5.6", "5.7", "6.1", "7.1"] },
    { "id": 4, "tasks": ["6.2", "6.3", "7.2", "8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3", "8.4"] },
    { "id": 6, "tasks": ["12.1"] }
  ]
}
```
