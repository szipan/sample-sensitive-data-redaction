# Requirements Document

## Introduction

The shipped `llm-image-ocr` feature closed the *detection* half of the
embedded-image gap: `recognize_image_text` (`backend/agent/tools/ocr.py`) reads
the text — including CJK — out of an embedded image via a multimodal Bedrock
model and writes it to `ImageRegion.recognizedText`, and the downstream
detection sources (regex `match_rules`, Bedrock Guardrails, the LLM text
source) then flag PII in that text under the `"image:{index}"` Unit_Ref
namespace. But the *redaction* half is still open: both redactors deliberately
**skip** `image:`-namespaced spans. `redact_docx_document` only handles spans
whose `unitRef` starts with `"text:"`; `redact_pdf_document_with_report` routes
`text:` and `ocr:` spans and drops everything else (`image:` in practice) with
a bare `continue`. The result: a name like `周杰` inside a driving-license
image is *detected* but left fully visible in the output document — a real,
demonstrated leak.

This feature closes that gap. When PII is detected inside an embedded image,
the system blacks out the offending region(s) **on the image itself** and
re-embeds the modified image into the output `.docx`/`.pdf`, so the delivered
Redacted Document no longer shows the in-image PII.

The hard part is not drawing black boxes — it is knowing *where* on the image
to draw them. That requires per-element geometry (a bounding box for the
recognized text), which the shipped OCR tool does not produce: it returns text
only. This feature therefore adds a **localization** capability that produces,
for the recognized text of an image, the on-image bounding box(es) of the
detected PII — analogous to how the full-page PDF path (`ocr_pdf_pages`)
already returns `OcrWord`s carrying `startOffset`/`endOffset`/`boundingBox`,
and how `redact_pdf_document`'s `_rects_for_ocr_span` maps a span's offset
range onto those word boxes to produce rectangles.

### Findings carried in from a feasibility experiment (settled, do not re-derive)

A localization experiment was already run against a real Chinese
driving-license image. Its conclusions are inputs to this spec, not open
questions:

- A multimodal LLM returns **accurate text**; the open question was whether it
  can return **accurate bounding boxes** for that text.
- **Claude Haiku bounding boxes are unusable** — badly misplaced, would black
  out empty areas and leave PII visible. Haiku is sufficient for *reading* the
  text but not for *locating* it.
- **Claude Sonnet** (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) bounding
  boxes are tight and accurate — compliance-grade — on the tested image.
- Sonnet returned **pixel** coordinates (e.g. `[117, 99, 160, 124]` on a
  700×450 image) even when the prompt requested normalized `[0, 1]` fractions.
  The system must therefore robustly handle whatever the model returns —
  detecting whether coordinates are pixel or fractional and normalizing against
  the actual image dimensions.
- Conclusion: targeted in-image redaction is viable but **requires a
  Sonnet-class model for localization**; Haiku is insufficient for geometry.

### The overriding correctness rule

Because localization is probabilistic and this is a compliance system, the
governing rule of the whole feature is: **detected in-image PII must never
remain visible in the output.** If localization fails, returns nothing,
returns low-confidence or implausible boxes, or the image cannot be processed
at all, the system MUST fall back to a guaranteed-safe outcome — redacting the
**whole image** (or otherwise guaranteeing the detected PII is not visible) —
rather than emitting a partially- or un-redacted image. Over-redaction is the
safe direction; a missed in-image PII value is not an acceptable outcome.

### Relationship to the existing pipeline and its invariants

This feature builds **on top of** `llm-image-ocr` and must not regress the
established pipeline invariants:

- **Redaction is a PRIMARY pipeline step.** Unlike the supplementary detection
  sources (`detect_pii_with_guardrails`, `detect_pii_with_llm`,
  `recognize_image_text`), which are best-effort and never fail the Job, the
  redactors raise `RedactionError` on unrecoverable failure and that
  propagates to `handle_job`'s top-level handler, driving the Job to `failed`
  with no Redacted Document stored. In-image redaction is part of that primary
  step and inherits those semantics — with the one carve-out that a
  *localization* failure is handled by the whole-image safety fallback rather
  than by failing the Job (a localization failure has a safe redaction outcome;
  only an unrecoverable *redaction* failure fails the Job).
- **Downstream detection is unchanged.** This feature adds geometry to the OCR
  output and teaches the redactor to act on `image:` spans. The regex,
  Guardrails, and LLM detection over `recognizedText`, and the
  `_regroup_and_merge` step, are untouched.
- **SDK/import constraints.** New code must import no Strands Agents SDK
  symbol and must remain importable for unit testing without `boto3`,
  `strands`, or `bedrock_agentcore` installed (lazy client construction with
  `import boto3` inside the getter), mirroring `ocr.py` / `ocr_pdf.py`.
- **Determinism and PII-safe auditing.** Localization calls pin
  `temperature = 0` and emit audit records that never contain the image bytes,
  the recognized text, or box contents that would reveal PII — mirroring the
  shipped OCR audit approach.

### Open design decisions (flagged for review — see the "Open Design Decisions" section)

Four decisions are deliberately **not settled** in these requirements and are
enumerated at the end for the user to resolve before design:

1. **Localization model strategy** — switch image OCR entirely to Sonnet, vs a
   two-pass approach (Haiku reads text cheaply; Sonnet localizes only when PII
   was actually detected in that image).
2. **`.docx` vs `.pdf` scope** — implement both now, or `.docx` first with
   `.pdf` in a later phase.
3. **Image-manipulation dependency** — add Pillow as a real backend dependency,
   or use the PDF stack already in the backend (pikepdf / pypdfium2) for image decode/draw/encode.
4. **Extraction prerequisites** — the current extractors do not yet supply
   everything this feature needs (see "Assumptions and Dependencies"); the
   scope of the extraction changes needs confirmation.

### Open design decisions (RESOLVED)

The four decisions below were resolved with the user before design:

1. **Localization model strategy — RESOLVED: A1 (Sonnet-only).** Switch image
   OCR entirely to a Sonnet-class model; one Converse call returns both the
   recognized text and the per-element boxes. No two-pass, no second model, no
   new env var — the existing `IMAGE_OCR_MODEL_ID` is retargeted to Sonnet.
2. **`.docx` vs `.pdf` scope — RESOLVED: B1 (both).**
3. **Image-manipulation dependency — RESOLVED: C2 (add Pillow).**
4. **Extraction prerequisites — RESOLVED: in scope for THIS spec.** The
   extraction changes (populate `.pdf` Image_Region `imageBytes`; make `.docx`
   emit Image_Regions with a stable re-embedding handle) are part of this
   feature, since they are the root cause that today leaves in-image PII
   entirely undetected and unredacted.

The detailed rationale for each is retained below for the record.

## Glossary

- **In_Image_Redaction**: The capability, added by this feature, of covering
  detected PII inside an embedded image with solid black rectangles and
  re-embedding the modified image into the output document.
- **Image_Region**: An `ImageRegion` (`backend/shared/models.py`) representing
  an embedded image, carrying `zone="image"`, a `sourceLocation`, the raw
  encoded `imageBytes`, a `boundingBox` (the image's position within the
  document), and the `recognizedText` populated by `recognize_image_text`.
- **Recognized_Text**: The text `recognize_image_text` reads from an
  Image_Region and writes to `ImageRegion.recognizedText`.
- **Image_Span**: A `MatchSpan` whose `unitRef` begins with `"image:"` and
  whose `startOffset`/`endOffset` index into an Image_Region's Recognized_Text,
  as produced by the downstream detection sources under the existing
  `"image:{index}"` Unit_Ref namespace.
- **Localization**: The act of determining, for an Image_Region, the on-image
  bounding box(es) of a recognized text element, so that an Image_Span's offset
  range can be mapped to rectangle(s) on the image.
- **Localization_Model**: The pinned, versioned Sonnet-class Bedrock model
  used for embedded-image OCR, addressed through the `Converse` API, that in a
  **single** call returns both the Recognized_Text and the per-element bounding
  boxes for an Image_Region. Per the resolved model decision (A1), this is the
  same model the shipped `IMAGE_OCR_MODEL_ID` selects — retargeted to a
  Sonnet-class id — not a second, separate model.
- **Image_OCR_Model_Id_Variable**: The existing `IMAGE_OCR_MODEL_ID`
  environment variable, now carrying a pinned, versioned **Sonnet-class**
  identifier. No new, separate localization variable is introduced (resolved
  decision A1): one model, one variable, one call does both reading and
  localization.
- **Recognized_Element**: A recognized text element of an Image_Region carrying
  both its character offset range into the Recognized_Text and its on-image
  bounding box — the in-image analogue of `ocr_pdf.py`'s `OcrWord`.
- **Image_Coordinate_Normalizer**: The component that converts whatever
  coordinates the Localization_Model returns (pixel or fractional) into a
  consistent space relative to the image's actual pixel dimensions.
- **Box_Padding**: A small, configurable margin added around each localized
  rectangle so a slightly-tight box still fully covers the underlying glyphs.
- **Whole_Image_Fallback**: The guaranteed-safe redaction outcome — covering
  the entire image (or otherwise guaranteeing the detected PII is not visible)
  — applied whenever targeted Localization cannot be trusted for an
  Image_Region that has at least one Image_Span.
- **Image_Redaction_Backend**: The library used to decode an image, draw filled
  black rectangles on it, and re-encode it — either the existing PDF stack (pikepdf / pypdfium2) or Pillow,
  per the open dependency decision.
- **Solid_Redaction_Block**: A filled, opaque black rectangle drawn on the
  image, and (for consistency with the existing redactors) rendered so that
  none of the covered pixels reveal the underlying content.
- **In_Scope_Extension**: A source document extension for which in-image
  redaction is implemented (`.docx`, `.pdf`, or both), per the open scope
  decision.
- **Redaction_Agent**: The AgentCore-hosted deterministic pipeline
  (`backend/agent/orchestration.py`, `handle_job`) that extracts, OCRs,
  matches, redacts, verifies, stores, and updates the Redaction Job.
- **CDK_Stack**: `RedactionStack` (`backend/infra/redaction_stack.py`), which
  provisions the AgentCore Runtime, its execution role, and its environment
  variables.

## Assumptions and Dependencies

These are properties of the current codebase that this feature depends on, at
least one of which is **not yet satisfied** and is therefore surfaced as an
open decision (Open Design Decision 4).

1. **Image bytes must be available to the redactor.** In-image redaction
   requires the raw encoded bytes of each Image_Region both to localize PII and
   to redraw/re-encode the image. The `.pdf` extractor
   (`extraction_pdf._extract_image_regions`) today populates `sourceLocation`
   (`"page:{pageIndex}:xref:{xref}"`) and `boundingBox` but leaves
   `imageBytes` unset (`None`); the `.docx` extractor
   (`extraction_docx`) emits an **empty** `imageRegions` list and extracts no
   embedded images at all. Whichever In_Scope_Extension is chosen, the
   extraction path must be made to supply the Image_Regions (with `imageBytes`)
   that both `recognize_image_text` and this feature need. The exact extraction
   changes are in scope for design once the scope decision lands.
2. **Stable re-embedding handle.** The redactor must be able to locate, in the
   reopened source document, the exact image an Image_Region came from, so it
   can replace it. For `.pdf` the `sourceLocation` xref is that handle; for
   `.docx` an equivalent handle must be established.
3. **`recognize_image_text` mutates Image_Region instances in place** and
   returns the same instances in the same order; this feature's geometry output
   attaches to those same instances.

## Requirements

### Requirement 1: Act on image-namespaced spans (stop skipping)

**User Story:** As a compliance reviewer, I want PII detected inside an
embedded image to be blacked out in the delivered document, so that in-image
PII is not visible in the Redacted Document.

#### Acceptance Criteria

1. WHEN the redactor processes a Redaction Job for an In_Scope_Extension whose
   match spans include at least one Image_Span, THE Redaction_Agent SHALL cover
   the detected PII region(s) of the corresponding Image_Region with one or
   more Solid_Redaction_Blocks and re-embed the modified image into the output
   document.
2. WHEN an Image_Span references an Image_Region, THE Redaction_Agent SHALL map
   the span's `[startOffset, endOffset)` range within the Recognized_Text to
   on-image rectangle(s) via the Image_Region's Recognized_Elements.
3. WHERE an Image_Region has no Image_Span referencing it, THE Redaction_Agent
   SHALL leave that image unchanged in the output document.
4. WHEN the redactor encounters a `MatchSpan` whose `unitRef` begins with
   `"text:"` or `"ocr:"`, THE Redaction_Agent SHALL redact it exactly as it
   does today, unchanged by this feature.
5. THE Redaction_Agent SHALL leave the reading position, structure, and
   non-image content of the output document unchanged relative to the source,
   redacting only the image pixels covering detected PII (and, where the
   Whole_Image_Fallback applies, the whole image).

### Requirement 2: Per-element geometry for recognized image text

**User Story:** As a developer, I want the OCR output to carry per-element
bounding boxes for the recognized image text, so that an image-namespaced span
can be resolved to rectangles on the image, mirroring the full-page OCR path.

#### Acceptance Criteria

1. THE In_Image_Redaction capability SHALL produce, for an Image_Region that has
   at least one Image_Span, a set of Recognized_Elements, each carrying a
   character offset range into the Recognized_Text and an on-image bounding box.
2. THE Recognized_Element offset ranges SHALL index into the same
   Recognized_Text over which the downstream detection sources produced the
   Image_Spans, so that an Image_Span's offset range and a Recognized_Element's
   offset range are directly comparable.
3. WHEN an Image_Span's offset range overlaps a Recognized_Element's offset
   range, THE Redaction_Agent SHALL include that Recognized_Element's bounding
   box among the rectangles redacted for the span.
4. WHERE a recognized element carries geometry but no character it covers is
   part of any Image_Span, THE Redaction_Agent SHALL NOT redact that element's
   region.
5. THE per-element geometry SHALL be attached to the same Image_Region
   instances that `recognize_image_text` returns, under the existing
   `"image:{index}"` Unit_Ref namespace.

### Requirement 3: Sonnet-class model returns text and geometry in one call

**User Story:** As a system administrator, I want the embedded-image OCR model
retargeted to a pinned Sonnet-class model that returns both the recognized text
and its per-element bounding boxes in a single call, so that geometry is
compliance-grade and the model can be retargeted without a code change.

#### Acceptance Criteria

1. THE In_Image_Redaction capability SHALL obtain both the Recognized_Text and
   the per-element on-image bounding boxes for an Image_Region from the
   Localization_Model in a single Bedrock `Converse` call carrying the image
   content block.
2. THE In_Image_Redaction capability SHALL read the Localization_Model
   identifier from the existing `IMAGE_OCR_MODEL_ID` variable at invocation
   time (resolved decision A1: one model does both reading and localization).
3. THE `IMAGE_OCR_MODEL_ID` value SHALL be a pinned, versioned Sonnet-class
   Bedrock model or inference-profile identifier, since a non-Sonnet model
   (e.g. Haiku) does not localize accurately enough for compliance-grade
   redaction.
4. THE In_Image_Redaction capability SHALL accept any pinned, versioned Bedrock
   model or inference-profile identifier, independent of the model family it
   names, so the model can be retargeted by configuration alone.
5. IF `IMAGE_OCR_MODEL_ID` is absent, blank, or a floating alias (an identifier
   ending in `latest` or carrying no version segment), THEN embedded-image OCR
   is dormant (per the shipped `llm-image-ocr` behavior) and no Image_Spans are
   produced for any image, so there is nothing to redact and no fallback is
   triggered.
6. THE In_Image_Redaction capability SHALL send the Localization_Model text that
   instructs it to return, for each visible text element, that element's text
   and its bounding box, with no translation, transliteration, or commentary.
7. THE localization prompt SHALL be a module-level constant carrying a prompt
   version string.

### Requirement 4: Robust coordinate normalization

**User Story:** As a developer, I want the system to normalize whatever
coordinate form the model returns, so that pixel and fractional coordinates
both place boxes correctly on the real image.

#### Acceptance Criteria

1. WHEN the Localization_Model returns a bounding box, THE
   Image_Coordinate_Normalizer SHALL determine whether the coordinates are
   expressed in pixels or as normalized `[0, 1]` fractions and convert them to a
   single consistent space relative to the image's actual pixel dimensions.
2. THE Image_Coordinate_Normalizer SHALL derive the image's pixel dimensions
   from the image bytes themselves, not from any model-reported dimension.
3. WHEN a normalized box's coordinates fall outside the image bounds, THE
   Image_Coordinate_Normalizer SHALL clamp the box to the image bounds before
   it is used to redact.
4. IF a returned box cannot be normalized into a positive-area rectangle within
   the image bounds (for example, degenerate, inverted, or non-numeric
   coordinates), THEN THE In_Image_Redaction capability SHALL treat that box as
   implausible and SHALL trigger the Whole_Image_Fallback for that Image_Region
   rather than drawing an untrustworthy rectangle.
5. WHERE the Localization_Model returns coordinates that are ambiguous between
   pixel and fractional interpretation, THE Image_Coordinate_Normalizer SHALL
   resolve the interpretation deterministically for a given input.

### Requirement 5: Box padding

**User Story:** As a compliance reviewer, I want a small margin around each
redaction box, so that a slightly-tight box still fully covers the glyphs it is
meant to hide.

#### Acceptance Criteria

1. THE In_Image_Redaction capability SHALL expand each localized rectangle by a
   configurable Box_Padding margin before drawing the Solid_Redaction_Block.
2. WHEN expanding a rectangle by Box_Padding would extend it beyond the image
   bounds, THE In_Image_Redaction capability SHALL clamp the expanded rectangle
   to the image bounds.
3. THE Box_Padding value SHALL have a documented default and SHALL be
   overridable by configuration.

### Requirement 6: Whole-image safety fallback (overriding correctness)

**User Story:** As a compliance officer, I want any localization failure to
result in the whole image being redacted rather than PII being left visible, so
that detected in-image PII can never leak into the output.

#### Acceptance Criteria

1. IF an Image_Region has at least one Image_Span but targeted Localization
   cannot be trusted for it — because Localization is unavailable, the
   Localization_Model returns no usable boxes, a request fails after its
   retries, or a returned box is implausible per Requirement 4.4 — THEN THE
   In_Image_Redaction capability SHALL apply the Whole_Image_Fallback to that
   Image_Region.
2. WHEN the Whole_Image_Fallback is applied to an Image_Region, THE
   In_Image_Redaction capability SHALL guarantee that none of that image's
   detected PII is visible in the output document.
3. THE In_Image_Redaction capability SHALL NOT emit an output document in which
   an Image_Region carrying at least one Image_Span retains any visible
   detected PII.
4. WHEN the Whole_Image_Fallback is applied, THE Redaction_Agent SHALL record
   that the fallback occurred for that Image_Region in an audit log record,
   without recording the image bytes or the Recognized_Text.
5. WHERE an Image_Region has no Image_Span, THE In_Image_Redaction capability
   SHALL NOT apply the Whole_Image_Fallback and SHALL leave the image unchanged.

### Requirement 7: Error handling and pipeline posture

**User Story:** As a system operator, I want in-image redaction to follow the
existing redaction error semantics, so that a localization hiccup degrades to
safe over-redaction while a genuine redaction failure still fails the Job
loudly.

#### Acceptance Criteria

1. IF a localization request returns a throttling error or a server-side error,
   THEN THE In_Image_Redaction capability SHALL retry that request with
   exponential backoff for up to 3 total attempts, reusing the shared
   retryable-error decision used by the other Bedrock callers.
2. IF localization for an Image_Region fails after the retries in Criterion 1,
   THEN THE In_Image_Redaction capability SHALL apply the Whole_Image_Fallback
   for that Image_Region rather than raising.
3. IF the source image cannot be decoded, drawn on, or re-encoded by the
   Image_Redaction_Backend, THEN THE Redaction_Agent SHALL treat the failure as
   an unrecoverable redaction failure, consistent with the existing
   `RedactionError` semantics that drive the Redaction Job to `failed` with no
   Redacted Document stored.
4. THE Redaction_Agent SHALL NOT change a Redaction Job's status to `failed`
   solely because targeted Localization was unavailable or untrustworthy, since
   the Whole_Image_Fallback provides a safe redaction outcome in that case.
5. THE In_Image_Redaction capability SHALL leave the deterministic pipeline
   order, the supplementary sources' never-raise posture, and the primary-path
   redaction failure semantics otherwise unchanged.

### Requirement 8: Determinism and PII-safe auditing

**User Story:** As a compliance auditor, I want to know which model and prompt
localized a given image without the logs containing the image or its PII, so
that a probabilistic localization step is explainable during review.

#### Acceptance Criteria

1. THE In_Image_Redaction capability SHALL set the Localization_Model's
   temperature to 0 on every localization request.
2. WHERE the Localization_Model's inference configuration rejects specifying a
   nucleus-sampling parameter together with temperature, THE In_Image_Redaction
   capability SHALL send temperature alone and omit the nucleus-sampling
   parameter.
3. WHEN a localization request completes for an Image_Region, THE
   In_Image_Redaction capability SHALL emit an audit log record containing the
   Localization_Model identifier, the localization prompt version, a digest and
   byte length of the image sent, and the number of rectangles that will be
   redacted for that image.
4. THE audit log record SHALL contain neither the image bytes, the
   Recognized_Text, nor the coordinates or content of any box in a form that
   would reveal the detected PII.
5. THE In_Image_Redaction capability SHALL emit every warning and audit log
   record through a module-level `logging` logger, so that the records reach
   CloudWatch through the Agent Runtime's log configuration.

### Requirement 9: Unchanged public interfaces and downstream detection

**User Story:** As a developer, I want the OCR tool's contract and the
downstream detection code to stay as they are, so that this feature adds
geometry and redaction behavior without disturbing detection or merging.

#### Acceptance Criteria

1. THE Redaction_Agent SHALL leave the regex, Guardrails, and LLM detection
   over `recognizedText`, and the `_regroup_and_merge` step, unchanged.
2. THE `recognize_image_text` tool SHALL continue to accept a
   `list[ImageRegion]` and return the same `ImageRegion` instances in the same
   order, with any new geometry attached to those same instances.
3. THE In_Image_Redaction capability SHALL import no Strands Agents SDK symbol
   and SHALL remain importable for unit testing without the `boto3`, `strands`,
   or `bedrock_agentcore` packages installed.
4. THE In_Image_Redaction capability SHALL construct its Bedrock client lazily,
   sharing one client across invocations within an Agent Runtime process, as
   the other Bedrock-calling tools do.

### Requirement 10: Full-page OCR path unchanged

**User Story:** As a developer, I want the Textract page-OCR path left exactly
as it is, so that word-level redaction geometry for full-page PDF text is
preserved.

#### Acceptance Criteria

1. THIS feature SHALL introduce no change to `backend/agent/tools/ocr_pdf.py`.
2. THE Redaction_Agent SHALL continue to redact `text:` and `ocr:` spans
   exactly as it does today.
3. THE Redaction_Agent SHALL continue to treat a full-page OCR path failure as
   a primary-path failure that drives the Redaction Job to `failed`.

### Requirement 11: Infrastructure provisioning and operational control

**User Story:** As a system administrator, I want the localization model
identifier and its invocation permission provisioned through CDK with a
documented off switch, so that the feature can be enabled, pinned, and disabled
without a code change.

#### Acceptance Criteria

1. THE CDK_Stack SHALL grant the AgentCore Runtime execution role permission to
   invoke the Localization_Model, scoped to the provisioned Localization_Model's
   resource (and, where it is an inference profile, the underlying foundation
   model), and SHALL grant no broader Bedrock model permission.
2. THE CDK_Stack SHALL set the existing `IMAGE_OCR_MODEL_ID` variable on the
   AgentCore Runtime to a pinned, versioned **Sonnet-class** identifier
   (resolved decision A1), alongside the existing `GUARDRAIL_ID`,
   `GUARDRAIL_VERSION`, `PDF_OCR_DPI`, and `LLM_PII_MODEL_ID` variables. No new
   localization-model variable is added.
3. THE CDK_Stack SHALL scope the `bedrock:InvokeModel` grant to the Sonnet-class
   `IMAGE_OCR_MODEL_ID` resource (and its underlying foundation model when it is
   an inference profile), replacing the prior Haiku image-OCR grant.
4. WHEN an operator clears `IMAGE_OCR_MODEL_ID`, embedded-image OCR goes dormant
   (no Image_Spans are produced), so Redaction Jobs still complete and there is
   no in-image PII to leave visible.

## Resolved Design Decisions

All four decisions below are **RESOLVED** (A1 + B1 + C2 + D-in-scope). The
rationale is retained for the record.

### Decision A: Localization model strategy — Sonnet-only vs two-pass

The shipped `IMAGE_OCR_MODEL_ID` defaults to Haiku, which reads text well but
localizes poorly.

- **Option A1 — Switch image OCR entirely to Sonnet.** One model does both
  reading and localization. Simplest wiring, one Converse call per image.
  Higher per-image cost, since every embedded image (PII or not) is read by
  Sonnet.
- **Option A2 — Two-pass (recommended).** Keep Haiku (via `IMAGE_OCR_MODEL_ID`)
  for cheap text reading on every image, and invoke the Sonnet
  Localization_Model **only** when PII was actually detected in that image
  (i.e. the image has at least one Image_Span). Cheaper — Sonnet is called only
  for the rare image that both contains PII and reached redaction — at the cost
  of a second model call and a second prompt on those images.

Recommendation: **A2 (two-pass)**. Embedded images are rare, images with PII
rarer still, and localization only matters for the latter; paying Sonnet prices
only when localization is actually needed keeps the common case cheap while
still giving compliance-grade geometry where it counts. A2 also composes
cleanly with the two-model configuration in Requirement 3 (distinct env vars).

### Decision B: Extension scope — `.docx` and `.pdf`, or `.docx` first

`.docx` embedded-image replacement is straightforward (swap the image part's
bytes). `.pdf` embedded-image replacement is more involved (replace/redraw the
image XObject at its placement, or overlay opaque rectangles at the image's
page rectangle scaled by the localized boxes).

- **Option B1 — Both `.docx` and `.pdf` now.** Complete coverage; the demoed
  leak was a `.pdf` driving-license image, so `.pdf` is the motivating case.
- **Option B2 — `.docx` first, `.pdf` later.** Faster to a working slice; defers
  the harder PDF image-replacement work.

Recommendation: needs user input. If the motivating leak (driving-license image)
is `.pdf`, B2 would leave the demonstrated leak open, arguing for **B1** — but
B1 is more design work. Flagging for the user to choose.

### Decision C: Image-manipulation dependency — Pillow vs the existing PDF stack (pikepdf / pypdfium2)

Drawing black rectangles on a decoded image and re-encoding it needs an
imaging library.

- **Option C1 — Use the existing PDF stack (pikepdf / pypdfium2), already backend dependencies.** No new
  dependency. pypdfium2 can render a page bitmap, and for `.pdf` `pdf_images.write_back_image` can replace
  image streams; drawing arbitrary filled rectangles on a raster for
  re-embedding is more awkward than Pillow but feasible.
- **Option C2 — Add Pillow as a real backend dependency.** Pillow
  (`ImageDraw.rectangle`) is the natural fit for "decode → draw filled boxes →
  re-encode". Pillow was only pip-installed ad hoc for the experiment; making it
  a real dependency needs to be a deliberate choice (packaging into the agent
  bundle, size, maintenance).

Recommendation: needs user input, and it interacts with Decision B. For a
raster-drawing task Pillow is the ergonomic choice (**C2**); if avoiding a new
dependency is a priority, **C1** is workable, especially if scope is `.pdf`-only
where pikepdf already owns the document.

### Decision D: Extraction prerequisites

Per "Assumptions and Dependencies", the current extractors do not yet supply
image bytes to the redactor (`.pdf` sets `sourceLocation`/`boundingBox` but not
`imageBytes`; `.docx` emits no image regions at all). This feature depends on
that being fixed for the chosen In_Scope_Extension(s). The decision to confirm:
the extent of the extraction changes (populate `imageBytes` on `.pdf`
Image_Regions; begin emitting `.docx` Image_Regions with a stable re-embedding
handle) and whether they belong in this spec or a prerequisite one.

## Notes

- Cost and latency: Localization adds a Sonnet-class Converse call. Per the
  user this is acceptable for the PoC; it is noted here for visibility, and
  Decision A2 (two-pass) is the lever that bounds it to images that actually
  contain PII.
- The correctness spine of this feature is Requirement 6 (Whole_Image_Fallback):
  every failure mode of Localization funnels into a guaranteed-safe outcome, so
  that detected in-image PII is never left visible.
