/**
 * pi wal-extension: 用户消息前置落盘 (Write-Ahead Log)
 *
 * 背景（2026-09-15 事故）:
 *   pi-coding-agent 的 session-manager._persist() 有一条守卫:
 *     "本会话还没有 assistant 消息时, 用户消息只留在内存 fileEntries, 不写文件"
 *   —— 注释原文: "creates the file on the first assistant response"
 *   后果: 用户发出长消息后, 若本轮 agent 崩溃 / 页面重载 / session 重建
 *   (窗口期可长达数分钟, 如 9B 记忆抽取), 该消息永久消失, 不进任何日志。
 *
 * 本扩展的对策:
 *   在 before_agent_start (用户消息一提交、agent 循环尚未开始) 时,
 *   立刻以 append-only 方式把原始 prompt 落盘到独立的 WAL 文件。
 *   全程同步 fs 调用、不依赖网络/记忆服务、不阻塞主流程、异常静默降级。
 *
 * 设计要点:
 *   - 不碰 session 文件, 独立目录: ~/.pi/agent/wal/
 *   - 每个 session 一个 .jsonl + 全局 index 便于检索
 *   - 只写不改, 崩溃安全 (O_APPEND + 单行 JSON)
 *   - 内存队列 + 落盘确认, 失败仅 console.error
 *
 * 安装: 软链到 ~/.pi/agent/extensions/wal.ts, 然后 /reload 或重启 pi
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const WAL_DIR = process.env.PI_WAL_DIR ?? join(homedir(), ".pi", "agent", "wal");
const INDEX_FILE = join(WAL_DIR, "index.jsonl");
const SESSION_FILE = join(WAL_DIR, "sessions.jsonl");
/** 单个 prompt 落盘上限, 超过则截断并在日志里标注(防止异常大 payload 卡死) */
const MAX_PROMPT_BYTES = 2 * 1024 * 1024;

let dirReady = false;

function ensureDir(): boolean {
  if (dirReady) return true;
  try {
    if (!existsSync(WAL_DIR)) mkdirSync(WAL_DIR, { recursive: true });
    dirReady = true;
    return true;
  } catch (e: any) {
    console.error("[wal] 无法创建目录:", e?.message ?? e);
    return false;
  }
}

/** 同步、原子追加一行 JSON。失败只报错，绝不抛出。 */
function appendLine(file: string, obj: unknown): boolean {
  try {
    appendFileSync(file, JSON.stringify(obj) + "\n", { encoding: "utf8" });
    return true;
  } catch (e: any) {
    console.error("[wal] 写入失败:", e?.message ?? e);
    return false;
  }
}

function safeSessionId(ctx: any): string {
  try {
    return ctx?.sessionManager?.getSessionId?.() ?? "unknown";
  } catch {
    return "unknown";
  }
}

function safeSessionFile(ctx: any): string | null {
  try {
    return ctx?.sessionManager?.getSessionFile?.() ?? null;
  } catch {
    return null;
  }
}

/** 只保留安全字符，避免路径注入 / 奇怪文件名 */
function slug(s: string): string {
  return String(s).replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 120) || "unknown";
}

export default function walExtension(pi: ExtensionAPI) {
  ensureDir();

  // ==== 核心: 用户消息一进来就落盘 ====
  pi.on("before_agent_start", async (event, ctx) => {
    // 关键: 用 try 包住一切, 这个钩子在关键路径上, 绝不能阻断对话
    try {
      if (!ensureDir()) return;

      const prompt = event?.prompt ?? "";
      if (!prompt.trim()) return;

      const sid = safeSessionId(ctx);
      const sessionFile = safeSessionFile(ctx);
      const perSession = join(WAL_DIR, `session-${slug(sid)}.jsonl`);

      let body = prompt;
      let truncated = false;
      let bytes = Buffer.byteLength(body, "utf8");
      if (bytes > MAX_PROMPT_BYTES) {
        body = body.slice(0, MAX_PROMPT_BYTES);
        truncated = true;
        bytes = Buffer.byteLength(body, "utf8");
      }

      const rec = {
        ts: new Date().toISOString(),
        event: "prompt",
        sessionId: sid,
        sessionFile,
        cwd: ctx?.cwd ?? null,
        chars: prompt.length,
        bytes,
        truncated,
        prompt: body,
      };

      const ok = appendLine(perSession, rec) && appendLine(INDEX_FILE, { ...rec, prompt: undefined, ref: perSession });
      if (!ok) return;

      // 会话首次出现时登记一次, 方便排查"消息去哪了"
      try {
        const sidFile = join(WAL_DIR, `seen-${slug(sid)}.flag`);
        if (!existsSync(sidFile)) {
          appendLine(SESSION_FILE, {
            ts: rec.ts,
            sessionId: sid,
            sessionFile,
            cwd: rec.cwd,
            firstPromptChars: prompt.length,
          });
          writeFileSync(sidFile, rec.ts, { encoding: "utf8" });
        }
      } catch {
        /* 登记失败不影响主流程 */
      }
    } catch (e: any) {
      console.error("[wal] before_agent_start:", e?.message ?? e);
    }
    return undefined;
  });

  // ==== 助手首条消息落盘后, 标记该 prompt "已确认进会话" ====
  pi.on("agent_end", async (event, ctx) => {
    try {
      const sid = safeSessionId(ctx);
      const msgs = event?.messages ?? [];
      const hasAssistant = msgs.some((m: any) => m?.role === "assistant");
      if (!hasAssistant) return;
      const perSession = join(WAL_DIR, `session-${slug(sid)}.jsonl`);
      appendLine(perSession, {
        ts: new Date().toISOString(),
        event: "confirmed",
        sessionId: sid,
      });
    } catch (e: any) {
      console.error("[wal] agent_end:", e?.message ?? e);
    }
    return undefined;
  });

  // ==== 会话结束也记一笔 ====
  pi.on("session_shutdown", async (event, ctx) => {
    try {
      const sid = safeSessionId(ctx);
      appendLine(join(WAL_DIR, `session-${slug(sid)}.jsonl`), {
        ts: new Date().toISOString(),
        event: "shutdown",
        sessionId: sid,
        reason: (event as any)?.reason ?? null,
      });
    } catch {
      /* ignore */
    }
    return undefined;
  });

  // ==== /wal 命令: 查看 WAL 概况 / 找回最近 prompt ====
  pi.registerCommand("wal", {
    description: "查看消息落盘(WAL)概况; /wal last 显示最近一条原始 prompt",
    handler: async (args, ctx) => {
      try {
        if (!existsSync(WAL_DIR)) {
          ctx.ui.notify("WAL 目录不存在(尚未有消息落盘)", "info");
          return;
        }
        const files = readdirSync(WAL_DIR).filter((f) => f.startsWith("session-") && f.endsWith(".jsonl"));
        let prompts = 0;
        let bytes = 0;
        let latest: any = null;
        for (const f of files) {
          try {
            const lines = readFileSync(join(WAL_DIR, f), "utf8").split("\n");
            for (const l of lines) {
              if (!l.trim()) continue;
              let o: any;
              try {
                o = JSON.parse(l);
              } catch {
                continue;
              }
              if (o.event === "prompt") {
                prompts++;
                bytes += o.bytes ?? 0;
                if (!latest || o.ts > latest.ts) latest = o;
              }
            }
          } catch {
            /* skip */
          }
        }
        if (String(args ?? "").trim() === "last" && latest) {
          ctx.ui.notify(`最近一条 (${latest.ts})\n\n${latest.prompt}`, "info");
          return;
        }
        ctx.ui.notify(
          `WAL: ${files.length} 个会话 / ${prompts} 条用户消息 / ${(bytes / 1024).toFixed(1)}KB\n` +
            `最近: ${latest ? latest.ts + " (" + latest.chars + " 字)" : "无"}\n目录: ${WAL_DIR}`,
          "info",
        );
      } catch (e: any) {
        ctx.ui.notify(`/wal 出错: ${e?.message ?? e}`, "error");
      }
    },
  });
}
