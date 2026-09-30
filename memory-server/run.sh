#!/usr/bin/env bash
# pi 记忆中心 —— 守护启动器
#
# 用途: 保证 memory-server 常驻。进程挂掉自动拉起。
#
# 用法:
#   ./run.sh            前台运行 (带自动重启循环)
#   ./run.sh --once     只跑一次, 退出后不重启
#   nohup ./run.sh &    后台常驻
#
# 说明: macOS + arm64 上, 若 venv 内的 Python 是 x86_64 混编,
#       必须用 arch -arm64 启动, 否则 pydantic_core 会报架构不匹配。
#       本脚本会自动检测并加上该前缀。

cd "$(dirname "$0")" || exit 1

PY=".venv/bin/python"
LOG_DIR="../../logs"
LOG="$LOG_DIR/memory.log"
mkdir -p "$LOG_DIR"

ONCE=0
[ "${1:-}" = "--once" ] && ONCE=1

# ---------- 自动处理 arch 前缀 ----------
PYRUN="$PY"
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  if file "$PY" 2>/dev/null | grep -q "x86_64"; then
    PYRUN="arch -arm64 $PY"
  fi
fi

[ -x "$PY" ] || { echo "[run.sh] 找不到 $PY, 请先运行 ./install.sh"; exit 1; }

# ---------- 端口占用时先清理, 避免 bind 失败 ----------
PORT=$(grep -E '^\s*port:' config.yaml 2>/dev/null | head -1 | grep -oE '[0-9]+' || echo 8970)
if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[run.sh] 端口 $PORT 已被占用, 先停止旧进程"
  lsof -tiTCP:"$PORT" -sTCP:LISTEN | xargs kill 2>/dev/null
  sleep 2
fi

while true; do
  echo "[run.sh] $(date '+%F %T') 启动 memory-server" >> "$LOG"
  $PYRUN -u app.py >> "$LOG" 2>&1
  code=$?
  echo "[run.sh] $(date '+%F %T') 进程退出 (code=$code)" >> "$LOG"

  if [ $ONCE -eq 1 ]; then
    break
  fi

  # 崩溃重启限流: 退出过快说明起不来, 等久一点避免刷日志
  if [ $code -ne 0 ]; then
    echo "[run.sh] 10s 后重启..." >> "$LOG"
    sleep 10
  else
    echo "[run.sh] 正常退出, 5s 后重启..." >> "$LOG"
    sleep 5
  fi
done
