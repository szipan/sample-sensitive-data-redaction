# PII 文档脱敏 PoC (Sensitive Data Redaction PoC)

上传 `.pdf` / `.docx`,自动识别并遮盖其中的敏感信息(PII),包括文档正文和内嵌图片中的文字。

- **后端**: AWS Serverless(API Gateway + Lambda + DynamoDB + S3 + SQS),核心是跑在 **Amazon Bedrock AgentCore Runtime** 上的 Redaction Agent。用 AWS CDK (Python) 部署。
- **前端**: React + TypeScript + Vite,通过 HTTPS 调用后端 API。
- **流水线**(固定顺序、确定性,刻意不让 LLM 规划步骤): extract → 图片 OCR → 页面 OCR(.pdf) → match(正则 + Bedrock Guardrails + LLM 三个检测源) → redact → verify → store → update。入口在 `backend/agent/orchestration.py` 的 `handle_job`。

更多背景见 `项目介绍-PII文档脱敏.md`、`HANDOFF.md`、`docs/流水线-pipeline.drawio`。

## 目录结构

```
backend/
  app.py                    CDK 入口(Stack: PiiDocumentRedactionPocStack)
  infra/redaction_stack.py  所有 AWS 资源定义
  lambdas/                  API / 调度类 Lambda
  agent/                    AgentCore Runtime 里运行的 Redaction Agent 与各工具
  shared/                   后端共用模型与工具
  scripts/                  agent bundle 构建/同步脚本
  agent_bundle/             构建产物(gitignore),CDK 部署其中的 package_clean/
  tests/                    后端测试
frontend/                   React 前端
```

## 部署前置条件

| 项目 | 要求 |
| --- | --- |
| 操作系统 | **Linux**(bundle 构建必须在 Linux 主机运行)。Windows 上跑不通(见"常见问题") |
| Python | 3.12(`python3.12` 在 PATH 中) |
| Node.js | `^22.22.2` 或 `^24.15.0`(见 `frontend/package.json` 的 engines) |
| AWS CDK CLI | `npm install -g aws-cdk` |
| AWS CLI | 已配置有权限创建 IAM / Lambda / S3 / DynamoDB / SQS / API Gateway / Bedrock / AgentCore / CloudFormation 的凭证 |
| Region | **`us-west-2`** |
| CDK bootstrap | 目标账号 + region 做过一次 `cdk bootstrap` |
| Bedrock 模型访问 | 在 us-west-2 开通 Anthropic Claude Haiku 4.5 和 Claude Sonnet 4.5(使用 `us.` 跨区推理配置) |

> **务必先固定 region。** CDK stack 没有硬编码 region,而是取当前环境的 region。如果 shell 里残留 `AWS_DEFAULT_REGION=us-east-1`,整套资源会被建到错误的 region。每次部署都先执行:
>
> ```bash
> export AWS_REGION=us-west-2
> export AWS_DEFAULT_REGION=us-west-2
> aws sts get-caller-identity   # 确认账号正确
> ```

## 一、后端部署

所有命令在 `backend/` 目录下执行。

### 1. 创建虚拟环境并安装依赖(仅首次)

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

`requirements.txt` 含 CDK 依赖(`aws-cdk-lib`、`constructs`),以及 `python-docx`、`strands-agents`、`bedrock-agentcore` 和下面列出的 PDF 依赖等。

#### PDF 依赖与许可证

版本以 `backend/requirements.txt` / `requirements-dev.txt` / `agent-runtime-requirements.txt` 为准。完整的第三方许可证文本见仓库根目录的 `THIRD-PARTY-LICENSES`。

| 包 | 精确版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| `pdfminer.six` | 20260107 | MIT | PDF 文本与字符坐标提取 |
| `pikepdf` | 10.17.0 | MPL-2.0(wheel 内捆绑 qpdf,Apache-2.0 等) | PDF 结构读写与内容流脱敏 |
| `pypdfium2` | 5.14.0 | BSD-3-Clause(PDFium 及其捆绑组件的许可证见 `THIRD-PARTY-LICENSES`) | PDF 页面渲染 |
| `Pillow` | 11.0.0 | MIT-CMU(HPND) | 图片解码、遮盖与重新编码 |
| `pypdf` | 6.21.4 | BSD-3-Clause | 仅开发/测试使用,不进入 bundle |
| `hypothesis` | 6.112.1 | MPL-2.0 | 仅开发/测试使用,不进入 bundle |

关于 MPL-2.0: `pikepdf` 与 `hypothesis` 均以**未修改**的形式使用(只从 PyPI 安装,不改动其源码,也不重新分发修改版),MPL-2.0 的文件级 copyleft 义务不会扩散到本项目代码。如果许可证扫描工具把 MPL-2.0 标记为弱 copyleft,可依据公司的弱 copyleft 分发政策自行关闭对应工单。

### 2. 跑测试(可选,建议)

```bash
.venv/bin/python -m pytest tests -q
```

`test_llm_cjk_recall.py` 是需要真实模型调用的测试,默认跳过。

### 3. 构建 Agent Bundle

AgentCore Runtime 部署的是 `backend/agent_bundle/package_clean/`。它由第三方依赖(Linux ARM64 / CPython 3.12 的 wheel)加上本仓库的 `agent/`、`shared/`、根 `main.py` 组成。**这个目录不入库,每次部署前、以及每次修改 `agent/`、`shared/` 下任何代码后,都必须重新构建:**

```bash
python scripts/build_agent_bundle.py   # 在 backend/ 目录,使用 .venv 的 python
```

脚本分三步(幂等,可重复运行): 下载 aarch64 wheel → 安装到 `package/` → 去掉 `*.dist-info`/`bin`/`__pycache__` 得到 `package_clean/`,最后用 `scripts/sync_agent_bundle.py` 覆盖 first-party 代码。运行时依赖在 `agent-runtime-requirements.txt`(**不要**把 `aws-cdk-lib` 放进去)。

构建完成后可快速核对:

```bash
ls agent_bundle/package_clean | head
find agent_bundle/package_clean \( -name '*.pyd' -o -name '*.dll' \) | wc -l   # 应为 0
cmp agent_bundle/package_clean/main.py agent/main.py && echo OK                # 应为 OK
```

> 构建目标是 Linux ARM64 / CPython 3.12,脚本必须在 **Linux 主机**上运行(Windows 不支持,macOS 请改用 Linux 主机或容器)。脚本使用平台标签 `manylinux2014_aarch64` 和 `manylinux_2_28_aarch64`,**不传** `--abi`: `cryptography` 的 abi3 wheel 与 `pypdfium2` 的 `py3-none` wheel 需要靠平台标签匹配,显式指定 `--abi` 会让它们匹配不上。不要手动"简化"脚本里的 pip 参数。

### 4. 部署

```bash
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2
cdk diff                          # 先看变更,首次部署会列出全部新建资源
cdk deploy --require-approval never
```

要点:

- 必须在 `backend/` 目录运行,CDK 用相对路径 `agent_bundle/package_clean` 取 bundle。
- 对**已存在**的环境应该是 **UPDATE**(Lambda 代码、AgentCore Runtime artifact 更新)。如果 `cdk diff` 显示几十个资源全部 `[+]` 新建,先检查 region 和账号是否正确,再继续。
- 部署完成后确认 stack 状态:

```bash
aws cloudformation describe-stacks --region us-west-2 \
  --stack-name PiiDocumentRedactionPocStack \
  --query 'Stacks[0].StackStatus' --output text      # UPDATE_COMPLETE / CREATE_COMPLETE
```

### 5. 获取 API 地址

Stack 没有定义 CloudFormation Output,用下面的命令查 HTTP API 的地址:

```bash
aws apigatewayv2 get-apis --region us-west-2 \
  --query "Items[?Name=='PiiDocumentRedactionPocApi'].ApiEndpoint" --output text
```

返回形如 `https://xxxxxxxx.execute-api.us-west-2.amazonaws.com`,前端构建时要用。

### 6. CORS 允许的前端来源

API 的 CORS 白名单写在 `backend/infra/redaction_stack.py` 的 `cors_preflight.allow_origins`(默认含 `http://localhost:5173`)。**如果前端部署在其他域名/IP,必须把该来源加进去并重新 `cdk deploy`,否则浏览器会因 CORS 报错。**

### 7. 端到端验证

```bash
BASE=https://<api-id>.execute-api.us-west-2.amazonaws.com

# 上传(字段名 file,支持 .pdf / .docx),返回 {"jobId": ..., "status": "processing"}
curl -s -X POST "$BASE/jobs" -F "file=@sample.docx;filename=sample.docx"

# 轮询直到 status 变为 completed(或 failed)
curl -s "$BASE/jobs/<jobId>"

# 下载脱敏后的文件
curl -s -o redacted.docx "$BASE/jobs/<jobId>/download"
```

确认 Agent 以 Strands agent 形态运行(CloudWatch Logs):

```bash
aws logs describe-log-groups --region us-west-2 \
  --log-group-name-prefix /aws/bedrock-agentcore/runtimes/pii_redaction_agent
# 在对应日志组里搜索: "Redaction Strands agent (12 registered tools) handling invocation"
```

### 处理耗时参考

带大量内嵌图片的文档较慢: 每张图片需要一次多模态 OCR(约 8–17 秒),每个文本段落需要一次 LLM 和 Guardrails 检测。这些调用现在是**并发**执行的,可用 Agent 运行时的环境变量调整并发度:

| 环境变量 | 默认 | 作用 |
| --- | --- | --- |
| `OCR_MAX_WORKERS` | 4 | 图片 OCR 并发数 |
| `LLM_MAX_WORKERS` | 8 | 逐段落 LLM PII 检测并发数 |
| `GUARDRAILS_MAX_WORKERS` | 8 | 逐段落 Guardrails 检测并发数 |

参考: 一个 1.3MB、17 张图片、约 576 段落的 .docx,并发改造后约 2 分钟完成。并发调高可能触发 Bedrock 限流(调用内置了有限次重试)。

### 检测范围与误报控制

只把**自然人**的信息当作 PII:姓名、联系方式、具体到街道/楼栋的地址、出生日期/年龄、证件号、银行卡/账户号、凭证、车牌等。公司/机构/产品/云服务名称、单独的国家或城市、可用区、网站 URL、硬件规格等**不算 PII**。如果某个客户名称也需要隐藏,请在前端"规则管理"里加一条关键字规则,不要靠模型识别。

误报由以下几层控制:

| 层 | 位置 | 作用 |
| --- | --- | --- |
| LLM 提示词 v3 | `agent/tools/llm_pii_detect.py` 的 `PROMPT_TEMPLATE` | 明确 PII 只指自然人,并列出不算 PII 的类别 |
| 姓名/地址来源默认"仅用 LLM" | `shared/models.py` 的 `DETECTION_SOURCE_DEFAULT` | Guardrails 在中文商务文档里常把公司名、产品名判成 NAME,把"美国""AZ"判成 ADDRESS。上传时仍可选"两者都用" |
| 格式校验 | `agent/tools/span_validation.py` | 丢弃格式不符的模型结果:URL 一律不报;MAC/IP/邮箱/电话/卡号等必须符合格式;单独一个数字不算 AGE(除非紧挨"岁""年龄");超长片段丢弃。用户自定义的正则规则不受影响 |
| 审核列表按文本合并 | `agent/tools/detection_results.py` | 同一个值只显示一行,行内列出所有来源和类型;勾选或取消对该值的所有出现位置生效 |

调整这些规则后,请用含真实 PII 的样例文档和不含 PII 的技术文档各测一次,确认召回没有下降、误报没有回升。

## 二、前端部署

所有命令在 `frontend/` 目录下执行。

```bash
cd frontend
npm ci
```

### 本地开发

在 `frontend/.env.local`(已 gitignore)里写入后端地址,然后启动:

```bash
echo 'VITE_API_BASE_URL=https://<api-id>.execute-api.us-west-2.amazonaws.com' > .env.local
npm run dev            # 默认 http://localhost:5173
```

地址必须是 `https://`,非 HTTPS 会在构造时被拒绝。生产构建中缺少 `VITE_API_BASE_URL` 会直接失败(故意不回退到 localhost)。**不要把真实 API 地址提交进 `.env.*` 文件。**

### 生产构建

```bash
VITE_API_BASE_URL=https://<api-id>.execute-api.us-west-2.amazonaws.com npm run build
```

产物在 `frontend/dist/`,是纯静态文件,可放到任意静态托管(S3 + CloudFront、Nginx 等)。托管后的访问来源要加入后端 CORS 白名单(见上文"CORS")。

### 校验

```bash
npm run typecheck
npm run lint
npm test
```

### 界面语言(中文 / English)

顶部栏右侧的下拉框切换界面语言,选择会保存在浏览器 localStorage(键 `pii-redaction.lang`),默认简体中文。切换时页面标题和 `<html lang>` 也会同步。

- 所有界面文案都在 `frontend/src/i18n/index.tsx` 的词典里,每个 key 同时写中文和英文,组件里用 `const { t } = useI18n()` 再 `t('key')` 取文案,变量用 `{name}` 占位,如 `t('review.selectRow', { value })`。key 是类型检查过的,拼错会编译失败。
- 新增文案:在词典里加一条 `{ zh, en }`,中英文的 `{占位符}` 必须一致。新增语言:给 `Lang` 加一个值、`LANGUAGES` 加一项,并给每条词典补上该语言。
- 不翻译的内容:文件名、检测到的原文及其上下文、Debug 模式里的类型标识、正则表达式。
- 任务失败原因:后端不再把面向用户的中文/英文句子写进任务记录,而是写 `errorCode`(以及可选的 `errorParams`,如 OCR 失败的页码 `{"page": 3}`),`GET /jobs/{jobId}` 一并返回,前端按 `job.error.<CODE>` 翻译显示。`errorMessage` 仍保存英文诊断信息供排查,**不会**显示给用户。现有的码在 `backend/agent/tools/job_status_update.py`(`REVIEW_TIMEOUT`、`STRUCTURE_VERIFICATION_FAILED`、`OCR_PAGE_FAILED`、`OCR_FAILED`、`PROCESSING_FAILED`)和 `upload_handler.py`(`STORAGE_WRITE_FAILED`)。新增失败原因时:后端用新的 `error_code` 调 `update_job_status`,并在 `frontend/src/i18n/index.tsx` 加一条 `job.error.<CODE>`。前端不认识的码(或没有码的旧任务)会回退显示 `errorMessage`。
- 没有套 `LanguageProvider` 时(例如单独渲染组件做测试)按中文显示。

### 前端超时说明

- 单个 API 请求超时: 30 秒(`src/api/client.ts` 的 `DEFAULT_TIMEOUT_MS`)。
- 处理超时: Job 一直处于 processing 超过 **20 分钟**,界面提示"处理超时,请点击返回重试"(`src/views/UploadView.tsx` 的 `PROCESSING_TIMEOUT_MS`)。

## 常见问题

**构建 bundle 时报 `ResolutionImpossible` / pywin32**: 你在 Windows 上。`strands-agents` → `mcp` 依赖 `pywin32; sys_platform == "win32"`,pip 在 Windows 上解析时会去找没有 aarch64 wheel 的 pywin32。请换到 Linux 主机(或 WSL)构建。

**资源建到了错误的 region**: 见"部署前置条件"。需要在错误的 region 里 `cdk destroy` 清理,再在 us-west-2 重新部署。

**任务一直是 processing,Agent 日志里也没有这个 jobId**: 说明任务还没派发到 Agent。`JobDispatcher` Lambda 从 DynamoDB Stream 读新任务并调用 `InvokeAgentRuntime`,这个调用要等 Agent 入口函数返回才结束。所以 `agent/main.py` 的入口必须立刻返回,把流水线放到后台线程里跑(并用 `app.add_async_task` 保持会话)。如果入口改回同步执行,审核模式的任务会让 Lambda 等满 300 秒超时,Stream 重试整批记录,后面上传的任务全部排队卡住。排查时看 `JobDispatcher` 日志里的 `Duration`,正常应在几秒内。`handle_job` 只处理状态为 `processing` 的任务,重复派发会被跳过。

**前端提示"处理超时"**: 任务确实处理很久,或后端卡住。先用 `GET /jobs/{jobId}` 看状态,再查 Agent 的 CloudWatch 日志。

**浏览器报 CORS 错误**: 前端来源不在 `allow_origins` 里,见"CORS 允许的前端来源"。

**修改了 `agent/` 或 `shared/` 但线上行为没变**: 没有重新构建 bundle。先运行 `scripts/build_agent_bundle.py` 再 `cdk deploy`。

## 设计约束(修改代码前请阅读)

1. `handle_job` 必须保持确定性,不要改成由 Strands agent 的推理循环去调用工具。Strands agent 在运行时是装饰性的,这是有意为之。
2. `agent/tools/llm_pii_detect.py` 不能 import strands(`test_2_16` 会强制检查)。Strands 相关代码只放在 `agent/redaction_agent.py` 和 `main.py` 的 `invoke` 入口。
3. `agent/runtime.py` 的 `process_invocation` 不 import strands / bedrock_agentcore。
4. LLM 检测源只增不减,且绝不抛异常。
5. 部署 region 固定为 `us-west-2`。

## 关键常量

- `LLM_PII_MODEL_ID = us.anthropic.claude-haiku-4-5-20251001-v1:0`(只发 `temperature=0.0`,不发 `topP`)
- `IMAGE_OCR_MODEL_ID = us.anthropic.claude-sonnet-4-5-20250929-v1:0`
- 两者都定义在 `backend/infra/redaction_stack.py`。
