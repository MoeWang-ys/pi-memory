"""语义检索: top_n + 绝对阈值 + 断崖检测 + 关键词加权"""
import numpy as np

from config import load_config
import store


def _keyword_hit(query: str, content: str, category: str) -> float:
    """关键词加权: 完整词/核心词命中加分"""
    q = query.lower()
    c = content.lower()
    if not q or not c:
        return 0.0
    boost = 0.0
    for word in q.split():
        if len(word) >= 2 and word in c:
            boost += 0.05
    # 类别命中(用户问"我的偏好" → preference 类加成)
    if "偏好" in q and category == "preference":
        boost += 0.1
    if "目标" in q and category == "goal":
        boost += 0.1
    if "决定" in q and category == "decision":
        boost += 0.1
    if "事实" in q and category == "fact":
        boost += 0.1
    return min(boost, 0.3)


class Retriever:
    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        r = cfg.get("retrieval", {})
        self.top_n = int(r.get("top_n", 5))
        self.min_score = float(r.get("min_score", 0.5))
        self.cliff_ratio = float(r.get("cliff_ratio", 1.5))
        self.keyword_boost = float(r.get("keyword_boost", 0.15))
        self.k_candidates = int(r.get("k_candidates", 30))

    def search(self, query: str, query_vec: np.ndarray,
               source: str | None = None, n: int | None = None) -> list[dict]:
        """返回 [{"id","content","category","source","score","project",...}] 按相关度降序"""
        top_n = n or self.top_n
        meta, matrix = store.load_embeddings()
        if not meta or query_vec.size == 0:
            return []

        scores = store.cosine_similarity(query_vec, matrix)
        if source:
            keep = [i for i, m in enumerate(meta) if m["source"] == source]
        else:
            keep = list(range(len(meta)))
        if not keep:
            return []
        keep = np.array(keep)
        cand = max(min(self.k_candidates, len(keep)), 1)

        # 候选集(排除知识库文档防止占满注入位, 由调用方决定是否过滤)
        idx = keep[np.argsort(-scores[keep])[:cand]]

        results = []
        for i in idx:
            m = meta[i]
            score = float(scores[i])
            kw = _keyword_hit(query, m["content"], m["category"])
            final = score + kw * self.keyword_boost * 5  # 关键词加成
            if final < self.min_score:
                continue
            results.append({**m, "score": round(score, 4),
                            "boost": round(kw * self.keyword_boost * 5, 4),
                            "final_score": round(final, 4)})

        # 断崖检测: 若 top1 明显高于后续, 只保留头部
        if len(results) >= 3:
            top1, top3 = results[0]["final_score"], results[2]["final_score"]
            if top3 > 0 and top1 / top3 > self.cliff_ratio:
                results = results[:3]
            elif top1 / max(top3, 1e-6) > self.cliff_ratio * 1.5:
                results = results[:2]

        return results[:top_n]

    def build_inject_context(self, results: list[dict]) -> str:
        """把检索结果拼成注入 system prompt 的文本"""
        if not results:
            return ""
        labels = {"fact": "事实", "preference": "偏好", "goal": "目标",
                  "decision": "决定", "knowledge": "知识"}
        lines = ["关于用户或任务的已知信息（来自跨会话记忆/知识库，供参考）："]
        for r in results:
            label = labels.get(r["category"], r["category"])
            src = "知识库" if r["source"] == "knowledge" else "记忆"
            lines.append(f"- [{label}][{src}] {r['content']}")
        return "\n".join(lines)
