---
title: "自定义模型服务接入指南"
aliases: ["custom-model-service", "custom-model-guide", "minimax-guide"]
tags: [custom-model, codex, droid, ccr, minimax, openai-responses, claude-console]
created: 2026-05-07
updated: 2026-05-07
status: active
---

# 自定义模型服务接入指南

本文档说明如何把你自己的模型服务挂到 CRS 里，并让 Codex、Droid CLI、Claude Code / CCR 这些客户端直接使用。

适用场景：

- 你有一个兼容 OpenAI Responses API 的模型服务
- 你有一个兼容 OpenAI Chat Completions 的模型服务
- 你有一个兼容 Anthropic Messages API 的模型服务
- 你想通过 CRS 统一做 API Key、调度、权限和统计

## 先选接入方式

CRS 里已经有四条比较稳定的接入路径：

| 目标客户端 | 推荐 CRS 账户类型 | 客户端访问入口 | 适合的上游协议 |
|---|---|---|---|
| Codex CLI | `OpenAI-Responses` | `/openai` | OpenAI Responses API |
| Droid CLI 自定义模型 | `Droid` / 现有 CRS 统一入口 | `/droid/claude`、`/droid/openai`、`/droid/comm/v1/` | Anthropic / OpenAI Responses / Chat Completions |
| Claude Code 通过 CCR | `CCR` | CCR -> CRS -> 你的模型服务 | Anthropic 风格路由，或由 CCR 做模型转换 |
| Claude 兼容请求直接走 MiniMax | `MiniMax` | 标准 Claude 接口 + `minimax,` 模型前缀 | MiniMax Anthropic 兼容接口 |

简单判断：

- 如果你的上游本身就是 OpenAI 风格，优先走 `OpenAI-Responses`
- 如果你的客户端是 Droid CLI，需要在本地“添加自定义模型”，优先走 `/droid/*`
- 如果你的客户端是 Claude Code，但上游不是 Claude 原生模型，优先参考 CCR 方案
- 如果你的上游就是 MiniMax 的 Anthropic 兼容接口，优先走 `MiniMax`，不要挂到 `CCR` 下面

## 方案一：给 Codex 接一个自定义模型服务

这是最省事的方式，适合 OpenAI 兼容上游。

### 1. 在 CRS 后台创建 `OpenAI-Responses` 账户

后台字段建议这样填：

| 字段 | 说明 |
|---|---|
| `Base API` | 你的上游根地址，例如 `https://api.example.com/v1` 或 `https://api.example.com` |
| `API Key` | 上游服务的密钥 |
| `Provider Endpoint` | 一般选 `responses`；如果上游路径已经完整兼容 CRS 转发路径，再考虑 `auto` |
| `User-Agent` | 可选；上游有白名单要求时再填 |

补充说明：

- CRS 会把客户端请求打到 `/openai`
- `providerEndpoint = responses` 时，CRS 会把 `/chat/completions` 归一化到 `/responses`
- `baseApi` 如果已经带 `/v1`，CRS 会避免拼成重复的 `/v1/v1/...`

### 2. 创建 CRS API Key

给实际使用者创建一个 CRS API Key，后续客户端只拿这个 `cr_...` Key 访问 CRS，不直接暴露你的上游密钥。

### 3. 配置 Codex CLI

在 `~/.codex/config.toml` 顶部加入：

```toml
model_provider = "crs"
model = "gpt-5.1-codex-max"
model_reasoning_effort = "high"
disable_response_storage = true
preferred_auth_method = "apikey"

[model_providers.crs]
name = "crs"
base_url = "http://127.0.0.1:3000/openai"
wire_api = "responses"
requires_openai_auth = true
```

在 `~/.codex/auth.json` 中写入：

```json
{
  "OPENAI_API_KEY": "cr_xxxxxxxxxxxxxxxxx"
}
```

### 4. 验证

- CRS 后台先点一次账户测试
- 再用 Codex 发一个最简单的问题确认链路通了
- 如果走了 Nginx 反代，记得开启 `underscores_in_headers on;`

## 方案二：给 Droid CLI 添加自定义模型

Droid CLI 适合把多个自定义模型直接挂到本地配置里。

它读取 `~/.factory/config.json`，关键是 `custom_models`。

### 路由怎么选

| 上游协议 | CRS 入口 | `provider` 推荐值 |
|---|---|---|
| Anthropic Messages | `/droid/claude` | `anthropic` |
| OpenAI Responses | `/droid/openai` | `openai` |
| OpenAI Chat Completions / 通用兼容接口 | `/droid/comm/v1/` | `generic-chat-completion-api` |

### 配置示例

```json
{
  "custom_models": [
    {
      "model_display_name": "Sonnet 4.5 [crs]",
      "model": "claude-sonnet-4-5-20250929",
      "base_url": "http://127.0.0.1:3000/droid/claude",
      "api_key": "cr_xxxxxxxxxxxxxxxxx",
      "provider": "anthropic",
      "max_tokens": 64000
    },
    {
      "model_display_name": "GPT5-Codex [crs]",
      "model": "gpt-5-codex",
      "base_url": "http://127.0.0.1:3000/droid/openai",
      "api_key": "cr_xxxxxxxxxxxxxxxxx",
      "provider": "openai",
      "max_tokens": 16384
    },
    {
      "model_display_name": "GLM-4.6 [crs]",
      "model": "glm-4.6",
      "base_url": "http://127.0.0.1:3000/droid/comm/v1/",
      "api_key": "cr_xxxxxxxxxxxxxxxxx",
      "provider": "generic-chat-completion-api",
      "max_tokens": 202800
    }
  ]
}
```

实践建议：

- `model_display_name` 明确标出 `[crs]`，方便区分本地直连和 CRS 转发
- `model` 填客户端实际会发送的模型名
- 不确定上游最大输出时，先给一个保守的 `max_tokens`

## 方案三：给 Claude Code / CCR 接一个自定义模型服务

如果你的目标是“Claude Code 使用非 Claude 模型”，推荐沿用项目里现成的 CCR 方案。

链路通常是：

```text
Claude Code -> CCR -> CRS -> 你的模型服务
```

### 什么时候用 `CCR` 账户

适合这些场景：

- 你希望 Claude Code 继续走 Anthropic 风格接口
- 你需要把 `claude-*` 模型名映射成上游真实模型名
- 你想继续复用 CRS 里的 Claude 调度、分组和统计能力

### CRS 后台怎么配

创建 `CCR` 账户时，重点是这几个字段：

| 字段 | 说明 |
|---|---|
| `API URL` | 你的 CCR 或兼容 Anthropic 的中间层地址 |
| `API Key` | 中间层密钥 |
| `supportedModels` | 模型映射表，左边填客户端请求的模型名，右边填真实上游模型名 |
| `maxConcurrentTasks` | 可选；需要串行或限制并发时再配 |

模型映射建议这样理解：

```json
{
  "claude-opus-4-1-20250805": "gemini-3-pro-preview",
  "claude-sonnet-4-5-20250929": "gemini-3-pro-preview",
  "claude-haiku-4-5-20251001": "gemini-2.5-flash"
}
```

这表示：

- 客户端继续请求 `claude-*`
- CRS/CCR 在转发前把模型名替换成真实上游模型

更完整的 Claude Code + CCR 示例，可以直接看 `docs/claude-code-gemini3-guide/README.md`。

## 方案四：接入 MiniMax 模型

如果你的目标是把请求直接路由到 MiniMax，项目里已经有一条独立于 CCR 的 MiniMax provider。

这条链路不是：

```text
Claude Client -> CCR -> MiniMax
```

而是：

```text
Claude-compatible request -> CRS scheduler -> MiniMax account pool -> MiniMax API
```

### 核心结论

- MiniMax 不是 CCR 的一个子配置
- MiniMax 走独立账户类型 `MiniMax`
- 路由开关靠模型名前缀 `minimax,`
- 模型名映射仍然用 `supportedModels`

### 1. 在 CRS 后台创建 `MiniMax` 账户

MiniMax 账户的关键字段：

| 字段 | 说明 |
|---|---|
| `API URL` | MiniMax Anthropic 兼容地址，默认可用 `https://api.minimax.io/anthropic` |
| `API Key` | MiniMax 密钥 |
| `supportedModels` | 可填白名单数组，也可填模型映射对象 |
| `priority` | 调度优先级 |
| `rateLimitDuration` | 限流恢复时间 |
| `dailyQuota` | 每日额度限制 |

一个典型配置可以是：

```json
{
  "name": "minimax-prod-1",
  "apiUrl": "https://api.minimax.io/anthropic",
  "apiKey": "YOUR_MINIMAX_KEY",
  "priority": 50,
  "accountType": "shared",
  "supportedModels": {
    "claude-sonnet-4-5-20250929": "MiniMax-M1",
    "claude-opus-4-1-20250805": "MiniMax-M1"
  },
  "dailyQuota": 0,
  "quotaResetTime": "00:00"
}
```

### 2. 决定 `supportedModels` 用数组还是映射对象

有两种常见用法：

- 数组：表示“这个 MiniMax 账户允许哪些模型名”
- 对象：表示“把客户端请求模型名映射成 MiniMax 上游模型名”

白名单示例：

```json
{
  "supportedModels": ["MiniMax-M1", "MiniMax-Text-01"]
}
```

映射示例：

```json
{
  "supportedModels": {
    "claude-sonnet-4-5-20250929": "MiniMax-M1",
    "claude-haiku-4-5-20251001": "MiniMax-Text-01"
  }
}
```

如果你希望客户端继续发 Claude 模型名，推荐用“对象映射”。

### 3. 请求时必须加 `minimax,` 前缀

MiniMax 调度不是默认混进 Claude 普通池，而是靠 vendor prefix 显式切换。

也就是说，请求里要写：

```json
{
  "model": "minimax,claude-sonnet-4-5-20250929",
  "max_tokens": 1024,
  "messages": [
    { "role": "user", "content": "hello" }
  ]
}
```

调度过程会这样工作：

1. CRS 识别 `minimax,`
2. 去掉前缀，得到基础模型名
3. 在 MiniMax 账户池里找可用账号
4. 如果 `supportedModels` 是映射对象，再把模型名映射成真实上游模型
5. 最后转发到 MiniMax 的 `/v1/messages`

### 4. MiniMax 和 CCR 的区别

| 维度 | CCR | MiniMax |
|---|---|---|
| 路由前缀 | `ccr,` | `minimax,` |
| 账户类型 | `CCR` | `MiniMax` |
| 认证方式 | `Bearer` 或 `x-api-key`，取决于 key 类型 | 固定 `x-api-key` |
| 上游定位 | 泛化的 Claude Code Router / Anthropic 兼容中间层 | MiniMax Anthropic 兼容接口 |

### 5. 最小可用测试方法

先保证 CRS 里已经有可调度的 MiniMax 账户，然后发一个标准 Claude 请求：

```bash
curl -X POST http://127.0.0.1:3000/api/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cr_xxxxxxxxxxxxxxxxx" \
  -d '{
    "model": "minimax,claude-sonnet-4-5-20250929",
    "max_tokens": 128,
    "messages": [
      { "role": "user", "content": "Say hello in one sentence." }
    ]
  }'
```

如果你配置了映射：

- 请求模型可以继续写 `claude-*`
- MiniMax 上游实际收到的是映射后的模型名

### 6. 分组和调度注意点

- MiniMax 支持共享池和分组调度
- 分组筛选逻辑后续专门补过 quota / overload 检查
- 如果账户过载、超额、被限流，调度器会跳过该账户

如果你要混用：

- 普通 Claude 账户
- CCR 账户
- MiniMax 账户

建议明确区分模型名前缀，避免误以为“不带前缀也会自动落到 MiniMax”

## 模型映射怎么选

项目里目前有两种常见做法：

- `supportedModels` 是数组：表示“这个账户支持哪些模型”
- `supportedModels` 是对象：表示“把请求模型映射到上游模型”

如果你接的是“伪装成另一个模型族”的服务，优先用对象映射。

例如：

- 客户端发 `claude-sonnet-4-5-20250929`
- 上游实际要的是 `deepseek-v3`
- 那就配成 `"claude-sonnet-4-5-20250929": "deepseek-v3"`
- 如果上游是 MiniMax，也同理可以配成 `"claude-sonnet-4-5-20250929": "MiniMax-M1"`

## 推荐落地顺序

建议按下面顺序做，排查最省时间：

1. 先在 CRS 后台把账户测试跑通
2. 再确认账户状态是可调度
3. 再创建 CRS API Key
4. 最后再配 Codex / Droid / CCR 客户端

不要一上来就先怀疑客户端，先保证 CRS 后台单账户测试是通的。

## 常见问题

### 1. 该选 `responses` 还是 `auto`？

优先选 `responses`。

只有当你的上游已经完整兼容 CRS 当前转发路径，且你明确知道不需要 CRS 做路径归一化时，再用 `auto`。

### 2. 上游只支持 Chat Completions，不支持 Responses，Codex 还能不能接？

直接给 Codex 走 `/openai` 不稳妥，优先改成：

- 用一个兼容 Responses 的中间层
- 或让 Droid / 其他支持 Chat Completions 的客户端走 `/droid/comm/v1/`

### 3. 为什么模型名填了但没生效？

优先检查：

- 你是“模型白名单”还是“模型映射”场景
- `supportedModels` 存的是数组还是对象
- 客户端实际发出的 `model` 是否和映射左值完全一致

### 4. 为什么明明账户可用，但客户端还是报 401 / 429 / 529？

先分开看：

- 401：通常是上游密钥、Header 或中间层鉴权不一致
- 429：通常是上游限流，先看账户状态和恢复时间
- 529：通常是上游过载，CCR 账户会走过载保护，恢复期间可能被调度器跳过

### 5. 为什么我配了 MiniMax 账户，但请求还是没走 MiniMax？

优先检查：

- `model` 有没有写成 `minimax,xxx`
- `supportedModels` 是否允许当前模型
- MiniMax 账户是否是 `shared` 或在可用分组里
- 账户是否因为限流、过载、超额被调度器跳过

## 相关文档

- `README.md`
- `docs/claude-code-gemini3-guide/README.md`
- `docs/account-types.md`
