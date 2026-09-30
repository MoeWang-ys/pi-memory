"""本地 embedding 客户端

2026-10-01 重构: 真正的请求逻辑下沉到 providers.py,
本文件只保留"对外稳定接口 + 无语义降级兜底", 供 app.py / backfill.py 使用。

历史教训(见 FIXES-20260913.md):
  - 配置的 embedding 模型从未安装时, 曾经静默降级到 hash 向量,
    导致检索等于瞎搜却毫无报错。现在降级必定打日志。
  - httpx 默认走系统代理, 会让 127.0.0.1 请求被劫持成 502,
    故 provider 层统一 trust_env=False。
"""
import numpy as np

import providers
from config import load_config


class Embedder:
    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        emb = cfg.get("embedding", {})
        self._provider = providers.build_embedding(emb)
        self.model = self._provider.model
        self.dim = self._provider.dim
        self.base_url = self._provider.base_url
        self.batch = self._provider.batch
        self.provider_name = self._provider.provider_name
        # 未显式配置 dim 时, 用最近一次真实返回值回填
        if not self.dim:
            self.dim = int(emb.get("dim", 0)) or 1024

    def ensure_loaded(self) -> bool:
        return self._provider.ensure_loaded()

    def _is_loaded(self) -> bool:
        try:
            import stability
            return stability.is_loaded(self.model, cache_ttl=0)
        except Exception:
            return True

    def unload(self):
        self._provider.unload()

    def embed(self, texts: list[str]) -> np.ndarray:
        """向量化一批文本, 返回 (n, dim) float32。"""
        texts = [t for t in texts if t]
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        try:
            if not self._provider.ensure_loaded():
                print(f"[embedder] ⚠ {self.model} 无法加载, 降级 hash 向量(无语义!)")
                return self._fallback_embed(texts)
            arr = self._provider.embed(texts)
            if arr.size:
                self.dim = arr.shape[1]  # 以真实维度为准
            return arr
        except Exception as e:
            print(f"[embedder] ⚠ 向量化失败({type(e).__name__}: {e}), 降级 hash 向量(无语义!)")
            return self._fallback_embed(texts)

    def _fallback_embed(self, texts: list[str]) -> np.ndarray:
        """降级: 确定性 hash 向量(无语义, 保证服务不中断)。"""
        import hashlib
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            seed = hashlib.blake2b(t.encode("utf-8"), digest_size=64).digest()
            data = b""
            while len(data) < self.dim:  # 迭代哈希扩展字节流
                data += seed
                seed = hashlib.blake2b(seed).digest()
            out[i] = np.frombuffer(data[:self.dim], dtype=np.uint8).astype(np.float32) / 255.0
        return out
