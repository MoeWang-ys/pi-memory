# Memory Center

[English](README.md) · [中文](README.zh-CN.md)

<img src="docs/assets/hero-en.svg" alt="Memory Center — cross-session long-term memory for your AI assistant" width="100%" />

> Give your AI assistant a **cross-session memory** — the preferences you stated, the decisions you made, the pitfalls you hit. Next time, it remembers.

---

## The problem

Every new session, your AI starts from zero:

- You said "answer me in Chinese" — next session it's back to English
- You spent half an hour explaining your business context — new window, explain it again
- You settled a technical decision last week — this week it proposes the opposite
- Your hard-won lessons are scattered across dozens of conversations, unfindable

**The root cause**: an LLM's context is volatile. The conversation ends, it's wiped. You don't re-introduce yourself to a colleague every morning — but your AI meets a stranger every time.

## What you get

<img src="docs/assets/flow-en.svg" alt="How memories are written and read" width="100%" />

| | |
|---|---|
| **Automatic** | Nothing to remember by hand. Every turn, the memory is extracted in the background |
| **Semantic** | Not keyword matching. "how do I make it faster" finds the "performance tuning" memory |
| **Categorized** | `fact` / `preference` / `goal` / `decision` / `knowledge` |
| **Local-first** | On the local path, not a single character leaves your machine |
| **Deduplicated** | Semantic dedup (cos ≥ 0.95) — the same fact never gets stored eight times |
| **Self-healing** | Model gets unloaded, it reloads it. Service crashes, it restarts |

**Measured on the author's instance**: 387 memories, all 1024-dim, top retrieval similarity steady at 0.64–0.80.

## How it works

**Write**: after each turn, the conversation goes to an extraction model — "anything here worth remembering later?" → becomes a vector, gets stored.
**Read**: on a new session, your current task becomes a vector too → the **nearest** few are found → those are the semantically relevant memories.

```mermaid
flowchart LR
    C["Your conversation"]
    subgraph W["Write (automatic, background)"]
        direction TB
        E1["Extract<br/>find what's worth keeping"]
        E2["Embed<br/>text → 1024-dim vector"]
        E3["Dedup + store<br/>cos ≥ 0.95 = duplicate"]
        E1 --> E2 --> E3
    end
    subgraph R["Read (on a new session)"]
        direction TB
        R1["Retrieve<br/>use the current task as query"]
        R2["Rank + filter<br/>drop low scores, cut at the cliff"]
        R1 --> R2
    end
    DB["SQLite memory store"]
    C -->|write| E1
    E3 --> DB
    DB --> R1
    R2 -->|inject relevant memories| C
```

**Why vectors and not keywords?** You ask "how do I stop it crashing" while the memory says "service stability hardening" — not one word matches, but they mean the same thing. Vectors catch that.

### Architecture

<img src="docs/assets/architecture-en.svg" alt="Memory Center architecture: frontends, engine, pluggable inference backends" width="100%" />

**The key design: the inference backend is pluggable.** Local LM Studio and cloud APIs sit behind the same abstraction layer — one line of config switches between them.

## Getting started

```bash
git clone https://github.com/MoeWang-ys/pi-web-extensions.git
cd pi-web-extensions/memory-server
./install.sh
```

`install.sh` finds Python, creates a virtualenv, installs dependencies, then runs an **interactive wizard**: it probes what's on your machine (LM Studio / Ollama / cloud), lists the available models for you to pick, measures the real embedding dimension, verifies connectivity, and writes the config.

### Two paths, pick one

| | **A: Local models** | **B: Cloud API** |
|---|---|---|
| Cost | Free | Very cheap |
| Privacy | ✅ Stays on your machine | ❌ Uploaded to the provider |
| Hardware | ~8GB RAM for models | None |
| Best for | Macs, privacy-minded | Running in 5 minutes |

**A**: Install [LM Studio](https://lmstudio.ai) → start its server (`127.0.0.1:1234`) → download two models → run `./install.sh`

**B**: Run `./install.sh` → choose "cloud" in the wizard → pick a provider → paste your key

Or skip the key in the file entirely and use environment variables (recommended, keeps secrets out of config):

```bash
export MEMORY_EMBEDDING_API_KEY="sk-xxx"
export MEMORY_EXTRACT_API_KEY="sk-xxx"
```

### Start it

```bash
nohup ./run.sh &                                    # run persistently
curl -s http://127.0.0.1:8970/api/health            # look for "ok": true
```

## Choosing models (the only part that needs thought)

The wizard asks you twice. These two models are chosen in **completely different** ways.

### Embedding model — decides whether search works at all

| Model | Dim | Chinese | Notes |
|---|---|---|---|
| **`bge-m3`** | 1024 | ⭐⭐⭐⭐⭐ | **Best for Chinese**. One of the strongest open multilingual embedding models |
| `nomic-embed-text` | 768 | ⭐⭐ | Lightweight, but **poor Chinese discrimination** — Chinese sentences all collapse into 0.99 similarity, and retrieval effectively fails |
| `text-embedding-3-small` | 1536 | ⭐⭐⭐⭐ | OpenAI, good quality, paid |

> ⚠️ **Don't use an English-oriented model for Chinese.** This is the single most common failure.

### Extraction model — decides how *accurately* it remembers

**It runs on every single turn, so cost matters.**

| | |
|---|---|
| ✅ A local 7B model | Free, private, good enough |
| ✅ A cheap cloud model (DeepSeek etc.) | Fractions of a cent |
| ❌ GPT-4 / Claude Opus | **Your bill will explode** |

### ⚠️ The iron rule when changing embedding models

Change the model and **all existing memories become invalid** (old and new vectors don't live in the same coordinate space). You must rebuild the index:

```bash
.venv/bin/python reembed.py
```

The service checks for dimension mismatches on startup and warns you. The dedup threshold must also be recalibrated: `bge-m3` → `0.95`, `nomic` → `0.88`.

## Connecting to your tools

Once the engine is running, pick a frontend:

<details>
<summary><b>pi CLI extension</b> (4 tools + 3 hooks + a <code>/memory</code> command)</summary>

```bash
ln -s "$(pwd)/memory-extension/index.ts" ~/.pi/agent/extensions/memory.ts
```
Then `/reload` inside pi. Symlink rather than copy, so source edits take effect immediately.
</details>

<details>
<summary><b>PI-Desktop plugin</b> (single <code>Memory</code> tool, dispatched by <code>action</code>)</summary>

```bash
cd pi-memory && pi-plugin pack .     # → dist/local.pi-memory-<ver>.piplug
```
Install that file in PI-Desktop. Supports `recall` / `search` / `remember` / `extract` / `list` / `forget` / `status`.
</details>

## Layout

```
memory-server/       the engine (core, required)
├── providers.py     inference backend abstraction ← the dual-backend key
├── app.py           FastAPI service + all endpoints
├── extractor.py     extraction    embedder.py    embedding
├── retrieval.py     retrieval     store.py       SQLite
├── stability.py     model self-healing daemon
├── setup.py         interactive wizard
├── install.sh       one-command install
└── run.sh           watchdog launcher

memory-extension/    pi CLI frontend
pi-memory/           PI-Desktop frontend
tts-server/          text-to-speech (standalone)
wal-extension/       other extensions (standalone)
```

## FAQ

<details><summary><b>Service won't start / port already in use</b></summary>

```bash
lsof -tiTCP:8970 -sTCP:LISTEN | xargs -r kill -9 && nohup ./run.sh &
```
</details>

<details><summary><b>macOS: <code>pydantic_core</code> architecture mismatch</b></summary>

Your system Python is x86_64 but the machine is arm64. Prefix with `arch -arm64`. Both `install.sh` and `run.sh` handle this automatically.
</details>

<details><summary><b>Retrieval is bad / nothing is found</b></summary>

In order:
1. Is your embedding model English-oriented? (`nomic` for Chinese) → switch to `bge-m3`
2. Do the dimensions match? Check `/api/health` or the startup log
3. Changed models without running `reembed.py`?
4. Never processed your history? → `backfill.py`
</details>

<details><summary><b>502 / connections being hijacked</b></summary>

A system proxy is routing `127.0.0.1` too. The code forces `trust_env=False`; do the same if you write your own client.
</details>

<details><summary><b><code>Model is unloaded</code></b></summary>

A self-healing daemon polls every 60s and reloads it. If it still flaps, set `ttl_seconds` to `0` (resident).

Past incident: `ttl_seconds: 3600` and LM Studio's global `jitModelTTL` (1 hour) expired together, so the model was unloaded and reloaded every hour.
</details>

<details><summary><b>Extraction returns empty / no JSON</b></summary>

`max_tokens` is too small. Reasoning models need `3000`+ or the JSON gets truncated.
</details>

## Maintenance

```bash
./run.sh                    # foreground (auto-restart on crash)
nohup ./run.sh &            # background, persistent
.venv/bin/python reembed.py # rebuild index (required after changing embedding model)
.venv/bin/python backfill.py --min-chars 150 --chunk 12   # process conversation history
./install.sh                # reconfigure (backs up the old config)
```

## Design notes

**Why not reuse the AI tool's own model config?**
That's usually your flagship model — expensive. But **extraction runs on every turn**: chatting is "ask once, answer once", extraction is "runs in the background every turn". Different order of magnitude. And extraction is just information classification — a small model is plenty. So it's a separate config, but it supports any OpenAI-compatible endpoint.

**Why is LM Studio a "first-class citizen"?**
It's the only backend offering model lifecycle management (load / keep resident / auto-reload) — a bare API endpoint can't do that. Under a cloud provider the watchdog daemon shuts itself off rather than doing pointless probing.

## License

MIT
