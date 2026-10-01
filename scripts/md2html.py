#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/*.md 转成与项目其它交付物同风格的 HTML。

用法：
    python scripts/md2html.py docs/00-需求规格.md [输出路径]

设计取舍：
- 只支持本项目文档实际用到的 Markdown 子集（标题/表格/列表/引用/分隔线/段落 + 行内 code/粗体/链接）。
- 刻意不引入第三方依赖：宁可少支持语法，也不要让仓库因为一个文档脚本多一棵依赖树。
- 表头参与锚点目录（TOC）生成，便于长文档导航。
"""
import io
import os
import re
import sys

CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#16202e; --ink2:#4a5766; --muted:#7a8798;
  --line:#e3e7ec; --accent:#2b5cff; --accent2:#0aa06e; --warn:#b45309; --danger:#c02626;
  --code:#f2f4f7; --chip:#eef2ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 font-size:15px;line-height:1.75;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 22px 90px}
.hero{background:linear-gradient(135deg,#16202e 0%,#24406b 55%,#2b5cff 100%);color:#fff;
 border-radius:16px;padding:30px 28px;box-shadow:0 10px 30px rgba(20,40,80,.18)}
.hero h1{margin:0 0 8px;font-size:27px;letter-spacing:.5px}
.hero p{margin:6px 0;color:#dce6f7;font-size:14.5px}
h2{margin:38px 0 12px;font-size:21px;border-left:5px solid var(--accent);padding-left:11px;line-height:1.35}
h3{margin:24px 0 8px;font-size:17px;color:#1d3557}
h4{margin:16px 0 6px;font-size:15px;color:#26364d}
p{margin:8px 0}
ul,ol{margin:8px 0 8px 22px;padding:0}
li{margin:4px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:14px 0;
 box-shadow:0 1px 3px rgba(16,32,64,.04)}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.6px;background:#fff}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:#eef1f6;font-weight:600;color:#25384f}
pre{background:var(--code);border:1px solid var(--line);border-left:4px solid var(--accent);
 border-radius:8px;padding:12px 14px;overflow-x:auto;font-size:13px;line-height:1.6;margin:10px 0}
code{font-family:"JetBrains Mono",Consolas,"Courier New",monospace;font-size:13px}
p code,li code,td code{background:var(--code);padding:1px 5px;border-radius:4px}
.box{border-radius:10px;padding:12px 15px;margin:12px 0;border:1px solid}
.idea{background:#f1f4ff;border-color:#c9d4ff;color:#23376e}
.ok{background:#eefaf4;border-color:#b7e6cf;color:#10603f}
.warn{background:#fff8ec;border-color:#f2d9a7;color:#7c4a08}
.toc{columns:2;column-gap:26px;font-size:14px}
.toc a{color:#2c4a86;text-decoration:none;display:block;padding:2px 0}
.toc a:hover{text-decoration:underline}
blockquote{margin:10px 0;padding:8px 14px;border-left:4px solid var(--accent);
 background:#f7f9ff;color:#2b3d5c;border-radius:0 8px 8px 0}
hr{border:0;border-top:1px solid var(--line);margin:26px 0}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}
img{display:block;max-width:100%;height:auto;margin:16px auto}
@media(max-width:720px){
 .wrap{padding:20px 12px 48px}.hero{padding:20px 18px}.hero h1{font-size:23px}
 .toc{columns:1}table{display:block;overflow-x:auto}th,td{min-width:110px}
}
"""


def esc(t: str) -> str:
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def inline(t: str) -> str:
    """行内：先转义，再处理 code / 粗体 / 链接（顺序很重要）。"""
    t = esc(t)
    t = re.sub(r'`([^`]+)`', r'<code>\1</code>', t)
    t = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'<img src="\2" alt="\1">', t)
    t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', t)
    t = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', t)
    return t


def slug(t: str) -> str:
    base = re.sub(r'[^0-9A-Za-z一-鿿]+', '-', t).strip('-').lower()
    return base or 'sec'


def convert(md: str, title_hint: str = '', diagram_prefix: str = '') -> str:
    lines = md.split('\n')
    out, toc = [], []
    i, n = 0, len(lines)
    hero_title, hero_body = '', []
    diagram_number = 0
    # 提取首行 H1 与其后的引用块作为 hero
    if lines and lines[0].startswith('# '):
        hero_title = lines[0][2:].strip()
        i = 1
        while i < n and (lines[i].strip() == '' or lines[i].startswith('> ')):
            if lines[i].startswith('> '):
                hero_body.append(inline(lines[i][2:].strip()))
            i += 1

    while i < n:
        line = lines[i]
        s = line.strip()

        if s == '':
            i += 1
            continue

        # 分隔线 / 表格分隔行
        if re.fullmatch(r'-{3,}', s):
            i += 1
            continue

        # 表格
        if s.startswith('|') and i + 1 < n and re.match(r'^\|[\s:\-|]+\|$', lines[i + 1].strip()):
            header = [c.strip() for c in s.strip('|').split('|')]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith('|'):
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            html = ['<table>', '<thead><tr>']
            html += ['<th>{}</th>'.format(inline(h)) for h in header]
            html.append('</tr></thead><tbody>')
            for r in rows:
                html.append('<tr>' + ''.join('<td>{}</td>'.format(inline(c)) for c in r) + '</tr>')
            html.append('</tbody></table>')
            out.append('\n'.join(html))
            continue

        # 标题
        m = re.match(r'^(#{2,4})\s+(.*)$', s)
        if m:
            lvl, text = len(m.group(1)), m.group(2).strip()
            anchor = slug(text)
            out.append('<h{0} id="{1}">{2}</h{0}>'.format(lvl, anchor, inline(text)))
            if lvl == 2:
                toc.append((anchor, text))
            i += 1
            continue

        # 引用块（hero 之后的普通引用）
        if s.startswith('> '):
            buf = []
            while i < n and lines[i].strip().startswith('> '):
                buf.append(lines[i].strip()[2:])
                i += 1
            out.append('<blockquote>{}</blockquote>'.format(inline(' '.join(buf))))
            continue

        # 列表
        if re.match(r'^[-*]\s+', s) or re.match(r'^\d+\.\s+', s):
            ordered = bool(re.match(r'^\d+\.\s+', s))
            items = []
            while i < n and (re.match(r'^[-*]\s+', lines[i].strip()) or re.match(r'^\d+\.\s+', lines[i].strip())):
                items.append(inline(re.sub(r'^([-*]|\d+\.)\s+', '', lines[i].strip())))
                i += 1
            tag = 'ol' if ordered else 'ul'
            out.append('<{0}>{1}</{0}>'.format(tag, ''.join('<li>{}</li>'.format(x) for x in items)))
            continue

        # 代码块
        if s.startswith('```'):
            language = s[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith('```'):
                buf.append(lines[i])
                i += 1
            i += 1
            if language == 'mermaid' and diagram_prefix:
                diagram_number += 1
                out.append('<img src="{}{}.svg" alt="架构与执行关系图 {}">'.format(
                    esc(diagram_prefix), diagram_number, diagram_number))
                out.append('<details><summary>查看 Mermaid 源码</summary><pre><code>{}</code></pre></details>'.format(
                    esc('\n'.join(buf))))
            else:
                out.append('<pre><code>{}</code></pre>'.format(esc('\n'.join(buf))))
            continue

        # 普通段落
        buf = []
        while i < n and lines[i].strip() != '' and not re.match(r'^(\||#{2,4}\s|> |[-*]\s|\d+\.\s|```|-)', lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append('<p>{}</p>'.format(inline(' '.join(buf))))
        else:
            i += 1

    toc_html = ''
    if toc:
        toc_html = '<div class="card toc">' + ''.join(
            '<a href="#{}">{}</a>'.format(a, esc(t)) for a, t in toc) + '</div>'

    hero = ''
    if hero_title:
        body = ''.join('<p>{}</p>'.format(b) for b in hero_body)
        hero = '<div class="hero"><h1>{}</h1>{}</div>'.format(esc(hero_title), body)

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style></head>
<body><div class="wrap">
{hero}
{toc}
{body}
<div class="foot">由 scripts/md2html.py 从 Markdown 自动生成，请勿手改此文件；改源文件后重跑脚本。</div>
</div></body></html>
""".format(title=esc(hero_title or title_hint), css=CSS, hero=hero, toc=toc_html, body='\n'.join(out))


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else 'docs/00-需求规格.md'
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + '.html'
    md = io.open(src, encoding='utf-8').read()
    diagram_path = os.path.join(os.path.dirname(src), 'diagrams',
                               '{}-current-'.format(os.path.basename(src).split('-')[0]))
    diagram_prefix = os.path.relpath(diagram_path, os.path.dirname(dst) or '.').replace(os.sep, '/')
    html = convert(md, os.path.basename(src), diagram_prefix)
    io.open(dst, 'w', encoding='utf-8').write(html)
    print('written:', dst, len(html), 'chars')


if __name__ == '__main__':
    main()
