#!/usr/bin/env python3
"""检测 SVG 文本是否溢出画布。
用法: check_overflow.py <file.svg> <W> <H>
"""
import json, html, re, subprocess, sys
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

def check(svg_path, W, H):
    svg = open(svg_path, encoding='utf-8').read()
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
      f'<style>body{{margin:0}}#host{{width:{W}px;height:{H}px}}</style></head>'
      f'<body><div id="host">{svg}</div><script>setTimeout(function(){{'
      'var o=[];document.querySelectorAll("#host svg text").forEach(function(t){'
      'var b=t.getBBox();o.push({t:t.textContent.trim().slice(0,46),'
      'x2:+(b.x+b.width).toFixed(1),y2:+(b.y+b.height).toFixed(1)});});'
      'document.title=JSON.stringify(o);},700);</script></body></html>')
    open('/tmp/_ov.html','w',encoding='utf-8').write(page)
    r = subprocess.run([CHROME,"--headless","--disable-gpu","--no-sandbox",
        "--virtual-time-budget=7000","--dump-dom","file:///tmp/_ov.html"],
        capture_output=True, text=True)
    m = re.search(r'<title>(.*?)</title>', r.stdout, re.S)
    if not m:
        print("  ✗ 测量失败"); return 1
    d = json.loads(html.unescape(m.group(1)))
    bad = [i for i in d if i['x2'] > W or i['y2'] > H]
    print(f"  {svg_path}: {len(d)} 个文本, 溢出 {len(bad)}")
    for b in bad:
        print(f"     ⚠ 「{b['t'][:40]}」 x2={b['x2']} y2={b['y2']}")
    return 1 if bad else 0

if __name__ == '__main__':
    sys.exit(check(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
