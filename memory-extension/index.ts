/**
 * pi memory-extension: 跨会话记忆 + 知识库
 *
 * 能力:
 *   - 自动抽取: 每 2 轮对话(agent_end)抽取记忆入库; 会话结束(session_shutdown)全量补抽
 *   - 自动注入: 每轮对话开始(before_agent_start)以用户 prompt 语义检索, 注入 system prompt
 *   - 主动检索: memory_recall / knowledge_search / memory_save / knowledge_import 工具(注册后 system prompt 会告知 agent)
 *
 * 依赖: memory-server (默认 http://127.0.0.1:8970, 可用环境变量 PI_MEMORY_SERVER 覆盖)
 *
 * 安装: 复制/软链到 ~/.pi/agent/extensions/memory.ts, 然后 /reload 或重启 pi
 */
// 注意: 扩展运行于 ~/.pi/agent/extensions/, 工具参数 schema 手写为 JSON Schema 对象(TypeBox 兼容)。
// 运行时仅依赖 node 内置模块 + 全局 fetch, 零 npm 依赖。
import type { ExtensionAPI, AgentToolResult } from "@earendil-works/pi-coding-agent";
import { readFile, stat } from "node:fs/promises";
import { basename, extname, resolve } from "node:path";

const SERVER = process.env.PI_MEMORY_SERVER ?? "http://127.0.0.1:8970";
const INJECT_TIMEOUT_MS = 6000;    // 记忆注入超时: 它在 before_agent_start 关键路径上, 宁可不注入也绝不能阻塞本轮对话
const EXTRACT_EVERY_N_TURNS = 2;   // 每 N 轮用户消息抽取一次
const IMPORT_MAX_BYTES = 10 * 1024 * 1024;  // 单文件导入上限 10MB
const IMPORT_EXTS = new Set([".md", ".markdown", ".txt"]);

// 每个会话缓存的待抽取消息(会话内存活)
const pendingBySession = new Map<string, Array<{ role: string; content: string }>>();

function getText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((c) => c && typeof c === "object" && (c as any).type === "text")
      .map((c) => (c as any).text)
      .join("")
      .trim();
  }
  return "";
}

function extractMessages(messages: readonly any[]): Array<{ role: string; content: string }> {
  const out: Array<{ role: string; content: string }> = [];
  for (const m of messages) {
    const role = m?.role;
    if (role !== "user" && role !== "assistant") continue;
    const text = getText(m?.content);
    if (text) out.push({ role, content: text });
  }
  return out;
}

function fmtMessages(messages: Array<{ role: string; content: string }>) {
  return messages.map((m) => ({ role: m.role, content: m.content.slice(0, 2000) }));
}

async function post(path: string, body: unknown, timeoutMs = 300_000): Promise<any> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${SERVER}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`memory-server ${r.status}: ${t.slice(0, 200)}`);
    }
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 读取本地 md/txt 文件并上传到 memory-server 知识库 */
async function importLocalFile(filePath: string): Promise<string> {
  const abs = resolve(filePath);
  const ext = extname(abs).toLowerCase();
  if (!IMPORT_EXTS.has(ext)) {
    return `不支持的文件类型: ${ext || "(无扩展名)"}。仅支持 md/txt (${[...IMPORT_EXTS].join(", ")})。`;
  }
  let st;
  try {
    st = await stat(abs);
  } catch {
    return `文件不存在: ${abs}`;
  }
  if (!st.isFile()) {
    return `不是普通文件: ${abs}`;
  }
  if (st.size > IMPORT_MAX_BYTES) {
    return `文件过大 (${(st.size / 1024 / 1024).toFixed(1)}MB), 单文件上限 ${IMPORT_MAX_BYTES / 1024 / 1024}MB。`;
  }
  const buf = await readFile(abs);
  const name = basename(abs);
  const fd = new FormData();
  fd.append("file", new Blob([buf]), name);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 300_000);
  try {
    const r = await fetch(`${SERVER}/api/knowledge/upload`, { method: "POST", body: fd, signal: ctrl.signal });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return `已导入 ${d.added}/${d.chunks} 块: ${d.title}`;
  } catch (e: any) {
    return `导入失败: ${e?.message ?? e}`;
  } finally {
    clearTimeout(timer);
  }
}

function textResult(text: string): AgentToolResult<any> {
  return { content: [{ type: "text" as const, text }], details: {} };
}

export default function memoryExtension(pi: ExtensionAPI) {
  // ---- 自动注入: 非阻塞预取 + 本轮消费上一轮结果 ----
  // 重要(2026-09-15 事故复盘): 本钩子在 before_agent_start 关键路径上, 且所有扩展
  // 的 handler 是串行 await 的 —— 一旦这里 await 网络(最长 6s) 或记忆服务抖动,
  // 会拖住整个 agent 启动, 拉长"用户消息未落盘"的危险窗口(见 wal.ts)。
  // 因此: 命中缓存的上一轮结果立即返回(微秒级), 同时在后台预取下一轮。
  const injectCache = new Map<string, { query: string; context: string }>();
  let lastWarm: Promise<void> | null = null;

  function warmInject(query: string, project: string): void {
    const q = query.slice(0, 2000);
    // 单飞: 上一轮预取未完成则跳过, 避免堆积
    if (lastWarm) return;
    lastWarm = (async () => {
      try {
        const d = await post("/api/inject", { query: q, project }, INJECT_TIMEOUT_MS);
        if (d?.context) injectCache.set(project, { query: q, context: d.context });
      } catch (e: any) {
        console.error("[memory] 预取失败:", e?.message ?? e);
      } finally {
        lastWarm = null;
      }
    })();
  }

  pi.on("before_agent_start", async (event, ctx) => {
    // 关键路径: 绝不做网络等待, 只读缓存 + 触发后台预取
    try {
      const prompt = (event.prompt ?? "").trim();
      if (!prompt) return;
      const project = ctx.cwd;
      const hit = injectCache.get(project);
      // 缓存只在"不同 query"时视为有效(同一轮不会重复注入)
      if (hit && hit.query !== prompt.slice(0, 2000)) {
        warmInject(prompt, project);
        return { systemPrompt: `${event.systemPrompt}\n\n${hit.context}` };
      }
      warmInject(prompt, project);
    } catch (e: any) {
      console.error("[memory] 注入异常:", e?.message ?? e);
    }
    return undefined;
  });

  // ---- 自动抽取: 每 2 轮用户消息触发; 会话结束补抽 ----
  function queueExtract(sessionId: string, messages: Array<{ role: string; content: string }>) {
    const buf = pendingBySession.get(sessionId) ?? [];
    buf.push(...messages);
    // 只保留最近 N*3 条
    const max = EXTRACT_EVERY_N_TURNS * 3 * 2;
    const trimmed = buf.slice(-max);
    pendingBySession.set(sessionId, trimmed);

    const userCount = trimmed.filter((m) => m.role === "user").length;
    if (userCount >= EXTRACT_EVERY_N_TURNS) {
      void flushExtract(sessionId);
    }
  }

  async function flushExtract(sessionId: string) {
    const buf = pendingBySession.get(sessionId);
    if (!buf || buf.length < 2) return;
    pendingBySession.set(sessionId, []);
    try {
      await post("/api/extract", {
        messages: fmtMessages(buf),
        session_id: sessionId,
      });
    } catch (e: any) {
      // 失败则留回缓冲, 下次再试
      const cur = pendingBySession.get(sessionId) ?? [];
      pendingBySession.set(sessionId, [...buf, ...cur].slice(-40));
      console.error("[memory] 抽取失败:", e?.message ?? e);
    }
  }

  pi.on("agent_end", async (event, ctx) => {
    try {
      const sid = ctx.sessionManager.getSessionId();
      const msgs = extractMessages(event.messages);
      if (msgs.length) queueExtract(sid, msgs);
    } catch (e: any) {
      console.error("[memory] agent_end:", e?.message ?? e);
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    try {
      const sid = ctx.sessionManager.getSessionId();
      await flushExtract(sid);
      pendingBySession.delete(sid);
    } catch (e: any) {
      console.error("[memory] session_shutdown:", e?.message ?? e);
    }
  });

  // ---- 主动检索工具 ----
  pi.registerTool({
    name: "memory_recall",
    label: "回忆跨会话记忆",
    description:
      "检索跨会话记忆库中与查询相关的用户事实/偏好/目标/决定。当用户问'我之前说过/决定过/偏好是什么'或需要个性化背景时调用。",
    promptSnippet: "用 memory_recall 回忆用户过去的偏好、目标、决定",
    promptGuidelines: [
      "当用户问题涉及过去的对话、偏好、决定时, 先调用 memory_recall 再回答。",
    ],
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "检索查询, 描述想回忆的主题" },
        n: { type: "number", description: "返回条数, 默认 5" },
      },
      required: ["query"],
    } as any,
    execute: async (_id, params) => {
      try {
        const d = await post("/api/search", { query: params.query, n: params.n ?? 5 });
        const r = d.results ?? [];
        if (!r.length) {
          return textResult("记忆库中未找到相关记忆。");
        }
        const lines = r.map((m: any, i: number) =>
          `[${m.category}] (相关度 ${m.final_score.toFixed(2)}) ${m.content}`);
        return textResult(`以下是相关记忆:\n${lines.join("\n")}`);
      } catch (e: any) {
        return textResult(`记忆检索失败: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "knowledge_search",
    label: "检索知识库",
    description:
      "在知识库中检索与查询相关的文档片段。当用户问题需要项目文档、规范、参考资料等知识库内容时调用。",
    promptSnippet: "用 knowledge_search 检索知识库文档",
    promptGuidelines: [
      "当任务需要查阅知识库文档(规范/参考资料)时, 先调用 knowledge_search。",
    ],
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "检索查询" },
        n: { type: "number", description: "返回条数, 默认 5" },
      },
      required: ["query"],
    } as any,
    execute: async (_id, params) => {
      try {
        const d = await post("/api/search", { query: params.query, source: "knowledge", n: params.n ?? 5 });
        const r = d.results ?? [];
        if (!r.length) {
          return textResult("知识库中未找到相关内容。");
        }
        const lines = r.map((m: any, i: number) =>
          `[片段${i + 1}] (相关度 ${m.final_score.toFixed(2)})\n${m.content}`);
        return textResult(`知识库内容:\n${lines.join("\n\n")}`);
      } catch (e: any) {
        return textResult(`知识库检索失败: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "memory_save",
    label: "保存记忆",
    description:
      "把用户明确表达的重要信息(偏好/目标/决定/事实)保存到跨会话记忆。当用户说'记住这个/以后都这样/我偏好'时调用。",
    promptSnippet: "用 memory_save 保存用户明确要求记住的信息",
    promptGuidelines: [
      "当用户明确要求记住某事, 或表达了重要偏好/决定时, 调用 memory_save。",
    ],
    parameters: {
      type: "object",
      properties: {
        content: { type: "string", description: "要记住的内容" },
        category: {
          type: "string", enum: ["fact", "preference", "goal", "decision"],
          description: "类别, 默认 fact",
        },
      },
      required: ["content"],
    } as any,
    execute: async (_id, params) => {
      try {
        const d = await post("/api/memories", {
          content: params.content,
          category: params.category ?? "fact",
        });
        return textResult(`已保存记忆: ${d.memory?.content ?? ""}`);
      } catch (e: any) {
        return textResult(`保存失败: ${e?.message ?? e}`);
      }
    },
  });

  pi.registerTool({
    name: "knowledge_import",
    label: "导入本地文件到知识库",
    description:
      "把本地 md/txt 文件导入知识库(切块向量化), 之后 knowledge_search 可检索。当用户给了一个文件路径/文件名并要求记住、学习、导入或加入知识库时调用。",
    promptSnippet: "用 knowledge_import 把用户指定的本地文件导入知识库",
    promptGuidelines: [
      "当用户提供本地文件路径并要求'导入知识库/学习这个文档/记住这个文件'时, 调用 knowledge_import(path)。",
      "支持 .md/.markdown/.txt, 单文件 ≤ 10MB。相对路径基于当前工作目录解析。",
    ],
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "本地文件路径(相对或绝对), 如 ./docs/README.md 或 /Users/x/note.txt" },
      },
      required: ["path"],
    } as any,
    execute: async (_id, params) => {
      return textResult(await importLocalFile(String(params.path ?? "")));
    },
  });

  // ---- /memory 命令: 查看当前记忆库概况 ----
  pi.registerCommand("memory", {
    description: "查看跨会话记忆库状态",
    handler: async (_args, ctx) => {
      try {
        const d = await post("/api/status", {});
        const s = d.stats ?? {};
        ctx.ui.notify(
          `记忆库: 共 ${s.total} 条 (对话 ${s.by_source?.conversation ?? 0}, ` +
          `知识库 ${s.by_source?.knowledge ?? 0}, 手动 ${s.by_source?.manual ?? 0})`,
          "info",
        );
      } catch (e: any) {
        ctx.ui.notify(`memory-server 不可用: ${e?.message ?? e}`, "error");
      }
    },
  });
}
