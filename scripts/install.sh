#!/bin/bash
# pi-web-extensions 一键安装
# 1. 安装 memory-server 依赖并后台启动
# 2. 将 memory-extension 软链到 ~/.pi/agent/extensions/
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVER_DIR="$ROOT/memory-server"
VENV="$SERVER_DIR/.venv"
EXT_SRC="$ROOT/memory-extension/index.ts"
EXT_TARGET="$HOME/.pi/agent/extensions/memory.ts"
PORT=8970

echo "==> [1/3] 安装 memory-server 依赖"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -r "$SERVER_DIR/requirements.txt"

echo "==> [2/3] 启动 memory-server (127.0.0.1:$PORT)"
if lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "    已有一个服务占用 $PORT, 跳过启动 (如无响应请手动重启)"
else
  cd "$SERVER_DIR"
  nohup "$VENV/bin/python" app.py > "$SERVER_DIR/server.log" 2>&1 &
  echo "    已后台启动, 日志: $SERVER_DIR/server.log"
fi

echo "==> [3/3] 安装 pi 扩展"
mkdir -p "$HOME/.pi/agent/extensions"
if [ -L "$EXT_TARGET" ] || [ -f "$EXT_TARGET" ]; then
  echo "    已存在 $EXT_TARGET, 先移除旧文件"
  rm -f "$EXT_TARGET"
fi
ln -s "$EXT_SRC" "$EXT_TARGET"
echo "    已软链: $EXT_TARGET"

echo ""
echo "✔ 安装完成。请重启 pi 使扩展生效:"
echo "    - CLI: 重启 pi, 或在会话中输入 /reload"
echo "    - pi-web: 刷新页面后新会话生效"
echo ""
echo "  管理面板: http://127.0.0.1:$PORT"
echo "  配置:     $SERVER_DIR/config.yaml"
echo "  卸载:     rm $EXT_TARGET && lsof -tiTCP:$PORT | xargs kill"
