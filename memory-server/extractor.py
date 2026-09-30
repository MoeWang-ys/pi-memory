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
        if not items:
            print(f"[extractor] 抽取为空 raw={raw[:200]!r}")
        return items
