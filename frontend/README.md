# React + TypeScript + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Oxc](https://oxc.rs)
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the Oxlint configuration

If you are developing a production application, we recommend enabling type-aware lint rules by installing `oxlint-tsgolint` and editing `.oxlintrc.json`:

```json
{
  "$schema": "./node_modules/oxlint/configuration_schema.json",
  "plugins": ["react", "typescript", "oxc"],
  "options": {
    "typeAware": true
  },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }]
  }
}
```

See the [Oxlint rules documentation](https://oxc.rs/docs/guide/usage/linter/rules) for the full list of rules and categories.

## 部署与后端 API 配置（Requirements 6.1 / 6.3）

前端与后端是**可分别独立部署**的两个组件。前端构建产物是一堆静态资源（`dist/`），
托管在任意静态托管方案上（默认假设 S3 + CloudFront）。前端与后端之间唯一的耦合，
是构建时通过环境变量注入的后端 API 基址 `VITE_API_BASE_URL`——前端源码**不 import
`backend/`、不共享构建步骤**，因此后端内部实现的任何变更都不需要前端重新构建，反之亦然。

### 环境变量

| 变量 | 说明 |
|---|---|
| `VITE_API_BASE_URL` | 已部署后端 API（API Gateway）的基址。**必须是 `https://`**；非 HTTPS 值会在构造 API 客户端时被拒绝（见 `src/api/client.ts` 的 `toHttpsBaseUrl`）。 |

按环境区分的配置文件（Vite 约定，仅 `VITE_` 前缀变量会进入前端产物）：

- `.env.example`：提交到仓库，用占位符记录所需变量，**不含真实地址**。
- `.env.development`：开发模式（`vite dev`）默认值，指向本地/端口转发的后端。
- `.env.production`：生产模式（`vite build`）配置；**不硬编码真实后端地址**，
  变量留空，要求构建时由部署环境提供。
- `.env.local` / `.env.*.local`：机器级覆盖，已被 `.gitignore` 忽略，用于放真实地址。

### 针对某个已部署后端构建前端

```bash
# 生产构建：在构建时注入已部署后端的 HTTPS 基址
VITE_API_BASE_URL=https://your-api-id.execute-api.your-region.amazonaws.com npm run build

# 产物位于 frontend/dist/，将其部署到静态托管（S3 + CloudFront 等）即可
```

shell/CI 提供的 `VITE_API_BASE_URL` 优先于 `.env.production` 文件中的值。

### 生产环境缺失变量的行为（无回退，快速失败）

生产构建（`import.meta.env.PROD`）下若 `VITE_API_BASE_URL` 缺失或为空，
`resolveApiBaseUrl`（`src/api/apiConfig.ts`）会**直接抛错**，而**不会**回退到
`https://localhost:4000` 或任何 HTTP 地址。这样可避免配置错误的生产部署静默指向
错误的后端（Requirements 6.1、6.3）。便捷的 `localhost` 回退**仅在开发模式**生效。
