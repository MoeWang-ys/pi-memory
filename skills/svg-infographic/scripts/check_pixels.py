#!/usr/bin/env python3
"""像素级验证：墨迹分布 + 最暗亮度 + 横带内容分布。
用法: check_pixels.py <file.png> [带数=6]
"""
import sys
from PIL import Image
import numpy as np

p = sys.argv[1]; bands = int(sys.argv[2]) if len(sys.argv) > 2 else 6
a = np.asarray(Image.open(p).convert('RGB')).astype(int)
lum = a.mean(axis=2); ink = lum < 160
print(f"  {p}: {a.shape[1]}x{a.shape[0]}")
print(f"    墨迹占比 {100*ink.sum()/ink.size:.2f}%   最暗 {lum.min():.0f}")
if lum.min() > 90:
    print("    ⚠ 没有深色像素，文字可能没渲染")
sums = [int(b.sum()) for b in np.array_split(ink, bands, axis=0)]
empty = [i for i, s in enumerate(sums) if s == 0]
# 首尾留白是正常的（标题上下、图底部），只有中间空才算异常
middle_empty = [i for i in empty if 0 < i < bands - 1]
print("    各横带墨迹: " + " ".join(str(s) for s in sums))
if middle_empty:
    print(f"    ⚠ 中间带 {middle_empty} 是空白，可能有元素没渲染")
elif empty:
    print(f"    首尾带留白（正常）")
else:
    print(f"    {bands} 个横带均有内容 ✓")
