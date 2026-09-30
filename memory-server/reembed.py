#!/usr/bin/env python3
"""把库中旧维度(wrong-dim)的向量用当前 embedding 模型重嵌入。

背景: 老记忆是 bge-m3(1024维, 实际从未加载成功 -> 走的 hash 降级向量)存的,
      nomic-embed-text-v1.5 为 768 维。混存会导致检索维度不匹配 / 检索质量崩坏。
      本脚本把所有与当前 dim 不符的向量重算。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data/memory.db"
sys.path.insert(0, str(ROOT))
import embedder as embedder_mod  # noqa: E402
import store  # noqa: E402


def main():
    emb = embedder_mod.Embedder()
    dim = emb.dim
    print(f"[reembed] 目标 dim={dim} model={emb.model}")

    rows = store.list_memories(limit=100000)
    # list_memories 不返回 embedding 字段, 故用 SQL 直读真实向量
    import sqlite3
    conn = sqlite3.connect(DB)
    dims = {mid: (len(blob) // 4 if blob else 0)
            for mid, blob in conn.execute("SELECT id, embedding FROM memories").fetchall()}
    conn.close()

    fixed, ok, failed = 0, 0, 0
    for m in rows:
        size = dims.get(m["id"], 0)
        if size == dim:
            ok += 1
            continue
        old = size
        try:
            new = emb.embed([m["content"]])[0]
            if new.size != dim:
                failed += 1
                print(f"  [fail] dim {new.size} != {dim}: {m['content'][:40]}")
                continue
            store.update_embedding(m["id"], new)
            fixed += 1
            print(f"  [fix] {old} -> {dim}  {m['content'][:60]}")
        except Exception as e:
            failed += 1
            print(f"  [err] {e}")
    print(f"\n[reembed] 已合规 {ok}, 重嵌入 {fixed}, 失败 {failed}")


if __name__ == "__main__":
    main()
