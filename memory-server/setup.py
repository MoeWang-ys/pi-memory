#!/usr/bin/env python3
"""pi 记忆中心 —— 交互式安装向导

做三件事:
  1. 探测本机可用的推理后端 (LM Studio / Ollama / 云端 API)
  2. 引导用户为「向量化」与「记忆抽取」各选一个模型
  3. 写入 config.yaml, 并在需要时提示重建索引

用法:
  arch -arm64 .venv/bin/python setup.py      # macOS(system python 混编时需 arch 前缀)
  .venv/bin/python setup.py                  # Linux / 原生 arm64
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

CONFIG_PATH = ROOT / "config.yaml"

# 常见云端 OpenAI 兼容端点预设
CLOUD_PRESETS = [
    ("硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1", "BAAI/bge-m3"),
    ("DeepSeek",             "https://api.deepseek.com/v1", ""),
    ("OpenAI",               "https://api.openai.com/v1", "text-embedding-3-small"),
    ("智谱 BigModel",        "https://open.bigmodel.cn/api/paas/v4", "embedding-3"),
    ("自定义",               "", ""),
]

# 本地 embedding 常见候选(供提示, 不强制)
LOCAL_EMBED_HINTS = [
    ("text-embedding-bge-m3", 1024, "中文最强, 推荐", "在 LM Studio 搜索 bge-m3 (gpustack/bge-m3-GGUF)"),
    ("text-embedding-nomic-embed-text-v1.5", 768, "轻量, 中文区分度弱", "在 LM Studio 搜索 nomic"),
]

C = {"b": "\033[1m", "d": "\033[2m", "g": "\033[32m", "y": "\033[33m",
     "r": "\033[31m", "c": "\033[36m", "0": "\033[0m"}


def say(msg=""):
    print(msg)


def head(msg):
    say(f"\n{C['b']}{C['c']}{msg}{C['0']}")


def ok(msg):
    say(f"  {C['g']}✓{C['0']} {msg}")


def warn(msg):
    say(f"  {C['y']}!{C['0']} {msg}")


def err(msg):
    say(f"  {C['r']}✗{C['0']} {msg}")


def ask(prompt, default=""):
    tip = f" {C['d']}[{default}]{C['0']}" if default else ""
    try:
        v = input(f"{C['b']}?{C['0']} {prompt}{tip}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("\n已取消。")
        sys.exit(1)
    return v or default


def ask_choice(prompt, options, default=1):
    """options: [(label, value), ...]"""
    say(f"\n{C['b']}{prompt}{C['0']}")
    for i, (label, _) in enumerate(options, 1):
        mark = f"{C['g']}→{C['0']}" if i == default else " "
        say(f" {mark} {i}) {label}")
    while True:
        raw = ask("请输入序号", str(default))
        try:
            n = int(raw)
            if 1 <= n <= len(options):
                return options[n - 1][1], options[n - 1][0]
        except ValueError:
            pass
        err(f"请输入 1-{len(options)} 之间的数字")


def load_existing():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def detect_with_module():
    """复用 providers.detect_local_backends()"""
    try:
        import providers
        return providers.detect_local_backends()
    except Exception as e:
        warn(f"后端探测模块不可用({type(e).__name__}), 改用内置探测")
        import httpx
        found = {}
        for name, url in (("lmstudio", "http://127.0.0.1:1234/v1"),
                          ("ollama", "http://127.0.0.1:11434/v1")):
            try:
                r = httpx.get(f"{url}/models", timeout=3, trust_env=False)
                if r.status_code == 200:
                    found[name] = {"base_url": url,
                                   "models": [m.get("id", "") for m in r.json().get("data", [])]}
            except Exception:
                pass
        return found


def guess_dim(model_id: str) -> int:
    """按模型名猜维度(仅用于默认值, 实际以接口返回为准)。"""
    m = model_id.lower()
    if "bge-m3" in m or "bge_m3" in m:
        return 1024
    if "nomic" in m:
        return 768
    if "text-embedding-3-small" in m:
        return 1536
    if "text-embedding-3-large" in m:
        return 3072
    if "embedding-3" in m:      # 智谱
        return 2048
    if "bge-large" in m or "bge-m1" in m:
        return 1024
    if "bge-small" in m:
        return 512
    return 1024


def check_model_available(base_url, model, api_key="", timeout=8):
    """实探 /embeddings, 返回 (ok, dim, msg)。"""
    import httpx
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/embeddings",
                       json={"model": model, "input": ["测试"]},
                       headers=headers, timeout=timeout, trust_env=False)
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                return True, len(data[0].get("embedding", [])), "成功"
            return False, 0, "返回数据为空"
        return False, 0, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {e}"


def check_chat_available(base_url, model, api_key="", timeout=60):
    import httpx
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/chat/completions",
                       json={"model": model, "max_tokens": 16,
                             "messages": [{"role": "user", "content": "回复 OK"}]},
                       headers=headers, timeout=timeout, trust_env=False)
        if r.status_code == 200:
            return True, "成功"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def choose_backend(be, purpose):
    """为某个用途选择 provider + base_url + api_key。返回 (provider, base_url, api_key)。"""
    options = []
    if "lmstudio" in be:
        options.append(("LM Studio (本地, 零成本, 推荐)",
                        ("lmstudio", be["lmstudio"]["base_url"], "")))
    if "ollama" in be:
        options.append(("Ollama (本地)", ("openai", be["ollama"]["base_url"], "ollama")))
    options.append(("云端 OpenAI 兼容 API", ("cloud", "", "")))

    chosen, _ = ask_choice(f"为「{purpose}」选择推理后端:", options, default=1)

    if chosen[0] == "cloud":
        presets = [(name, (url, key)) for name, url, key in CLOUD_PRESETS]
        preset, name = ask_choice(f"选择「{purpose}」的云服务商:", presets, default=1)
        url = preset[0] or ask("请输入 base_url (形如 https://xxx/v1)")
        key = ask("请输入 API Key (留空则从环境变量读)")
        return "openai", url, key

    return chosen


def main():
    say(f"\n{C['b']}════ pi 记忆中心 · 安装向导 ════{C['0']}")
    say(f"{C['d']}  为「向量化」与「记忆抽取」各选一个模型。回车即用默认值。{C['0']}")
    say(f"{C['d']}  配置写入 {CONFIG_PATH}{C['0']}")

    # 备份现有配置
    if CONFIG_PATH.exists():
        bak = CONFIG_PATH.with_suffix(".yaml.bak-setup")
        shutil.copy2(CONFIG_PATH, bak)
        ok(f"已备份原配置 → {bak.name}")

    old = load_existing()
    old_emb = old.get("embedding", {}) or {}
    old_ext = old.get("extract", {}) or {}

    # ---------- 探测 ----------
    head("① 探测本机推理后端")
    be = detect_with_module()
    if be:
        for name, info in be.items():
            ok(f"{name} 在线 @ {info['base_url']}  ({len(info['models'])} 个模型)")
    else:
        warn("未发现本地推理后端 (LM Studio / Ollama 都没在跑)")
        warn("可用云端 API 继续；或先启动 LM Studio 后重跑本向导")

    # ---------- embedding ----------
    head("② 选择向量化模型 (embedding)")
    say(f"{C['d']}  提示: 维度由模型决定，换模型后必须重建索引。{C['0']}")
    provider, base_url, api_key = choose_backend(be, "向量化")


    # 列候选模型
    if provider == "lmstudio" and "lmstudio" in be and be["lmstudio"]["models"]:
        embed_like = [m for m in be["lmstudio"]["models"]
                      if "embed" in m.lower() or "bge" in m.lower() or "nomic" in m.lower()]
        if embed_like:
            say(f"\n  LM Studio 中可用的 embedding 模型:")
            for m in embed_like:
                say(f"    · {m}")
        else:
            warn("LM Studio 里没找到 embedding 模型，请先在其界面搜索 bge-m3 下载")

    default_emb = old_emb.get("model") or "text-embedding-bge-m3"
    model = ask("embedding 模型名", default_emb)
    dim = guess_dim(model)

    say(f"{C['d']}  正在探测 {model} 的实际维度...{C['0']}")
    okk, real_dim, msg = check_model_available(base_url, model, api_key)
    if okk:
        dim = real_dim
        ok(f"连通成功，实际维度 = {dim}")
    else:
        err(f"连通失败: {msg}")
        warn("请确认模型名正确、且该模型已在后端加载/下载")
        if not ask("仍要写入此配置吗? (y/N)", "N").lower().startswith("y"):
            say("已取消，未修改配置。")
            sys.exit(1)
        dim = int(ask("请手动指定维度", str(dim)))

    emb_cfg = {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "dim": dim,
        "api_key": api_key,
        "ttl_seconds": 0 if provider == "lmstudio" else 0,
        "batch_size": 16,
        "auto_load": True,
        "timeout": 120,
    }

    # ---------- 旧维度比对 ----------
    old_dim = old_emb.get("dim")
    dim_changed = bool(old_dim) and int(old_dim) != int(dim)

    # ---------- extract ----------
    head("③ 选择记忆抽取模型 (chat)")
    say(f"{C['d']}  每次对话后台调用，建议用便宜/本地小模型，别用旗舰模型。{C['0']}")
    if provider == "lmstudio" and "lmstudio" in be and be["lmstudio"]["models"]:
        chat_like = [m for m in be["lmstudio"]["models"]
                     if not any(k in m.lower() for k in ("embed", "bge", "nomic"))]
        if chat_like:
            say(f"\n  LM Studio 中可用的对话模型:")
            for m in chat_like[:12]:
                say(f"    · {m}")

    eprovider, ebase, ekey = provider, base_url, api_key
    if ask("抽取用与 embedding 相同的后端? (Y/n)", "Y").lower().startswith("n"):
        eprovider, ebase, ekey = choose_backend(be, "记忆抽取")

    default_ext = old_ext.get("model") or ""
    emodel = ask("抽取模型名", default_ext)
    if not emodel:
        warn("未填写抽取模型，抽取功能将不可用（手动记忆仍可用）")

    if emodel:
        say(f"{C['d']}  正在探测 {emodel} ...{C['0']}（reasoning 模型可能需十几秒）")
        cok, cmsg = check_chat_available(ebase, emodel, ekey)
        if cok:
            ok("抽取模型连通成功")
        else:
            err(f"连通失败: {cmsg}")
            warn("配置仍会写入，之后可在 config.yaml 修正")

    ext_cfg = {
        "provider": eprovider,
        "base_url": ebase,
        "model": emodel,
        "api_key": ekey,
        "temperature": 0.2,
        "max_tokens": 3000,
        "auto_load": True,
        "ttl_seconds": 0,
        "timeout": 300,
    }

    # ---------- 写入 ----------
    head("④ 写入配置")
    cfg = old or {}
    cfg["server"] = cfg.get("server") or {"host": "127.0.0.1", "port": 8970}
    cfg["embedding"] = emb_cfg
    cfg["extract"] = ext_cfg
    cfg.setdefault("retrieval", {"top_n": 5, "min_score": 0.5, "cliff_ratio": 1.5,
                                 "keyword_boost": 0.15, "k_candidates": 30})
    # 去重阈值随 embedding 模型调整
    default_dedup = 0.95 if dim == 1024 else (0.88 if dim == 768 else 0.9)
    prev_dedup = (cfg.get("dedup") or {}).get("sim_threshold")
    if prev_dedup and not dim_changed and provider == old_emb.get("provider"):
        default_dedup = prev_dedup
    dedup_val = ask("语义去重阈值 (0-1，越大越严格)", str(default_dedup))
    try:
        dedup_val = float(dedup_val)
    except ValueError:
        dedup_val = float(default_dedup)
    cfg["dedup"] = {"enabled": True, "sim_threshold": dedup_val}
    cfg.setdefault("stability", {"healthcheck_interval": 60, "auto_load_retry": 3,
                                 "retry_backoff": 5, "keepalive_ttl": 0})
    cfg.setdefault("extract_every_n_turns", 2)

    header = ("# pi 记忆中心 —— 配置文件\n"
              "# 由 setup.py 生成。可手工编辑；改 embedding 模型后请重跑本向导或 reembed.py。\n\n")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    ok(f"已写入 {CONFIG_PATH}")
    ok(f"  embedding: {provider} / {model} (dim={dim})")
    ok(f"  extract  : {eprovider} / {emodel or '(未配置)'}")
    ok(f"  dedup    : {cfg['dedup']['sim_threshold']}")

    # ---------- 后续提示 ----------
    head("⑤ 下一步")
    if dim_changed:
        warn(f"检测到维度变化: {old_dim} → {dim}")
        warn("已有记忆的向量与新模型不兼容，必须重建索引:")
        say(f"      {C['b']}arch -arm64 .venv/bin/python reembed.py{C['0']}")
    db = ROOT / "data" / "memory.db"
    if not db.exists():
        say("  这是首次安装，数据库会自动创建。")
    say(f"\n  启动服务:  {C['b']}nohup ./run.sh &{C['0']}   或   {C['b']}arch -arm64 .venv/bin/python -u app.py{C['0']}")
    say(f"  健康检查:  {C['b']}curl -s http://127.0.0.1:{cfg['server']['port']}/api/health{C['0']}")
    say()


if __name__ == "__main__":
    main()
