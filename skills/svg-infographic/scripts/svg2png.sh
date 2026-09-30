#!/usr/bin/env bash
# SVG → PNG（再用 Pillow 压缩）
# 用法: svg2png.sh <in.svg> <out.png> <width> <height> [scale=1.5]
set -euo pipefail
IN="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
OUT="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
W="$3"; H="$4"; SCALE="${5:-1.5}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || { echo "找不到 Chrome: $CHROME"; exit 1; }
"$CHROME" --headless --disable-gpu --no-sandbox --hide-scrollbars \
  --force-device-scale-factor="$SCALE" --window-size="${W},${H}" \
  --screenshot="$OUT" "file://$IN" 2>/dev/null
python3 - "$OUT" <<'PY'
import sys, os
from PIL import Image
import numpy as np
p = sys.argv[1]
im = Image.open(p).convert('RGB'); before = os.path.getsize(p)
q = im.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE)
q.save(p, optimize=True)
err = np.abs(np.asarray(im).astype(int) - np.asarray(q.convert('RGB')).astype(int)).mean()
print(f"  {p}: {before//1024}KB -> {os.path.getsize(p)//1024}KB  色差={err:.2f}"
      + ("  ⚠过大" if err > 1.5 else ""))
PY
