"""记忆存储: SQLite(向量 BLOB) + numpy 内存检索"""
import json
import sqlite3
import struct
import time
import uuid
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "memory.db"
F32 = struct.Struct("f")


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT NOT NULL,       -- fact | preference | goal | decision | knowledge
        source TEXT DEFAULT '',       -- conversation | knowledge | manual
        project TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        embedding BLOB,               -- float32 数组
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_cat ON memories(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_source ON memories(source)")
    return conn


# ---------- 向量编解码 ----------

def pack_vec(v: np.ndarray) -> bytes:
    return v.astype(np.float32).tobytes()


def unpack_vec(b: bytes) -> np.ndarray:
    if not b:
        return np.array([], dtype=np.float32)
    return np.frombuffer(b, dtype=np.float32)


# ---------- CRUD ----------

def add_memory(content: str, category: str = "fact", source: str = "manual",
               project: str = "", session_id: str = "", embedding: np.ndarray | None = None) -> dict:
    now = time.time()
    mid = uuid.uuid4().hex[:12]
    conn = _conn()
    conn.execute(
        "INSERT INTO memories (id, content, category, source, project, session_id, embedding, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, content, category, source, project, session_id,
         pack_vec(embedding) if embedding is not None else None, now, now),
    )
    conn.commit()
    conn.close()
    return {"id": mid, "content": content, "category": category, "source": source,
            "project": project, "session_id": session_id, "created_at": now, "updated_at": now}


def update_embedding(mid: str, embedding: np.ndarray):
    conn = _conn()
    conn.execute("UPDATE memories SET embedding = ?, updated_at = ? WHERE id = ?",
                 (pack_vec(embedding), time.time(), mid))
    conn.commit()
    conn.close()


def delete_memory(mid: str):
    conn = _conn()
    conn.execute("DELETE FROM memories WHERE id = ?", (mid,))
    conn.commit()
    conn.close()


def get_memory(mid: str) -> dict | None:
    conn = _conn()
    row = conn.execute("SELECT id, content, category, source, project, session_id, created_at, updated_at "
                       "FROM memories WHERE id = ?", (mid,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "content": row[1], "category": row[2], "source": row[3],
            "project": row[4], "session_id": row[5], "created_at": row[6], "updated_at": row[7]}


def list_memories(limit: int = 500, category: str | None = None, source: str | None = None) -> list[dict]:
    sql = "SELECT id, content, category, source, project, session_id, created_at, updated_at FROM memories WHERE 1=1"
    args = []
    if category:
        sql += " AND category = ?"
        args.append(category)
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    conn = _conn()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [{"id": r[0], "content": r[1], "category": r[2], "source": r[3],
             "project": r[4], "session_id": r[5], "created_at": r[6], "updated_at": r[7]}
            for r in rows]


def stats() -> dict:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    with_vec = conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL").fetchone()[0]
    by_cat = dict(conn.execute("SELECT category, COUNT(*) FROM memories GROUP BY category").fetchall())
    by_source = dict(conn.execute("SELECT source, COUNT(*) FROM memories GROUP BY source").fetchall())
    conn.close()
    return {"total": total, "with_embedding": with_vec, "by_category": by_cat, "by_source": by_source}


# ---------- 向量检索 (numpy 内存) ----------

def load_embeddings() -> tuple[list[dict], np.ndarray]:
    """加载全部带向量的记忆 + 矩阵。几千条规模内存 < 数十 MB。

    使佳性: 会按“出现最多的向量维度”过滤, 丢弃维度不一致的条目。
    否则一旦库中混入旧维度向量(如换 embedding 模型后), np.stack 会直接 500。
    """
    conn = _conn()
    rows = conn.execute("SELECT id, content, category, source, project, created_at, embedding "
                        "FROM memories WHERE embedding IS NOT NULL").fetchall()
    conn.close()

    # 第一遍: 统计各维度出现次数, 取主流维度
    from collections import Counter
    dims = Counter()
    for r in rows:
        v = unpack_vec(r[6])
        if v.size:
            dims[v.size] += 1
    if not dims:
        return [], np.zeros((0, 0), dtype=np.float32)
    target_dim = dims.most_common(1)[0][0]

    meta, vecs = [], []
    for r in rows:
        v = unpack_vec(r[6])
        if v.size != target_dim:  # 跳过维度不一致的脏数据(需跑 reembed.py 修复)
            continue
        meta.append({"id": r[0], "content": r[1], "category": r[2],
                     "source": r[3], "project": r[4], "created_at": r[5]})
        vecs.append(v)
    if not vecs:
        return [], np.zeros((0, 0), dtype=np.float32)
    return meta, np.stack(vecs).astype(np.float32)


def cosine_similarity(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    q = np.nan_to_num(query.astype(np.float32))
    matrix = np.nan_to_num(matrix.astype(np.float32))
    qn = np.linalg.norm(q)
    if qn == 0:
        return np.zeros(matrix.shape[0], dtype=np.float32)
    q = q / qn
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1
    return np.nan_to_num((matrix @ q) / norms)  # cosine 相似度 [0..1]
