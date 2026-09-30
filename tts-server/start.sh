#!/usr/bin/env bash
# Pi Web 神经语音服务一键启动
# 用法: bash start.sh   （首次会自动建 .venv 并装依赖）
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[tts-server] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "[tts-server] 安装/检查依赖 ..."
./.venv/bin/pip install -q -r requirements.txt

echo "[tts-server] 启动服务 http://127.0.0.1:8971 (Ctrl+C 停止)"
exec ./.venv/bin/python server.py
