#!/usr/bin/env python3
"""历史会话记忆补处理 (backfill)

用途: 把 ~/.pi/agent/sessions 下所有历史会话逐条抽取记忆并入库。
背景: 2026-09-13 前抽取器有两处致命 bug 导致自动抽取从未成功:
      1) EXTRACT_PROMPT 用 .format() 解析, 但 prompt 内含 {"category":...} -> KeyError 被吞
      2) httpx 默认走系统代理, 127.0.0.1 请求被劫持成 502 (需 trust_env=False)
      3) 部分本地模型模板要求必须存在 user 消息, 仅发 system 会 400
      已全部修复。本脚本用于把历史欠账补上。

用法:
  arch -arm64 .venv/bin/python backfill.py [--limit N] [--dry-run] [--min-chars 200]
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SESS_ROOT = Path.home() / ".pi/agent/sessions"
DB = ROOT / "data/memory.db"
STATE = ROOT / "data/backfill-state.json"

sys.path.insert(0, str(ROOT))
import extractor as extractor_mod  # noqa: E402
import embedder as embedder_mod  # noqa: E402
import store  # noqa: E402


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"done": []}


def save_state(st: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def iter_sessions():
    """产出 (path, cwd_hint) 按修改时间升序"""
    files = sorted(SESS_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for p in files:
        yield p


def read_session(path: Path, max_chars: int = 6000):
    """解析 jsonl 会话, 返回 (session_id, cwd, messages[user/assistant])"""
    sid, cwd = "", ""
    msgs = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                if t == "session":
                    sid = obj.get("id", "")
                    cwd = obj.get("cwd", "")
                elif t == "message":
                    m = obj.get("message") or {}
                    role = m.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content = m.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                parts.append(c.get("text", ""))
                        text = "\n".join(parts)
                    text = (text or "").strip()
                    # 跳过工具调用回显/系统噪声
                    if not text or text.startswith("<") or len(text) < 5:
                        continue
                    msgs.append({"role": role, "content": text[:4000]})
    except Exception as e:
        print(f"  [warn] 读取失败 {path.name}: {e}")
        return sid, cwd, []

    # 只保留最后 max_chars 字符, 避免超上下文
    total = 0
    kept = []
    for m in reversed(msgs):
        total += len(m["content"])
        if total > max_chars:
            break
        kept.append(m)
    kept.reverse()
    return sid, cwd, kept


def chunk_messages(msgs, size=8):
    """把消息按轮次分块抽取, 减少单次上下文压力"""
    for i in range(0, len(msgs), size):
        yield msgs[i:i + size]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个会话")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-chars", type=int, default=200, help="会话最小字符数, 太短的跳过")
    ap.add_argument("--chunk", type=int, default=10, help="每块消息数")
    args = ap.parse_args()

    st = load_state()
    done = set(st["done"])
    ex = extractor_mod.Extractor()
    emb = embedder_mod.Embedder()
    print(f"[backfill] 抽取模型={ex.model}  embedding={emb.model}(dim={emb.dim})")

    existing = {m["content"] for m in store.list_memories(limit=100000)}
    print(f"[backfill] 库中已有 {len(existing)} 条记忆")

    sessions = list(iter_sessions())
    todo = [p for p in sessions if str(p) not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[backfill] 会话总数={len(sessions)} 待处理={len(todo)}")

    total_added, total_skipped = 0, 0
    for idx, path in enumerate(todo, 1):
        sid, cwd, msgs = read_session(path)
        chars = sum(len(m["content"]) for m in msgs)
        if chars < args.min_chars:
            done.add(str(path))
            continue

        proj = Path(cwd).name if cwd else ""
        print(f"[{idx}/{len(todo)}] {path.parent.name}/{path.name[:40]} "
              f"sid={sid[:8]} msgs={len(msgs)} chars={chars} proj={proj}")

        added_here = []
        for chunk in chunk_messages(msgs, args.chunk):
            try:
                items = ex.extract(chunk)
            except Exception as e:
                print(f"    [extract-err] {e}")
                continue
            for it in items:
                content = (it.get("content") or "").strip()
                if not content or content in existing:
                    total_skipped += 1
                    continue
                if args.dry_run:
                    print(f"    + [{it.get('category')}] {content[:90]}")
                    existing.add(content)
                    added_here.append(content)
                    continue
                try:
                    vec = emb.embed([content])[0]
                    store.add_memory(content, category=it.get("category", "fact"),
                                     source="conversation", project=proj,
                                     session_id=sid, embedding=vec)
                    added_here.append(content)
                    total_added += 1
                except Exception as e:
                    print(f"    [store-err] {e}")
            time.sleep(0.2)

        if not args.dry_run:
            print(f"    -> 新增 {len(added_here)} 条")
            done.add(str(path))
            st["done"] = sorted(done)
            save_state(st)

    print(f"\n[backfill] 完成: 新增 {total_added} 条, 去重跳过 {total_skipped} 条")


if __name__ == "__main__":
    main()
