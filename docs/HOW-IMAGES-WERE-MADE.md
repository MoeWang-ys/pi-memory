# 插图是怎么做出来的

> 记录 2026-09-30 那次给 README 配图所用的**方法**，不是产物。
> 下次要再做图，照这个来，别从零摸索。

## 结论先行：用的不是"生图 AI"

**没有用任何文生图模型**（Nano Banana / GPT-Image / Gemini 都没用上）。

用的是：**手写 SVG + Chrome headless 转 PNG**。

原因后面「为什么不用生图」一节讲，先讲怎么做。

---

## 完整方法（5 步）

### 第 1 步：手写 SVG

直接写 SVG 源码，不借助任何工具。关键决策：

| 项 | 选择 | 理由 |
|---|---|---|
| 尺寸 | `viewBox="0 0 1200 620"`，宽固定 1200 | 视网膜屏 1.5x 渲染就是 1800px，够清晰 |
| 底 | 暖砂渐变 `#FBF8F2 → #F2EBDE` | 见「配色」一节 |
| 文字 | 每个 `<text>` 都写全字体栈 | 跨平台，见下方「字体陷阱」 |
| 结构 | 全部用 `<g transform="translate(x,y)">` 分组 | 改一个组的坐标，整块跟着走，不用逐个改数字 |

**字体栈必须写全**（否则 Windows/Linux 上中文变方块）：

```xml
font-family="ui-sans-serif,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"
```

各平台回退链：macOS 走 `PingFang SC` → Windows 走 `Microsoft YaHei` → Linux 走 `sans-serif`。

### 第 2 步：Chrome headless 转 PNG

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor=1.5 \
  --window-size=1200,620 \
  --screenshot=/tmp/out.png \
  "file:///绝对路径/hero.svg"
```

- `--force-device-scale-factor=1.5` → 2x 太大（文件肥），1x 在高分屏糊，1.5 是甜点
- `--window-size` 必须 **≥ viewBox 尺寸**，否则被裁
- 路径要**绝对路径**，`file://` + 相对路径不可靠

### 第 3 步：PNG 压缩（必须做）

Chrome 直出的 PNG 是 430~650KB，对 README 太重。用 Pillow 压：

```python
from PIL import Image
im = Image.open('hero.png').convert('RGB')
q = im.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
q.save('hero.png', optimize=True)
```

**效果**：430KB → 140KB（**-67%**），平均色差 **0.4~0.8/255**（肉眼不可见）。

要点：
- `colors=128` 够用（扁平插画配色没那么多层次），256 也没小多少
- `dither=Image.NONE` ★ 关键 —— 开抖动反而增大体积且引入噪点
- 压完**必须验证色差**，别压出偏色：

```python
import numpy as np
a = np.asarray(im).astype(int); b = np.asarray(q.convert('RGB')).astype(int)
print(np.abs(a-b).mean())   # 应 < 1.0
```

### 第 4 步：验证（★ 这步最重要，见下）

### 第 5 步：README 引用

```markdown
<img src="docs/assets/hero.svg" alt="..." width="100%" />
```

**用 SVG 而非 PNG** —— 矢量在任何分辨率都清晰，且只有 8KB（PNG 是 140KB）。
PNG 只在**本地预览**时用（因为 base64 内嵌需要位图）。

---

## ★ 验证方法：不能靠眼睛，要靠程序

**根本约束**：当时用的模型**不支持图片输入**，也就是说 —— 我看不见自己画的东西。

所以整个流程靠**程序化验证**，而不是"应该没问题"。

### ① 文本溢出检测（最常用）

原理：在 Chrome 里渲染 SVG，用 `getBBox()` 量每个 `<text>` 的真实包围盒，跟画布边界比。

```javascript
// 注入到页面里执行
document.querySelectorAll('svg text').forEach(function(t){
  var b = t.getBBox();
  out.push({txt: t.textContent.trim(), x2: b.x+b.width, y2: b.y+b.height});
});
document.title = JSON.stringify(out);   // 用 title 带出来
```

```bash
"$CHROME" --headless --virtual-time-budget=6000 \
  --dump-dom "file:///tmp/measure.html" > dom.html
# 再从 <title> 里正则提取 JSON
```

拿到数据后逐条判断 `x2 > 1200` 或 `y2 > 620`。

**实测战绩**：
- 三张中文图：**86 个文本元素，0 溢出**
- 三张英文图重排版后：**0 溢出**

### ② 像素分析（判断"东西画出来没有"）

```python
from PIL import Image
import numpy as np
a = np.asarray(Image.open('x.png').convert('RGB')).astype(int)
lum = a.mean(axis=2)
ink = lum < 160                      # 深色像素 = 墨迹
print(f"墨迹占比 {100*ink.sum()/ink.size:.2f}%  最暗 {lum.min():.0f}")
# 按横带切分，确认每块区域都有内容
for band in np.array_split(ink, 6, axis=0):
    print(band.sum())
```

这招用来确认：标题区、卡片区、底部注解**都渲染了**，而不是某块空白。

### ③ 主色采样（确认配色没跑偏）

```python
print(im.getpixel((x*2, y*2)))   # 注意 ×2，因为是 1.5x/2x 渲染
```

采样已知位置，确认颜色符合设计（比如色条应为赤陶橙 `RGB(200,87,36)`）。

### ④ 模拟 GitHub 加载（★ 最容易被忽略）

README 里图片是 `<img src="...">`，这跟直接打开 SVG **不一样**。

```html
<img src="file:///.../hero.svg">
```

用 Chrome 截图后数「内容带」数量 —— 应为 3（3 张图）。

**这步抓出过一个真 bug**：Chrome 对 `file://` 有本地文件安全限制，`naturalWidth` 返回 `0` 表示**图片根本没加载**。后来把预览改成 base64 内嵌才解决。

---

## 配色：为什么是暖砂 + 赤陶橙

选了 **暖砂底 `#FBF8F2` + 赤陶橙 `#C04A1A` + 墨青 `#1F4E4A`**。

依据来自本机 skill `huashu-design`（`~/.agents/skills/huashu-design/`）里明写的**审美禁区**：

> ❌ **GitHub-dark 偷懒解**：均匀深蓝底（`#0D1117`）+ 通用青/紫霓虹 glow
> ❌ 激进紫渐变万能公式、emoji 当图标、圆角卡片+左彩 border accent
> ❌ **封面图加个人署名/水印**

所以刻意避开：不用深蓝底、不用紫渐变、不加署名水印。

选暖砂是因为记忆这个主题天然偏"旧物/笔记/纸感"，赤陶橙做强调色比冷色更有温度。

---

## 为什么没用生图 AI

当时**主动找过**，找到 3 个 skill，全部缺运行条件：

| Skill | 位置 | 缺什么 |
|---|---|---|
| `nano-banana-pro` | `~/.workbuddy/skills-marketplace/skills/` | `GEMINI_API_KEY` ❌ + `uv` ❌ |
| `openai-image-gen` | 同上 | `OPENAI_API_KEY` ❌ + `openai` 包 ❌ |
| `nano-banana-expert` | `~/.workbuddy/connectors-marketplace/` | 需要 AI-HIVE Connector |

环境实测：

```
GEMINI_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY / ANTHROPIC_API_KEY   全部未设置
uv   未安装
openai python 包   未安装
```

还上网搜了 GitHub 上知名的（`Cocoon-AI/architecture-diagram-generator` **7,375 stars**、`op7418/NanoBanana-PPT-Skills` 3.3k、`YouMind-OpenLab/nano-banana-pro-prompts-recommend-skill` 1.9k），**全部需要 API key**。

**唯一不需要 key 的**是 `Cocoon-AI/architecture-diagram-generator`，已装到：

```
~/.pi/agent/skills/architecture-diagram/
├── SKILL.md              8383B
├── LICENSE               MIT
└── resources/template.html  17492B
```

但它是**暗色霓虹风**（`#020617` 底 + 青色发光），跟暖砂调子冲突，所以只**借了两个技巧**，没直接套样式：

1. **箭头要画在方框之前** —— SVG 按文档顺序绘制，后画会盖住先画
2. **组件间最小间距 40px** —— 否则容易重叠

（照第 1 条回头检查了自己的图：4 条箭头端点全落在空白处，没被遮挡。）

### 那手写 SVG 图什么

| | 生图 AI | 手写 SVG |
|---|---|---|
| 要 key | 要 | 不要 |
| 文字 | **经常画错字**（尤其中文） | 100% 准确可控 |
| 改一个字 | 重新生成 → 全变 | 改一处 → 只变一处 |
| 体积 | 几百 KB~几 MB | 8KB（矢量） |
| 风格一致 | 每次飘一点 | 完全一致 |

**结论**：做**信息图/架构图/对比图**（有准确文字）→ 手写 SVG 完胜。
做**插画/氛围图/写实图** → 才轮到生图 AI。

---

## 踩过的坑（都实测过）

| 坑 | 现象 | 解法 |
|---|---|---|
| **中英文字宽不同** | 中文替换成英文后溢出 | 英文版单独量一遍包围盒；调小 `font-size` |
| **CJK 双宽** | ASCII 图算好的对齐，中文一放就歪 | 别用 ASCII 画含中文的图；要画就按 `2` 宽计 |
| `east_asian_width` 不可靠 | `·`/`─` 在不同字体会宽窄不一 | 改用 Mermaid 或不依赖对齐的布局 |
| `file://` 图片被拦 | `naturalWidth = 0`，预览不显示 | base64 内嵌进 HTML |
| `fetch()` 在 `file://` 被 CORS 拦 | 注入脚本拿不到 SVG 内容 | 把 SVG 直接**内联**进 HTML 字符串 |
| 检查脚本跨 group 误报 | 报"箭头被遮挡"其实没有 | `transform` 坐标要累加；或改用 `getBoundingClientRect()` |
| cairosvg 装不上 | `libcairo.2.dylib` 找不到 | 直接用 Chrome headless，别装 cairo |
| `--window-size` 小于 viewBox | 图被裁 | window 尺寸 ≥ viewBox 尺寸 |

---

## 可复用的工具脚本

### 截图

```bash
#!/bin/bash
# shot.sh <in.svg> <out.png> <width> <height> [scale]
IN="$1"; OUT="$2"; W="$3"; H="$4"; SCALE="${5:-1.5}"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor="$SCALE" --window-size="${W},${H}" \
  --screenshot="$OUT" "file://$IN"
```

### 文本溢出检查

```bash
python3 - <<'PY'
import re, json, html, subprocess
SVG, W, H = 'assets/hero.svg', 1200, 620
svg = open(SVG, encoding='utf-8').read()
page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
 f'<style>body{{margin:0}}#host{{width:{W}px;height:{H}px}}</style></head>'
 f'<body><div id="host">{svg}</div><script>setTimeout(function(){{'
 'var o=[];document.querySelectorAll("#host svg text").forEach(function(t){'
 'var b=t.getBBox();o.push({t:t.textContent.trim().slice(0,44),'
 'x2:+(b.x+b.width).toFixed(1),y2:+(b.y+b.height).toFixed(1)});});'
 'document.title=JSON.stringify(o);},600);</script></body></html>')
open('/tmp/_chk.html','w',encoding='utf-8').write(page)
r = subprocess.run(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "--headless","--disable-gpu","--no-sandbox","--virtual-time-budget=6000",
  "--dump-dom","file:///tmp/_chk.html"], capture_output=True, text=True)
d = json.loads(html.unescape(re.search(r'<title>(.*?)</title>', r.stdout, re.S).group(1)))
bad = [i for i in d if i['x2'] > W or i['y2'] > H]
print(f"{len(d)} 个文本, 溢出 {len(bad)}")
for b in bad: print("  ⚠", b['t'][:40], b['x2'], b['y2'])
PY
```

---

## 本次产物

```
docs/assets/
  hero.svg / hero-en.svg              105/110 KB   首屏 before-after 对比
  flow.svg / flow-en.svg              134/137 KB   说一次，以后都记得
  architecture.svg / architecture-en.svg  137/140 KB   它由哪几块组成

共 12 个文件（6 SVG + 6 PNG），1.1MB
```

中英各一套 —— 因为**文字内容不同**，不是同一个文件翻译一下就能用。

---

**日期**：2026-09-30
**适用场景**：README 插图、架构图、流程对比图、任何"文字必须准确"的信息图
**不适用**：插画、写实图、氛围图（那种还是得生图 AI）

---

## 附：为什么这次没用生图 skill（选型判断）

如果你的环境**有** API key，判断标准是：

| 要做的东西 | 用什么 |
|---|---|
| 架构图、流程图、对比图、任何带准确文字的信息图 | **手写 SVG**（本方法） |
| 插画、氛围图、写实场景、人物 | 生图 AI（Nano Banana / GPT-Image） |
| 幻灯片整页 | `frontend-slides` / `beautiful-html-templates`（HTML 原生） |

**分界线是「文字是否重要」**。生图 AI 画中文几乎必错，而信息图的价值全在文字上。

如果确实要用生图 AI，本机可用的 skill：

```bash
# 需要 GEMINI_API_KEY + uv
~/.workbuddy/skills-marketplace/skills/nano-banana-pro/

# 需要 OPENAI_API_KEY + openai python 包
~/.workbuddy/skills-marketplace/skills/openai-image-gen/
```

装依赖后跑法见各自 `SKILL.md`。
