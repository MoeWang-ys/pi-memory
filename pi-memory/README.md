# 记忆中心（PI-Desktop 插件）

把本地 **pi 记忆中心**（`memory-server`，默认 `http://127.0.0.1:8970`）接成 PI-Desktop 的 Agent 工具 `Memory`，
让 agent 在任意会话里都能跨会话检索 / 写入记忆。

## 前置条件

记忆中心本体在 `../memory-server/`，需要先跑起来：

```bash
cd ../memory-server && ./run.sh        # 前台守护；或 nohup ./run.sh &
```

它依赖 LM Studio（`127.0.0.1:1234`）提供 embedding 与抽取模型。检查：

```bash
curl -s http://127.0.0.1:8970/api/health
```

## 工具

单一工具 `Memory`，通过 `action` 分派：

| action | 作用 | 是否 Plan 安全 |
|---|---|---|
| `recall` | 以当前任务为 query 检索，返回可注入的上下文 | ✅ |
| `search` | 主动语义检索，可用 `source` 限定「个人记忆 / 知识库」 | ✅ |
| `list` | 浏览记忆，可按 `category` / `source` 过滤 | ✅ |
| `status` | 服务状态、统计、模型加载情况 | ✅ |
| `remember` | 写入一条记忆（`content` + `category`） | ❌ |
| `extract` | 从一段对话 `messages` 里抽记忆 | ❌ |
| `forget` | 按 `id` 删除记忆 | ❌ |

`category` 取值：`fact` / `preference` / `goal` / `decision` / `knowledge`。

在对话里说「搜记忆」「remember 一下」时，agent 可用 `ToolSearch` 搜 `memory` / `记忆` 加载本工具。

## 设置

| key | 默认 | 说明 |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8970` | 记忆中心地址 |

## 权限

- `agent.tool.register` — 注册工具
- `net.fetch` — 访问本机服务；`net.domains` 只放行 `127.0.0.1` / `localhost`

> 校验器会对放行本地域名给出 `net.local-domain` 警告（不是错误）：本插件确实以本机服务为目标。

## 开发

```bash
pi-plugin check .      # 校验
pi-plugin pack .       # 打包成 dist/local.pi-memory-<version>.piplug
```

本目录以**开发插件**形式加载，保存即热重载。
