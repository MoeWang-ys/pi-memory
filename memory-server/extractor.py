"""记忆抽取: 用对话 LLM 从对话中提取事实/偏好/目标/决定"""
import json
import re


import providers
from config import load_config

EXTRACT_PROMPT = (
    "你是用户的长期记忆管家。从下面的对话中抽取【值得跨会话长期记住】的关于用户的信息。\n"
    "\n"
    "只抽这四类：\n"
    "- fact: 用户的稳定身份/背景/经历/持有的资源与事实\n"
    "- preference: 用户的稳定偏好、口味、风格要求、协作习惯（要能跨对话复用）\n"
    "- goal: 用户的长期目标/方向/规划\n"
    "- decision: 用户已拍板的方法论、路线选择、规则约定\n"
    "\n"
    "【关键把关】宁缺勿滥。以下一律不抽：\n"
    "- 单次任务中的临时诉求（如“增加评测场景”“修复 400 错误”“调一下配置”）\n"
    "- 对助手工作过程的评价/寒暄/确认（如“看着还行”“可以了”“好的”）\n"
    "- 模型或工具的一次性输出、报错、文件名、命令\n"
    "- 无法脱离本次对话上下文理解的碎片（如“用户名为 gavin”这种顺手提到的）\n"
    "- 你（助手）自己做的事/说的话，只记用户侧的信息\n"
    "\n"
    "【安全红线】以下内容一律不得抽取（即使看起来像 fact/preference）:\n"
    "- 任何凭据: API key、token、密码、口令、私钥、连接串、Bearer 值\n"
    "- 形如 ghp_/sk-/AKIA/xoxb-/AIza 等开头的密钥字符串\n"
    "- 身份证号、银行卡号、手机号等敏感个人信息\n"
    "记忆库会被保留并注入后续会话，写入凭据等于把钥匙抄进日记。\n"
    "\n"
    "规则：\n"
    "1. 只输出 JSON 数组，每个元素为 {{\"category\": \"fact|preference|goal|decision\", \"content\": \"简短中文描述\"}}\n"
    "2. content 必须自包含: 脱离本对话也能读懂(把“它/这个”换成具体对象)\n"
    "3. 优先抽稳定、可复用、半年后仍然成立的信息; 拿不准就不抽\n"
    "4. 没有值得长期保留的信息时输出 []\n"
    "5. 不要输出任何其他内容\n"
    "\n"
    "[对话]\n{text}"
)


VALID_CATEGORIES = ("fact", "preference", "goal", "decision")

# ── 敏感信息过滤 ────────────────────────────────────────────────
# 提示词里已经要求"不得抽凭据", 但模型不一定听 —— 这里做第二道拦截,
# 因为漏一个 token 进记忆库的代价很高: 记忆会持久保存、注入后续会话。
# 2026-10-01 新增: 起因是一个 GitHub token 真被抽成了 [fact]。
_SENSITIVE_PATTERNS = [
    # 常见密钥前缀
    r"\bghp_[A-Za-z0-9]{16,}",          # GitHub PAT (classic)
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",  # GitHub PAT (fine-grained)
    r"\bsk-[A-Za-z0-9_-]{16,}",          # OpenAI / Anthropic
    r"\bAKIA[0-9A-Z]{16}\b",             # AWS Access Key
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",   # Slack
    r"\bAIza[0-9A-Za-z_-]{30,}",         # Google API key
    r"\bglpat-[A-Za-z0-9_-]{16,}",       # GitLab PAT
    r"\bnpm_[A-Za-z0-9]{30,}",           # npm token
    r"\bsk_live_[A-Za-z0-9]{16,}",       # Stripe
    # 私钥块
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    # 带标签的凭据（中英文）
    r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*\S{6,}",
    r"(?:密码|口令|密钥|令牌|私钥)\s*[:=：]\s*\S{4,}",
    r"Bearer\s+[A-Za-z0-9._-]{20,}",
    # 中国身份证 (18 位) / 银行卡 (16-19 位纯数字)
    r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_PATTERNS), re.IGNORECASE)


def is_sensitive(text: str) -> bool:
    """内容里是否含凭据/敏感个人信息。用于入库前拦截。"""
    t = text or ""
    if not t:
        return False
    if _SENSITIVE_RE.search(t):
        return True
    # 未知格式的密钥: 必须同时满足
    #   ① 出现"密钥类词"
    #   ② 出现一段长得像密钥的随机串（不能是路径/URL）
    # 早期版本只要求"长串+关键词"，结果把含 /Users/gavin/... 的普通记忆
    # 和长中文段落误杀了（实测 7/440 误报），所以收紧到只认真正的
    # base64/hex 随机串，且排除路径与 URL。
    # 注意: 不能用 \b 包裹中文 —— CJK 字符两侧都不构成词边界，
    # \b密钥\b 永远匹配不上（实测踩过这个坑）。
    if not re.search(r"(?:api[_\s-]?key|access[_\s-]?token|secret|密钥|令牌|凭据)", t, re.IGNORECASE):
        return False
    for cand in re.findall(r"[A-Za-z0-9+/=_-]{32,}", t):
        if cand.startswith(("/", "http", "~"), ) or "/" in cand or "." in cand:
            continue                      # 路径 / 域名 / 文件名，不是密钥
        if re.fullmatch(r"[A-Fa-f0-9]{32,}", cand):
            return True                   # 纯 hex（MD5/SHA/密钥）
        if re.search(r"[a-z]", cand) and re.search(r"[A-Z]", cand) and re.search(r"\d", cand):
            return True                   # 大小写+数字混排 = 典型密钥
    return False


def _norm_category(raw: str | None) -> str:
    """把模型输出的自由分类归一到合法四类之一。
    实测模型会输出 key_info / key information / info 等不在枚举内的值, 会污染统计与筛选。
    """
    c = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if c in VALID_CATEGORIES:
        return c
    alias = {
        "key_info": "fact", "key_information": "fact", "info": "fact",
        "information": "fact", "background": "fact", "profile": "fact",
        "user_fact": "fact", "identity": "fact",
        "prefer": "preference", "likes": "preference", "dislike": "preference",
        "plan": "goal", "target": "goal", "objectif": "goal",
        "decide": "decision", "choice": "decision", "conclusion": "decision",
    }
    return alias.get(c, "fact")


def _parse_json(raw: str) -> list[dict]:
    """从模型输出中抽出 JSON 数组。

    reasoning 型模型(qwythos 等)会把思维链写进 content, JSON 可能出现在末尾;
    故从后往前找所有 `[...]` 片段, 逐个尝试解析, 取第一个合法且形如记忆项的数组。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    # 去掉 markdown 围栏
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    # 从后往前找, 优先取末尾的 JSON(思维链之后的结论)
    spans = list(re.finditer(r"\[.*?\]", raw, re.S))
    for m in reversed(spans):
        try:
            data = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(data, list) and any(
            isinstance(d, dict) and d.get("content") for d in data
        ):
            return data
        if isinstance(data, list) and not data:
            return []
    return []


class Extractor:
    """从对话抽取记忆。

    2026-10-01 重构: 请求下沉到 providers.py, 支持 LM Studio 与任意 OpenAI 兼容端点。
    """

    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        ex = cfg.get("extract", {})
        self._provider = providers.build_chat(ex)
        self.model = self._provider.model
        self.provider_name = self._provider.provider_name

    def _is_loaded(self) -> bool:
        fn = getattr(self._provider, "_is_loaded", None)
        return fn() if fn else True

    def ensure_loaded(self) -> bool:
        return self._provider.ensure_loaded()

    def extract(self, messages: list[dict]) -> list[dict]:
        """从消息列表抽取记忆, 返回 [{"category", "content"}, ...]"""
        text = "\n".join(
            f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in messages
            if m.get("content")
        )[-4000:]
        if not text.strip():
            return []
        try:
            raw = self._provider.chat(
                system=EXTRACT_PROMPT.format(text=text),
                user="请按上述规则抽取，只输出 JSON 数组。",
            )
        except Exception as e:
            body = ""
            if hasattr(e, "response") and getattr(e, "response", None) is not None:
                try:
                    body = (e.response.text or "")[:150]
                except Exception:
                    body = ""
            print(f"[extractor] extract 异常: {type(e).__name__}: {e} {body}")
            return []
        items = [it for it in _parse_json(raw) if it.get("content")]
        for it in items:
            it["category"] = _norm_category(it.get("category"))
        # 第二道拦截: 模型不听提示词时, 代码兜底滤掉凭据
        safe, blocked = [], 0
        for it in items:
            if is_sensitive(it.get("content", "")):
                blocked += 1
                continue
            safe.append(it)
        if blocked:
            print(f"[extractor] 已拦截 {blocked} 条疑似凭据/敏感信息（不入库）")
        items = safe
        if not items:
            print(f"[extractor] 抽取为空 raw={raw[:200]!r}")
        return items
