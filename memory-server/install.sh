#!/usr/bin/env bash
# pi 记忆中心 —— 一键安装
#
# 做四件事:
#   1. 找 Python (>= 3.9)
#   2. 建虚拟环境 .venv
#   3. 装依赖
#   4. 跑交互式配置向导 setup.py
#
# 用法:  ./install.sh
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; NC=$'\033[0m'
ok()   { echo "  ${GREEN}✓${NC} $1"; }
warn() { echo "  ${YELLOW}!${NC} $1"; }
err()  { echo "  ${RED}✗${NC} $1"; }
die()  { err "$1"; exit 1; }

echo
echo "${BOLD}${CYAN}════ pi 记忆中心 · 安装 ════${NC}"
echo "${DIM}  目录: $ROOT${NC}"
echo

# ---------- 1. 找 Python ----------
echo "${BOLD}① 检查 Python${NC}"
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")
    if [ -n "$ver" ]; then
      major=${ver%%.*}; minor=${ver##*.}
      if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then PY="$cand"; break; fi
    fi
  fi
done
[ -n "$PY" ] || die "未找到 Python 3.9+。请先安装: brew install python@3.12  (或 https://www.python.org/downloads/)"
ok "$PY ($("$PY" -V 2>&1))"

# ---------- 2. 建 venv ----------
echo
echo "${BOLD}② 创建虚拟环境${NC}"
if [ -d ".venv" ]; then
  ok ".venv 已存在，跳过"
else
  "$PY" -m venv .venv || die "创建 .venv 失败"
  ok "已创建 .venv"
fi

VENV_PY="$ROOT/.venv/bin/python"
[ -x "$VENV_PY" ] || die "找不到 $VENV_PY"

# macOS: 若 python 是 x86_64 混编而系统是 arm64, 需 arch 前缀，否则 pydantic-core 会报架构错
PYRUN="$VENV_PY"
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
  if file "$VENV_PY" 2>/dev/null | grep -q "x86_64"; then
    PYRUN="arch -arm64 $VENV_PY"
    warn "检测到 x86_64 混编 Python，后续用 'arch -arm64' 前缀运行"
  fi
fi

# ---------- 3. 装依赖 ----------
echo
echo "${BOLD}③ 安装依赖${NC}"
$PYRUN -m pip install --upgrade pip -q 2>/dev/null || warn "pip 升级跳过"
if $PYRUN -m pip install -q -r requirements.txt; then
  ok "依赖安装完成"
else
  die "依赖安装失败。试试换国内镜像:
       $PYRUN -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
fi

# ---------- 4. 配置向导 ----------
echo
echo "${BOLD}④ 配置推理后端${NC}"
if [ -f "config.yaml" ]; then
  warn "config.yaml 已存在，向导会覆盖它 (会自动备份)"
fi
$PYRUN setup.py || die "配置向导未完成"

# ---------- 完成 ----------
echo "${BOLD}${GREEN}════ 安装完成 ════${NC}"
echo
echo "  启动服务:"
echo "    ${BOLD}./run.sh${NC}                # 前台守护 (推荐, 前台运行)"
echo "    ${BOLD}nohup ./run.sh &${NC}        # 后台常驻"
echo
echo "  健康检查:"
echo "    curl -s http://127.0.0.1:8970/api/health"
echo
echo "  接入 pi:"
echo "    见 README.md 「接入 pi」章节"
echo
