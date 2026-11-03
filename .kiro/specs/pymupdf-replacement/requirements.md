# Requirements Document

## Introduction

本功能从 PII 文档脱敏 PoC 的后端中完全移除 PyMuPDF（AGPL-3.0 / Artifex 商业许可），改用采用宽松许可证（MIT、BSD、Apache-2.0 等）的依赖，或在必要时依据公司弱 copyleft 分发政策以未修改形式使用的 MPL-2.0 依赖，实现等价的 PDF 文本提取、整页光栅化（供 OCR 路径使用）、内嵌图片提取以及 PDF 脱敏能力。目的是让依赖许可证审查（PCSR / 自认证）不再标记 AGPL 依赖。方向已定为"替换"，不考虑购买商业许可或整体改用 AGPL。

当前受影响的源码模块只有三个：`backend/agent/tools/extraction_pdf.py`、`backend/agent/tools/ocr_pdf.py`、`backend/agent/tools/redaction_pdf.py`。DOCX 路径与前端不使用 PyMuPDF，行为必须保持不变。

范围之外（本需求不覆盖，需另行征得用户同意）：git commit / push、AWS 部署（旧栈与新栈 CDK 栈名同为 `PiiDocumentRedactionPocStack`，存在覆盖冲突风险）、购买商业许可。规格文档中不得写入 AWS 账号 ID、密钥或真实客户文档内容。

本文档只描述"要达到什么"，不指定具体替代库。候选方案（例如 pypdfium2 + pdfminer.six + pikepdf/pypdf 的内容流脱敏方案、整页光栅化脱敏方案及其他方案）的比较与推荐放在设计阶段完成，评估维度由需求 11 约束。

## Glossary

- **PDF_Extractor**：`extraction_pdf.py` 提供的 `.pdf` 内容提取组件，对外函数为 `extract_pdf_content`，产出文本层单元（`text:{i}`）、图片区域（`ImageRegion`）及页数 / 页序元数据，失败时抛出 `ExtractionError`。以空用户密码即可打开的加密 PDF（仅设置了所有者密码）照常提取；只有需要非空用户密码才能打开的 PDF 才抛出错误（见需求 4 第 10 条）。
- **OCR_Path**：`ocr_pdf.py` 提供的 OCR 检测路径，对外函数为 `ocr_pdf_pages`，将每页光栅化后调用 Amazon Textract `DetectDocumentText`，每页产出一个 `zone="ocr"` 的 OCR 页单元（`ocr:{i}`），失败时抛出 `OcrPathError`。
- **PDF_Redactor**：`redaction_pdf.py` 提供的 `.pdf` 脱敏组件，对外函数为 `redact_pdf_document` 与 `redact_pdf_document_with_report`，返回脱敏后字节及 `PdfRedactionReport`，失败时抛出 `RedactionError`。
- **Structure_Verifier**：`structure_verification.py` 中的 `verify_structure`，校验源文档与脱敏文档的页数和页序一致。
- **Agent_Bundle_Builder**：`backend/scripts/build_agent_bundle.py` 与 `backend/scripts/sync_agent_bundle.py`，为 AgentCore Runtime（Linux ARM64 / CPython 3.12）下载并安装依赖 wheel 至 `agent_bundle/package_clean`。
- **Dependency_Manifest**：`backend/requirements.txt` 与 `backend/agent-runtime-requirements.txt`（以及 `requirements-dev.txt` 中与 PDF 相关的条目）。
- **Third_Party_License_File**：新增的 `THIRD-PARTY-LICENSES`（或等价文件），列出所有直接依赖、精确版本与许可证；对于许可证属于 Weak_Copyleft_License 的依赖，还收录其许可证全文、版权声明与原始源码下载链接（见需求 2 第 10 条）。
- **Permissive_License**：MIT、BSD（2/3 条款）、Apache-2.0、ISC、PSF、HPND 等不具有 copyleft 传染性的许可证；AGPL、GPL、LGPL 及仅商业许可均不属于此类。MPL-2.0 不属于此类，而归为 Weak_Copyleft_License，按需求 2 中针对 Weak_Copyleft_License 的验收标准（第 9 至 12 条）管理。
- **Weak_Copyleft_License**：MPL-2.0（本项目仅允许此项弱 copyleft，LGPL 按需求 2 第 8 条一律不可选用；CDDL、EPL 等其他弱 copyleft 按需求 2 第 4 条需写明理由并经用户确认）。
- **Removed_Library_Identifier**：字符串 `fitz`、`pymupdf`、`PyMuPDF`（不区分大小写匹配 `fitz` 与 `pymupdf`）。
- **Text_Unit**：`ExtractedTextUnit`，PDF 文本层单元的 `zone="body"`，`position.pageIndex` 为 0 起始页索引，`boundingBox` 为页面矩形。
- **OCR_Unit**：`zone="ocr"` 的 `ExtractedTextUnit`，携带 `ocrWords`（每个词的偏移区间和 Textract 归一化 `[0, 1]` 包围框）。
- **Page_Coordinates**：PDF 页面坐标，单位为 pt（1/72 英寸），原点在页面左上角，y 轴向下，且考虑 `/Rotate` 后的可见页面矩形（即现有实现中的 `page.rect` 语义）。注：现有实现中 Text_Unit 的 `boundingBox` 使用的是未旋转的 MediaBox；新契约统一采用 Page_Coordinates。对于未旋转且 MediaBox 原点为 (0, 0) 的页面，两者完全相同。
- **Character_Offset_Mapping**：文本层单元 `text` 中偏移 `i` 与页面上第 `i` 个字符矩形之间一一对应的关系，提取侧与脱敏侧必须基于同一字符遍历得到。
- **MatchSpan**：检测结果，`unitRef` 取值命名空间为 `text:`、`ocr:`、`image:`，`startOffset` / `endOffset` 为半开区间。
- **Redaction_Region**：PDF_Redactor 在页面上（Page_Coordinates）要涂黑的矩形。
- **Image_Handle**：`ImageRegion.sourceLocation` 中 PDF 图片的定位句柄，格式为 `page:{pageIndex}:xref:{xref}`，脱敏时据此把修改后的图片写回原图片对象。
- **Independent_Text_Extractor**：与 PDF_Redactor 所用库不同的、独立的 PDF 文本提取工具，用于泄漏测试。
- **Raster_Resolution**：OCR_Path 光栅化 DPI，由环境变量 `PDF_OCR_DPI` 配置，默认 200，合法范围 `[72, 400]`，超过 Textract 同步 10 MB 限制时以 150 DPI 重渲染一次。
- **Backend_Test_Suite**：`backend/tests` 下的 pytest 测试集，基线为 397 passed、5 skipped。
- **Frontend_Checks**：`frontend` 目录下的 `npx tsc -b` 与 `npx vitest --run`，基线为 tsc 通过、68 passed。
- **Legacy_Tracking_Comment**：三个 PDF 模块中引用内部任务编号或需求编号的注释（例如 "task 16.2"、"Requirement 3.3"、"Requirements 1.1, 1.9"），约 55 处。

## Requirements

### Requirement 1: 完全移除 PyMuPDF

**User Story:** 作为合规负责人，我希望仓库中不再包含或引用 PyMuPDF，以便依赖许可证审查不再标记 AGPL 依赖。

#### Acceptance Criteria

1. THE Dependency_Manifest SHALL 列出零个包名以 `pymupdf` 开头（不区分大小写，含 `PyMuPDF`、`PyMuPDFb`、`pymupdf-fonts`）或名为 `fitz` 的包条目。
2. WHEN 在全新的 CPython 3.12 虚拟环境中按 Dependency_Manifest 安装全部依赖后，THE 虚拟环境 SHALL 包含零个包名以 `pymupdf` 开头（不区分大小写）或名为 `fitz` 的已安装发行包（含传递依赖），且 `import fitz` 与 `import pymupdf` 均抛出 `ModuleNotFoundError`。
3. WHEN Agent_Bundle_Builder 完成一次构建时，THE Agent_Bundle_Builder SHALL 以零退出码生成 `agent_bundle/package_clean`，且构建过程中下载的 wheel 集合不包含任何文件名以 `pymupdf` 开头（不区分大小写）的 wheel。
4. WHEN Agent_Bundle_Builder 完成一次构建时，THE `agent_bundle/package_clean` SHALL 包含零个名称为 `fitz`、`pymupdf` 或以 `pymupdf` 开头（不区分大小写）的顶层目录、模块文件或 `*.dist-info` 目录，包括此前构建遗留的同名条目。
5. THE 仓库 SHALL 在以下范围内对 Removed_Library_Identifier 做不区分大小写搜索时返回零个匹配：`backend/` 下的源码、测试、requirements 文件与脚本，根目录及各子目录的 README，代码注释，以及 `.kiro/specs/*` 文档；搜索范围不包括 `.git/`、虚拟环境目录、`agent_bundle/` 构建产物与 `node_modules/`。
6. THE 第 5 条的零匹配要求 SHALL 仅对 `.kiro/specs/pymupdf-replacement/` 目录下的 `requirements.md`、`design.md` 与 `tasks.md` 豁免；其他任何文件中出现的 Removed_Library_Identifier 均判定为不满足本需求。
7. WHILE 当前虚拟环境未安装任何包名以 `pymupdf` 开头的发行包，WHEN 运行 Backend_Test_Suite 时，THE Backend_Test_Suite SHALL 在收集阶段产生零个导入错误，且零个测试因 PyMuPDF 或 `fitz` 缺失而被跳过。
8. IF `.kiro/specs/pymupdf-replacement/` 之外的 `.kiro/specs/*` 文档（设计、需求或任务文档）描述了 PyMuPDF 的 API、行为或依赖关系，THEN THE 该文档 SHALL 改为描述替代后的库与实现，且修改后的描述与替代后源码中实际调用的库和函数一致。

### Requirement 2: 依赖许可证与版本约束

**User Story:** 作为合规负责人，我希望每个新增依赖都是宽松许可证，或在满足合规条件时使用未修改的 MPL-2.0，并精确固定版本，以便审查结果可复现。

#### Acceptance Criteria

1. THE Dependency_Manifest SHALL 仅包含许可证属于 Permissive_License 或 Weak_Copyleft_License 的新增直接依赖，其中许可证属于 Weak_Copyleft_License 的依赖还须满足本需求第 9 至 12 条；对于多许可证（例如 "Apache-2.0 OR BSD-3-Clause"）的依赖，至少一个可选许可证属于 Permissive_License 或 Weak_Copyleft_License 即视为满足，且所采用的许可证 SHALL 记录在设计文档中。
2. THE Dependency_Manifest SHALL 以 `==` 加完整版本号（不含通配符 `*`、不含范围运算符 `>=`、`<`、`~=`）精确固定每个新增直接依赖的版本，且同一依赖在 `requirements.txt`、`agent-runtime-requirements.txt` 与 `requirements-dev.txt` 中出现时 SHALL 固定为同一版本。
3. WHEN 选定新增依赖时，THE 设计文档 SHALL 为该依赖及其捆绑的每个二进制组件（例如 PDFium 等原生库）分别记录：许可证标识、核实来源（PyPI 项目页或项目仓库 LICENSE 文件的链接）与核实日期（格式 YYYY-MM-DD）。
4. IF 某候选依赖（含其捆绑的二进制组件，例如 PDFium 等原生库）的许可证无法通过 PyPI 或项目仓库核实为 Permissive_License 或 Weak_Copyleft_License，或其许可证既不在 Permissive_License 明确列举的范围内、也不是 MPL-2.0（例如 MPL-2.0 以外的其他弱 copyleft 许可证），THEN THE 设计文档 SHALL 将该候选依赖标记为不可选用，除非设计文档写明该许可证的归类理由且用户已明确确认可选用。
5. THE Third_Party_License_File SHALL 为 Dependency_Manifest 中的每个直接依赖列出一条记录，包含名称、与 Dependency_Manifest 相同的精确版本和 SPDX 许可证标识；对于捆绑二进制组件的依赖，该记录 SHALL 同时列出每个捆绑组件的名称与许可证标识。
6. THE Third_Party_License_File SHALL 与 Dependency_Manifest 中的直接依赖保持一致：两者的依赖名称集合完全相同（无缺失、无多余），且每个依赖的版本号完全相同；每次增加、删除直接依赖或变更其版本时，二者 SHALL 在同一次变更中一起更新。
7. THE Agent_Bundle_Builder 生成的 `agent_bundle/package_clean` 中，因新增直接依赖而引入的每个传递依赖 SHALL 具有属于 Permissive_License 或 Weak_Copyleft_License 的许可证（后者须满足本需求第 9 至 12 条），且设计文档 SHALL 列出每个传递依赖的名称、版本与许可证标识。
8. IF 任一新增直接依赖或其传递依赖（含其捆绑的原生库）的许可证为 AGPL、GPL、LGPL 或仅商业许可，THEN THE 设计文档 SHALL 将对应的新增直接依赖标记为不可选用，且 THE Dependency_Manifest SHALL 不包含该直接依赖；Weak_Copyleft_License 的放行不适用于上述许可证。
9. WHERE 某直接依赖或传递依赖的许可证属于 Weak_Copyleft_License，THE Dependency_Manifest SHALL 仅从 PyPI 上未经修改的官方 wheel 安装该依赖，不使用补丁、fork、源码修改或 vendored 副本。
10. WHERE 某直接依赖或传递依赖的许可证属于 Weak_Copyleft_License，THE Third_Party_License_File SHALL 为该依赖收录版权持有人、MPL-2.0 许可证全文与版权声明，以及指向与 Dependency_Manifest 中精确固定版本相同的原始源码下载位置（PyPI sdist 或项目仓库对应版本 tag）的链接，以满足源码可获取义务。
11. WHERE 某直接依赖或传递依赖的许可证属于 Weak_Copyleft_License，THE README SHALL 写明该依赖采用 MPL-2.0、以未经修改的形式使用，并说明许可证扫描工具若标记该依赖，可依据公司弱 copyleft 分发政策自行关闭该工单。
12. IF 某许可证属于 Weak_Copyleft_License 的候选依赖需要修改（打补丁、fork、修改源码或以 vendored 副本形式引入）才能满足本规格，THEN THE 设计文档 SHALL 将该候选依赖标记为不可选用，除非该修改已有获批的合规工单。
13. WHERE 某依赖的 wheel 内捆绑了原生库（例如 pikepdf wheel 中的 qpdf 及其依赖库、pypdfium2 wheel 中的 PDFium），THE 设计文档 SHALL 逐一列出每个捆绑原生库的名称与许可证标识。
14. WHERE 某依赖的 wheel 内捆绑了原生库（例如 pikepdf wheel 中的 qpdf 及其依赖库、pypdfium2 wheel 中的 PDFium），THE Third_Party_License_File SHALL 逐一列出每个捆绑原生库的名称与许可证标识。

### Requirement 3: AgentCore Runtime 打包兼容性

**User Story:** 作为运维人员，我希望新依赖能被打包进 Linux ARM64 / CPython 3.12 的 Agent bundle，以便 AgentCore Runtime 可以正常导入和运行。

#### Acceptance Criteria

1. THE Dependency_Manifest SHALL 仅包含满足以下条件的新增直接依赖：该依赖及其全部传递依赖在 PyPI 上均提供至少一个平台标签为 `manylinux2014_aarch64`、`manylinux_2_17_aarch64`、`manylinux_2_28_aarch64` 之一或为 `any` 的 wheel，且该 wheel 的 Python/ABI 标签为 `cp312`、`abi3`（最低版本不高于 cp312）或 `py3-none`。
2. WHEN Agent_Bundle_Builder 在 Linux 主机上以 `--only-binary=:all:`、`--python-version 312`、`--implementation cp` 与 aarch64 manylinux 平台参数执行 `pip download -r agent-runtime-requirements.txt` 时，THE Agent_Bundle_Builder SHALL 以退出码 0 结束，并在 `agent_bundle/deps/` 中为每个新增直接依赖及其传递依赖各留下恰好一个 `.whl` 文件，且不含任何源码包（`.tar.gz` / `.zip` sdist）。
3. IF 任一新增依赖或其传递依赖在上述平台参数下没有可用 wheel，THEN THE Agent_Bundle_Builder SHALL 在下载阶段以非零退出码结束，输出指明下载失败的错误信息，且不进入 `package/` 与 `package_clean/` 的生成阶段。
4. THE Agent_Bundle_Builder SHALL 只保留这样的 `--platform` 取值：解析结果中至少有一个 wheel 仅能通过该取值匹配；对任一保留的取值，删除它后第 2 条所述的下载 SHALL 失败。
5. THE Agent_Bundle_Builder SHALL 在脚本注释中为每个保留的 `--platform` 取值写明依赖它的 wheel 文件名（或包名与版本），并说明是否显式传递 `--abi` 及其原因。
6. WHEN Agent_Bundle_Builder 完成构建时，THE `agent_bundle/package_clean/` SHALL 包含每个新增依赖的导入包目录，且其中所有原生扩展均为 Linux aarch64 `.so` 文件，`.pyd` 与 `.dll` 文件数为零。
7. WHEN 在 Linux aarch64 / CPython 3.12 环境中以 `agent_bundle/package_clean/` 作为唯一第三方包路径导入 `agent.tools.extraction_pdf`、`agent.tools.ocr_pdf` 与 `agent.tools.redaction_pdf` 时，THE 导入 SHALL 以零个 `ImportError` 完成。
8. THE README SHALL 列出每个替代后 PDF 依赖的包名、与 Dependency_Manifest 一致的精确版本及许可证标识，并写明 bundle 构建命令（`build_agent_bundle.py`）、目标平台（Linux ARM64 / CPython 3.12）以及须在 Linux 主机上运行构建这一前提。

### Requirement 4: 文本层提取契约

**User Story:** 作为检测流水线的维护者，我希望替换后的 PDF_Extractor 产出与现有契约一致的文本层单元，以便匹配、Guardrails 检测和脱敏无需改动。

#### Acceptance Criteria

1. WHEN 提供一个可读、未加密的 PDF 时，THE PDF_Extractor SHALL 为每个"拼接文本去除首尾空白字符后长度大于 0"的页面恰好产出一个 `zone="body"`、`position.index` 为 0 的 Text_Unit，且 `textUnits` 按 `position.pageIndex` 严格升序排列。
2. WHEN 某页面的拼接文本为空字符串或仅由空白字符（Python `str.strip()` 判定的字符）组成时，THE PDF_Extractor SHALL 为该页面产出零个 Text_Unit，且后续页面的 Text_Unit 的 `position.pageIndex` 保持为其实际页面索引，不因跳过而前移。
3. THE PDF_Extractor SHALL 将每个 Text_Unit 的 `position.pageIndex` 设置为其所在页面的 0 起始索引，取值范围为 `[0, pageCount-1]`。
4. THE PDF_Extractor SHALL 将每个 Text_Unit 的 `boundingBox` 设置为该页面以 Page_Coordinates 表示的可见页面矩形，其 `left`、`top`、`width`、`height` 与该页面应用 `/Rotate` 后的可见矩形各分量误差不超过 0.01 pt。
5. THE PDF_Extractor SHALL 按 Character_Offset_Mapping 所用的同一字符遍历顺序拼接 Text_Unit 的 `text`：PDF 中实际渲染的每个字符（含空格字形）恰好输出一次，不插入原文中不存在的换行符、空格或其他分隔符，不跳过页面内任何仅含空白字符的文本块。
6. WHEN 一个 PII 值在页面上跨两行或多行渲染且换行处没有空格字形时（例如第一行以 `123-45-` 结尾、下一行以 `6789` 开头），THE PDF_Extractor SHALL 在该页 Text_Unit 的 `text` 中输出连续、无中间字符的该 PII 值（例如包含子串 `123-45-6789`）。
7. WHEN 页面包含字体带有有效 ToUnicode 映射的 CJK 字符时，THE PDF_Extractor SHALL 在 Text_Unit 的 `text` 中按渲染顺序输出与源文本逐字符相同的 Unicode 码点序列，不输出替换字符（U+FFFD）或字形编号。
8. WHEN PDF_Extractor 成功返回结果时，THE PDF_Extractor SHALL 将 `pageCount` 设置为文档总页数（包含产出零个 Text_Unit 的页面），并将 `pageOrder` 设置为 `[0, 1, …, pageCount-1]`。
9. IF 输入字节为空或无法解析为 PDF，THEN THE PDF_Extractor SHALL 抛出 `ExtractionError`（替代库抛出的任何异常都包装为 `ExtractionError`），且不返回任何部分结果。
10. IF 输入 PDF 已加密且需要非空用户密码才能打开，THEN THE PDF_Extractor SHALL 抛出 `ExtractionError`，且不返回任何部分结果。
11. IF 文档已成功打开但某一页的文本字符无法读取，THEN THE PDF_Extractor SHALL 抛出说明该页 0 起始索引的 `ExtractionError`，且不返回已提取页面的部分结果。

### Requirement 5: 坐标系与字符偏移映射

**User Story:** 作为脱敏组件的维护者，我希望字符偏移与页面矩形之间的映射在提取侧和脱敏侧完全一致，以便涂黑框精确落在匹配文本上。

#### Acceptance Criteria

1. THE PDF_Extractor SHALL 以 Page_Coordinates 输出所有包围框（Text_Unit `boundingBox`、`ImageRegion.boundingBox`），且每个包围框满足 `left ≤ right`、`top ≤ bottom`，并在 0.01 pt 误差内落在该页可见页面矩形之内。
2. IF 替代库的原生坐标系与 Page_Coordinates 不同（例如原点在左下角、y 轴向上、未应用 `/Rotate`，或 CropBox / MediaBox 原点不为 (0, 0)），THEN THE PDF_Extractor SHALL 在返回结果之前把所有包围框转换为 Page_Coordinates，使调用方得到的任何包围框都不是替代库原生坐标。
3. THE PDF_Redactor SHALL 以 Page_Coordinates 接收所有包围框（Text_Unit `boundingBox`、按页面矩形缩放后的 `ocrWords` 包围框），并在调用替代库的涂黑或删除操作之前，把每个 Redaction_Region 转换为替代库所需坐标；PDF_Redactor 中计算 Redaction_Region 的逻辑 SHALL 只使用 Page_Coordinates。
4. FOR ALL 位于可见页面矩形内的矩形，从 Page_Coordinates 转换到替代库原生坐标再转换回 Page_Coordinates SHALL 得到四条边的误差都不超过 0.01 pt 的矩形（往返属性）。该属性覆盖 `/Rotate` 为 0、90、180、270 的页面，以及 CropBox 原点不为 (0, 0) 的页面。
5. FOR ALL Text_Unit，THE Character_Offset_Mapping SHALL 满足以下不变量：`text` 的长度等于 PDF_Redactor 对同一页面遍历得到的字符位置总数；当一个字形对应多个 Unicode 字符时（例如连字 "ﬁ" 展开为 "fi"），每个展开后的字符各算一个字符位置，并共用该字形的矩形；对任一偏移 `i`（`0 ≤ i < len(text)`），PDF_Redactor 在位置 `i` 处定位到的字符 SHALL 等于 `text[i]`。
6. WHEN PDF_Redactor 处理一个满足 `0 ≤ startOffset < endOffset ≤ len(text)` 的 `text:` MatchSpan，且该页的字符位置可以读取时，THE PDF_Redactor SHALL 生成一组 Redaction_Region。每个有字形矩形的字符，只要偏移在 `[startOffset, endOffset)` 内，SHALL 在 0.01 pt 误差内被完全包含在这组区域中；每个 Redaction_Region SHALL 等于该 MatchSpan 在同一行内各字符矩形的最小外接矩形。没有字形矩形的字符（例如矩形为空的空格）SHALL 不参与计算。
7. WHEN 一个 `text:` MatchSpan 的字符分布在 N 行时（N ≥ 1，"行"指 PDF_Redactor 遍历字符时所用的同一行分组），THE PDF_Redactor SHALL 为该 MatchSpan 恰好生成 N 个 Redaction_Region，每行一个，按行在遍历中出现的顺序排列。这些区域 SHALL 不覆盖跨行之间未匹配字符的字形矩形。
8. IF 某页面的字符位置无法读取（替代库读取字符时抛出异常，或该页遍历得到零个字符位置），或 MatchSpan 区间内没有任何字符带有字形矩形，THEN THE PDF_Redactor SHALL 为该 MatchSpan 生成且只生成一个估算的 Redaction_Region，而不是跳过它。该区域的上下边与 Text_Unit `boundingBox` 相同；左边为 `left + (startOffset / len(text)) × width`，右边为 `left + (endOffset / len(text)) × width`，两个比例都限制在 `[0, 1]` 之内。如果 `len(text)` 为 0，该区域就是整个 `boundingBox`。
9. IF 一个 `text:` MatchSpan 的偏移不满足 `0 ≤ startOffset < endOffset ≤ 字符位置总数`，THEN THE PDF_Redactor SHALL 按第 8 条的比例估算方式生成 Redaction_Region，不跳过该 MatchSpan，也不抛出异常。

### Requirement 6: 图片区域提取契约

**User Story:** 作为图片 OCR 与图片内脱敏的维护者，我希望图片区域的字节与定位句柄语义保持不变，以便图片内 PII 仍能被识别并涂黑。

#### Acceptance Criteria

1. WHEN 页面直接引用或经其使用的 Form XObject 间接引用一个或多个图片 XObject 时，THE PDF_Extractor SHALL 为该页引用的每个不同图片对象恰好产出一个 `zone="image"`、`recognizedText` 为 `None` 的 `ImageRegion`：同一图片对象在同一页多次放置时只产出一个；同一图片对象被多个页面引用时每页各产出一个；内联图片（不具有对象编号）与不引用图片的页面产出零个 `ImageRegion`。
2. THE PDF_Extractor SHALL 将 `ImageRegion.boundingBox` 设置为该图片在页面内容流绘制顺序中首个放置位置的可见矩形（Page_Coordinates），其 `left`、`top`、`width`、`height` 与该矩形的对应值相差均不超过 0.01 pt，包括 `/Rotate` 为 0、90、180、270 的页面。
3. THE PDF_Extractor SHALL 将 `ImageRegion.sourceLocation` 设置为 Image_Handle 格式 `page:{pageIndex}:xref:{xref}`，其中 `pageIndex` 为该页的 0 起始索引，`xref` 为该图片 XObject 在输入 PDF 中的正整数间接对象编号；同一图片对象在不同页面上的 `xref` 值相同。
4. THE PDF_Extractor SHALL 将 `ImageRegion.imageBytes` 设置为非空字节串，Pillow 能对其完成打开与像素加载且不抛出异常，解码后的像素宽高等于该图片对象的 `/Width` 与 `/Height`。
5. IF 某内嵌图片的字节在读取或转换为可被 Pillow 解码的格式时出错，或得到的字节串长度为 0，THEN THE PDF_Extractor SHALL 仍为该图片产出 `sourceLocation` 与 `boundingBox` 均按准则 2、3 填写且 `imageBytes` 为 `None` 的 `ImageRegion`，不抛出 `ExtractionError`，并照常产出该文档的全部 Text_Unit 与其余 `ImageRegion`。
6. THE DOCX 提取 SHALL 对同一 DOCX 输入产出与替换前逐字相同的、以 `docx:part:` 开头的 `ImageRegion.sourceLocation` 值。
7. THE PDF_Extractor SHALL 按 `pageIndex` 升序排列 `imageRegions`，同一页内的 `ImageRegion` 顺序在对同一输入字节的重复提取中保持一致，使 `image:` MatchSpan 的索引始终指向同一图片。
8. IF 页面引用的某图片对象在页面内容中没有任何放置位置，THEN THE PDF_Extractor SHALL 为该图片产出 `boundingBox` 为 `None` 的 `ImageRegion`，其余字段按准则 1、3、4、5 填写。

### Requirement 7: OCR 路径光栅化

**User Story:** 作为检测流水线的维护者，我希望 OCR_Path 在新库下仍按相同规则光栅化每一页，以便文本层损坏（如 ToUnicode CMap 错误）时仍能检出 PII。

#### Acceptance Criteria

1. WHEN 提供一个可读、未加密、页数为 N（N ≥ 0）的 PDF 时，THE OCR_Path SHALL 按页序返回恰好 N 个 `zone="ocr"` 的 OCR_Unit，且第 `i` 个（0 起始）OCR_Unit 的 `position.index` 与 `position.pageIndex` 均等于 `i`。
2. WHEN Textract 在某页未返回任何同时具有非空文本与有效 `Geometry.BoundingBox` 的 `WORD` 块时，THE OCR_Path SHALL 为该页产出 `text=""` 且 `ocrWords` 为空列表的 OCR_Unit，并继续处理后续页面。
3. WHEN OCR_Path 处理某页时，THE OCR_Path SHALL 以 Raster_Resolution 将整个可见页面矩形（已应用 `/Rotate`）光栅化为一张 PNG 图像，其像素宽高分别等于可见页面矩形宽高（pt）× DPI / 72，允许每个维度 ±1 像素误差，再将该 PNG 字节提交给 Textract。
4. THE OCR_Path SHALL 将每个 OCR_Unit 的 `boundingBox` 设置为第 3 条中被光栅化的同一可见页面矩形的 Page_Coordinates（`left`、`top`、`width`、`height` 单位为 pt，与 Page_Coordinates 的误差不超过 0.01 pt）。
5. WHEN 光栅化得到的 PNG 字节数大于 10,485,760（10 × 1024 × 1024）且当前 DPI 不等于 150 时，THE OCR_Path SHALL 以 150 DPI 重新光栅化该页恰好一次，并将重新光栅化的 PNG 提交给 Textract，不论其大小；IF 当前 DPI 已等于 150，THEN THE OCR_Path SHALL 不重新光栅化，直接提交原 PNG。
6. IF `PDF_OCR_DPI` 缺失、去除首尾空白后为空、无法解析为整数或解析值不在闭区间 `[72, 400]` 内，THEN THE OCR_Path SHALL 使用 200 DPI 作为 Raster_Resolution（不夹取到最近边界）；IF 解析值在 `[72, 400]` 内，THEN THE OCR_Path SHALL 使用该值。
7. IF 某页无法加载，或在当前 DPI 或 150 DPI 下无法光栅化，THEN THE OCR_Path SHALL 抛出 `pageIndex` 等于该页 0 起始索引、且错误消息包含该页索引的 `OcrPathError`，并且不返回任何 OCR_Unit。
8. IF 输入字节无法解析为 PDF、PDF 已加密，或 Textract 客户端无法创建，THEN THE OCR_Path SHALL 抛出 `pageIndex` 为 `None` 的 `OcrPathError`，不调用 Textract `DetectDocumentText`，并且不返回任何 OCR_Unit。
9. THE OCR_Path SHALL 对每页调用同步 Textract `DetectDocumentText`，以该页 PNG 字节作为文档输入，每页的成功调用恰好一次。
10. IF 某页的 `DetectDocumentText` 调用以限流或服务端（5xx）错误失败，THEN THE OCR_Path SHALL 对该页最多共尝试 3 次，第 1 次与第 2 次重试前分别等待 1 秒与 2 秒；IF 错误不属于限流或服务端错误，THEN THE OCR_Path SHALL 不再重试。
11. IF 某页的 `DetectDocumentText` 在第 10 条的重试后仍失败，THEN THE OCR_Path SHALL 抛出 `pageIndex` 等于该页 0 起始索引、且错误消息包含该页索引的 `OcrPathError`，并且不返回任何 OCR_Unit。
12. WHEN OCR_Path 由 Textract 响应构建 OCR_Unit 时，THE OCR_Path SHALL 按 `LINE` 块的返回顺序及每行 `CHILD` 关系所列顺序拼接 `WORD` 文本，同一行内的词之间插入一个空格，相邻行之间插入一个换行符，跳过文本为空或缺少有效包围框的词，并为每个保留的词记录其在 `text` 中的半开偏移区间与原样保存的 Textract 归一化 `[0, 1]` 包围框。

### Requirement 8: 真脱敏

**User Story:** 作为数据保护负责人，我希望涂黑区域下的文本被真正删除，以便任何工具都无法从脱敏后的 PDF 中恢复被脱敏的 PII。

#### Acceptance Criteria

1. WHEN PDF_Redactor 处理一个 Redaction_Region 时，THE PDF_Redactor SHALL 从输出 PDF 中删除该 Redaction_Region 所在页面上所有字形矩形与该 Redaction_Region 的交集面积大于 0 的文本字符，其中字形矩形指 Character_Offset_Mapping 所用的、以 Page_Coordinates 表示的字符矩形，删除范围包括页面内容流、页面内容流引用的 Form XObject 中的文本，以及不可见渲染模式（文本渲染模式 3）的文本。
2. WHEN PDF_Redactor 处理一个 Redaction_Region 时，THE PDF_Redactor SHALL 在输出 PDF 的该页面上绘制一个纯黑（RGB 0,0,0）、不透明度为 1.0、无软蒙版的填充矩形，该矩形以 Page_Coordinates 表示时在每条边上覆盖 Redaction_Region 的误差不超过 0.01 pt，并绘制在该页面全部其他页面内容（含内嵌图片）之上。
3. WHEN 对脱敏后的 PDF 使用 Independent_Text_Extractor 提取全文时，THE 提取结果在删除全部空白字符后 SHALL 不包含任何被脱敏 MatchSpan 对应的 PII 字符串（即所引用单元 `text` 中 `[startOffset, endOffset)` 的子串删除全部空白字符后的结果）。
4. WHEN 对脱敏后的 PDF 使用 PDF_Extractor 提取全文时，THE 提取结果在删除全部空白字符后 SHALL 不包含任何被脱敏 MatchSpan 对应的 PII 字符串（定义同准则 3）。
5. THE PDF_Redactor SHALL 在输出 PDF 中保留所有字形矩形与全部 Redaction_Region 的交集面积均为 0 的文本字符，并保持这些字符的 Unicode 值与相对顺序不变，且每个字符矩形在 Page_Coordinates 中与源文档对应字符矩形的偏差在每条边上不超过 0.01 pt。
6. WHEN `match_spans` 解析后不产生任何 Redaction_Region 时，THE PDF_Redactor SHALL 输出这样一个 PDF：每页经 PDF_Extractor 提取的 Text_Unit `text` 与源文档逐字符相同，且每页以 200 DPI 光栅化后的像素与源文档以 200 DPI 光栅化后的像素逐一相同。
7. THE PDF_Redactor SHALL 输出与源文档页数相同、页序相同、每页可见页面矩形与 `/Rotate` 值均与源文档相同的 PDF，使 Structure_Verifier 校验通过。
8. WHEN 来自 `text:` 与 `ocr:` 的 Redaction_Region 在同一页重叠时，THE PDF_Redactor SHALL 对每一个 Redaction_Region 分别执行准则 1 与准则 2，不做去重、合并或丢弃。
9. IF 任何被脱敏 MatchSpan 对应的 PII 字符串（删除全部空白字符后，并在字体编码经 ToUnicode 或内置编码解码、流解压之后）出现在脱敏后 PDF 文件的任何对象中，包括页面与 Form XObject 内容流、注释（含其内容与外观流）、AcroForm 表单字段值、文档信息字典、XMP 元数据、未被引用的对象以及先前的增量更新修订版本，THEN THE 泄漏测试 SHALL 判定失败。
10. THE PDF_Redactor SHALL 将输出 PDF 写为单一修订版本的完整文件，其中不含增量更新段、不含未被引用的对象，也不含用于标记待脱敏区域的注释对象。
11. IF PDF_Redactor 无法从一个 Redaction_Region 下删除准则 1 要求删除的全部字符（例如内容流无法解析或字体编码无法解码），THEN THE PDF_Redactor SHALL 抛出 `RedactionError` 且不返回任何输出字节，不得退化为只绘制黑色矩形而保留底层文本的输出。

### Requirement 9: 图片内脱敏

**User Story:** 作为数据保护负责人，我希望图片中检出的 PII 仍被画上黑框并写回原图片位置，以便图片内 PII 不会泄漏。

#### Acceptance Criteria

1. WHEN 存在引用某 `ImageRegion` 的一个或多个 `image:` MatchSpan 时，THE PDF_Redactor SHALL 对该图片恰好调用一次 `redaction_image.redact_image_bytes`，传入该 `ImageRegion` 的 `imageBytes` 与引用它的全部 `image:` MatchSpan，并将返回的字节写回 Image_Handle 中 `xref` 所标识的图片对象；多个被引用的图片按 `image:{index}` 中 `index` 升序处理。
2. WHEN 一张图片被写回后，THE 脱敏后的 PDF SHALL 在该图片对象的每个页面放置位置显示写回后的图片，其解码后的像素宽高与 `redact_image_bytes` 输出相同，且 `redact_image_bytes` 涂黑区域内的每个像素均为纯黑（RGB 0,0,0）。
3. WHEN 一个 `text:` 或 `ocr:` 的 Redaction_Region 与某内嵌图片在页面上重叠，且该图片同时被 `image:` MatchSpan 引用时，THE PDF_Redactor SHALL 让写回后的图片（而非未脱敏的原图或其副本）显示在页面上的原放置位置，并且仍按需求 8 绘制该 Redaction_Region 的黑色填充矩形。
4. WHEN 一张图片被写回后，THE 脱敏后的 PDF SHALL 不含任何解码后字节与该图片脱敏前原始图片数据流字节完全相同的对象数据流（包括未被页面引用的孤立对象）。
5. WHEN 脱敏后的 PDF 未修改任何图片时，THE PDF_Redactor SHALL 输出字节大小不超过源文档字节大小 2 倍的 PDF。
6. IF `image:` MatchSpan 的 `unitRef` 无法解析出整数索引、索引越界（小于 0 或不小于 `image_regions` 长度）、对应 `ImageRegion` 的 `imageBytes` 为 `None` 或空、`sourceLocation` 不符合 Image_Handle 格式、Image_Handle 中的 `pageIndex` 超出文档页数、`redact_image_bytes` 抛出 `ImageRedactionError`，或写回图片对象失败，THEN THE PDF_Redactor SHALL 抛出说明失败的 `image:` 索引的 `RedactionError`，并且不返回任何脱敏后 PDF 字节。
7. WHEN 某内嵌图片未被任何 `image:` MatchSpan 引用时，THE PDF_Redactor SHALL 在输出 PDF 中保留该图片，解码后像素与源文档中的同一图片相同（因需求 8 而绘制在页面上的 Redaction_Region 填充矩形不计入）。
8. WHEN 至少一张图片被写回时，THE PDF_Redactor SHALL 输出字节大小不超过"源文档字节大小的 2 倍加上全部 `redact_image_bytes` 输出字节大小之和"的 PDF。

### Requirement 10: 对外接口与报告兼容

**User Story:** 作为编排层（`handle_job`）的维护者，我希望 PDF 模块的对外接口与报告保持不变，以便编排、DOCX 路径和前端无需修改。

#### Acceptance Criteria

1. THE PDF_Extractor、OCR_Path 与 PDF_Redactor SHALL 保持以下对外函数的参数名、参数顺序、默认值与返回类型不变：`extract_pdf_content(file_bytes)` 返回 `ExtractionResult`；`ocr_pdf_pages(file_bytes)` 返回 `List[ExtractedTextUnit]`；`redact_pdf_document(file_bytes, text_units, match_spans, image_regions=None)` 返回 `bytes`；`redact_pdf_document_with_report(file_bytes, text_units, match_spans, ocr_text_units=None, image_regions=None)` 返回携带 `documentBytes` 与 `report` 的 `PdfRedactionResult`。
2. THE PDF_Extractor、OCR_Path 与 PDF_Redactor SHALL 保持 `ExtractionError`、`OcrPathError`、`RedactionError` 在各自模块中的定义位置与类名不变，且 `OcrPathError` 继续携带出错页的 0 起始页索引（页级错误时）。
3. THE PDF_Redactor SHALL 在 `PdfRedactionReport` 中报告：`textLayerRegions` 等于由 `text:` MatchSpan 解析出的 Redaction_Region 数量，`ocrRegions` 等于由 `ocr:` MatchSpan 解析出的 Redaction_Region 数量，`agreedRegions` 等于与同页至少一个 `text:` Redaction_Region 有面积大于 0 的交集的 `ocr:` Redaction_Region 数量（仅边界相接不计入）；三项均按 Redaction_Region 计数而非按 MatchSpan 计数，`image:` MatchSpan 不计入任何一项，且 `agreedRegions` 不超过 `ocrRegions`。
4. WHEN `match_spans` 为空列表或解析后不产生任何 `text:` / `ocr:` Redaction_Region 时，THE PDF_Redactor SHALL 返回 `textLayerRegions`、`ocrRegions`、`agreedRegions` 均为 0 的 `PdfRedactionReport`。
5. THE PDF_Redactor SHALL 保持 `MatchSpan.unitRef` 命名空间 `text:{index}`、`ocr:{index}`、`image:{index}` 的解析规则（`index` 为对应输入列表 `text_units`、`ocr_text_units`、`image_regions` 的 0 起始索引），并对前缀不属于这三者的 MatchSpan 跳过处理，既不抛出异常，也不计入 `PdfRedactionReport`。
6. IF `text:` 或 `ocr:` MatchSpan 的 `index` 不是整数、为负数或不小于对应单元列表长度（含 `ocr_text_units` 为 `None` 或空列表的情况），或单元缺少 `pageIndex` / `boundingBox`（文本层）或 `pageIndex` / `ocrWords`（OCR），或页索引超出 `[0, 文档页数-1]`，THEN THE PDF_Redactor SHALL 抛出 `RedactionError` 且不返回任何脱敏字节。
7. IF 输入字节无法解析为 PDF，THEN THE PDF_Redactor SHALL 抛出 `RedactionError`。
8. IF 替代库在 PDF_Extractor、OCR_Path 或 PDF_Redactor 内部抛出其自有异常类型，THEN 对应组件 SHALL 将其转换为 `ExtractionError`、`OcrPathError` 或 `RedactionError` 后再抛出，使调用方捕获到的异常类型集合与替换前一致。
9. THE DOCX 提取（`extraction_docx.py`）、DOCX 脱敏（`redaction_docx.py`）与 `frontend` 目录 SHALL 零源码改动（注释中删除 Removed_Library_Identifier 的改动除外），且其现有测试在不修改的情况下全部通过。

### Requirement 11: 方案评估

**User Story:** 作为项目负责人，我希望在实施前看到候选方案的对比和推荐，以便确认后再动手修改代码。

#### Acceptance Criteria

1. THE 设计文档 SHALL 至少比较 3 个候选方案：(a) 基于 pypdfium2 与 pdfminer.six 提取、以 pikepdf 或 pypdf 在内容流层面删除文字的方案；(b) 整页光栅化后用 Pillow / img2pdf 重建 PDF 的方案；(c) 至少一个不同于 (a)、(b) 的其他方案，且该方案的每个直接依赖在 PyPI 上至少有一个已发布版本。
2. THE 设计文档 SHALL 以对比表的形式为每个方案逐项填写以下 6 个维度，且任一单元格不得为空或写作"待定"：(1) 每个直接依赖及其捆绑原生库（例如 PDFium）的许可证标识，以及该许可证属于 Permissive_License、Weak_Copyleft_License 还是两者均不属于；(2) 每个直接依赖的候选精确版本，以及该版本在 PyPI 上是否提供 Linux aarch64 且与 CPython 3.12 兼容（`cp312`、`abi3` 或 `py3-none-any`）的 wheel；(3) 产出 Page_Coordinates 并建立 Character_Offset_Mapping 的方式；(4) 对需求 8 第 1 至 9 条逐条标明"满足"、"部分满足"或"不满足"，并对后两者说明原因；(5) 需改写的 Backend_Test_Suite 测试文件列表，以及对需求 10 所列对外接口与报告字段的影响；(6) 至少 1 条主要风险及对应的缓解措施。
3. THE 设计文档 SHALL 给出恰好一个推荐方案，其理由须引用第 2 条对比表中的具体维度，并说明该方案相对其余每个候选方案的取舍。
4. WHILE 用户尚未在对话中明确确认设计方案（明确批准推荐方案或指定另一个候选方案），THE 实施工作 SHALL 不修改仓库中的任何源码、测试、Dependency_Manifest 或脚本文件，`.kiro/specs/pymupdf-replacement/` 下的规格文档除外。
5. IF 某候选方案的任一直接依赖或捆绑原生库既不属于 Permissive_License、也不属于满足需求 2 第 9 至 12 条的 Weak_Copyleft_License，或缺少第 2 条第 (2) 项所述的 wheel，或对需求 8 中任一条标为"部分满足"或"不满足"，THEN THE 设计文档 SHALL 将该方案标记为不可推荐，并写明触发标记的具体维度。
6. IF 所有候选方案均被标记为不可推荐，THEN THE 设计文档 SHALL 不给出推荐方案，并列出每个方案未满足的具体需求条款，交由用户决定后续方向。

### Requirement 12: 泄漏测试

**User Story:** 作为数据保护负责人，我希望有一个独立工具校验的泄漏测试，以便脱敏缺陷能被自动发现。

#### Acceptance Criteria

1. THE Backend_Test_Suite SHALL 包含一个泄漏测试：构造一个至少 2 页、至少包含 4 个 PII 值的 PDF，其中至少 1 个为纯拉丁字符值、至少 1 个为含 CJK 字符的值、至少 1 个为跨行渲染且换行处无空格字符的值，且至少 1 个 PII 值位于第 2 页及之后；经 PDF_Redactor 脱敏后，用 Independent_Text_Extractor 提取全部页面的全文，断言提取结果既不以原样子串形式、也不在删除全部空白字符（空格、制表符、换行）后以子串形式包含任何被脱敏的 PII 字符串。
2. THE 泄漏测试 SHALL 同时断言至少一个未被脱敏的非 PII 字符串（长度至少 4 个字符，且与某个被脱敏 PII 值位于同一页面同一行）在脱敏后仍可被 Independent_Text_Extractor 原样提取，以排除"整页文本被清空"或"整行文本被删除"的假通过。
3. THE 泄漏测试 SHALL 覆盖 `text:` 与 `ocr:` 两种来源的 Redaction_Region：至少 1 个 PII 值仅由 `text:` MatchSpan 覆盖，至少 1 个 PII 值仅由 `ocr:` MatchSpan 覆盖且不被任何 `text:` MatchSpan 覆盖；`ocr:` MatchSpan 所依赖的 OCR_Unit 由测试内预设的 `ocrWords` 提供，测试运行不调用 Amazon Textract 且不需要网络或 AWS 凭证。
4. THE 泄漏测试 SHALL 所使用的 Independent_Text_Extractor 许可证属于 Permissive_License，且与 PDF_Redactor 读取或写入 PDF 所用的任何库都不是同一个库。
5. WHEN 泄漏测试在调用 PDF_Redactor 之前用 Independent_Text_Extractor 提取源 PDF 全文时，THE 泄漏测试 SHALL 断言每个待脱敏 PII 字符串在删除全部空白字符后的源文本中均可被找到，以排除测试数据本身无法被提取导致的假通过。
6. WHEN 泄漏测试检查脱敏后的 PDF 时，THE 泄漏测试 SHALL 断言每个被脱敏的 PII 字符串均不出现在任何解压后的页面内容流、注释、表单字段值与文档元数据中；任一处出现即判定测试失败。
7. IF Independent_Text_Extractor 在测试环境中无法导入，THEN THE 泄漏测试 SHALL 判定为失败，而不是被跳过。

### Requirement 13: 测试基线与等价覆盖

**User Story:** 作为开发者，我希望替换后现有测试全部通过或被等价改写，以便确认行为未退化。

#### Acceptance Criteria

1. WHEN 在 `backend` 目录下、未安装 PyMuPDF 的虚拟环境中运行 `.venv/bin/python -m pytest tests -q -p no:cacheprovider` 时，THE Backend_Test_Suite SHALL 以退出码 0 结束，报告 0 failed、0 errors，passed 数量不少于（397 + 新增测试数量 − 等价改写映射表中记录的被合并测试数量），skipped 数量不超过 5。
2. WHERE 现有测试依赖 PyMuPDF 构造 PDF（包括 `tests/conftest.py`、`test_extraction_pdf.py`、`test_redaction_pdf.py`、`test_structure_verification.py`），THE Backend_Test_Suite SHALL 改用许可证属于 Permissive_License 的库构造 PDF，并为以下每个场景各包含至少 1 个测试：CJK 文本提取与脱敏、图片区域（Image_Handle）提取与写回、OCR_Path、页数与页序（Structure_Verifier）、跨行渲染的 PII、零匹配（无 MatchSpan 时输出与输入页数和文本一致）、越界偏移输入（抛出 `RedactionError`）、损坏 PDF 输入（抛出 `ExtractionError` 或 `RedactionError`）。
3. THE Backend_Test_Suite SHALL 对每条改写前存在的脱敏断言保留同等或更严格的断言；以下任一情况 SHALL 视为断言放宽并判定不满足本需求：删除断言且无等价替代、将相等断言改为包含或不等断言、放大数值或坐标容差、减少被断言的 PII 值或页面数量、将失败断言改为 skip 或 xfail。
4. WHERE 测试受环境变量保护（`test_llm_cjk_recall.py`、`test_patti_regression.py`），THE Backend_Test_Suite SHALL 在未安装 PyMuPDF 时零导入错误地收集这些测试，在保护环境变量未设置时以与改写前相同的条件和相同的测试数量跳过，在保护环境变量已设置时执行这些测试。
5. WHEN 在 `frontend` 目录下运行 `npx tsc -b` 与 `npx vitest --run` 时，THE Frontend_Checks SHALL 使 `npx tsc -b` 以退出码 0 结束且无类型错误，并使 `npx vitest --run` 报告 0 failed 且 passed 数量不少于 68。
6. WHEN 某个改写前存在的测试被删除、重命名或合并时，THE Backend_Test_Suite SHALL 在设计文档或测试变更说明中附带等价改写映射表，为每个此类测试列出原测试标识与替代它的测试标识，且每个原测试至少对应 1 个替代测试。
7. IF Backend_Test_Suite 中出现 skipped 测试，THEN THE Backend_Test_Suite SHALL 仅因准则 4 所述环境变量保护而跳过，任何因缺少 PyMuPDF 或缺少 PDF 构造依赖而跳过的测试 SHALL 视为不满足本需求。

### Requirement 14: 注释清理

**User Story:** 作为代码阅读者，我希望 PDF 模块的注释读起来像正常的代码说明，以便不需要了解内部任务编号就能理解代码。

#### Acceptance Criteria

1. THE `extraction_pdf.py`、`ocr_pdf.py` 与 `redaction_pdf.py` SHALL 包含零个 Legacy_Tracking_Comment，检查范围包括 `#` 注释、`#:` 常量注释、模块 / 类 / 函数 docstring 中的文字；判定方式为：在上述文字中不区分大小写搜索 `task` 或 `requirement` / `requirements` 后跟数字（含 `13.1/13.2`、`3.3-3.6`、`1.1, 1.9` 这类组合形式）的引用，结果为零个匹配。
2. WHEN 删除 Legacy_Tracking_Comment 中的编号引用时，THE 清理 SHALL 只删除编号引用本身以及因此变空的括号、分隔符或连接词，同一注释或 docstring 中的其余说明性文字 SHALL 保留在原来所属的模块、类、函数或常量的注释或 docstring 中；仅当这些文字描述的是 PyMuPDF 的 API（由验收标准 3 处理）时，才允许改写这些文字。
3. WHEN 清理完成时，THE 三个 PDF 模块的注释与 docstring SHALL 仍然说明以下每一项设计理由（允许改写措辞，但不允许删除）：(a) PDF_Extractor 与 PDF_Redactor 必须基于同一字符遍历顺序，以保持 Character_Offset_Mapping；(b) OCR_Path 在任一页失败时必须抛出 `OcrPathError` 而非静默跳过，且错误信息包含失败页索引；(c) OCR_Path 对每页都产出一个 OCR 单元，即使该页没有识别出文字，以保持 `ocr:{index}` 等于页索引；(d) `ocr:` 匹配按整词包围框脱敏；(e) 两条检测路径的重叠 Redaction_Region 都会绘制，不做去重；(f) Textract 每页最多尝试 `MAX_ATTEMPTS` 次，以及超过 10 MB 时以 150 DPI 重渲染一次的回退。
4. THE 三个 PDF 模块的注释与 docstring SHALL 包含零个 Removed_Library_Identifier，并且 SHALL 包含零个 PyMuPDF 专有 API 名称（`page.rect`、`get_pixmap`、`rawdict`、`add_redact_annot`、`apply_redactions`、`replace_image`、`PDF_REDACT_IMAGE_NONE`）。
5. WHERE 某条注释或 docstring 按名称引用了库函数、类或方法，THE 被引用的名称 SHALL 是该模块在替换后实际导入或调用的名称。
