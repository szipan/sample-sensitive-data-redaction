# Requirements Document

## Introduction

PII Document Redaction POC 验证一个前后端分离、部署在 AWS 的无服务器文档脱敏主流程：匿名演示用户上传 DOCX 或文本型 PDF，系统由 AgentCore 脱敏 Agent 使用预置或自定义规则识别 PII、生成同格式且已遮盖的副本，并提供下载。

## Glossary

- **PII**：可用于识别自然人的个人身份信息。
- **PII Document Redaction POC**：本 POC 的前端、后端和文档脱敏能力的整体。
- **Anonymous Demonstration User**：无需登录即可操作本 POC 的单一演示用户。
- **Supported Document**：扩展名为 `.docx` 的 Word 文档或包含可提取文本的 `.pdf` 文档。
- **Redaction Rule**：由唯一名称、正则表达式和启用状态组成的全局 PII 匹配规则。
- **Rule Operation**：Anonymous Demonstration User 对 Redaction Rule 发起的创建、启用/禁用或删除请求。
- **Terminal Result**：Rule Operation 已成功应用或已返回可显示的验证错误的状态。
- **Built-in Rule**：系统预置且默认启用、且不可被删除的中国居民身份证号码或中国大陆手机号码 Redaction Rule。
- **Redaction Job**：从接收 Supported Document 至产生终态（`completed` 或 `failed`）的异步处理记录。
- **AgentCore Redaction Agent**：运行在 Amazon Bedrock AgentCore 中、协调文档提取、规则匹配、遮盖和结果存储工具的后端 Agent。
- **Image Text Recognition**：从嵌入图片、扫描页或图形中提取可供 Redaction Rule 匹配的文字的处理（OCR）。
- **Solid Redaction Block**：覆盖完整匹配文本且不包含原始 PII 字符的实心不透明图形。
- **Redacted Document**：由 Redaction Job 生成、与源文档具有相同文件扩展名的文档副本。

## Requirements

### Requirement 1: 上传受支持文档

**User Story:** 作为 Anonymous Demonstration User，我想上传受支持的文档，以便验证文档脱敏流程。

#### Acceptance Criteria

1. WHEN an Anonymous Demonstration User selects a Supported Document for upload, THE PII Document Redaction POC SHALL accept the Supported Document, create a Redaction Job with the `processing` status, and display a reference to the created Redaction Job to the Anonymous Demonstration User.
2. IF an Anonymous Demonstration User selects a file that is not a Supported Document, THEN THE PII Document Redaction POC SHALL reject the file, display the list of supported file extensions, and take no action to create a Redaction Job.
3. WHEN the PII Document Redaction POC accepts a Supported Document, THE PII Document Redaction POC SHALL store the Supported Document for use by the AgentCore Redaction Agent.
4. IF the PII Document Redaction POC cannot store an accepted Supported Document, THEN THE PII Document Redaction POC SHALL change the associated Redaction Job status to `failed` and display a failure indication to the Anonymous Demonstration User.

### Requirement 2: 管理 PII 遮盖规则

**User Story:** 作为 Anonymous Demonstration User，我想管理全局 PII 规则，以便验证新增 PII 类型的遮盖效果。

#### Acceptance Criteria

1. THE PII Document Redaction POC SHALL provide a Built-in Rule whose regular expression matches an 18-character Chinese resident identity card number consisting of 17 numeric digits followed by one checksum character that is either a digit (0-9) or an uppercase "X", and a Built-in Rule whose regular expression matches an 11-digit mainland China mobile phone number.
2. WHEN the PII Document Redaction POC initializes the Redaction Rule collection with no existing Redaction Rule records, THE PII Document Redaction POC SHALL set every Built-in Rule to the enabled state.
3. IF a Built-in Rule already has a stored enabled or disabled state from a prior system start, THEN THE PII Document Redaction POC SHALL retain that stored state without resetting it to enabled.
4. WHEN the PII Document Redaction POC receives a Rule Operation, THE PII Document Redaction POC SHALL start processing that Rule Operation only after the immediately preceding Rule Operation reaches a Terminal Result.
5. THE PII Document Redaction POC SHALL provide an interface for an Anonymous Demonstration User to submit a Redaction Rule consisting of a name between 1 and 100 characters and a regular expression between 1 and 500 characters.
6. WHEN an Anonymous Demonstration User submits a Redaction Rule with a name that does not exactly match, case-sensitively, any existing Redaction Rule name, and a regular expression that compiles without error under the pattern syntax supported by the AgentCore Redaction Agent, THE PII Document Redaction POC SHALL store the Redaction Rule in the enabled state.
7. IF an Anonymous Demonstration User submits a Redaction Rule whose name exactly matches, case-sensitively, an existing Redaction Rule name, THEN THE PII Document Redaction POC SHALL reject the submission and identify the duplicate name.
8. IF an Anonymous Demonstration User submits a Redaction Rule whose regular expression does not compile under the pattern syntax supported by the AgentCore Redaction Agent, THEN THE PII Document Redaction POC SHALL reject the submission and identify the invalid regular expression.
9. WHEN an Anonymous Demonstration User requests to enable or disable an existing Redaction Rule, THE PII Document Redaction POC SHALL apply the requested enabled or disabled state to that Redaction Rule.
10. WHEN an Anonymous Demonstration User requests to delete an existing custom Redaction Rule, THE PII Document Redaction POC SHALL remove that Redaction Rule from the Redaction Rule collection.
11. IF an Anonymous Demonstration User requests to enable, disable, or delete a Redaction Rule name that does not exactly match, case-sensitively, any existing Redaction Rule name, THEN THE PII Document Redaction POC SHALL reject the request and identify the unrecognized name.
12. IF an Anonymous Demonstration User requests to delete a Built-in Rule, THEN THE PII Document Redaction POC SHALL reject the request and indicate that Built-in Rules cannot be deleted.
13. THE PII Document Redaction POC SHALL display the current Redaction Rule collection, including each Redaction Rule's name, pattern, and enabled state, to the Anonymous Demonstration User.

### Requirement 3: 执行 Agent 驱动的脱敏

**User Story:** 作为 Anonymous Demonstration User，我想让 Agent 处理上传文档，以便验证 AgentCore 协调脱敏流程的可行性。

#### Acceptance Criteria

1. WHEN a Redaction Job is created, THE AgentCore Redaction Agent SHALL use every enabled Redaction Rule to identify matching text in the Supported Document.
2. WHEN the AgentCore Redaction Agent identifies matching text, THE AgentCore Redaction Agent SHALL create a Redacted Document that replaces every matched text span with a Solid Redaction Block.
3. IF two or more enabled Redaction Rules match overlapping text spans, THEN THE AgentCore Redaction Agent SHALL replace the combined extent of the overlapping text spans with a single Solid Redaction Block.
4. IF the AgentCore Redaction Agent identifies no matching text, THEN THE AgentCore Redaction Agent SHALL create a Redacted Document that retains the visible content of the Supported Document.
5. WHEN the AgentCore Redaction Agent completes a Redaction Job, THE PII Document Redaction POC SHALL change the Redaction Job status to `completed`.
6. IF the AgentCore Redaction Agent cannot parse the text of an accepted Supported Document, encounters an internal processing error, or is otherwise unable to create a Redacted Document, THEN THE PII Document Redaction POC SHALL change the Redaction Job status to `failed` and display a failure state.

### Requirement 4: 保持输出格式与识别内容范围

**User Story:** 作为 Anonymous Demonstration User，我想获得保持源格式的已遮盖文档，以便评估 POC 是否能用于现有文档。

#### Acceptance Criteria

1. WHEN the AgentCore Redaction Agent creates a Redacted Document, THE AgentCore Redaction Agent SHALL retain the file extension of the Supported Document.
2. WHEN the AgentCore Redaction Agent creates a Redacted Document, THE AgentCore Redaction Agent SHALL produce the Redacted Document with the same page count and the same page order as the source Supported Document.
3. WHEN the AgentCore Redaction Agent creates a Redacted Document, THE AgentCore Redaction Agent SHALL render each portion of nonmatching visible text, including headers, footers, and images that contain no matched text, identically to the source Supported Document and in the same reading position.
4. WHEN a Supported Document contains one or more tables, THE AgentCore Redaction Agent SHALL preserve each table's structure, defined as the same number of rows, the same number of columns, and the same mapping of cell content to cell position as in the source Supported Document.
5. IF the AgentCore Redaction Agent cannot retain the source Supported Document's page count, page order, nonmatching visible text, headers, footers, or table structure as defined in Criteria 2-4, THEN THE PII Document Redaction POC SHALL change the Redaction Job status to `failed` and display a failure state.
6. WHEN a Supported Document contains a regular-expression match in body text, table text, header text, or footer text, THE AgentCore Redaction Agent SHALL evaluate the match for a Solid Redaction Block.
7. WHEN a Supported Document contains text only in an embedded image, scanned page, or graphic, THE AgentCore Redaction Agent SHALL use Image Text Recognition to evaluate the text against every enabled Redaction Rule.

### Requirement 5: 展示处理状态并下载结果

**User Story:** 作为 Anonymous Demonstration User，我想查看处理结果并下载文件，以便完成 POC 演示。

#### Acceptance Criteria

1. WHILE a Redaction Job has the `processing` status, THE PII Document Redaction POC SHALL display the `processing` status to the Anonymous Demonstration User.
2. WHEN a Redaction Job reaches the `completed` status, THE PII Document Redaction POC SHALL display a download action for the associated Redacted Document.
3. WHILE a Redaction Job has a status other than `completed`, THE PII Document Redaction POC SHALL hide the download action for the associated Redacted Document.
4. WHEN an Anonymous Demonstration User selects a visible download action, THE PII Document Redaction POC SHALL verify that the associated Redaction Job has the `completed` status at the time of the verification.
5. IF the associated Redaction Job does not have the `completed` status at the time of the verification in Criterion 4, THEN THE PII Document Redaction POC SHALL reject the download request and display an indication that the Redacted Document is unavailable.
6. WHEN the verification in Criterion 4 confirms the `completed` status, THE PII Document Redaction POC SHALL deliver the associated Redacted Document to the Anonymous Demonstration User.
7. WHEN a Redaction Job reaches the `failed` status, THE PII Document Redaction POC SHALL display a failure indication to the Anonymous Demonstration User.

### Requirement 6: 以 AWS 无服务器服务部署前后端分离 POC

**User Story:** 作为 POC Sponsor，我想在 AWS 上运行前后端分离的 POC，以便验证无服务器部署路线。

#### Acceptance Criteria

1. THE PII Document Redaction POC SHALL expose the browser user interface and the backend processing capability as independently deployable components, such that either component can be deployed, updated, or redeployed without requiring a redeployment of the other component.
2. THE PII Document Redaction POC SHALL run the AgentCore Redaction Agent in Amazon Bedrock AgentCore.
3. THE PII Document Redaction POC SHALL expose the backend processing capability through an API that the frontend component calls exclusively over HTTPS.
4. IF the API that exposes the backend processing capability receives a request over a connection that is not HTTPS, THEN THE PII Document Redaction POC SHALL reject the request without processing it.
5. IF the frontend component does not receive a response from the API that exposes the backend processing capability within 30 seconds of an API call, THEN THE PII Document Redaction POC SHALL display a failure indication to the Anonymous Demonstration User and SHALL preserve the previously displayed Redaction Job status unchanged.
6. THE PII Document Redaction POC SHALL use AWS managed serverless services for document storage, job coordination, and backend execution that automatically scale compute or storage capacity to demand and do not require the POC team to provision, patch, or manage the underlying servers.

### Requirement 7: 验证最低可行演示

**User Story:** 作为 POC Sponsor，我想执行一套完整演示，以便判定主流程是否可行。

#### Acceptance Criteria

1. WHEN the POC demonstration submits a DOCX sample document or a text-based PDF sample document containing Built-in Rule matches, THE PII Document Redaction POC SHALL accept the submitted sample document.
2. WHEN an Anonymous Demonstration User creates an enabled custom Redaction Rule and submits a Supported Document containing a match for the custom Redaction Rule, THE PII Document Redaction POC SHALL produce a Redacted Document containing a Solid Redaction Block at the custom-rule match.
3. WHEN the PII Document Redaction POC completes a demonstration Redaction Job, THE PII Document Redaction POC SHALL make the corresponding Redacted Document of the source format available for download.
4. IF the AgentCore Redaction Agent cannot create a Redacted Document with the Supported Document file extension, THEN THE PII Document Redaction POC SHALL change the Redaction Job status to `failed` and display a failure state.

## Out of Scope

- Login, identity management, authorization, and multi-user data isolation.
- Document types other than DOCX and text-based PDF.
- Output format conversion.
- Third-party PII classification services, including Amazon Macie integration.
- Production performance, security hardening, retention controls, and high-availability targets.
- A conversational chat interface for the AgentCore Redaction Agent.
