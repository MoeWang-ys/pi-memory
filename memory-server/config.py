"""配置加载

支持两种推理后端 (provider):
  - lmstudio : 本地 LM Studio (默认, 零成本/隐私, 带自愈守护)
  - openai   : 任意 OpenAI 兼容端点 (OpenAI / DeepSeek / 硅基流动 / Ollama / vLLM ...)

向后兼容: 老配置文件没有 provider 字段时, 默认按 lmstudio 处理。
"""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def _default_embedding() -> dict:
    return {
        "provider": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "text-embedding-bge-m3",
        "dim": 1024,
        "ttl_seconds": 0,
        "batch_size": 16,
        "auto_load": True,
    }


def _default_extract() -> dict:
    return {
        "provider": "lmstudio",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "",
        "temperature": 0.2,
        "max_tokens": 3000,
        "auto_load": True,
        "ttl_seconds": 0,
    }


def _default_stability() -> dict:
    return {"healthcheck_interval": 60, "auto_load_retry": 3,
            "retry_backoff": 5, "keepalive_ttl": 0}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    cfg.setdefault("server", {"host": "127.0.0.1", "port": 8970})

    emb = cfg.setdefault("embedding", {})
    for k, v in _default_embedding().items():
        emb.setdefault(k, v)
    # 兼容: 老配置里 embedding 没写 base_url 但写了别的段落时, 从 extract 兜底
    if not emb.get("base_url"):
        emb["base_url"] = cfg.get("extract", {}).get("base_url", "http://127.0.0.1:1234/v1")
    emb["dim"] = int(emb.get("dim") or 0)

    ext = cfg.setdefault("extract", {})
    for k, v in _default_extract().items():
        ext.setdefault(k, v)
    if not ext.get("base_url"):
        ext["base_url"] = emb.get("base_url", "http://127.0.0.1:1234/v1")

    cfg.setdefault("retrieval", {"top_n": 5, "min_score": 0.5, "cliff_ratio": 1.5,
                                 "keyword_boost": 0.15, "k_candidates": 30})
    cfg.setdefault("dedup", {"enabled": True, "sim_threshold": 0.95})
    st = cfg.setdefault("stability", _default_stability())
    for k, v in _default_stability().items():
        st.setdefault(k, v)
    cfg.setdefault("extract_every_n_turns", 2)

    # 环境变量覆盖 API Key (避免把密钥写进文件/提交到 git)
    if os.environ.get("MEMORY_EMBEDDING_API_KEY") and "api_key" not in emb:
        emb["api_key"] = "${MEMORY_EMBEDDING_API_KEY}"
    if os.environ.get("MEMORY_EXTRACT_API_KEY") and "api_key" not in ext:
        ext["api_key"] = "${MEMORY_EXTRACT_API_KEY}"

    return cfg


def save_config(cfg: dict):
    """写回配置文件(供 setup 向导使用)。保留注释无法做到, 故 setup 会另行备份。"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
