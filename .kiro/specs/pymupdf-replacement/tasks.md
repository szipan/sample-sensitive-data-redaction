# Implementation Plan: pymupdf-replacement

## Overview

按设计文档的方案 A（pdfminer.six 共用字符遍历 + pikepdf 内容流字形删除 + pypdfium2 光栅化与残留校验）实施，实现语言为 Python 3.12。

实施顺序：

1. 修正设计文档、记录已确认决定，建立改动前的测试基线。
2. 先加入新依赖（暂时保留旧库，让现有测试在过渡期保持可运行），编写只依赖 pypdf 的测试 PDF 构造器。
3. 依次实现新的纯模块：`pdf_geometry` → `pdf_glyphs` → `pdf_content_edit` / `pdf_images`。
4. 重写 `extraction_pdf`、`ocr_pdf`、`redaction_pdf`（含脱敏后复核、失败即报错），改写现有测试，加入泄漏测试。
5. 所有测试改写完成后，从依赖清单中删除已移除的 PDF 库，重建虚拟环境，更新 bundle 脚本、THIRD-PARTY-LICENSES、README，清理全仓库残留引用。

已确认的决定（用户在评审设计后要求创建任务，视为批准）：方案 A；Zlib、IJG、FTL、libpng-2.0、Unicode-3.0、MIT-0、MIT-CMU、CC-BY-4.0、AGG 归为 Permissive_License（在公司预批准名单中）；hypothesis（MPL-2.0，现有开发依赖）与 pikepdf 同样在 THIRD-PARTY-LICENSES 与 README 中处理；`constructs==10.8.1`；脱敏输出不加密；无法证明删除时以 `RedactionError` 使任务失败，不做整页光栅化回退。

范围之外：git commit / push、AWS 部署（新旧栈同名 `PiiDocumentRedactionPocStack`，有覆盖风险）、购买商业许可。规格与代码中不得写入 AWS 账号 ID、密钥或真实客户文档内容。

## Tasks

- [x] 1. 设计文档修订与改动前基线
  - [x] 1.1 修正 design.md 中的核实日期并记录已确认决定
    - 把 design.md 中所有"2026-11-02"改为"2026-11-01"（研究说明、表 D 系列、bundle 实测表、"其他文档与注释"检索日期等处）；pikepdf"用户已于 2026-11-01 核实"的表述改为"同日复核一致"
    - 把"需用户确认的决定"一节改写为"已确认的决定"，逐条记录：方案 A；许可证归类表中各项（Zlib、IJG、FTL、libpng-2.0、Unicode-3.0、MIT-0、MIT-CMU、CC-BY-4.0、LicenseRef-AGG-2.3 等）按公司预批准名单归为 Permissive_License；hypothesis 与 pikepdf 同样处理；`constructs==10.8.1`；输出不加密（R11）；需求 7.8 的"已加密"按"需要非空用户密码"解释；需求 9.4 的理解；失败而非光栅化回退
    - "需用户确认的许可证归类"一节标题与结尾说明同步改为已确认
    - 只改 `.kiro/specs/pymupdf-replacement/design.md`
    - _Requirements: 2.3, 2.4, 11.4_

  - [x] 1.2 创建后端虚拟环境并记录改动前的后端测试基线
    - 在 `backend` 下用 `python3.12 -m venv .venv` 创建虚拟环境，`.venv/bin/pip install -r requirements-dev.txt`（此时仍为旧依赖清单）
    - 运行 `.venv/bin/python -m pytest tests -q -p no:cacheprovider -rs`，记录 passed / skipped 数量（期望 397 passed、5 skipped）以及每个 skipped 测试的标识与跳过原因
    - 把结果写入 design.md 新增的"实施基线"小节；若数量与期望不符，停下来询问用户
    - 确认 `.venv/` 已被 `.gitignore` 忽略
    - _Requirements: 13.1, 13.4, 13.7_

  - [x] 1.3 记录改动前的前端检查基线
    - 在 `frontend` 下运行 `npm ci`、`npx tsc -b`、`npx vitest --run`，记录 tsc 退出码与 vitest passed 数量（期望 68 passed）
    - 结果追加到 design.md 的"实施基线"小节；不改动 `frontend` 下任何源码
    - _Requirements: 13.5, 10.9_

- [x] 2. 新增依赖与测试 PDF 构造器
  - [x] 2.1 在依赖清单中加入新依赖（暂时保留旧库）
    - `backend/requirements.txt` 与 `backend/agent-runtime-requirements.txt` 新增 `pdfminer.six==20260107`、`pikepdf==10.17.0`、`pypdfium2==5.14.0`，两个文件版本一致
    - `backend/requirements-dev.txt` 新增 `pypdf==6.21.4`（仅测试用）
    - `backend/requirements.txt` 中 `constructs` 固定为 `constructs==10.8.1`
    - 旧 PDF 库条目暂不删除（在任务 14.1 删除），以便过渡期现有测试仍可运行
    - 重新执行 `.venv/bin/pip install -r requirements-dev.txt`，确认四个新库可导入
    - _Requirements: 2.1, 2.2, 3.1_

  - [x] 2.2 编写测试 PDF 构造器 `backend/tests/pdf_fixtures.py`
    - 只使用 pypdf 的 `PdfWriter` 与 generic 对象（必要时直接拼装原始 PDF 字节），不导入任何其他 PDF 库
    - 提供：标准 14 字体文本页（`Tj` / `TJ` 含字距数组 / `'` / `"`，可设 `Tc` / `Tw` / `Tz` / `Ts` / `Tr`）；Type0 / Identity-H CJK 字体与自建 ToUnicode CMap（含一字形映射多字符的条目）；可多次、跨页、嵌套放置的 Form XObject；图片 XObject（FlateDecode RGB / Gray、DCTDecode、带 `/SMask`、Indexed、`/ImageMask`）与内联图片；非零原点 MediaBox、CropBox、`/Rotate`；FreeText 注释、Widget 与 AcroForm 字段值、文档信息字典、XMP、增量更新修订；仅所有者密码与需要用户密码的加密
    - 为 Hypothesis 策略提供可组合的页面 / 文档描述数据结构
    - _Requirements: 13.2, 12.1_

  - [x] 2.3 为测试 PDF 构造器编写冒烟测试 `backend/tests/test_pdf_fixtures.py`
    - 用 pypdf 读取构造结果，检查页数、`/Rotate`、CropBox、图片对象与加密标志符合描述
    - _Requirements: 13.2_

- [x] 3. 实现坐标模块 `pdf_geometry.py`
  - [x] 3.1 实现 `backend/agent/tools/pdf_geometry.py`
    - 按设计实现 `PageGeometry`、`normalize_rotation`（PDFium 规则）、`make_page_geometry`（可见矩形 = CropBox ∩ MediaBox，交集为空或缺失时用 MediaBox）、点与矩形的双向换算（设计中 `/Rotate` 0/90/180/270 的公式表）、`page_rect_as_bounding_box`、`rects_intersect`（`INTERSECT_EPS_PT = 1e-6`）、`normalized_box_to_page_rect`
    - 纯函数，不导入任何 PDF 库
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 4.4_

  - [x] 3.2 编写坐标换算往返的属性测试
    - **Property 1: 坐标换算往返**
    - **Validates: Requirements 5.4, 5.2**
    - 文件 `backend/tests/test_pdf_geometry.py`；Hypothesis 生成任意 MediaBox、内部 CropBox 或缺失、任意整数 `/Rotate`；`@settings(max_examples=100, deadline=None)`，注释标注 `# Feature: pymupdf-replacement, Property 1: 坐标换算往返`

  - [x] 3.3 编写坐标换算的示例测试
    - 在 `test_pdf_geometry.py` 中覆盖四种 `/Rotate` × CropBox 原点为 (50, 50) 的页面，核对设计公式表与页面宽高
    - 覆盖 `rects_intersect` 对仅边界相接、1e-13 级伪重叠返回 False
    - _Requirements: 5.4, 10.3_

- [x] 4. 实现共用字符遍历 `pdf_glyphs.py`
  - [x] 4.1 实现文档打开与内部数据类
    - 在 `backend/agent/tools/pdf_glyphs.py` 中定义 `PdfOpenError`、`PdfPasswordError`、`PageReadError`（携带 `page_index`）、`PdfHandle`、`GlyphRecord`、`CharPosition`、`ImagePlacement`、`PageGlyphs`、`ContextPath`
    - 实现 `open_pdf`：空字节或任一方（pdfminer.six、pikepdf）打开失败 → `PdfOpenError`；需要非空用户密码 → `PdfPasswordError`；仅所有者密码以空密码打开；两者页数不一致 → `PdfOpenError`
    - _Requirements: 4.9, 4.10, 10.8_

  - [x] 4.2 实现内容流解释器与字形记录
    - `_GlyphInterpreter`（继承 `PDFPageInterpreter`）以单位 CTM 执行页面内容，不调用 `process_page`；重写 `execute` 为每个关键字操作符（含未知操作符与 `EI`）递增序号
    - 重写 `do_Do`：Form XObject 压入 `(Do 序号, objid)` 上下文路径；Image XObject 记录 `ImagePlacement`（xref、首个放置的外接矩形、上下文、序号）；内联图片不记录
    - `_GlyphDevice`（继承 `PDFTextDevice`）逐元素遍历 TJ 数组并记录元素序号；按字体拆出 `(cid, 字节起点, 字节终点)`（简单字体、`IdentityCMap`、`IdentityCMapByte`、其他 CMap 前缀树匹配）；用 `LTChar` 计算字形框，按 PDF 规范计算位移；`to_unichr` 失败时用单个 U+FFFD；不区分渲染模式（含模式 3）
    - 字形框经 `pdf_geometry` 换算为 Page_Coordinates，记录 `raw_rect` 与裁剪后的 `rect`（宽或高 ≤ 0 时为 `None`）
    - 解释异常转为 `PageReadError(page_index)`
    - _Requirements: 4.5, 4.7, 5.1, 5.2, 6.1, 6.2, 8.1, 4.11_

  - [x] 4.3 实现字符位置展开、行分组与对外函数
    - 实现 `collect_page_glyphs(handle, page_index)` 与 `collect_glyphs_from_bytes(file_bytes)`
    - 字符位置展开：字形文本长度为 n 则产出 n 个连续位置共用矩形，n = 0 不产出位置但保留字形；`text` 为字符直接拼接，不插入分隔符
    - 行分组按设计规则（上下文路径不同、书写方向不同、垂直距离 > 0.5h、回退 < −0.5h 时开始新行），`line_id` 单调递增
    - 模块 docstring 说明提取侧与脱敏侧必须共用该遍历以保持字符偏移映射
    - _Requirements: 4.5, 4.6, 5.5, 5.7, 14.3_

  - [x] 4.4 编写字符偏移映射不变量的属性测试
    - **Property 4: 字符偏移映射不变量**
    - **Validates: Requirements 5.5**
    - 文件 `backend/tests/test_pdf_glyphs.py`；生成含连字类多字符 ToUnicode 条目的页面

  - [x] 4.5 编写 `pdf_glyphs` 的示例测试
    - 在 `test_pdf_glyphs.py` 中覆盖：行分组（跨行无空格 SSN 的两行、同行多 TJ）；无 ToUnicode 时输出 U+FFFD 而非 `(cid:N)`；渲染模式 3 字形产出字符位置；Form XObject 字形的上下文路径；畸形内容流抛 `PageReadError`；仅所有者密码可打开、需要用户密码抛 `PdfPasswordError`
    - _Requirements: 4.6, 4.7, 4.10, 4.11, 5.7_

- [x] 5. Checkpoint - 确认新模块测试通过
  - 运行 `.venv/bin/python -m pytest tests -q -p no:cacheprovider`，确认原有测试数量不变且全部通过，新测试通过。Ensure all tests pass, ask the user if questions arise.

- [x] 6. 实现内容流改写 `pdf_content_edit.py`
  - [x] 6.1 实现删除计划与位移计算
    - 在 `backend/agent/tools/pdf_content_edit.py` 中定义 `ContentEditError`、`GlyphDeletion`，实现 `plan_deletions(page_glyphs, regions)`：任一字形 `raw_rect` 与任一区域相交（`rects_intersect`）即删除，含不产出字符位置的字形
    - 按设计计算替代 TJ 数值（横排 `n = −tx × 1000 / (Tfs × Th)`，竖排 `n = −ty × 1000 / Tfs`）；Tfs 或 Th 为 0 时抛 `ContentEditError`
    - 计划按上下文路径分组
    - _Requirements: 8.1, 8.5, 8.8, 8.11_

  - [x] 6.2 实现操作符改写与序号对齐校验
    - 实现 `apply_page_plan`：用 pikepdf `parse_content_stream` 解析同一流（页面 `/Contents` 为数组时整体拼接），按设计改写 `Tj` / `TJ` / `'` / `"`，同一流一次遍历完成
    - 校验每个待改写序号的操作符名与字符串字节区间与 pdfminer.six 记录一致，不符抛 `ContentEditError`
    - 页面使用内容含文本操作符的平铺图案且该页有区域时抛 `ContentEditError`
    - _Requirements: 8.1, 8.5, 8.11_

  - [x] 6.3 实现 Form XObject 写时复制
    - 上下文路径非空时，从最外层起复制路径上每个表单为新流，以不冲突名称（`/RdxFm{n}`）登记到父资源字典，并改写父流对应序号 `Do` 的操作数
    - 保证同一表单的其他放置与其他页面不受影响
    - _Requirements: 8.1, 8.5, 8.9_

  - [x] 6.4 实现注释移除与黑框绘制
    - `remove_intersecting_annotations`：`/Rect` 换算为 Page_Coordinates 后与区域相交的注释从 `/Annots` 移除；Widget 同时从 `/AcroForm /Fields` 或父字段 `/Kids` 移除
    - `draw_black_boxes`：原内容包裹为 `q … Q`，追加新内容流，使用新增 ExtGState `/ca 1 /CA 1 /SMask /None /BM /Normal`，以 `page_to_user_rect` 换算的矩形填充 `0 0 0 rg`；名称冲突时追加数字后缀；每个区域各画一个，不去重
    - _Requirements: 8.2, 8.8, 8.9, 5.3_

  - [x] 6.5 实现文档级残留清理
    - `scrub_document_strings(pdf, pii_values)`：遍历可达字符串对象（信息字典、书签、名称树、注释 `/Contents` / `/T` / `/TU`、字段 `/V` / `/DV`、JavaScript），解码去空白后含 PII 则置空；XMP 含 PII 则移除 `/Metadata` 引用；保留注释的 `/AP /N` 外观流经 `pdf_glyphs` 遍历含 PII 则移除注释；嵌入文件含 PII 则移除附件
    - _Requirements: 8.9, 8.10_

  - [x] 6.6 编写 `pdf_content_edit` 的单元测试 `backend/tests/test_pdf_content_edit.py`
    - 每种操作符改写后保留字形 Unicode、顺序与位置不变（≤ 0.01 pt），被删字形不可再遍历到
    - Form XObject 写时复制：同页两次放置只删第一次、跨页放置、嵌套表单
    - 渲染模式 3 文本被删除；`text:` 与 `ocr:` 重叠区域都进入删除计划并各画一个黑框
    - 序号不符、Tfs = 0、平铺图案含文本时抛 `ContentEditError`
    - 注释与 Widget 移除；信息字典、XMP、字段值、附件中的 PII 被清理
    - _Requirements: 8.1, 8.2, 8.5, 8.8, 8.9, 8.11_

- [x] 7. 实现图片解码与写回 `pdf_images.py`
  - [x] 7.1 实现图片列表与解码
    - 在 `backend/agent/tools/pdf_images.py` 中实现 `iter_page_image_refs`：按放置顺序每个 xref 每页一项，`boundingBox` 取首个放置；再追加资源字典中有但未放置的图片（按资源名排序，`boundingBox=None`）
    - 实现 `decode_image(pdf, xref)`：符合条件的 DCTDecode 原样返回 JPEG；其余经 `pikepdf.PdfImage` 转 PNG（非 RGB / L 转 RGB，`/SMask` 作为 alpha）；任何异常或空结果返回 `None`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.7, 6.8_

  - [x] 7.2 实现图片写回
    - 实现 `write_back_image(pdf, xref, new_bytes)` 与 `ImageWriteBackError`：JPEG 写为 `/DCTDecode`；其余编码为带 PNG 预测器的 FlateDecode；更新 `/Width`、`/Height`、`/ColorSpace`、`/BitsPerComponent`，删除 `/Decode`、`/SMaskInData`、`/Mask`、`/Alternates`；按输出是否含 alpha 写入或删除 `/SMask`；`/ImageMask true` 抛错
    - _Requirements: 9.1, 9.2, 9.4, 9.5, 9.8, 9.6_

  - [x] 7.3 编写 `pdf_images` 的单元测试 `backend/tests/test_pdf_images.py`
    - 各类图片（Flate RGB / Gray、DCT、`/SMask`、Indexed）解码后 Pillow 可加载且宽高等于 `/Width` / `/Height`；损坏流返回 `None`
    - 写回后用 pypdfium2 渲染放置位置，涂黑像素为 (0, 0, 0)；写回后无对象数据流等于原图数据；`/ImageMask` 写回抛 `ImageWriteBackError`
    - _Requirements: 6.4, 6.5, 9.2, 9.4, 9.6_

- [x] 8. 重写 PDF_Extractor
  - [x] 8.1 重写 `backend/agent/tools/extraction_pdf.py`
    - 保持 `extract_pdf_content(file_bytes)`、`ExtractionError`、`ExtractionResult` 不变；`_extract_image_bytes` 参数改为 `(pdf, xref)` 并委托 `pdf_images.decode_image`
    - 用 `open_pdf` 与 `collect_page_glyphs` 产出 Text_Unit（`strip()` 非空才产出，`boundingBox=(0, 0, width, height)`）与 `ImageRegion`（`sourceLocation="page:{i}:xref:{xref}"`）；`pageCount` / `pageOrder` 按设计
    - 所有替代库异常、`PdfOpenError`、`PdfPasswordError`、`PageReadError` 以 `raise … from exc` 转为 `ExtractionError`，页级错误消息含页索引，不返回部分结果
    - 注释按需求 14 清理：删除任务 / 需求编号引用，保留设计理由，不出现已移除库标识与其专有 API 名称，只引用实际调用的名称
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.8, 4.9, 4.10, 4.11, 6.1, 6.3, 6.5, 10.1, 10.2, 10.8, 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 8.2 改写 `tests/conftest.py` 与 `tests/test_extraction_pdf.py`
    - `_build_pdf_bytes` 与各构造函数改为调用 `pdf_fixtures`
    - 图片解码断言改用 Pillow，新增"像素宽高等于 `/Width` / `/Height`"断言；`_extract_image_bytes` 替身签名改为 `(pdf, xref)`；`_FakeDocument` 改为模拟 `pikepdf.Pdf`
    - 按 design.md 等价改写映射表重命名三个 `_extract_image_bytes` 单元测试；其他测试名与断言不变，不放宽任何断言、不加 skip / xfail
    - _Requirements: 13.2, 13.3, 13.6_

  - [x] 8.3 编写文本层单元结构与内容的属性测试
    - **Property 3: 文本层单元结构与内容**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.5, 4.6, 4.7, 4.8**
    - 文件 `backend/tests/test_extraction_pdf_properties.py`

  - [x] 8.4 编写图片区域契约的属性测试
    - **Property 7: 图片区域契约**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.7**
    - 文件 `test_extraction_pdf_properties.py`

  - [x] 8.5 编写提取侧输入错误的属性测试
    - **Property 15: 输入错误只以模块自有异常抛出（提取部分）**
    - **Validates: Requirements 4.9, 10.8**
    - 文件 `test_extraction_pdf_properties.py`；随机截断与篡改字节

  - [x] 8.6 编写提取侧示例测试
    - 在 `test_extraction_pdf_properties.py` 中覆盖：跨行无空格 SSN 输出连续 `123-45-6789`；CJK 逐码点一致；仅所有者密码可提取；需要用户密码抛 `ExtractionError`；页级读取失败抛含页索引的 `ExtractionError`；未放置图片 `boundingBox=None`；`/Rotate 90` + 非零 CropBox 页面的 `boundingBox`
    - _Requirements: 4.4, 4.6, 4.7, 4.10, 4.11, 6.8_

- [x] 9. 重写 OCR_Path
  - [x] 9.1 重写 `backend/agent/tools/ocr_pdf.py`
    - 保持 `ocr_pdf_pages(file_bytes)`、`OcrPathError`（含 `pageIndex`）、全部常量与 Textract 响应解析函数不变
    - 打开改用 `pypdfium2.PdfDocument`（失败、需要用户密码、空字节 → `OcrPathError(pageIndex=None)`，不调用 Textract）；`document[i]` 与 `page.render(scale=dpi / 72, draw_annots=True).to_pil()` 失败 → `OcrPathError(pageIndex=i)`；PNG 编码沿用 Pillow；`boundingBox=(0, 0, w, h)` 取 `page.get_size()`；返回前 `close()`
    - DPI 解析、10 MB 时以 150 DPI 重渲染一次、重试（1 s、2 s）、每页一个单元的逻辑保持不变
    - 注释按需求 14 清理，保留理由 (b)(c)(f)
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 10.1, 10.2, 10.8, 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 9.2 编写 OCR_Path 示例测试 `backend/tests/test_ocr_pdf.py`
    - 使用假 Textract 客户端，不访问网络
    - 覆盖：PNG 像素尺寸符合 DPI（±1）；PNG > 10 MB 时恰重渲染一次、DPI 已为 150 时不重渲染；无有效词的页产出空单元；页加载 / 光栅化失败抛 `OcrPathError(pageIndex=i)`；打开失败、需要用户密码、客户端创建失败抛 `OcrPathError(pageIndex=None)` 且未调用 Textract；限流 / 5xx 重试与耗尽；非重试错误不重试
    - _Requirements: 7.2, 7.3, 7.5, 7.7, 7.8, 7.10, 7.11_

  - [x] 9.3 编写 OCR 每页一个单元的属性测试
    - **Property 17: OCR 每页一个单元**
    - **Validates: Requirements 7.1, 7.9**
    - 文件 `test_ocr_pdf.py`

  - [x] 9.4 编写 DPI 配置解析的属性测试
    - **Property 18: DPI 配置解析**
    - **Validates: Requirements 7.6**
    - 文件 `test_ocr_pdf.py`

  - [x] 9.5 编写 Textract 响应拼接的属性测试
    - **Property 19: Textract 响应拼接**
    - **Validates: Requirements 7.12, 7.2**
    - 文件 `test_ocr_pdf.py`

  - [x] 9.6 编写页面几何与 PDFium 一致的属性测试
    - **Property 2: 页面几何与 PDFium 一致**
    - **Validates: Requirements 4.4, 7.3, 7.4, 5.2**
    - 文件 `test_pdf_geometry.py`

- [x] 10. Checkpoint - 提取与 OCR 路径
  - 运行完整后端测试，确认改写后的 `test_extraction_pdf.py` 与新增测试全部通过，且无断言被放宽。Ensure all tests pass, ask the user if questions arise.

- [x] 11. 重写 PDF_Redactor
  - [x] 11.1 实现区域解析与报告
    - 在 `backend/agent/tools/redaction_pdf.py` 中保持对外签名、`RedactionError`、`PdfRedactionReport`、`PdfRedactionResult`、前缀常量不变
    - `open_pdf` 失败 → `RedactionError`；`text:` 按需求 10.6 校验后用按页缓存的 `PageGlyphs` 按行求最小外接矩形，不满足条件时按比例估算一个区域；`ocr:` 按整词包围框产出区域；`image:` 解析并分组；未知前缀跳过
    - 报告：`textLayerRegions`、`ocrRegions` 按区域计数，`agreedRegions` 用 `rects_intersect` 判定
    - _Requirements: 5.3, 5.6, 5.7, 5.8, 5.9, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [x] 11.2 实现图片写回、页面改写与保存
    - 按 `image:{index}` 升序每图调用一次 `redact_image_bytes`，再 `write_back_image`；失败原因转为含 `image:{index}` 的 `RedactionError`
    - 对每个有区域的页面依次 `plan_deletions` → `apply_page_plan` → `remove_intersecting_annotations` → `draw_black_boxes`，`text:` 与 `ocr:` 区域合并处理不去重；有区域时 `scrub_document_strings`
    - `remove_unreferenced_resources()` 后以 `compress_streams=True`、`object_stream_mode=generate`、`encryption=False` 完整保存；无区域且无写回时只做规范化保存
    - `ContentEditError` 与替代库异常转为 `RedactionError`
    - _Requirements: 8.1, 8.2, 8.6, 8.7, 8.8, 8.10, 9.1, 9.2, 9.3, 9.6, 10.8_

  - [x] 11.3 实现脱敏后复核（失败即报错）
    - 有区域时：(a) `collect_glyphs_from_bytes` 复核含区域页面无字形与区域相交，保留字形序列与源页面不相交字形序列一致、矩形四边偏差 ≤ 0.01 pt；(b) pypdfium2 检查含区域页面上非生成字符的中心点不落在区域内（各边内缩 0.01 pt）；(c) 全部页面拼接文本去空白后不含任何被脱敏 PII
    - 任一失败抛 `RedactionError`，不返回字节；错误消息只含页索引、`unitRef`、对象编号与原因类别，不含页面文本或 PII
    - 注释按需求 14 清理，保留理由 (a)(d)(e)
    - _Requirements: 8.1, 8.4, 8.5, 8.11, 14.1, 14.2, 14.3, 14.4, 14.5_

  - [x] 11.4 改写 `backend/tests/test_redaction_pdf.py`
    - 构造器改用 `pdf_fixtures`；图片写回监视改为 `mock.patch.object(redaction_pdf, "write_back_image", wraps=…)`，保留原有 `call_count`、xref 参数、字节非空与像素断言；`_normalized_box_for` 与 `_displayed_image_black_fraction` 改用 pypdfium2
    - 测试名不变；每条脱敏断言保持同等或更严格，不放大容差、不减少 PII 或页面、不加 skip / xfail
    - 补充：CJK 文本脱敏、跨行 PII 脱敏、零匹配输出文本一致、越界偏移抛 `RedactionError`、损坏 PDF 抛 `RedactionError`（已有则保留）
    - _Requirements: 13.2, 13.3, 8.3, 8.4, 9.1, 9.2, 9.3, 10.6, 10.7_

  - [x] 11.5 编写泄漏测试 `backend/tests/test_pdf_leak.py`
    - 模块顶层直接 `import pypdf`，不使用 `importorskip`，导入失败即收集报错
    - 构造 2 页文档：第 1 页 `John Smith`（拉丁，`text:`）、`张三丰`（CJK + ToUnicode，`text:`）、跨行无空格 `123-45-` / `6789`（`text:`），与 `John Smith` 同行的 `Contact:`；第 2 页 `13800001111`，仅由 `ocr:` 覆盖，`ocrWords` 按 pypdfium2 字符框预设，不调用 Textract、不需要网络或凭证
    - 脱敏前用 pypdf 断言 4 个 PII（去空白）均可提取；脱敏后断言原样与去空格 / 制表符 / 换行后均不出现，`Contact:` 原样可提取
    - 用 pypdf 遍历输出全部对象：解压后的页面与 Form XObject 内容流、注释内容与外观流、AcroForm 字段值、信息字典、XMP，断言 PII（UTF-8 / UTF-16BE / 字面字符串）均不出现
    - 第二个子测试在源文档中植入信息字典标题、XMP、FreeText 注释、表单字段值与含 PII 的增量更新旧修订，断言输出中均不存在，且输出只有一个 `%%EOF`
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 8.3, 8.9, 8.10_

  - [x] 11.6 编写 `text:` 区域按行精确覆盖的属性测试
    - **Property 5: `text:` 区域按行精确覆盖**
    - **Validates: Requirements 5.6, 5.7**
    - 文件 `backend/tests/test_redaction_pdf_properties.py`

  - [x] 11.7 编写比例估算回退的属性测试
    - **Property 6: 比例估算回退**
    - **Validates: Requirements 5.8, 5.9**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.8 编写删除与保留精确划分的属性测试
    - **Property 8: 删除与保留的精确划分**
    - **Validates: Requirements 8.1, 8.5**
    - 文件 `test_redaction_pdf_properties.py`；含紧密行距生成用例（风险 R12）

  - [x] 11.9 编写独立工具提取不到 PII 的属性测试
    - **Property 9: 独立工具提取不到被脱敏的 PII**
    - **Validates: Requirements 8.3, 8.4**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.10 编写黑框覆盖与层级的属性测试
    - **Property 10: 黑框覆盖且位于最上层**
    - **Validates: Requirements 8.2, 5.3**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.11 编写结构保持与单一修订输出的属性测试
    - **Property 11: 结构保持与单一修订输出**
    - **Validates: Requirements 8.7, 8.10, 9.5**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.12 编写零区域等价输出的属性测试
    - **Property 12: 零区域时输出与源文档等价**
    - **Validates: Requirements 8.6**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.13 编写图片写回的属性测试
    - **Property 13: 图片写回**
    - **Validates: Requirements 9.1, 9.2, 9.4, 9.7, 9.8**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.14 编写报告计数的属性测试
    - **Property 14: 报告计数**
    - **Validates: Requirements 10.3, 10.4, 10.5, 8.8**
    - 文件 `test_redaction_pdf_properties.py`

  - [x] 11.15 编写脱敏侧输入错误与无效引用的属性测试
    - **Property 15: 输入错误只以模块自有异常抛出（脱敏与 OCR 部分）**
    - **Property 16: 无效引用一律报错**
    - **Validates: Requirements 10.7, 10.8, 10.6, 9.6**
    - 文件 `test_redaction_pdf_properties.py`；两个属性各写一个测试函数

- [x] 12. 结构校验与其余测试改写
  - [x] 12.1 更新 `structure_verification.py` 注释并改写其测试
    - `backend/agent/tools/structure_verification.py` 只改注释，改为描述 `pdf_glyphs` 的字符遍历
    - `backend/tests/test_structure_verification.py` 的 `_build_multi_page_pdf` 改用 `pdf_fixtures`，测试名与断言不变
    - _Requirements: 8.7, 13.2, 13.3, 1.5_

  - [x] 12.2 改写受环境变量保护的测试与桩模块名单
    - `test_patti_regression.py`：渲染与 `_black_fraction` 改用 pypdfium2，环境变量保护与跳过条件不变
    - `test_llm_cjk_recall.py`："缺库即收集失败"的导入改为 `pypdfium2`、`pdfminer`、`pikepdf`，保护条件与参数化数量不变
    - `test_regroup_and_merge.py`：桩模块名单换成 `pdfminer`、`pikepdf`、`pypdfium2`（仅不可导入时桩替）
    - 三个文件中不再出现已移除库标识
    - _Requirements: 13.4, 13.7, 1.5, 1.7_

  - [x] 12.3 编写对外接口兼容测试 `backend/tests/test_pdf_public_interface.py`
    - 用 `inspect.signature` 比对四个对外函数的参数名、顺序与默认值；检查三个异常类的定义模块与类名；`OcrPathError` 携带 `pageIndex`
    - _Requirements: 10.1, 10.2_

  - [x] 12.4 在 design.md 中核对等价改写映射表
    - 按 8.2、11.4、12.1、12.2 的实际改动更新"等价改写映射表"，每个被删除、重命名或合并的原测试至少对应一个替代测试，并更新"被合并测试数量"
    - _Requirements: 13.6, 13.1_

- [x] 13. Checkpoint - 脱敏路径与测试改写
  - 运行完整后端测试，确认 0 failed、0 errors，`test_pdf_leak.py` 通过，skipped 仅为环境变量保护的测试。Ensure all tests pass, ask the user if questions arise.

- [x] 14. 移除旧库与更新 bundle 脚本
  - [x] 14.1 从依赖清单中删除已移除的 PDF 库并重建虚拟环境
    - 删除 `requirements.txt` 与 `agent-runtime-requirements.txt` 中的旧库条目，改写清单注释（删除旧 wheel 与 abi3 说明）
    - 删除并重建 `backend/.venv`（python3.12），重新 `pip install -r requirements-dev.txt`，确认 `pip list` 中无旧库发行包、两个旧导入名均抛 `ModuleNotFoundError`
    - _Requirements: 1.1, 1.2, 1.7_

  - [x] 14.2 更新 `backend/scripts/build_agent_bundle.py`
    - `PLATFORMS` 改为 `manylinux2014_aarch64` 与 `manylinux_2_28_aarch64`，删除 `manylinux_2_17_aarch64`；注释写明依赖每个取值的 wheel 文件名，以及不传 `--abi` 的原因（cryptography abi3 wheel、pypdfium2 `py3-none`）
    - 新增 `check_no_removed_library()`：在 `deps/` 与 `package_clean/` 中查找旧库名称的 wheel、顶层目录、模块文件、`*.dist-info`，发现即非零退出；被检索的名称用字符串拼接构造，源码中不出现字面量
    - 新增断言：`deps/` 中无 `.tar.gz` / `.zip`；`package_clean/` 中无 `.pyd` / `.dll`
    - `print_summary` 的关键 wheel 前缀与导入目录名单换成新库，并列出 `deps/` 中每个 wheel 的名称与版本
    - _Requirements: 1.3, 1.4, 3.2, 3.3, 3.4, 3.5, 3.6, 2.7_

  - [x] 14.3 更新 `backend/scripts/sync_agent_bundle.py` 注释
    - 只改注释中列举的第三方包名，删除旧库名称
    - _Requirements: 1.5_

  - [x] 14.4 编写 bundle 脚本测试 `backend/tests/test_build_agent_bundle.py`
    - `check_no_removed_library` 对植入旧库名称（拼接构造）的临时目录树失败、对干净目录通过
    - 下载阶段 pip 返回非零时抛错且不调用安装与清理阶段（mock `subprocess`）
    - `PLATFORMS` 恰为两个取值；sdist 断言对含 `.tar.gz` 的目录失败
    - _Requirements: 1.3, 1.4, 3.2, 3.3, 3.4_

- [x] 15. 许可证文件与 README
  - [x] 15.1 新增仓库根目录 `THIRD-PARTY-LICENSES`
    - 直接依赖总表：三个清单中每个直接依赖的名称、与清单相同的精确版本、SPDX 标识、所在清单
    - 捆绑原生库表：pypdfium2（表 D-3）、pikepdf（表 D-2）、Pillow（表 D-4，逐节核对 LICENSE）
    - Weak_Copyleft 专节：pikepdf 10.17.0 与 hypothesis 6.112.1 各写版权持有人、"以未修改的官方 PyPI wheel 形式使用"、MPL-2.0 全文与版权声明、与清单版本相同的 sdist 与仓库 tag 链接
    - 一致性说明（增删依赖或改版本时同一次变更更新）
    - _Requirements: 2.5, 2.6, 2.10, 2.14_

  - [x] 15.2 更新根目录 `README.md`
    - 依赖段落列出新 PDF 依赖的包名、精确版本与许可证；MPL-2.0 说明（pikepdf 与 hypothesis 以未修改形式使用，扫描工具标记时可依据公司弱 copyleft 分发政策自行关闭工单）
    - bundle 段落写明 `python scripts/build_agent_bundle.py`、目标 Linux ARM64 / CPython 3.12、必须在 Linux 主机运行，替换旧的平台与 `--abi` 说明
    - 删除全部旧库名称
    - _Requirements: 2.11, 3.8, 1.5_

  - [x] 15.3 编写依赖合规测试 `backend/tests/test_dependency_compliance.py`
    - 清单无旧库条目（名称拼接构造）；新增直接依赖为 `==` 精确版本且跨文件一致；THIRD-PARTY-LICENSES 与清单直接依赖名称集合、版本完全一致；无 AGPL / GPL / LGPL 许可证依赖；Weak_Copyleft 依赖不以 URL / 路径 / VCS 方式安装；MPL 专节含版本一致的源码链接与许可证全文；README 含 MPL 说明、依赖版本与构建说明；捆绑原生库已列出
    - _Requirements: 1.1, 2.2, 2.5, 2.6, 2.8, 2.9, 2.10, 2.11, 2.14, 3.8_

- [x] 16. 全仓库残留引用清理
  - [x] 16.1 清理后端其余源码与测试中的旧库引用
    - 只改注释 / docstring：`agent/main.py`、`agent/tools/extraction_docx.py`、`agent/tools/redaction_docx.py`、`shared/models.py`、`lambdas/shared/models.py`（`boundingBox`、`pageIndex`、`sourceLocation`、`OcrWord.boundingBox` 改为以 Page_Coordinates 与 Image_Handle 描述）
    - 只改注释：`test_guardrails_pii_detect.py`、`test_llm_pii_detect.py`、`test_orchestration.py`、`test_redaction_image.py`
    - DOCX 模块与 `frontend` 不做任何非注释改动
    - _Requirements: 1.5, 10.9_

  - [x] 16.2 更新其他规格文档中的旧库描述
    - `agent-log-delivery`（design、tasks）、`in-image-redaction`（requirements、design、tasks）、`llm-cjk-pii-recall`（design、tasks）、`llm-image-ocr`（design、tasks）、`pdf-dual-path-pii-detection`（design、tasks）、`pii-document-redaction-poc`（design、tasks）
    - 把描述旧库 API、行为或依赖的段落改为描述实际调用的库与函数（`pdf_glyphs.collect_page_glyphs`、`pypdfium2.PdfPage.render`、`pdf_content_edit`、`pdf_images.write_back_image` 等），不保留旧 API 名称
    - _Requirements: 1.5, 1.8_

  - [x] 16.3 编写已移除库检查测试 `backend/tests/test_removed_library_absent.py`
    - 被检索的名称全部以拼接方式构造
    - 已安装发行包中无旧库，两个旧导入名抛 `ModuleNotFoundError`（需求 1.2）
    - 不区分大小写检索 `backend/` 源码、测试、requirements、脚本，根目录与各子目录 README，`.kiro/specs/*`；排除 `.git/`、虚拟环境、`agent_bundle/`、`node_modules/`；只豁免 `.kiro/specs/pymupdf-replacement/` 下的 `requirements.md`、`design.md`、`tasks.md`（需求 1.5、1.6）
    - 三个 PDF 模块的注释与 docstring 中无任务 / 需求编号引用、无旧库标识与旧专有 API 名称（需求 14.1、14.4）
    - _Requirements: 1.2, 1.5, 1.6, 14.1, 14.4_

- [x] 17. Checkpoint - 完整后端与前端检查
  - 在已重建、不含旧库的 `backend/.venv` 中运行 `.venv/bin/python -m pytest tests -q -p no:cacheprovider -rs`：0 failed、0 errors、0 收集导入错误，passed ≥ 397 + 新增测试数 − 被合并测试数，skipped ≤ 5 且只因环境变量保护
  - 在 `frontend` 下运行 `npx tsc -b`（退出码 0）与 `npx vitest --run`（0 failed、≥ 68 passed）
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Linux ARM64 手动验证
  - [x] 18.1 准备并记录 Linux ARM64 上的手动验证步骤
    - 当前主机为 x86_64，以下两项只能由用户在 Linux ARM64 / CPython 3.12 主机上手动执行：完整运行 `python scripts/build_agent_bundle.py`（检查退出码 0、`deps/` 无 sdist、`package_clean/` 无旧库条目与 `.pyd` / `.dll`）；以 `package_clean/` 为唯一第三方路径导入 `agent.tools.extraction_pdf`、`agent.tools.ocr_pdf`、`agent.tools.redaction_pdf`（零 `ImportError`）
    - 把上述命令与预期结果写入 design.md "未核实事项"一节，标明待用户在 ARM64 主机执行；不在本主机写入 `backend/agent_bundle/`
    - _Requirements: 3.2, 3.6, 3.7_

- [x] 19. Final checkpoint - 残留标识零匹配
  - 按需求 1.5 的范围对已移除库标识做不区分大小写检索，除 1.6 豁免的三份文档外结果为零；`test_removed_library_absent.py` 通过
  - 再次确认完整后端测试与前端检查通过；不执行 git commit / push，不执行任何 AWS 部署
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标 `*` 的子任务为可选测试任务，可为加快 MVP 跳过；泄漏测试（11.5）、核心脱敏测试（6.6、11.4）、提取与 OCR 示例测试、合规与残留检查测试均为必做。
- 旧库条目在任务 14.1 才从清单删除，此前现有测试可继续运行；任务 17 在不含旧库的全新虚拟环境中验证。
- 测试中构造 PDF 只用 pypdf（BSD-3-Clause）或原始字节；泄漏测试的 Independent_Text_Extractor 为 pypdf，与脱敏所用 pdfminer.six、pikepdf、pypdfium2 都不同。
- 改写测试时不得放宽任何脱敏断言（不放大容差、不减少 PII 或页面、不改相等为包含、不加 skip / xfail）。
- 检查脚本与测试中的已移除库名称一律用拼接方式构造，避免违反需求 1.5 的零匹配。
- 不执行 git commit / push；不执行 AWS 部署（新旧 CDK 栈同名 `PiiDocumentRedactionPocStack`，有覆盖风险）。这些操作需另行征得用户同意。
- 需求 3.7 的 aarch64 导入与完整 bundle 构建只能在 Linux ARM64 主机上手动验证（任务 18.1）。
- 属性测试使用现有开发依赖 hypothesis 6.112.1，`@settings(max_examples=100, deadline=None)`，每个测试上方标注 `# Feature: pymupdf-replacement, Property {N}: {标题}`。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "2.1"] },
    { "id": 3, "tasks": ["2.2", "3.1"] },
    { "id": 4, "tasks": ["2.3", "3.2", "4.1"] },
    { "id": 5, "tasks": ["3.3", "4.2"] },
    { "id": 6, "tasks": ["4.3"] },
    { "id": 7, "tasks": ["4.4", "6.1", "7.1"] },
    { "id": 8, "tasks": ["4.5", "6.2", "7.2"] },
    { "id": 9, "tasks": ["6.3", "7.3", "8.1"] },
    { "id": 10, "tasks": ["6.4", "8.2", "9.1"] },
    { "id": 11, "tasks": ["6.5", "8.3", "9.2"] },
    { "id": 12, "tasks": ["6.6", "8.4", "9.3", "9.6"] },
    { "id": 13, "tasks": ["8.5", "9.4", "11.1"] },
    { "id": 14, "tasks": ["8.6", "9.5", "11.2"] },
    { "id": 15, "tasks": ["11.3"] },
    { "id": 16, "tasks": ["11.4", "11.5", "12.1", "12.2", "12.3", "12.4"] },
    { "id": 17, "tasks": ["11.6", "14.2", "14.3", "16.1", "16.2"] },
    { "id": 18, "tasks": ["11.7", "14.4", "15.2"] },
    { "id": 19, "tasks": ["11.8"] },
    { "id": 20, "tasks": ["11.9"] },
    { "id": 21, "tasks": ["11.10"] },
    { "id": 22, "tasks": ["11.11"] },
    { "id": 23, "tasks": ["11.12"] },
    { "id": 24, "tasks": ["11.13"] },
    { "id": 25, "tasks": ["11.14"] },
    { "id": 26, "tasks": ["11.15"] },
    { "id": 27, "tasks": ["14.1"] },
    { "id": 28, "tasks": ["15.1"] },
    { "id": 29, "tasks": ["15.3", "16.3"] },
    { "id": 30, "tasks": ["18.1"] }
  ]
}
```
