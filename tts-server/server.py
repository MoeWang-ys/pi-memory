#!/usr/bin/env python3
"""Pi Web 神经语音服务：基于 edge-tts（微软免费神经语音），替代 macOS 系统语音。

功能:
    GET  /health  -> {"ok": true, "voices": [...]}   健康检查 + 声音列表
    POST /tts     -> 生成 {text, voice, rate} 的 mp3 音频

用法:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python server.py          # 默认 127.0.0.1:8971

依赖: 需要能访问微软 Edge 在线语音服务（需联网）。
"""

import asyncio
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

HOST = "127.0.0.1"
PORT = 8971
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_TEXT = 8000  # 单次请求文本上限（字符）
MAX_CACHE = 256  # 内存缓存条数

# 音色列表（中文优先，含方言/情感/童声；完整清单见 https://github.com/rany2/edge-tts）
VOICES = [
    # ---- 中文·普通话 ----
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓（女·温柔）"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊（女·活泼）"},
    {"id": "zh-CN-HuihuiNeural", "name": "慧慧（女·情感丰富）"},
    {"id": "zh-CN-YaoyaoNeural", "name": "瑶瑶（女·童声）"},
    {"id": "zh-CN-YunxiNeural", "name": "云希（男·阳光）"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬（男·新闻播报）"},
    {"id": "zh-CN-YunjianNeural", "name": "云健（男·沉稳）"},
    {"id": "zh-CN-YunfanNeural", "name": "云凡（男·低沉）"},
    {"id": "zh-CN-YunhaoNeural", "name": "云皓（男·温和）"},
    {"id": "zh-CN-YunyeNeural", "name": "云野（男·活力）"},
    {"id": "zh-CN-YunxiaNeural", "name": "云夏（男·少年音）"},
    {"id": "zh-CN-KangkangNeural", "name": "康康（男·儿童）"},
    # ---- 中文·方言 ----
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北（东北话·女）"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮（陕西话·女）"},
    {"id": "zh-CN-YunfengNeural", "name": "云枫（浙江话·男）"},
    # ---- 中文·台湾 / 粤语 ----
    {"id": "zh-TW-HsiaoChenNeural", "name": "曉臻（台湾·女）"},
    {"id": "zh-TW-HsiaoYuNeural", "name": "曉雨（台湾·女）"},
    {"id": "zh-TW-YunJheNeural", "name": "雲哲（台湾·男）"},
    {"id": "zh-HK-HiuMaanNeural", "name": "曉曼（粤语·女）"},
    {"id": "zh-HK-HiuGaaiNeural", "name": "曉佳（粤语·女）"},
    {"id": "zh-HK-WanLungNeural", "name": "雲龍（粤语·男）"},
    # ---- 英文 ----
    {"id": "en-US-AriaNeural", "name": "Aria（美音·女）"},
    {"id": "en-US-JennyNeural", "name": "Jenny（美音·女）"},
    {"id": "en-US-MichelleNeural", "name": "Michelle（美音·女）"},
    {"id": "en-US-GuyNeural", "name": "Guy（美音·男）"},
    {"id": "en-US-ChristopherNeural", "name": "Christopher（美音·男）"},
    {"id": "en-US-RogerNeural", "name": "Roger（美音·男）"},
    {"id": "en-US-EricNeural", "name": "Eric（美音·男）"},
    {"id": "en-GB-SoniaNeural", "name": "Sonia（英音·女）"},
    {"id": "en-GB-LibbyNeural", "name": "Libby（英音·女）"},
    {"id": "en-GB-RyanNeural", "name": "Ryan（英音·男）"},
    {"id": "en-GB-ThomasNeural", "name": "Thomas（英音·男）"},
    {"id": "en-AU-NatashaNeural", "name": "Natasha（澳音·女）"},
    {"id": "en-IN-NeerjaNeural", "name": "Neerja（印度·女）"},
    # ---- 日语 ----
    {"id": "ja-JP-NanamiNeural", "name": "奈々美（日语·女）"},
    {"id": "ja-JP-AoiNeural", "name": "葵（日语·女）"},
    {"id": "ja-JP-KeitaNeural", "name": "圭太（日语·男）"},
    # ---- 韩语 ----
    {"id": "ko-KR-SunHiNeural", "name": "선히（韩语·女）"},
    {"id": "ko-KR-InJoonNeural", "name": "인준（韩语·男）"},
    # ---- 法语 / 德语 ----
    {"id": "fr-FR-DeniseNeural", "name": "Denise（法语·女）"},
    {"id": "fr-FR-HenriNeural", "name": "Henri（法语·男）"},
    {"id": "de-DE-KatjaNeural", "name": "Katja（德语·女）"},
    {"id": "de-DE-ConradNeural", "name": "Conrad（德语·男）"},
    # ---- 西语 / 葡语 / 俄语 / 意语 ----
    {"id": "es-ES-ElviraNeural", "name": "Elvira（西语·女）"},
    {"id": "es-ES-AlvaroNeural", "name": "Alvaro（西语·男）"},
    {"id": "pt-BR-FranciscaNeural", "name": "Francisca（葡语·女）"},
    {"id": "ru-RU-SvetlanaNeural", "name": "Svetlana（俄语·女）"},
    {"id": "ru-RU-DmitryNeural", "name": "Dmitry（俄语·男）"},
    {"id": "it-IT-ElsaNeural", "name": "Elsa（意语·女）"},
    {"id": "it-IT-DiegoNeural", "name": "Diego（意语·男）"},
]

_cache = {}  # (text, voice, rate) -> mp3 bytes

# 去掉控制字符（edge-tts 会拒收，导致 No audio was received 错误）
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _clean_text(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


# edge-tts 的 rate 格式: "+0%" / "+10%" / "-10%"
def _rate_str(rate: float) -> str:
    pct = int(round((rate - 1.0) * 100))
    return f"{pct:+d}%"


async def _try_synthesize(text: str, voice: str, rate_str: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice, rate=rate_str)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "error":
            raise RuntimeError(str(chunk.get("error") or "edge-tts error"))
    return bytes(audio)


async def synthesize(text: str, voice: str, rate: float) -> bytes:
    """合成音频；请求的声音失败时自动回退到默认声音（对前端透明）。"""
    rate_str = _rate_str(rate)
    last_err = None
    for attempt in (voice, DEFAULT_VOICE):
        try:
            audio = await _try_synthesize(text, attempt, rate_str)
            if audio:
                if attempt != voice:
                    print(
                        f"[tts] 回退到默认声音: {voice!r} -> {attempt!r} (text={text[:20]!r}…)",
                        file=sys.stderr,
                    )
                return audio
        except Exception as e:
            last_err = e
            print(
                f"[tts] 合成失败 voice={attempt!r}: {str(e)[:200]}",
                file=sys.stderr,
            )
            if attempt == voice and attempt != DEFAULT_VOICE:
                print(f"[tts] 正在回退到默认声音 {DEFAULT_VOICE!r}…", file=sys.stderr)
            continue
    raise last_err or RuntimeError("合成失败")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默日志
        pass

    # 允许浏览器跨域调用（pi-web 在 30141/31415，本服务在 8971）
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/voices"):
            self._send_json(200, {"ok": True, "voices": VOICES})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/tts":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad request"})
            return

        text = _clean_text(str(req.get("text", "")).strip())
        voice = str(req.get("voice") or DEFAULT_VOICE)
        try:
            rate = float(req.get("rate", 1.0))
        except (TypeError, ValueError):
            rate = 1.0

        if not text:
            self._send_json(400, {"ok": False, "error": "empty text"})
            return
        if len(text) > MAX_TEXT:
            self._send_json(400, {"ok": False, "error": f"text too long (> {MAX_TEXT})"})
            return

        key = (text, voice, rate)
        audio = _cache.get(key)
        if audio is None:
            try:
                audio = asyncio.run(synthesize(text, voice, rate))
            except Exception as e:  # 网络/语音名等错误
                self._send_json(500, {"ok": False, "error": str(e)[:300]})
                return
            if not audio:
                self._send_json(502, {"ok": False, "error": "no audio generated"})
                return
            if len(_cache) >= MAX_CACHE:
                _cache.clear()
            _cache[key] = audio

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)


if __name__ == "__main__":
    print(f"Pi Web TTS server listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
