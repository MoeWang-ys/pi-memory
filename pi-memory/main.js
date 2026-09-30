/**
 * 记忆中心 — PI-Desktop 插件入口。
 *
 * 把本地 pi 记忆中心（memory-server，默认 http://127.0.0.1:8970）
 * 暴露成 Agent 工具 `Memory`，让 PI-Desktop 的 agent 能跨会话读写记忆。
 *
 * 宿主注入全局 `pi`；每次调用都受 manifest.json 中声明的权限约束：
 *   - agent.tool.register : 注册工具
 *   - net.fetch           : 访问本机 memory-server
 *
 * 对应 memory-server 的接口（见 pi-web-extensions/memory-server/app.py）：
 *   GET    /api/status                   服务器状态与统计
 *   GET    /api/health                   主动健康检查（模型是否加载）
 *   POST   /api/inject   {query}         检索并返回可注入 system 的上下文
 *   POST   /api/search   {query, source, n}
 *   GET    /api/memories ?category&source&limit
 *   POST   /api/memories {content, category}
 *   DELETE /api/memories/{id}
 *   POST   /api/extract  {messages, project, session_id}
 *
 * 设计约定：
 *   1) execute 永不抛异常 —— 服务没起来 / 超时 / 返回非法 JSON，都降级成 {ok:false, error}，
 *      避免记忆中心故障把整轮对话带崩。
 *   2) 只读动作放进 planSafeActions，Plan / Goal 模式下依然可用（对齐内置 pi.browser 的做法）。
 */

"use strict";

const DEFAULT_BASE_URL = "http://127.0.0.1:8970";

const ACTIONS = ["search", "recall", "remember", "extract", "list", "forget", "status"];

/** 只读动作：Plan / Goal 模式下也允许。 */
const PLAN_SAFE_ACTIONS = ["search", "recall", "list", "status"];

const CATEGORIES = ["fact", "preference", "goal", "decision", "knowledge"];

const CATEGORY_LABELS = {
  fact: "事实",
  preference: "偏好",
  goal: "目标",
  decision: "决定",
  knowledge: "知识",
};

let settings = {};

function baseUrl() {
  const raw = String(settings?.baseUrl ?? "").trim() || DEFAULT_BASE_URL;
  return raw.replace(/\/+$/, "");
}

/**
 * 调用 memory-server。
 * 返回 { status, json }；json 为 null 表示响应体不是合法 JSON。
 */
async function request(path, { method = "GET", body, timeoutMs = 30000 } = {}) {
  const res = await pi.net.fetch({
    url: baseUrl() + path,
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    timeoutMs,
  });
  let json = null;
  if (res && res.bodyText) {
    try {
      json = JSON.parse(res.bodyText);
    } catch {
      json = null;
    }
  }
  return { status: res ? res.status : 0, json };
}

/** 服务端错误（HTTP != 2xx）统一转成一句话。 */
function serverError(status, json) {
  if (json && typeof json.error === "string" && json.error) return json.error;
  if (json && json.detail) return String(json.detail);
  return `memory-server 返回 HTTP ${status}`;
}

/** 检索结果裁剪成给模型看的紧凑形态。 */
function compactHits(results) {
  return (Array.isArray(results) ? results : []).map((r) => ({
    id: r.id,
    content: r.content,
    category: r.category,
    category_label: CATEGORY_LABELS[r.category] ?? r.category,
    source: r.source,
    project: r.project || undefined,
    score: r.final_score ?? r.score,
  }));
}

function clampInt(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.trunc(n), min), max);
}

async function runStatus() {
  const { status, json } = await request("/api/status", { timeoutMs: 10000 });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "status", error: serverError(status, json) };
  }
  return {
    ok: true,
    action: "status",
    baseUrl: baseUrl(),
    stats: json?.stats ?? null,
    embedding: json?.embedding ?? null,
    extract: json?.extract ?? null,
    retrieval: json?.retrieval ?? null,
    stability: json?.stability ?? null,
  };
}

async function runRecall(args) {
  const query = String(args?.query ?? "").trim();
  if (!query) {
    return { ok: false, action: "recall", error: "recall 需要 query" };
  }
  const { status, json } = await request("/api/inject", {
    method: "POST",
    body: { query },
    timeoutMs: 60000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "recall", error: serverError(status, json) };
  }
  const hits = compactHits(json?.results);
  return {
    ok: true,
    action: "recall",
    query,
    context: json?.context ?? "",
    count: hits.length,
    results: hits,
    degraded: Boolean(json?.error),
  };
}

async function runSearch(args) {
  const query = String(args?.query ?? "").trim();
  if (!query) {
    return { ok: false, action: "search", error: "search 需要 query" };
  }
  const source = args?.source === "knowledge" || args?.source === "memory" ? args.source : undefined;
  const { status, json } = await request("/api/search", {
    method: "POST",
    body: { query, source, n: clampInt(args?.n, 8, 1, 50) },
    timeoutMs: 60000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "search", error: serverError(status, json) };
  }
  const hits = compactHits(json?.results);
  return {
    ok: true,
    action: "search",
    query,
    source: source ?? "all",
    count: hits.length,
    results: hits,
  };
}

async function runList(args) {
  const params = new URLSearchParams();
  if (typeof args?.category === "string" && CATEGORIES.includes(args.category)) {
    params.set("category", args.category);
  }
  if (typeof args?.source === "string" && args.source) {
    params.set("source", args.source);
  }
  params.set("limit", String(clampInt(args?.limit, 20, 1, 500)));
  const { status, json } = await request(`/api/memories?${params.toString()}`, {
    timeoutMs: 15000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "list", error: serverError(status, json) };
  }
  const memories = Array.isArray(json?.memories) ? json.memories : [];
  return {
    ok: true,
    action: "list",
    count: memories.length,
    memories: memories.map((m) => ({
      id: m.id,
      content: m.content,
      category: m.category,
      category_label: CATEGORY_LABELS[m.category] ?? m.category,
      source: m.source,
      created_at: m.created_at,
    })),
  };
}

async function runRemember(args) {
  const content = String(args?.content ?? "").trim();
  if (!content) {
    return { ok: false, action: "remember", error: "remember 需要 content" };
  }
  const category =
    typeof args?.category === "string" && CATEGORIES.includes(args.category)
      ? args.category
      : "fact";
  const { status, json } = await request("/api/memories", {
    method: "POST",
    body: { content, category },
    timeoutMs: 30000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "remember", error: serverError(status, json) };
  }
  return {
    ok: true,
    action: "remember",
    memory: json?.memory
      ? {
          id: json.memory.id,
          content: json.memory.content,
          category: json.memory.category,
        }
      : null,
  };
}

async function runForget(args) {
  const id = String(args?.id ?? "").trim();
  if (!id) {
    return { ok: false, action: "forget", error: "forget 需要 id" };
  }
  const { status, json } = await request(`/api/memories/${encodeURIComponent(id)}`, {
    method: "DELETE",
    timeoutMs: 15000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "forget", error: serverError(status, json) };
  }
  return { ok: true, action: "forget", id };
}

async function runExtract(args) {
  const messages = Array.isArray(args?.messages) ? args.messages : [];
  if (!messages.length) {
    return { ok: false, action: "extract", error: "extract 需要非空 messages 数组" };
  }
  const { status, json } = await request("/api/extract", {
    method: "POST",
    body: {
      messages,
      project: typeof args?.project === "string" ? args.project : "",
      session_id: typeof args?.session_id === "string" ? args.session_id : "",
    },
    // 抽取要跑本地 LLM，给足时间。
    timeoutMs: 180000,
  });
  if (status < 200 || status >= 300) {
    return { ok: false, action: "extract", error: serverError(status, json) };
  }
  const extracted = Array.isArray(json?.extracted) ? json.extracted : [];
  return {
    ok: true,
    action: "extract",
    added: extracted.length,
    extracted,
    degraded: Boolean(json?.error),
  };
}

async function execute(args) {
  const action = String(args?.action ?? "").trim();
  try {
    switch (action) {
      case "status":
        return await runStatus();
      case "recall":
        return await runRecall(args);
      case "search":
        return await runSearch(args);
      case "list":
        return await runList(args);
      case "remember":
        return await runRemember(args);
      case "forget":
        return await runForget(args);
      case "extract":
        return await runExtract(args);
      default:
        return {
          ok: false,
          error: `未知 action "${action}"；可用：${ACTIONS.join(", ")}`,
        };
    }
  } catch (error) {
    // 网络/超时/宿主拒绝等都收敛到这里，绝不让异常冒到对话主流程。
    const detail = error && error.message ? error.message : String(error);
    return {
      ok: false,
      action: action || undefined,
      error: `调用记忆中心失败（${baseUrl()}）：${detail}`,
      hint: "确认 memory-server 已启动：cd pi-web-extensions/memory-server && ./run.sh",
    };
  }
}

async function onLoad() {
  settings = (await pi.plugin.getSettings()) ?? {};

  await pi.agent.registerTool({
    name: "Memory",
    description:
      "读写本地 pi 记忆中心（memory-server，默认 http://127.0.0.1:8970）的跨会话记忆。" +
      "action=recall 用当前任务做语义检索拿回相关记忆；action=search 主动检索；" +
      "action=remember 记一条新的；action=extract 从一段对话里抽记忆；" +
      "action=list 浏览；action=forget 按 id 删除；action=status 看服务状态。" +
      '需要时用 ToolSearch 搜 "memory" 或 "记忆" 加载本工具。',
    risk: "medium",
    planSafeActions: PLAN_SAFE_ACTIONS,
    schema: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ACTIONS,
          description: "要执行的操作。",
        },
        query: { type: "string", description: "action=search / recall 的检索语句。" },
        content: { type: "string", description: "action=remember 要写入的记忆内容。" },
        category: {
          type: "string",
          enum: CATEGORIES,
          description: "记忆类别；remember 时使用，list 时作为过滤条件。",
        },
        source: {
          type: "string",
          enum: ["memory", "knowledge"],
          description: "search 时限定来源：个人记忆或导入的知识库。",
        },
        id: { type: "string", description: "action=forget 要删除的记忆 id。" },
        messages: {
          type: "array",
          items: { type: "object" },
          description: "action=extract 的对话消息数组，形如 [{role, content}]。",
        },
        project: { type: "string", description: "extract 时随记忆一起记录的 project 标签。" },
        session_id: { type: "string", description: "extract 时随记忆一起记录的 session 标签。" },
        n: { type: "number", description: "action=search 返回条数上限（默认 8）。" },
        limit: { type: "number", description: "action=list 返回条数上限（默认 20）。" },
      },
      required: ["action"],
    },
    execute,
  });
}

async function onUnload() {
  await pi.agent.unregisterTool("Memory");
}

module.exports = { onLoad, onUnload };
