"""独立抽取 worker —— 平行于 pi 的进程, 从队列取任务慢慢处理

为什么要有这个进程
------------------
改造前: 抽取在 HTTP 请求里同步跑, 模型推理几分钟 → 堵住 uvicorn 事件循环
        → pi 连"读记忆"都要排队等 → 卡死 pi。
        实测: 空闲读 0.33s, 抽取进行中读 33.8s (慢 100 倍)。

改造后: HTTP 端点只往 SQLite 队列 INSERT 一行就返回(毫秒级)。
        这个进程慢慢消费队列, 抽多久都跟 pi 无关。

进程模型
--------
    pi ──POST /api/extract (毫秒返回)──> SQLite 队列 <──claim── worker.py
                                              │
                                              └──> 模型推理(几分钟) ──> 入库

崩溃恢复
--------
- 任务落盘 SQLite, worker 崩了任务还在
- 启动时把 running 超时的任务打回 pending
- 拿到任务后即使 worker 被杀, 超时后也会被重新认领

用法
----
    python worker.py              # 前台, 看日志
    python worker.py --once       # 处理完当前队列就退出(测试用)
"""
import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import queue_db
from config import load_config
from embedder import Embedder
from extractor import Extractor
import extract_core

POLL_INTERVAL = 3.0          # 队列空时睡多久再问
IDLE_DEEP_SLEEP_AFTER = 20   # 连续空转多少次后加大间隔(省 CPU)
DEEP_SLEEP_INTERVAL = 15.0
# ★ 任务之间主动歇一会儿: worker 和 pi 可能共用同一个 LM Studio,
#   连着跑会让 pi 的注入/回忆请求排队等模型。留个空档让 pi 优先。
COOLDOWN_BETWEEN_JOBS = 2.0
# 队列里只有 1 条任务时, 先等一会儿再消费 —— 单条任务往往是
# 用户刚聊完就关会话留下的尾巴, 稍等能避开 pi 下一次启动的注入窗口。
PICKUP_DELAY = 1.5

log = logging.getLogger("worker")


class Worker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.extractor = Extractor(cfg)
        self.embedder = Embedder(cfg)
        self.running = True
        self.processed = 0
        self.failed = 0

    def stop(self, *_):
        log.info("收到停止信号, 处理完当前任务后退出...")
        self.running = False

    def run_one(self) -> bool:
        """认领并处理一个任务。有任务返回 True。"""
        # 消费前留个空档, 降低和 pi 抢模型的概率
        time.sleep(PICKUP_DELAY)
        job = queue_db.claim()
        if not job:
            return False

        tid = job["id"]
        n = len(job["messages"])
        log.info(f"任务 #{tid} 开始 (第 {job['attempts']} 次尝试, {n} 条消息)")
        t0 = time.time()
        try:
            # 推理可能耗时几分钟, 这里阻塞完全没问题 —— 只卡住本进程
            result = extract_core.extract_and_store(
                messages=job["messages"],
                project=job["project"],
                session_id=job["session_id"],
                extractor=self.extractor,
                embedder=self.embedder,
                cfg=self.cfg,
            )
            elapsed = time.time() - t0
            got = len(result.get("extracted", []))
            dup = result.get("dup_skipped", 0)
            queue_db.finish(tid, error="")
            self.processed += 1
            log.info(f"任务 #{tid} 完成: 入库 {got} 条, 去重跳过 {dup} 条, 耗时 {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            queue_db.finish(tid, error=f"{type(e).__name__}: {e}")
            self.failed += 1
            # 失败不影响其他任务, 继续跑
            log.error(f"任务 #{tid} 失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")
        return True

    def loop(self, once: bool = False):
        idle = 0
        log.info("worker 就绪, 开始消费队列")
        while self.running:
            try:
                did = self.run_one()
            except Exception as e:
                # 认领环节本身的异常(比如 DB 锁), 不致命
                log.error(f"认领任务异常: {type(e).__name__}: {e}")
                did = False

            if did:
                idle = 0
                # 任务之间歇一下, 让 pi 的即时请求(注入/回忆)优先拿到模型
                time.sleep(COOLDOWN_BETWEEN_JOBS)
                continue
            if once:
                log.info("队列已空, --once 模式退出")
                break

            idle += 1
            time.sleep(DEEP_SLEEP_INTERVAL if idle > IDLE_DEEP_SLEEP_AFTER else POLL_INTERVAL)

        log.info(f"worker 退出 (处理 {self.processed} 个, 失败 {self.failed} 个)")


def main():
    ap = argparse.ArgumentParser(description="记忆抽取队列 worker")
    ap.add_argument("--once", action="store_true", help="处理完当前队列即退出")
    ap.add_argument("--verbose", action="store_true", help="打印 debug 日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [worker] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    queue_db.init_db()
    stale = queue_db.requeue_stale()
    if stale:
        log.warning(f"发现 {stale} 个超时未完成的任务, 已打回队列重试")
    purged = queue_db.purge_done()
    if purged:
        log.info(f"清理了 {purged} 条历史完成任务")
    # 回收空闲页: 完成的任务虽然逻辑上已抹掉 payload, 但原始字节可能仍留在
    # SQLite 空闲页里, 需要 VACUUM 才能真正覆写。启动时做一次即可。
    queue_db.vacuum()

    cfg = load_config()
    w = Worker(cfg)
    signal.signal(signal.SIGTERM, w.stop)
    signal.signal(signal.SIGINT, w.stop)
    w.loop(once=args.once)


if __name__ == "__main__":
    main()
