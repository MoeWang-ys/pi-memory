---
name: svg-infographic
description: 做信息图/架构图/流程图/对比图/README 配图的完整方法论 —— 手写 SVG + Chrome headless，附程序化验证脚本（测文本溢出、像素分布、配色）。当用户要求「给 README 配图」「画个架构图」「做个流程图」「画张图解释」「画图说明」「生成插图」「做个对比图」「画系统架构」「整个图看看」时使用。触发词：配图、插图、架构图、流程图、对比图、信息图、画图、画张图、画个图、README 配图、系统图、示意图、illustration、infographic、architecture diagram、SVG 图。（与 Mermaid 分工：要精确排版/视觉设计用本 skill；纯逻辑关系图可直接用 Mermaid 写进 markdown。）
---

# SVG 信息图制作

用**手写 SVG + Chrome headless 转 PNG** 做信息图。

> **不要用文生图 AI 画带文字的图**。生图模型画中文几乎必错，而信息图的价值 100% 在文字上。
> 分界线：**文字是否重要**。架构图/流程图/对比图 → 本 skill；插画/氛围图/写实图 → 才用生图 AI。

## 决策：该不该用本 skill

| 要做的东西 | 用什么 |
|---|---|
| 架构图、流程图、对比图、数据流图 | ✅ **本 skill（手写 SVG）** |
| README 首屏 banner（带准确文字） | ✅ 本 skill |
| 插画、氛围图、写实场景、人物、产品图 | ❌ 用生图 AI |
| 幻灯片整页 | ❌ 用 `frontend-slides` / `beautiful-html-templates` |
| 纯逻辑关系图（无精确排版要求） | 可用 Mermaid（GitHub 原生渲染，零成本） |

**为什么手写 SVG 而不是生图 AI**：

| | 生图 AI | 手写 SVG |
|---|---|---|
| 要 API key | 要 | **不要** |
| 文字准确性 | **经常画错字**（尤其中文） | 100% 可控 |
| 改一个字 | 重新生成 → 整张全变 | 改一处 → 只变一处 |
| 体积 | 几百 KB ~ 几 MB | **~8KB（矢量）** |
| 风格一致性 | 每次飘 | 完全一致 |
| 可版本管理 | ❌ 二进制 | ✅ 文本 diff |

---

## 完整流程（5 步）

### 第 1 步：手写 SVG

**基准尺寸**：宽固定 `1200`，高度按内容 480~700。

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 620" width="1200" height="620"
     role="img" aria-label="图的描述（无障碍 + SEO）">
```

**字体栈必须写全** —— 否则 Windows/Linux 上中文变方块：

```xml
font-family="ui-sans-serif,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif"
```

回退链：macOS `PingFang SC` → Windows `Microsoft YaHei` → Linux `sans-serif`

**用 `<g transform>` 分组**，改一个组的坐标整块跟着走：

```xml
<g transform="translate(52,128)">
  <rect .../>
  <text .../>
</g>
```

**箭头的 marker 定义**：

```xml
<marker id="arr" viewBox="0 0 10 10" refX="8" refY="5"
        markerWidth="6.5" markerHeight="6.5" orient="auto">
  <path d="M0 0 L10 5 L0 10 z" fill="#C08A6A"/>
</marker>
```

**z-order 铁律**：SVG 按文档顺序绘制，**后画的盖住先画的**。所以：
- 箭头如果会穿过方框 → **先画箭头，再画方框**
- 或者干脆别让箭头穿过方框（推荐，更简单）

### 第 2 步：Chrome headless 转 PNG

```bash
#!/bin/bash
# shot.sh <in.svg> <out.png> <width> <height> [scale]
IN="$1"; OUT="$2"; W="$3"; H="$4"; SCALE="${5:-1.5}"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor="$SCALE" \
  --window-size="${W},${H}" \
  --screenshot="$OUT" \
  "file://$IN"
```

**三个坑**：
- `--window-size` 必须 **≥ viewBox 尺寸**，否则被裁
- 路径要**绝对路径**，`file://` + 相对路径不可靠
- `SCALE=1.5` 是甜点：2x 文件太肥，1x 在高分屏糊

**别用 cairosvg** —— macOS 上缺 `libcairo.2.dylib`，装了也跑不起来。Chrome 已经有，直接用。

### 第 3 步：压缩 PNG（必须做）

Chrome 直出 430~650KB，对 README 太重。

```python
from PIL import Image
import numpy as np, os

im = Image.open('hero.png').convert('RGB')
before = os.path.getsize('hero.png')
q = im.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
q.save('hero.png', optimize=True)

# 必须验证色差，别压出偏色
a = np.asarray(im).astype(int)
b = np.asarray(q.convert('RGB')).astype(int)
err = np.abs(a - b).mean()
print(f"{before//1024}KB -> {os.path.getsize('hero.png')//1024}KB  色差={err:.2f}")
assert err < 1.5, "压缩过头了，调大 colors"
```

**实测效果**：430KB → 140KB（**-67%**），色差 **0.4~0.8/255**（肉眼不可见）。

关键点：
- `colors=128` 够（扁平插画配色层次少）；256 省不了多少
- **`dither=Image.NONE` ★ 必须关** —— 开抖动反而**增大体积**且引入噪点
- 压完**一定验证色差**

### 第 4 步：★ 程序化验证（最重要）

**假设你看不见图**（很多模型不支持图片输入）。所以不能靠"应该没问题"，要靠实测。

#### ① 文本溢出检测（查得最多）

在 Chrome 里渲染 SVG，用 `getBBox()` 量每个 `<text>` 的真实包围盒：

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
for b in bad: print("  ⚠", b['t'][:40], "x2=",b['x2'], "y2=",b['y2'])
PY
```

**为什么必须内联 SVG**：`fetch()` 在 `file://` 下被 CORS 拦，必须把 SVG 内容直接拼进 HTML 字符串。

**注意**：`getBBox()` 返回的是**元素自身坐标系**，如果 `<text>` 在 `<g transform>` 里，拿到的是组内坐标。要拿屏幕坐标用 `getBoundingClientRect()`：

```javascript
var b = t.getBoundingClientRect();
var host = document.getElementById('host').getBoundingClientRect();
o.push({l: b.left - host.left, r: b.right - host.left});
```

#### ② 像素分析（确认"东西真的画出来了"）

```python
from PIL import Image
import numpy as np

a = np.asarray(Image.open('x.png').convert('RGB')).astype(int)
lum = a.mean(axis=2)
ink = lum < 160     # 深色像素 = 墨迹

print(f"墨迹占比 {100*ink.sum()/ink.size:.2f}%")
print(f"最暗亮度 {lum.min():.0f}")   # 应该 < 80，否则说明没画出深色

# 按横带切分，确认每块区域都有内容（而不是某块空白）
for i, band in enumerate(np.array_split(ink, 6, axis=0)):
    print(f"  带{i}: {band.sum()}")
```

#### ③ 主色采样（确认配色没跑偏）

```python
print(im.getpixel((x, y)))   # 注意：1.5x 渲染的话坐标要 ×1.5
```

采样已知位置（比如色条），确认颜色符合设计。

#### ④ 模拟 GitHub 加载（★ 最容易被忽略）

README 里是 `<img src="...">`，**跟直接打开 SVG 不一样**：

```html
<img src="file:///绝对路径/hero.svg">
```

Chrome 截图后数「内容带」数量，应等于图片数。

**这步抓到过真 bug**：Chrome 对 `file://` 有本地文件安全限制，`naturalWidth` 返回 `0` 表示**图片根本没加载**。解法见「本地预览」一节。

### 第 5 步：README 引用

```markdown
<img src="docs/assets/hero.svg" alt="图的描述" width="100%" />
```

**引用 SVG 而非 PNG** —— 矢量任何分辨率都清晰，且只有 8KB（PNG 是 140KB）。
PNG 只在**本地预览**时用（base64 内嵌需要位图）。

---

## 配色

### 默认建议：暖砂 + 赤陶橙 + 墨青

| 用途 | 色值 |
|---|---|
| 底（渐变） | `#FBF8F2` → `#F2EBDE` |
| 主色（强调） | `#C04A1A` → `#DC8250` |
| 辅色（次要） | `#1F4E4A` → `#35857A` |
| 正文 | `#2B2118` |
| 次要文字 | `#8A7A66` |
| 极淡文字 | `#A89880` |
| 卡片底 | `#FFFFFF` (opacity 0.65~0.9) |
| 描边 | `#E0D5C4` |

### 审美禁区（来自 huashu-design）

> ❌ **GitHub-dark 偷懒解**：均匀深蓝底 `#0D1117` + 通用青/紫霓虹 glow
> ❌ 激进紫渐变万能公式
> ❌ Emoji 当图标（品牌本身用则例外）
> ❌ 圆角卡片 + 左彩色 border accent（烂大街组合）
> ❌ **封面图加个人署名/水印**

**边界**：「品牌本身用」是唯一能合法破例的理由。

如果用户有品牌色，**优先用品牌色** —— 本 skill 的配色只是默认值。

---

## 中英双语图

如果 README 有中英两版，图也要两套 —— **不是翻译一下就能用**。

**核心问题：中英文字宽不同**。

做法：写完 SVG 后，用脚本批量替换文案，**然后重新量一遍包围盒**：

```python
M = [("同一句话，两种体验", "The same sentence, two different experiences"),
     ("说一次，以后都记得", "Say it once. It remembers.")]
s = open('hero.svg', encoding='utf-8').read()
for zh, en in M:
    if f">{zh}<" in s:
        s = s.replace(f">{zh}<", f">{en}<")
    else:
        print("未命中:", zh)     # ★ 一定要检查，长句可能带换行
open('hero-en.svg','w',encoding='utf-8').write(s)
```

**注意**：
- 文案在 `<text>` 里独立成行时，`>{zh}<` 匹配不到 → 要按行处理
- 英文普遍**比中文宽**，替换后**必须重跑溢出检测**，超了就调小 `font-size` 或缩短文案
- 英文版可以用 `text-anchor="middle"` 居中，比左对齐更抗震

---

## 本地预览（给用户看效果）

用户可能想先看看图长什么样。生成一个预览 HTML：

**★ 必须把图片转 base64 内嵌** —— 否则 Chrome 拦 `file://` 图片，预览里是空白：

```python
import base64, re, markdown

def data_uri(p):
    with open(p,'rb') as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()

md = open('README.md', encoding='utf-8').read()
# 引用 PNG（base64 需要位图）
md = re.sub(r'src="(docs/assets/[^"]+)"',
            lambda m: 'src="' + data_uri(m.group(1).rsplit('.',1)[0] + '.png') + '"', md)
body = markdown.markdown(md, extensions=['tables','fenced_code'])
open('docs/preview.html','w',encoding='utf-8').write(f'<html><body>{body}</body></html>')
```

双语版可以加切换 tab（`display:none` 切两个容器）。

**预览文件加进 `.gitignore`**（自动生成的，不入库）。

---

## 架构图专用技法

> 吸收自 `Cocoon-AI/architecture-diagram-generator`（7,375 stars, MIT）。
> 它的暗色霓虹样式（`#020617` 底 + 青色发光）**不符合本 skill 默认配色**，别直接套；
> 但下面这几条**结构技法**是通用的，任何配色都适用。

### ① 箭头要画在方框之前（z-order 铁律）

SVG 按**文档顺序**绘制，后画的盖住先画的。所以如果箭头要穿过方框：

```xml
<!-- 先画箭头 -->
<path d="M100 50 H 300" stroke="#8A6A4E" marker-end="url(#arr)"/>
<!-- 后画方框（会盖住箭头穿过部分） -->
<rect x="150" y="30" width="100" height="40" fill="#FFFFFF"/>
```

**但更简单的做法是别让箭头穿过方框** —— 让箭头端点落在空白处。本 skill 推荐后者。

### ② 如果方框是半透明的，箭头会透出来

半透明填充（`rgba(..., 0.4)`）挡不住下面的箭头。要真遮住，得先在同样位置画一个**不透明**底：

```xml
<rect x="X" y="Y" width="W" height="H" rx="6" fill="#FFFFFF"/>          <!-- 不透明，遮箭头 -->
<rect x="X" y="Y" width="W" height="H" rx="6" fill="#C04A1A" opacity="0.1"/>  <!-- 样式层 -->
```

### ③ 组件间距

| 项 | 值 |
|---|---|
| 标准组件高度 | 60px（服务）· 80~120px（大组件） |
| **最小垂直间距** | **40px** |
| 中间连接元素 | 放在**间距里**，不要和组件重叠 |

```
组件A:  y=70,  高=60  → 结束于 y=130
间距:   y=130 ~ 170   → 40px，连接元素放在 y=140（高 20）
组件B:  y=170, 高=60  → 开始于 y=170    ← 不会重叠
```

### ④ 图例放在所有边界框外面

图例（legend）不能放在任何边界框（region / cluster / security group）里面。

- 先算出所有边界框的最低点
- 图例放在最低点**下方 ≥20px**
- 必要时**加大 viewBox 高度**来容纳

### ⑤ 想用暗色模板时

`references/dark-template.html` 是原 skill 的暗色模板（17KB，含 Copy/PNG/PDF 导出按钮）。

**仅当用户明确要暗色**时用，并且注意：不要做成「均匀深蓝底 `#0D1117` + 通用青紫霓虹」——
那是 huashu-design 明令禁止的偷懒解。暗色要有作者意图（电影级光影、暖色赛博等）。

把模板当**结构参考**（导出按钮那两个 CDN script + SRI hash 可直接复用），别当配色参考。

---

## 踩过的坑（都实测过）

| 坑 | 现象 | 解法 |
|---|---|---|
| **中英文字宽不同** | 中文换英文后溢出 | 英文版单独量包围盒；调小 font-size |
| **CJK 双宽** | ASCII 图算好的对齐，中文一放就歪 | 别用 ASCII 画含中文的图 |
| `·` `─` 宽度不可预测 | `east_asian_width` 算不准 | 别依赖对齐；改用 Mermaid |
| **`file://` 图片被拦** | `naturalWidth = 0`，预览空白 | 图片 base64 内嵌 |
| **`fetch()` 被 CORS 拦** | 注入脚本拿不到 SVG | SVG 内容直接**内联**进 HTML |
| 检查脚本跨 group 误报 | 报"箭头被遮挡"其实没有 | `transform` 坐标要累加；或改用 `getBoundingClientRect()` |
| cairosvg 装不上 | `libcairo.2.dylib` not found | 用 Chrome headless |
| `--window-size` < viewBox | 图被裁 | window ≥ viewBox |
| PNG 压缩开 dither | 体积反而变大 + 噪点 | `dither=Image.NONE` |

---

## 检查清单

做完一张图，逐条过：

- [ ] 字体栈写全（含 `PingFang SC` + `Microsoft YaHei`）
- [ ] 文本溢出检测：0 溢出
- [ ] 像素分析：墨迹占比合理（2%~4%），最暗 < 80
- [ ] 主色采样：符合设计
- [ ] `<img>` 引用测试：图能加载
- [ ] PNG 已压缩，色差 < 1.0
- [ ] 双语版各自量过溢出
- [ ] 无审美禁区（深蓝底+霓虹 / 紫渐变 / emoji / 署名）
- [ ] 预览 HTML 已生成（图片 base64 内嵌）
- [ ] 预览文件已加 `.gitignore`

---

## 完整产出示例

```
docs/assets/
  hero.svg          8.9KB   首屏：before/after 对比
  hero-en.svg       8.9KB
  flow.svg          7.9KB   流程：说一次，以后都记得
  flow-en.svg       7.9KB
  architecture.svg  7.0KB   架构：它由哪几块组成
  architecture-en.svg 7.1KB
  *.png                     各 ~140KB，给本地预览用
```

6 张图（中英各 3）共 1.1MB，SVG 单张仅 8KB。

---

## 实战记录

2026-09-30 给 `pi-web-extensions` 做 README 插图时总结。
当时实测战绩：**三张中文图 86 个文本元素 0 溢出**，英文版重排后同样 0 溢出，
6 张图全部通过 4 项程序化验证后推送上线。
