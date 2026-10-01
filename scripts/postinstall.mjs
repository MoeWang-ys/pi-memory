#!/usr/bin/env node
/**
 * pi-memory 后置安装脚本
 *
 * npm 只能装 JS 那一半（扩展 + skill）。真正的引擎是 Python 服务，
 * 装完必须初始化，否则扩展连不上，用户看到的就是"装了没用"。
 *
 * 这个脚本做三件事：
 *   1. 定位引擎目录（本地开发 / git 克隆 / 已装过的位置）
 *   2. 没初始化过 → 建 venv、装依赖、跑配置向导
 *   3. 打印怎么启动
 *
 * 设计原则：**任何一步失败都不阻断安装**（npm install 已成功），
 * 只打印清晰的下一步指引。
 */
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import os from "node:os";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOME = os.homedir();

const C = {
  b: "\x1b[1m", d: "\x1b[2m", g: "\x1b[32m", y: "\x1b[33m",
  r: "\x1b[31m", c: "\x1b[36m", n: "\x1b[0m",
};
const ok = (m) => console.log(`  ${C.g}✓${C.n} ${m}`);
const warn = (m) => console.log(`  ${C.y}!${C.n} ${m}`);
const err = (m) => console.log(`  ${C.r}✗${C.n} ${m}`);
const dim = (m) => console.log(`  ${C.d}${m}${C.n}`);

/** 找一个包含 app.py + requirements.txt 的引擎目录 */
function findEngine() {
  const candidates = [
    process.env.PI_MEMORY_HOME,
    // 这个 npm 包是从仓库根发布的 → 同级有 memory-server
    join(HERE, "..", "memory-server"),
    join(HERE, ".."),
    // 常见克隆位置
    // 新名字(仓库 2026-10-01 改名为 pi-memory)优先
    join(HOME, "Document/pi/pi-memory/memory-server"),
    join(HOME, "pi-memory/memory-server"),
    // 旧名字: 本地已有克隆的人仍然有效，不要删
    join(HOME, "Document/pi/pi-web-extensions/memory-server"),
    join(HOME, "pi-web-extensions/memory-server"),
  ].filter(Boolean);

  for (const c of candidates) {
    const p = resolve(c);
    if (existsSync(join(p, "app.py")) && existsSync(join(p, "requirements.txt"))) {
      return p;
    }
  }
  return null;
}

function which(cmd) {
  const r = spawnSync(process.platform === "win32" ? "where" : "which", [cmd], {
    encoding: "utf8",
  });
  return r.status === 0 ? r.stdout.trim().split("\n")[0] : null;
}

function findPython() {
  for (const c of ["python3.12", "python3.11", "python3.10", "python3.9", "python3", "python"]) {
    const p = which(c);
    if (!p) continue;
    const v = spawnSync(c, ["-c", "import sys;print('%d.%d'%sys.version_info[:2])"], {
      encoding: "utf8",
    });
    if (v.status !== 0) continue;
    const [maj, min] = v.stdout.trim().split(".").map(Number);
    if (maj === 3 && min >= 9) return { cmd: c, version: v.stdout.trim() };
  }
  return null;
}

/** macOS + arm64 上，混编 x86_64 的 Python 需要 arch 前缀 */
function pyRun(engine, args) {
  const venvPy = join(engine, ".venv", "bin", "python");
  let cmd = venvPy;
  let argv = args;
  if (process.platform === "darwin" && process.arch === "arm64" && existsSync(venvPy)) {
    const f = spawnSync("file", [venvPy], { encoding: "utf8" });
    if (f.stdout && f.stdout.includes("x86_64")) {
      cmd = "arch";
      argv = ["-arm64", venvPy, ...args];
    }
  }
  return spawnSync(cmd, argv, { cwd: engine, stdio: "inherit" });
}

function header(t) {
  console.log(`\n${C.b}${C.c}════ ${t} ════${C.n}\n`);
}

function main() {
  console.log(`\n${C.b}${C.c}pi-memory${C.n} ${C.d}· 记忆引擎初始化${C.n}`);

  // ---------- 1. 找引擎 ----------
  const engine = findEngine();
  if (!engine) {
    header("找不到记忆引擎");
    warn("这个 npm 包只含 JS 那一半（pi 扩展 + skill）。");
    warn("引擎是 Python 服务，需要单独获取。\n");
    console.log(`  ${C.b}获取引擎：${C.n}`);
    dim("git clone https://github.com/MoeWang-ys/pi-memory.git");
    dim("cd pi-memory/memory-server && ./install.sh");
    console.log();
    dim("装好后重新跑：npx pi-memory-setup");
    dim("或指定路径：PI_MEMORY_HOME=/path/to/memory-server npx pi-memory-setup");
    console.log();
    process.exit(0);
  }
  ok(`引擎位置 ${engine.replace(HOME, "~")}`);

  // ---------- 2. 已初始化过？ ----------
  const hasVenv = existsSync(join(engine, ".venv", "bin", "python"));
  const hasConfig = existsSync(join(engine, "config.yaml"));

  if (hasVenv && hasConfig) {
    ok("已初始化（venv + config.yaml 都在）");
    startHint(engine);
    return;
  }

  // ---------- 3. 初始化 ----------
  if (!hasVenv) {
    header("① 创建虚拟环境");
    const py = findPython();
    if (!py) {
      err("未找到 Python 3.9+");
      dim("macOS:  brew install python@3.12");
      dim("其他:   https://www.python.org/downloads/");
      process.exit(0);
    }
    ok(`Python ${py.version} (${py.cmd})`);
    const r = spawnSync(py.cmd, ["-m", "venv", ".venv"], { cwd: engine, stdio: "inherit" });
    if (r.status !== 0) {
      err("创建 venv 失败，请手动执行：");
      dim(`cd ${engine} && python3 -m venv .venv`);
      process.exit(0);
    }
    ok("已创建 .venv");
  } else {
    ok("虚拟环境已存在");
  }

  header("② 安装依赖");
  {
    const r = pyRun(engine, ["-m", "pip", "install", "-q", "-r", "requirements.txt"]);
    if (r.status !== 0) {
      warn("依赖安装失败，试试国内镜像：");
      dim(`cd ${engine}`);
      dim(".venv/bin/python -m pip install -r requirements.txt \\");
      dim("  -i https://pypi.tuna.tsinghua.edu.cn/simple");
    } else {
      ok("依赖就绪");
    }
  }

  if (!hasConfig) {
    header("③ 配置推理后端");
    dim("接下来是交互式向导，会探测你机器上可用的模型让你选。\n");
    pyRun(engine, ["setup.py"]);
  }

  startHint(engine);
}

function startHint(engine) {
  header("下一步：启动服务");
  const dir = engine ? engine.replace(HOME, "~") : "<引擎目录>";
  console.log(`  ${C.b}cd ${dir} && nohup ./run.sh &${C.n}\n`);
  console.log(`  ${C.d}或前台运行（能看日志）：${C.n}  ${C.b}cd ${dir} && ./run.sh${C.n}\n`);
  console.log(`  验证：${C.b}curl -s http://127.0.0.1:8970/api/health${C.n}`);
  dim('  看到 "ok": true 就成了\n');
  console.log(`  ${C.d}服务没起来时扩展不会报错，只是静默不工作。${C.n}`);
  console.log(`  ${C.d}在 pi 里用 /memory 状态 可以检查。${C.n}\n`);
}

main();
