"""抽取任务队列: SQLite 持久化 + 多进程安全

设计要点
--------
- **持久化**: 任务落盘, 进程崩溃/重启不丢任务(比内存队列强)
- **多进程安全**: SQLite WAL 模式 + `BEGIN IMMEDIATE` 原子认领,
  多个 worker 并发也不会重复消费
- **崩溃恢复**: 启动时把卡在 `running` 超时的任务打回 `pending`
- **入队即返回**: 生产者(HTTP 端点)只做一次 INSERT, 毫秒级返回

状态机
------
pending ──claim──> running ──成功──> done
                      │
                      ├──失败(未超次数)──> pending (下次重试)
                      └──失败(超次数)────> failed
"""
import json
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "memory.db"

# 同一任务最多尝试几次, 超过进 failed
MAX_ATTEMPTS = 3
# 超过这个秒数还在 running, 认为 worker 挂了, 打回 pending
STALE_RUNNING_SEC = 900


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    # WAL: 读写不互相阻塞, 这是"读不被写堵住"的关键
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """建队列表。幂等, 可重复调用。"""
    conn = _conn()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS extract_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT DEFAULT '',
            project TEXT DEFAULT '',
            payload TEXT NOT NULL,        -- JSON: [{role, content}, ...]
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT DEFAULT '',
            created_at REAL NOT NULL,
            claimed_at REAL,
            finished_at REAL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_q_status ON extract_queue(status, id)")
    finally:
        conn.close()


def enqueue(messages: list[dict], session_id: str = "", project: str = "") -> int:
    """入队。O(1) 一次 INSERT, 调用方立即返回。返回任务 id。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO extract_queue (session_id, project, payload, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (session_id, project, json.dumps(messages, ensure_ascii=False), time.time()),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def claim() -> dict | None:
    """原子认领一个待处理任务。没有则返回 None。

    用 BEGIN IMMEDIATE 拿写锁: 同一时刻只有一个 worker 能认领到同一条,
    避免多 worker 重复消费。
    """
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, session_id, project, payload, attempts FROM extract_queue "
            "WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        tid = row[0]
        conn.execute(
            "UPDATE extract_queue SET status='running', claimed_at=?, attempts=attempts+1 WHERE id=?",
            (time.time(), tid),
        )
        conn.execute("COMMIT")
        return {
            "id": tid,
            "session_id": row[1],
            "project": row[2],
            "messages": json.loads(row[3]),
            "attempts": row[4] + 1,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def finish(tid: int, error: str = ""):
    """标记任务完成; 有 error 则按重试次数决定回到 pending 还是 failed"""
    conn = _conn()
    try:
        if error:
            row = conn.execute("SELECT attempts FROM extract_queue WHERE id=?", (tid,)).fetchone()
            attempts = row[0] if row else MAX_ATTEMPTS
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE extract_queue SET status='failed', error=?, finished_at=? WHERE id=?",
                    (error[:500], time.time(), tid),
                )
            else:
                conn.execute(
                    "UPDATE extract_queue SET status='pending', error=?, claimed_at=NULL WHERE id=?",
                    (error[:500], tid),
                )
        else:
            # 任务成功 → 抹掉原始对话内容。
            # payload 是完整的原始对话，可能含凭据/隐私；抽完就没用了，
            # 留着只会让敏感数据长期驻盘（甚至会随备份/仓库外流）。
            # 2026-10-01: 实测在 queue 里发现了明文 GitHub token 才加的这一手。
            conn.execute(
                "UPDATE extract_queue SET status='done', error='', finished_at=?, payload='[]' "
                "WHERE id=?",
                (time.time(), tid),
            )
    finally:
        conn.close()


def requeue_stale():
    """把卡在 running 超时的任务打回 pending。worker 启动时调用。"""
    conn = _conn()
    try:
        cutoff = time.time() - STALE_RUNNING_SEC
        cur = conn.execute(
            "UPDATE extract_queue SET status='pending', claimed_at=NULL "
            "WHERE status='running' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        return cur.rowcount
    finally:
        conn.close()


def purge_done(keep_hours: int = 24):
    """清理已完成的旧任务, 避免表无限增长。"""
    conn = _conn()
    try:
        cutoff = time.time() - keep_hours * 3600
        cur = conn.execute(
            "DELETE FROM extract_queue WHERE status='done' AND finished_at < ?", (cutoff,)
        )
        return cur.rowcount
    finally:
        conn.close()


def stats() -> dict:
    """队列状态, 给 /api/health 用"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM extract_queue GROUP BY status"
        ).fetchall()
        out = {s: 0 for s in ("pending", "running", "done", "failed")}
        for s, c in rows:
            out[s] = c
        # 最老 pending 任务的等待时长
        row = conn.execute(
            "SELECT MIN(created_at) FROM extract_queue WHERE status='pending'"
        ).fetchone()
        out["oldest_pending_age"] = round(time.time() - row[0], 1) if row and row[0] else 0.0
        return out
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("extract_queue 已就绪:", stats())


def vacuum():
    """回收空闲页 —— 让 SQLite 真正覆写已删除数据的字节。

    finish() 会把完成任务的 payload 抹成 '[]'，但 SQLite 的页管理不会立刻
    覆写磁盘上的旧字节，明文可能仍残留在空闲页里。VACUUM 会重建整库。
    2026-10-01: 在 queue 里发现明文 token 残留后加的这一手。
    """
    conn = _conn()
    try:
        conn.execute("VACUUM")
        return True
    except Exception:
        return False
    finally:
        conn.close()
