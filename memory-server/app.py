"""memory-server FastAPI 入口"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import numpy as np

from config import load_config
import store
from embedder import Embedder
from extractor import Extractor
from retrieval import Retriever
import stability


def _uses_lmstudio(c: dict) -> bool:
    """embedding 或 extract 任一为 lmstudio 时, 才需要启动自愈守护。"""
    def p(sec):
        return (c.get(sec, {}).get("provider") or "lmstudio").lower()
    return p("embedding") == "lmstudio" or p("extract") == "lmstudio"


app = FastAPI(title="pi memory-server")

cfg = load_config()
embedder = Embedder(cfg)
extractor = Extractor(cfg)
retriever = Retriever(cfg)

# 启动 LM Studio 自愈守护线程(仅当后端是 lmstudio 时需要; 云端 provider 无此概念)
_daemon = stability.start_daemon() if _uses_lmstudio(cfg) else None

# 维度守卫: 库中向量维度与当前 embedding 配置不符时, 明确告警
# (换 embedding 模型后未重建索引是最常见的隐性故障)
def _check_dim_guard():
    try:
        _, mat = store.load_embeddings()
        if mat.size == 0:
            return
        db_dim = mat.shape[1]
        want = embedder.dim
        if want and db_dim != want:
            print(f"\n⚠️  维度不匹配: 库中向量为 {db_dim} 维, 但当前 embedding 模型 "
                  f"{embedder.model} 输出 {want} 维。\n"
                  f"    请运行: arch -arm64 .venv/bin/python reembed.py\n"
                  f"    (否则检索结果会不正确)\n")
    except Exception as e:
        print(f"[dim-guard] 检查失败: {type(e).__name__}: {e}")


_check_dim_guard()

WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# 注入日志(内存)
inject_log: list[dict] = []


def _cat_list() -> list[str]:
    return ["fact", "preference", "goal", "decision", "knowledge"]


def _log_inject(query: str, results: list[dict]):
    inject_log.insert(0, {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "query": query[:80],
        "count": len(results),
        "hits": [{"content": r["content"][:60], "score": r["final_score"]} for r in results],
    })
    del inject_log[200:]


# ==================== 页面 ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ==================== 核心 API ====================

@app.post("/api/extract")
async def api_extract(data: dict):
    """从对话消息抽取记忆并入库。

    去重采用双重策略(2026-09-27 加固):
      1) 精确匹配: content 完全相同直接跳过
      2) 语义去重: 向量 cosine > DEDUP_SIM 视为同一条
    (仅靠精确匹配不够: 模型两次生成同一事实时措辞会不同, 会重复入库)
    """
    messages = data.get("messages") or []
    if not messages:
        return JSONResponse({"error": "messages 不能为空"}, status_code=400)
    project = data.get("project", "")
    session_id = data.get("session_id", "")
    try:
        items = extractor.extract(messages)
        if not items:
            return {"extracted": []}

        texts = [it["content"] for it in items]
        vecs = embedder.embed(texts)
        existing = {m["content"] for m in store.list_memories(limit=5000)}

        # 语义去重: 预加载库中已有向量(单次调用只加载一次)
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
        if dup_skipped:
            print(f"[extract] 去重跳过 {dup_skipped} 条")
        return {"extracted": added}
    except Exception as e:
        # 抽取是后台任务, 失败只记日志, 绝不影响调用方
        print(f"[extract] 失败: {type(e).__name__}: {e}")
        return {"extracted": [], "error": f"{type(e).__name__}: {e}"}


@app.post("/api/inject")
async def api_inject(data: dict):
    """注入检索: 以 query 检索记忆, 返回注入 system 的文本"""
    query = (data.get("query") or "").strip()
    if not query:
        return {"context": "", "results": []}
    try:
        qv = embedder.embed([query])[0]
        results = retriever.search(query, qv)
        _log_inject(query, results)
        return {"context": retriever.build_inject_context(results), "results": results}
    except Exception as e:
        # 注入在对话关键路径上: 任何异常都必须降级为空上下文, 绝不能 500
        print(f"[inject] 检索失败, 降级为空上下文: {type(e).__name__}: {e}")
        return {"context": "", "results": [], "error": f"{type(e).__name__}: {e}"}


@app.post("/api/search")
async def api_search(data: dict):
    """agent 主动检索(仅知识库或全部)"""
    query = (data.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)
    try:
        qv = embedder.embed([query])[0]
        source = data.get("source")  # "knowledge" | "memory" | None(全部)
        results = retriever.search(query, qv, source=source, n=data.get("n") or 8)
        return {"results": results}
    except Exception as e:
        print(f"[search] 检索失败: {type(e).__name__}: {e}")
        return {"results": [], "error": f"{type(e).__name__}: {e}"}


# ==================== 记忆管理 ====================

@app.get("/api/memories")
async def api_memories(category: str | None = None, source: str | None = None, limit: int = 500):
    return {"memories": store.list_memories(limit=limit, category=category, source=source)}


@app.post("/api/memories")
async def api_add_memory(data: dict):
    content = (data.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "内容不能为空"}, status_code=400)
    category = data.get("category", "fact")
    if category not in _cat_list():
        return JSONResponse({"error": f"category 非法: {category}"}, status_code=400)
    vec = embedder.embed([content])[0]
    mem = store.add_memory(content, category=category, source="manual", embedding=vec)
    return {"memory": mem}


@app.delete("/api/memories/{mid}")
async def api_delete_memory(mid: str):
    store.delete_memory(mid)
    return {"status": "ok"}


# ==================== 知识库 ====================

def _chunk_text(text: str, max_len: int = 500) -> list[str]:
    """按段落切块, 超长再按句切"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_len:
            chunks.append(para)
            continue
        # 按句切
        for sent in para.replace("。", "。\n").replace("！", "！\n").replace("？", "？\n").split("\n"):
            sent = sent.strip()
            if sent:
                chunks.append(sent[:max_len])
    return [c for c in chunks if c]


def _import_text(text: str, title: str) -> dict:
    """切块 → 向量化 → 入库(category=knowledge), 返回 {added, chunks}"""
    chunks = _chunk_text(text)
    if not chunks:
        return {"added": 0, "chunks": 0}
    # 每块带标题前缀, 提升检索命中率
    tagged = [f"[{title}] {c}" for c in chunks]
    vecs = embedder.embed(tagged)
    existing = {m["content"] for m in store.list_memories(limit=5000)}
    added = 0
    for content, v in zip(tagged, vecs):
        if content in existing:
            continue
        store.add_memory(content, category="knowledge", source="knowledge", embedding=v)
        added += 1
    return {"added": added, "chunks": len(chunks)}


@app.post("/api/knowledge")
async def api_add_knowledge(data: dict):
    """导入知识库文档(粘贴文本): 切块 → 向量化 → 入库(category=knowledge)"""
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "文本不能为空"}, status_code=400)
    title = (data.get("title") or "").strip()[:60] or f"文档{int(time.time())}"
    return _import_text(text, title)


@app.post("/api/knowledge/upload")
async def api_upload_knowledge(file: UploadFile = File(...)):
    """导入知识库文档(上传文件, 支持 md/txt): 读取 → 切块 → 向量化 → 入库"""
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".md", ".markdown", ".txt", ""):
        return JSONResponse({"error": f"不支持的文件类型: {suffix or '(无扩展名)'}, 仅支持 md/txt"}, status_code=400)
    try:
        raw = await file.read()
    except Exception as e:
        return JSONResponse({"error": f"读取文件失败: {e}"}, status_code=400)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            return JSONResponse({"error": "文件编码无法识别, 请使用 UTF-8 编码"}, status_code=400)
    title = Path(filename).stem[:60]
    result = _import_text(text, title)
    return {"title": title, "filename": filename, **result}


@app.get("/api/knowledge")
async def api_knowledge():
    mems = store.list_memories(source="knowledge", limit=1000)
    # 按标题聚合计数
    from collections import Counter
    titles = Counter(
        (m["content"].split("]")[0].strip("[").strip() if "]" in m["content"] else "未命名")
        for m in mems
    )
    return {"documents": [{"title": t, "chunks": c} for t, c in titles.items()],
            "total_chunks": len(mems)}


# ==================== 状态与配置 ====================

@app.get("/api/status")
async def api_status():
    return {"stats": store.stats(),
            "embedding": {"model": embedder.model, "dim": embedder.dim,
                          "base_url": embedder.base_url},
            "extract": {"model": extractor.model},
            "retrieval": {k: getattr(retriever, k) for k in
                          ("top_n", "min_score", "cliff_ratio", "keyword_boost", "k_candidates")},
            "stability": {"daemon": stability.daemon_stats(),
                          "models": stability.stats()}}


@app.get("/api/health")
async def api_health():
    """主动健康检查: 探测模型是否可用(供外部监控/排障用)。"""
    if _uses_lmstudio(cfg):
        emb_ok = stability.is_loaded(embedder.model, cache_ttl=0)
        ext_ok = stability.is_loaded(extractor.model, cache_ttl=0)
    else:
        emb_ok = ext_ok = True  # 云端 provider: 不探测加载状态
    daemon = stability.daemon_stats() if _uses_lmstudio(cfg) else {"running": False}
    return {"ok": emb_ok and ext_ok,
            "providers": {"embedding": embedder.provider_name,
                          "extract": extractor.provider_name},
            "embedding": {"model": embedder.model, "dim": embedder.dim, "loaded": emb_ok},
            "extract": {"model": extractor.model, "loaded": ext_ok},
            "daemon": daemon}


@app.get("/api/logs")
async def api_logs():
    return {"logs": inject_log}


@app.get("/api/config")
async def api_get_config():
    return {"config": cfg}


@app.post("/api/config")
async def api_set_config(data: dict):
    """更新检索参数并保存到 config.yaml"""
    global retriever
    r = cfg.setdefault("retrieval", {})
    for k in ("top_n", "min_score", "cliff_ratio", "keyword_boost", "k_candidates"):
        if k in data:
            r[k] = data[k]
    import yaml
    from config import CONFIG_PATH
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    retriever = Retriever(cfg)
    return {"status": "ok", "retrieval": {k: getattr(retriever, k) for k in
                                          ("top_n", "min_score", "cliff_ratio", "keyword_boost", "k_candidates")}}


def main():
    server = cfg.get("server", {})
    host = server.get("host", "127.0.0.1")
    port = server.get("port", 8970)
    print(f"\npi memory-server: http://{host}:{port}")
    print(f"  embedding: {embedder.model} (dim={embedder.dim})")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
