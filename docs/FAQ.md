# 常见问题与运维

README 放不下的细节都在这。遇到问题先往下翻。

## 常见问题

<details><summary><b>服务起不来 / 端口被占用</b></summary>

```bash
lsof -tiTCP:8970 -sTCP:LISTEN | xargs -r kill -9 && nohup ./run.sh &
```
</details>

<details><summary><b>macOS 报 <code>pydantic_core</code> 架构不匹配</b></summary>

系统 Python 是 x86_64 混编但机器是 arm64。加 `arch -arm64` 前缀。`install.sh` 和 `run.sh` 已自动处理。
</details>

<details><summary><b>检索结果很差 / 搜不到</b></summary>

按顺序排查：
1. 向量模型是不是英文向的？（`nomic` 做中文）→ 换 `bge-m3`
2. 维度匹配吗？看 `/api/health` 或启动日志
3. 换过模型但没跑 `reembed.py`？
4. 历史对话没处理过？→ `backfill.py`
</details>

<details><summary><b>报 502 / 连接被劫持</b></summary>

系统代理把 `127.0.0.1` 也代理走了。代码已强制 `trust_env=False`，自己写脚本调用时记得同样处理。
</details>

<details><summary><b>模型被卸载（<code>Model is unloaded</code>）</b></summary>

引擎自带守护线程，每 60 秒巡检、自动拉起。若还反复掉，把 `ttl_seconds` 设为 `0`（常驻）。

历史踩坑：`ttl_seconds: 3600` 和 LM Studio 全局 `jitModelTTL`（1 小时）同时到期，导致每小时模型被卸了又拉。
</details>

<details><summary><b>抽取返回空 / 拿不到 JSON</b></summary>

`max_tokens` 给小了。reasoning 型模型需要 `3000` 以上。
</details>

<details><summary><b>记忆里出现不该存的敏感信息</b></summary>

抽取有两道防线：提示词里明确禁止抽凭据，代码里还有 `is_sensitive()` 正则兜底
（token / API key / 密码 / 私钥 / 身份证等）。命中的条目直接丢弃不入库。

如果发现历史遗留，扫描并清理：

```bash
cd memory-server
.venv/bin/python -c "
import store
from extractor import is_sensitive
c = store._conn()
for i, cat, t in c.execute('SELECT id, category, content FROM memories').fetchall():
    if is_sensitive(t): print(i, cat, t[:60])
"
```

任务队列里的原始对话在任务完成后会被抹除（`payload` 置空），老版本残留可用
`queue_db.vacuum()` 回收。
</details>

---

## 维护

```bash
./run.sh                    # 前台（崩溃自动重启）
nohup ./run.sh &            # 后台常驻；同时拉起 app.py + worker.py
.venv/bin/python reembed.py # 重建索引（换向量模型后必须）
.venv/bin/python backfill.py --min-chars 150 --chunk 12   # 补处理历史对话
./install.sh                # 重新配置（自动备份旧配置）
```

看队列状态（抽取是异步的，积压说明 worker 没在跑）：

```bash
curl -s http://127.0.0.1:8970/api/health | python3 -m json.tool | grep -A6 queue
tail -f ../logs/memory-worker.log
```

---

## 为什么这样设计

**为什么不用 AI 工具自带的模型配置？**

自带的通常是你的主力模型，很贵。但**抽取是每轮对话都要跑的**——聊天是"一问一答"，抽取是"每轮后台都跑"，量级差很多。而且抽取只是信息分类，小模型足够。所以独立配置，但支持任意 OpenAI 兼容端点。

**为什么 LM Studio 是"第一公民"？**

它是唯一提供模型生命周期管理的（加载/常驻/自动拉起）——纯 API 端点没这能力。用云端时守护线程自动关闭，不做无用探测。

**为什么抽取消费不在关键路径上？**

早期抽取是同步的，模型推理几十秒到几分钟会占住整个服务，导致"读记忆"从 0.3 秒变成 33 秒。现在写入只入队（毫秒返回），由独立 worker 进程消费。详见 [QUEUE-MODE.md](../memory-server/QUEUE-MODE.md)。
