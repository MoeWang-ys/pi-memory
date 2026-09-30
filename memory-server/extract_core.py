"""抽取核心: 从对话消息里提炼记忆并入库

**从 app.py 里抽出来的**, 目的是让两条路径共用同一份实现:
  - app.py  的 /api/extract (同步模式, 向后兼容)
  - worker.py 独立进程 (队列模式, 推荐)

逻辑一字未改, 只是搬了个家 —— 这样同步/异步两种模式的结果必然一致,
不会出现"队列模式抽到的记忆和同步模式不一样"这种鬼问题。
"""
import numpy as np

import store


def extract_and_store(messages: list[dict], project: str, session_id: str,
                      extractor, embedder, cfg: dict) -> dict:
    """抽对话 → 去重 → 入库。

    返回 {"extracted": [...], "dup_skipped": n}; 异常由调用方处理。
    去重采用双重策略:
      1) 精确匹配: content 完全相同直接跳过
      2) 语义去重: 向量 cosine > DEDUP_SIM 视为同一条
    """
    items = extractor.extract(messages)
    if not items:
        return {"extracted": [], "dup_skipped": 0}

    texts = [it["content"] for it in items]
    vecs = embedder.embed(texts)
    existing = {m["content"] for m in store.list_memories(limit=5000)}

    dcfg = cfg.get("dedup", {})
    DEDUP_SIM = float(dcfg.get("sim_threshold", 0.95))
    DEDUP_ON = bool(dcfg.get("enabled", True))
    try:
        meta_all, mat_all = store.load_embeddings()
    except Exception:
        meta_all, mat_all = [], None

    added, dup_skipped = [], 0
    for it, v in zip(items, vecs):
        content = it["content"]
        if content in existing:
            dup_skipped += 1
            continue
        # 向量相似度去重
        if DEDUP_ON and mat_all is not None and len(meta_all) and v.size == mat_all.shape[1]:
            sims = store.cosine_similarity(v, mat_all)
            if sims.size and float(np.max(sims)) >= DEDUP_SIM:
                dup_skipped += 1
                continue
        mem = store.add_memory(content, category=it.get("category", "fact"),
                               source="conversation", project=project,
                               session_id=session_id, embedding=v)
        # 新入库的也加入待比对集合, 避免同一批次内自重复
        existing.add(content)
        if mat_all is not None:
            mat_all = np.vstack([mat_all, v.reshape(1, -1)]) if mat_all.size else v.reshape(1, -1)
            meta_all = meta_all + [{"id": mem["id"]}]
        added.append({"id": mem["id"], "content": content, "category": mem["category"]})

    return {"extracted": added, "dup_skipped": dup_skipped}
