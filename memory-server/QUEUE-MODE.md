# 抽取改成独立进程 + 队列（2026-10-01）

## 问题：写记忆把 pi 堵死了

现象：pi 时不时卡一下，尤其在对话结束、记忆抽取触发的时候。

### 实测数据

改之前，读记忆（`/api/inject`）的延迟：

| 场景 | 耗时 |
|---|---|
| 空闲时 | **0.33 秒** |
| 抽取进行中 | **33.8 秒** |

**慢 100 倍。**

极端情况下 `/api/extract` 一个请求 200 秒都没返回（curl 直接超时）。

## 根因

```
pi ──POST /api/extract──> app.py（同步抽取）
                              │
                              └─ 模型推理 60～200 秒  ← 卡在这里
                                 （uvicorn 事件循环被占住）

pi ──POST /api/inject───> app.py  ← 排队等 → 33.8 秒
```

三个问题叠加：

1. **`/api/extract` 是同步的** —— FastAPI 的 `async def` 里直接调用了同步的
   `extractor.extract()`，把整个事件循环堵住
2. **模型推理很慢** —— reasoning 类模型一次抽取 60～200 秒
3. **读和写共用一个进程** —— 写堵住之后，读（`/api/inject`，
   在 `before_agent_start` 关键路径上）只能排队

结果：**pi 在等记忆服务，记忆服务在等模型。**

## 改法：把写从关键路径上摘出去

```
pi ──POST /api/extract (0.003秒返回)──> SQLite 队列
                                            │
                                            │ claim（原子认领）
                                            ▼
                                     worker.py 独立进程
                                            │
                                            └─ 模型推理 60～200 秒
                                               ← 只卡住它自己
pi ──POST /api/inject──> app.py  ← 不排队了 → 0.04 秒
```

### 三个新文件

| 文件 | 职责 |
|---|---|
| `queue_db.py` | SQLite 队列：入队 / 原子认领 / 重试 / 崩溃回收 |
| `worker.py` | 独立进程，循环消费队列 |
| `extract_core.py` | 抽取+去重逻辑（从 app.py 抽出来，两种模式共用，保证结果一致） |

### 关键设计

**① 队列落盘在 SQLite，不是内存**

进程崩了任务还在。用 WAL 模式，读写不互相阻塞。

**② 认领用 `BEGIN IMMEDIATE` 原子操作**

多个 worker 并发也不会重复消费同一条任务。

**③ 崩溃自动恢复**

worker 启动时 `requeue_stale()` 把卡在 `running` 超过 15 分钟的任务打回
`pending`。worker 被杀 → 任务超时后被重新认领。

**④ 失败重试 + 放弃**

同一任务最多试 3 次，每次 attempts+1；超过进 `failed` 不再重试。
单条失败不影响其他任务。

**⑤ worker 主动让路**

worker 和 pi 可能共用同一个 LM Studio。所以：
- 每个任务开始前 `sleep(1.5)`
- 任务之间 `sleep(2)`
- 队列空时睡 3 秒（连续空转 20 次后改睡 15 秒省 CPU）

**⑥ pi 退出时的最后一搏用 `keepalive`**

pi `--print` 模式跑完立刻退进程，未完成的 fetch 会被 abort，服务端根本
收不到 —— 最后一批对话就丢了。所以在 `session_shutdown` 里用
`fetch(..., { keepalive: true })`，由运行时把请求送完，不随进程死亡。

## 效果

| | 改之前 | 改之后 |
|---|---|---|
| `/api/extract` 返回 | 200 秒+ 超时 | **0.003 秒** |
| 抽取中读记忆 | 33.8 秒 | **0.04 秒** |
| 队列堆 5 个任务时读 | — | 1～2 秒 |
| worker 挂掉时读 | 服务全挂 | **0.5 秒**（照常工作） |
| worker 挂掉时写 | 服务全挂 | **0.004 秒**（入队等它回来） |
| 单条抽取耗时 | 阻塞 pi | 60～90 秒（pi 无感） |

## 开关

`config.yaml`：

```yaml
extract:
  mode: queue    # 默认。入队即返回，worker 异步抽取
  # mode: sync   # 旧行为，会阻塞服务（实测慢 100 倍）。仅调试用
```

设成 `sync` 时 `run.sh` 不会启动 worker。

## 运维

```bash
# 两个进程都由 run.sh 守护
cd memory-server && nohup ./run.sh &

# 看队列状态
curl -s http://127.0.0.1:8970/api/health | python3 -m json.tool | grep -A6 queue

# 看 worker 在干嘛
tail -f ../logs/memory-worker.log

# 手动清一次积压（--once 模式）
.venv/bin/python worker.py --once
```

`/api/health` 现在会返回队列状态：

```json
"queue": {"pending": 0, "running": 1, "done": 42, "failed": 0, "oldest_pending_age": 3.5}
```

- `pending` 堆积说明 worker 没在跑
- `failed` 增长说明抽取系统性出错（模型不响应等）
- `oldest_pending_age` 是队首任务等了多久，正常应该很小

## 验证过的边界

- ✅ 原子认领：连续 claim 拿到不同任务，认领过的不会被再次认领
- ✅ 失败重试：attempts 递增，未超次数回 pending
- ✅ 放弃：超过 3 次进 failed
- ✅ 崩溃回收：卡住的 running 任务被打回 pending
- ✅ worker 不在：pi 读写照常，任务在队列里等
- ✅ worker 恢复：自动消费积压
- ✅ 队列满载 + 持续推理：读延迟稳定在 1～2 秒
- ✅ pi 退出后任务仍送达（keepalive）
