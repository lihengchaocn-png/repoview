"""
viewcore —— localview / ghview 共用的渲染和界面。

  - 渲染：Markdown（目录、front matter、脚注、任务列表）、带行号高亮的代码、图片预览、
    PDF（服务端逐页渲染成 JPEG，浏览器懒加载）。
  - 界面：页面框架、CSS / JS（目录树、A−/A+ 字号、顶栏自动隐藏、原文件浮层、亮暗色）、
    目录树 / 文件列表 / 面包屑的 HTML。
  - HTTP：BaseHandler 负责 gzip、ETag/304、302 跳转、错误页、原始文件返回，
    以及 serve_file() —— 决定一个文件按 Markdown / 代码 / 图片 / PDF / 原文件 哪种方式展示。

数据从哪来（本地磁盘还是 GitHub）由各应用自己的 Handler 负责，这里不关心。
"""
from __future__ import annotations

import gzip
import hashlib
import html
import mimetypes
import base64
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Union
from pathlib import Path

try:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.anchors import anchors_plugin
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.front_matter import front_matter_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin
except ImportError:
    sys.exit("缺少依赖，请在项目目录下用 uv run 启动（或先 uv sync）")

try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
    from pygments.util import ClassNotFound

    HAVE_PYGMENTS = True
except ImportError:  # 没有 pygments 也能用，只是代码没有颜色
    HAVE_PYGMENTS = False

try:
    import pymupdf
except ImportError:  # 没有 pymupdf 时 PDF 只能下载原文件，交给浏览器自带的阅读器
    pymupdf = None

VERSION = "1.3"
APP_TITLE = "repoview"  # 添加到主屏幕时的默认名字，各应用启动时改成自己的名字
MD_EXT = {".md", ".markdown", ".mdown", ".mkd"}
TEXT_EXT = {
    ".txt", ".py", ".pyi", ".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".jsx", ".vue", ".json", ".jsonc", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
    ".plist", ".xml", ".css", ".scss", ".less", ".sql", ".go", ".rs", ".rb", ".java", ".kt", ".c",
    ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".m", ".lua", ".php", ".pl", ".r", ".scala",
    ".dart", ".hs", ".ex", ".exs", ".clj", ".el", ".vim", ".proto", ".graphql", ".gql", ".tf",
    ".cmake", ".mk", ".gradle", ".ps1", ".bat", ".diff", ".patch", ".rst", ".tex", ".lock",
    ".properties", ".log", ".csv", ".tsv", ".env", ".gitignore", ".gitattributes",
    ".editorconfig", ".dockerfile",
}
TEXT_NAMES = {"Dockerfile", "Makefile", "LICENSE", "Brewfile", "Procfile", "Gemfile", "Justfile"}
PDF_EXT = {".pdf"}
PDF_SCALE = 2.0  # 渲染倍率：A4 一页约 1190px 宽，iPad 高清屏上够清楚
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif"}
SKIP_NAMES = {".git", ".DS_Store", "node_modules", "__pycache__", ".venv", ".Trash"}
README_NAMES = ("README.md", "readme.md", "index.md", "INDEX.md", "SKILL.md")
MAX_TEXT_BYTES = 2 * 1024 * 1024
NB_EXT = {".ipynb"}
MAX_NB_BYTES = 30 * 1024 * 1024  # notebook 里常内嵌图片，上限比普通文本大
TREE_LIMIT = 500  # 目录树里一个文件夹最多列多少项，再多就去目录页看

# 目录树里文件名前小方块的颜色分组
FILE_KIND = {}
for _k, _exts in {
    "md": MD_EXT,
    "py": {".py", ".pyi", ".ipynb"},
    "js": {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue"},
    "cf": {".json", ".jsonc", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg",
           ".plist", ".xml", ".env", ".properties", ".lock"},
    "sh": {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat"},
    "im": IMAGE_EXT,
    "pdf": PDF_EXT,
    "src": {".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt", ".swift",
            ".rb", ".lua", ".php", ".css", ".scss", ".sql", ".html", ".htm"},
}.items():
    for _e in _exts:
        FILE_KIND[_e] = _k


# ---------------------------------------------------------------- Markdown 渲染

def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)  # \w 在 Python 里包含中文
    return re.sub(r"\s+", "-", s) or "section"


def _highlight(code: str, lang: str, _attrs) -> str:
    if not HAVE_PYGMENTS or not lang:
        return ""  # 返回空串时 markdown-it 会自己转义
    try:
        lexer = get_lexer_by_name(lang.split()[0])
    except ClassNotFound:
        return ""
    return _pyg_highlight(code, lexer, HtmlFormatter(nowrap=True))


def _render_front_matter(self, tokens, idx, options, env) -> str:
    rows: list[list[str]] = []
    for line in tokens[idx].content.splitlines():
        m = re.match(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$", line)
        if m:
            rows.append([m.group(1), m.group(2).strip().strip("'\"")])
        elif rows and line.strip():
            rows[-1][1] += " " + line.strip()
    if not rows:
        return ""
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>" for k, v in rows
    )
    return f'<table class="fm">{body}</table>\n'


def build_markdown(allow_html: bool) -> MarkdownIt:
    md = MarkdownIt(
        "commonmark",
        {"html": allow_html, "linkify": False, "typographer": False, "highlight": _highlight},
    )
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin)
    md.use(footnote_plugin)
    md.use(tasklists_plugin)
    md.use(anchors_plugin, min_level=1, max_level=4, slug_func=_slugify)
    md.add_render_rule("front_matter", _render_front_matter)
    return md


def render_markdown(md: MarkdownIt, text: str) -> tuple[str, str, str | None]:
    """返回 (正文 HTML, 目录 HTML, 第一个标题)"""
    env: dict = {}
    tokens = md.parse(text, env)
    heads: list[tuple[int, str, str]] = []
    first_title = None
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            level = int(t.tag[1])
            title = tokens[i + 1].content
            if first_title is None:
                first_title = title
            if 2 <= level <= 3:
                heads.append((level, t.attrGet("id") or "", title))
    body = md.renderer.render(tokens, md.options, env)
    toc = ""
    if len(heads) >= 3:
        items = "".join(
            f'<li class="l{lv}"><a href="#{html.escape(hid)}">{html.escape(title)}</a></li>'
            for lv, hid, title in heads
        )
        toc = f'<details class="toc"><summary>目录</summary><ul>{items}</ul></details>'
    return body, toc, first_title


def render_code(path: Path, text: str) -> tuple[str, int]:
    """带行号的代码块，返回 (HTML, 行数)。行号用 CSS 画，复制代码时不会带上。"""
    hl = None
    if HAVE_PYGMENTS:
        try:
            # stripnl=False：保留开头的空行，行号才对得上
            lexer = get_lexer_for_filename(path.name, text, stripnl=False)
            hl = _pyg_highlight(text, lexer, HtmlFormatter(nowrap=True))
        except ClassNotFound:
            pass
    if hl is None:
        hl = html.escape(text)
    # pygments 的 nowrap 输出会在每行末尾闭合 span，按换行切开是安全的
    lines = hl.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    n = len(lines) or 1
    body = "".join(
        f'<span class="l" id="L{i}"><a href="#L{i}"></a>{ln}\n</span>'
        for i, ln in enumerate(lines, 1)
    )
    gw = len(str(n))
    return f'<pre class="code" style="--gw:{gw}ch"><code>{body}</code></pre>', len(lines)


def binary_by_name(name: str) -> bool:
    """按文件名就能断定不是文本的类型"""
    mt = mimetypes.guess_type(name)[0] or ""
    return mt.startswith(("image/", "audio/", "video/", "font/")) or mt in (
        "application/pdf", "application/zip", "application/gzip", "application/octet-stream",
    )


def is_text(head: bytes) -> bool:
    """看开头一段（最多 8KB）判断是不是 UTF-8 文本"""
    head = head[:8192]
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return len(head) == 8192 and e.start >= len(head) - 4  # 只是截断在多字节字符中间
    return True


def text_by_name(name: str) -> bool:
    return Path(name).suffix.lower() in TEXT_EXT or name in TEXT_NAMES


# ---------------------------------------------------------------- 页面样式

def _pygments_css() -> str:
    if not HAVE_PYGMENTS:
        return ""

    def defs(style: str) -> str:
        try:
            css = HtmlFormatter(style=style).get_style_defs("pre code")
        except Exception:
            css = HtmlFormatter(style="monokai").get_style_defs("pre code")
        return css

    light = defs("friendly")
    dark = defs("github-dark")
    # 去掉 pygments 自带的背景色，统一用页面自己的代码块底色
    reset = "pre code, pre code span { background: transparent !important; }"
    return f"{light}\n@media (prefers-color-scheme: dark) {{\n{dark}\n}}\n{reset}"


BASE_CSS = """
:root {
  --bg: #fbfaf7; --fg: #24292f; --muted: #6a737d; --line: #e4e2dc;
  --code-bg: #f3f1ec; --link: #0b62c4; --accent: #c2410c; --card: #ffffff;
  --side: #f4f2ed; --hover: rgba(0,0,0,.05); --sel: rgba(11,98,196,.13); --hl: rgba(250,204,21,.25);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17181b; --fg: #dcdfe4; --muted: #8b929c; --line: #2c2f35;
    --code-bg: #202227; --link: #6cb2ff; --accent: #fb923c; --card: #1d1f23;
    --side: #1b1c20; --hover: rgba(255,255,255,.06); --sel: rgba(108,178,255,.18); --hl: rgba(250,204,21,.16);
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; height: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  display: flex; flex-direction: column; height: 100vh; height: 100dvh; overflow: hidden;
  font: 17px/1.75 -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
        "Helvetica Neue", "Microsoft YaHei", sans-serif;
}
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
header {
  flex: none; display: flex; align-items: center; gap: 8px; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 6px 12px;
  padding-top: max(6px, env(safe-area-inset-top));
  font-size: 14px; color: var(--muted);
}
header .cr { flex: 1; min-width: 0; white-space: nowrap; overflow-x: auto; padding: 4px 0; }
header a { color: var(--muted); }
header b { color: var(--fg); font-weight: 600; }
button.ib {
  flex: none; border: 0; background: none; color: var(--muted); cursor: pointer;
  width: 32px; height: 32px; border-radius: 6px; font-size: 18px; line-height: 1; padding: 0;
}
button.ib:hover { background: var(--hover); color: var(--fg); }
.wrap { flex: 1; display: flex; min-height: 0; position: relative; z-index: 1; background: var(--bg); }
/* 往下滚时收起顶栏：.wrap 用负 margin 盖住顶栏，只留顶部安全区那一条 */
header { position: relative; transition: margin-bottom .2s ease; }
.hh header { margin-bottom: calc(-1 * var(--hh, 0px)); }
#main { flex: 1; min-width: 0; overflow: auto; -webkit-overflow-scrolling: touch; }
main { max-width: 880px; margin: 0 auto; padding: 20px 16px 80px; font-size: calc(17px * var(--fz, 1)); }
button.ib.fz { font-size: 14px; font-weight: 600; width: 34px; touch-action: manipulation; }
main.wide { max-width: 1280px; }

/* ---- 左侧目录树 ---- */
#sb {
  flex: none; width: var(--sbw, 280px); background: var(--side); border-right: 1px solid var(--line);
  display: flex; flex-direction: column; position: relative; font-size: 14px; line-height: 1.55;
}
.sbx #sb { display: none; }
.sbh {
  flex: none; display: flex; align-items: center; padding: 4px 6px 4px 14px;
  font-size: 11px; letter-spacing: .06em; color: var(--muted); font-weight: 600;
}
.sbh span { flex: 1; }
.sbh button.ib { width: 26px; height: 26px; font-size: 15px; }
.tree { flex: 1; overflow: auto; padding: 0 0 40px; -webkit-overflow-scrolling: touch; }
.tree ul { list-style: none; margin: 0; padding: 0; }
.tree ul ul { margin-left: 13px; border-left: 1px solid var(--line); }
.tree li { margin: 0; }
.tree summary, .tree a {
  display: flex; align-items: center; gap: 6px; padding: 2px 8px 2px 6px; margin: 0 4px;
  white-space: nowrap; color: var(--fg); cursor: pointer; border-radius: 5px;
  -webkit-user-select: none; user-select: none;
}
.tree summary { list-style: none; }
.tree summary::-webkit-details-marker { display: none; }
.tree summary::before {
  content: ""; flex: none; width: 6px; height: 6px; margin: 0 3px 0 2px;
  border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted);
  transform: rotate(-45deg); transition: transform .12s;
}
.tree details[open] > summary::before { transform: rotate(45deg) translate(-1px, -1px); }
.tree summary:hover, .tree a:hover { background: var(--hover); text-decoration: none; }
.tree .nm { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.tree .fi { flex: none; width: 7px; height: 7px; margin: 0 2px 0 4px; border-radius: 2px; background: var(--muted); opacity: .55; }
.tree .k-md .fi { background: #3b82f6; opacity: .9; }
.tree .k-py .fi { background: #eab308; opacity: .9; }
.tree .k-js .fi { background: #f59e0b; opacity: .9; }
.tree .k-cf .fi { background: #a855f7; opacity: .8; }
.tree .k-sh .fi { background: #22c55e; opacity: .9; }
.tree .k-im .fi { background: #ec4899; opacity: .8; }
.tree .k-pdf .fi { background: #ef4444; opacity: .85; }
.tree .k-src .fi { background: #14b8a6; opacity: .9; }
.tree .cur > a, .tree .cur > details > summary { background: var(--sel); }
.tree .dot > a .nm, .tree .dot > details > summary .nm { opacity: .6; }
.tree details.root > summary { font-weight: 600; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; margin-top: 2px; }
.tree details.root > ul { margin-left: 0; border-left: 0; }
.tree .ph { padding: 2px 8px 2px 26px; color: var(--muted); font-size: 13px; }
.tree .ph.err { color: var(--accent); cursor: pointer; }
.tree a.more { color: var(--muted); font-size: 13px; }
#sbr { position: absolute; top: 0; right: -3px; width: 6px; height: 100%; cursor: col-resize; z-index: 2; touch-action: none; }
#sbr:hover, #sbr.on { background: var(--link); opacity: .4; }
#bd { display: none; }
@media (max-width: 800px) {
  #sb {
    position: absolute; left: 0; top: 0; bottom: 0; z-index: 20; width: min(85vw, 320px);
    transform: translateX(-102%); transition: transform .18s ease; box-shadow: 0 0 24px rgba(0,0,0,.25);
  }
  .sbx #sb { display: flex; }
  .sbo #sb { transform: none; }
  .sbo #bd { display: block; position: absolute; inset: 0; z-index: 15; background: rgba(0,0,0,.3); }
  #sbr { display: none; }
  .tree summary, .tree a { padding-top: 5px; padding-bottom: 5px; }
}

/* ---- 正文 ---- */
h1, h2, h3, h4 { line-height: 1.35; margin: 1.6em 0 .6em; scroll-margin-top: 12px; }
h1 { font-size: 1.75em; margin-top: .4em; }
h2 { font-size: 1.35em; padding-bottom: .3em; border-bottom: 1px solid var(--line); }
h3 { font-size: 1.15em; }
p, ul, ol, blockquote, table, pre { margin: 0 0 1em; }
ul, ol { padding-left: 1.5em; }
li { margin: .2em 0; }
li > ul, li > ol { margin: .2em 0; }
hr { border: 0; border-top: 1px solid var(--line); margin: 2em 0; }
blockquote { margin-left: 0; padding: .2em 1em; color: var(--muted); border-left: 4px solid var(--line); }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: calc(14px * var(--fz, 1)); }
:not(pre) > code { background: var(--code-bg); padding: .15em .4em; border-radius: 5px; color: var(--accent); }
pre {
  background: var(--code-bg); padding: 14px 16px; border-radius: 10px; tab-size: 4;
  overflow-x: auto; line-height: 1.55; -webkit-overflow-scrolling: touch;
}
pre code { background: none; padding: 0; color: inherit; }
table { display: block; overflow-x: auto; border-collapse: collapse; max-width: 100%; }
th, td { border: 1px solid var(--line); padding: 6px 12px; text-align: left; vertical-align: top; }
th { background: var(--code-bg); }
img { max-width: 100%; height: auto; }
.task-list-item { list-style: none; }
.task-list-item input { margin: 0 .4em 0 -1.3em; }
table.fm { display: table; width: 100%; font-size: calc(14px * var(--fz, 1)); margin-bottom: 1.6em; }
table.fm th { width: 1%; white-space: nowrap; color: var(--muted); }
details.toc {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 8px 14px; margin-bottom: 1.4em; font-size: calc(15px * var(--fz, 1));
}
details.toc summary { cursor: pointer; color: var(--muted); }
details.toc ul { list-style: none; padding-left: 0; margin: .5em 0 .2em; }
details.toc li.l3 { padding-left: 1.2em; }
.listing { list-style: none; padding: 0; margin: 0 0 2em;
  background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
.listing li { margin: 0; border-top: 1px solid var(--line); }
.listing li:first-child { border-top: 0; }
.listing a { display: flex; gap: 12px; align-items: baseline; padding: 12px 16px; color: var(--fg); }
.listing a:hover { background: var(--code-bg); text-decoration: none; }
.listing .ic { width: 1.4em; flex: none; text-align: center; }
.listing .nm { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.listing .md .nm { color: var(--link); }
.listing .meta { color: var(--muted); font-size: 13px; white-space: nowrap; }
.readme { border-top: 1px solid var(--line); padding-top: 1em; }
.empty, .note { color: var(--muted); }
.note { font-size: 14px; }

/* ---- 代码文件 ---- */
.fbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin: 0 0 10px; }
.fbar b { color: var(--fg); font-size: 15px; font-weight: 600; overflow-wrap: anywhere; }
.fbar .sp { flex: 1; }
.fbar button, .fbar a.bt {
  font: inherit; color: var(--muted); background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: 3px 10px; cursor: pointer;
}
.fbar a.bt:hover, .fbar button:hover { color: var(--fg); text-decoration: none; }
.cwrap .fbar #wr { color: var(--link); border-color: var(--link); }
pre.code { padding: 10px 0; font-size: calc(13px * var(--fz, 1)); line-height: 1.6; }
pre.code code { display: inline-block; min-width: 100%; }
pre.code .l { display: block; position: relative; padding: 0 16px 0 calc(var(--gw) + 30px); }
pre.code .l > a {
  position: absolute; left: 0; width: calc(var(--gw) + 16px); text-align: right;
  color: var(--muted); opacity: .55; -webkit-user-select: none; user-select: none;
}
pre.code .l > a::before { content: counter(ln); }
pre.code code { counter-reset: ln; }
pre.code .l { counter-increment: ln; }
pre.code .l > a:hover { opacity: 1; text-decoration: none; }
pre.code .l:target { background: var(--hl); }
.cwrap pre.code { white-space: pre-wrap; overflow-wrap: anywhere; }
.cwrap pre.code code { display: block; }
.imgv { text-align: center; }
#rv { position: fixed; inset: 0; z-index: 50; display: flex; flex-direction: column; background: var(--bg); }
#rv .rvh {
  flex: none; display: flex; align-items: center; gap: 10px; padding: 6px 12px;
  padding-top: max(6px, env(safe-area-inset-top)); border-bottom: 1px solid var(--line);
  font-size: 14px; color: var(--muted);
}
#rv .rvh b { flex: 1; min-width: 0; color: var(--fg); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#rv .rvh button, #rv .rvh a {
  font: inherit; color: var(--fg); background: var(--card); border: 1px solid var(--line);
  border-radius: 6px; padding: 4px 12px; cursor: pointer; white-space: nowrap; touch-action: manipulation;
}
#rv .rvh a:hover { text-decoration: none; }
#rv iframe { flex: 1; width: 100%; border: 0; background: #fff; }
.pdfv { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.pdfv img {
  display: block; width: calc(100% * var(--fz, 1)); max-width: none; height: auto; margin: 0 auto 14px;
  background: #fff; border-radius: 4px; box-shadow: 0 1px 4px rgba(0,0,0,.18);
}
/* ---- Jupyter notebook ---- */
.nb-md { margin: 0 0 1em; }
.nb-code { margin: 0 0 1.4em; }
.nb-in { position: relative; }
.nb-in pre { margin: 0; }
.nb-p {
  position: absolute; right: 10px; top: 5px; font: 11px ui-monospace, "SF Mono", Menlo, monospace;
  color: var(--muted); opacity: .75; -webkit-user-select: none; user-select: none;
}
.nb-out { border-left: 3px solid var(--line); padding: 6px 0 2px 12px; margin-top: 6px; overflow-x: auto; }
.nb-out pre {
  background: none; padding: 0; margin: 0 0 .6em; border-radius: 0;
  font-size: calc(13px * var(--fz, 1)); white-space: pre-wrap; overflow-wrap: anywhere;
}
.nb-out pre.nb-err { color: #cf222e; }
@media (prefers-color-scheme: dark) { .nb-out pre.nb-err { color: #ff7b72; } }
.nb-out img { display: block; max-width: 100%; height: auto; margin: 0 0 .6em; background: #fff; border-radius: 4px; }
.nb-out .nb-md { margin: 0; }
iframe.nb-html { display: block; width: 100%; height: 80px; border: 0; margin: 0 0 .6em; background: #fff; border-radius: 6px; }
pre.nb-raw { color: var(--muted); }
/* ---- 欢迎页 ---- */
.wl-form { display: flex; gap: 8px; margin: 1.2em 0 .4em; }
.wl-form input {
  flex: 1; min-width: 0; font: inherit; font-size: 16px; padding: 10px 12px; color: var(--fg);
  background: var(--card); border: 1px solid var(--line); border-radius: 8px;
}
.wl-form button {
  font: inherit; font-size: 16px; padding: 10px 18px; border-radius: 8px; border: 0;
  background: var(--link); color: #fff; cursor: pointer;
}
.wl-err { color: #cf222e; font-size: 14px; margin: 0 0 .8em; }
.wl-h { font-size: 13px; color: var(--muted); margin: 1.6em 0 .5em; letter-spacing: .04em; }
.wl-tips { font-size: 14px; color: var(--muted); padding-left: 1.2em; }
.wl-tips code { font-size: 13px; }
.imgv img { background: repeating-conic-gradient(var(--code-bg) 0% 25%, transparent 0% 50%) 0 0 / 16px 16px; border-radius: 6px; }
"""

# 放在 <head> 里：渲染前先恢复侧栏隐藏/宽度/换行设置，避免闪一下
HEAD_JS = """
try{var s=localStorage,e=document.documentElement;
if(s.getItem('mds.sb')==='0')e.classList.add('sbx');
if(s.getItem('mds.wrap')==='1')e.classList.add('cwrap');
var w=+s.getItem('mds.w');if(w)e.style.setProperty('--sbw',w+'px');
var z=+s.getItem('mds.fz');if(z)e.style.setProperty('--fz',z);}catch(_){}
"""

BODY_JS = r"""
(function(){
var d=document,de=d.documentElement,sb=d.getElementById('sb');
function get(k){try{return localStorage.getItem(k)}catch(_){return null}}
function put(k,v){try{localStorage.setItem(k,v)}catch(_){}}
var mob=window.matchMedia('(max-width: 800px)');
var tg=d.getElementById('tg');
if(tg)tg.onclick=function(){
  if(mob.matches){de.classList.toggle('sbo')}
  else{var h=de.classList.toggle('sbx');put('mds.sb',h?'0':'1')}
};
var fz=+get('mds.fz')||1;
function setfz(v){fz=Math.round(Math.max(.7,Math.min(2,v))*10)/10;de.style.setProperty('--fz',fz);put('mds.fz',fz)}
var fzd=d.getElementById('fzd'),fzu=d.getElementById('fzu');
if(fzd)fzd.onclick=function(){setfz(fz-.1)};
if(fzu)fzu.onclick=function(){setfz(fz+.1)};
if(fzd)fzd.ondblclick=fzu.ondblclick=function(e){e.preventDefault();setfz(1)};
d.addEventListener('click',function(e){
  var a=e.target.closest&&e.target.closest('a[data-raw]');
  if(!a||e.metaKey||e.ctrlKey||e.shiftKey)return;
  e.preventDefault();
  var name=decodeURIComponent(a.pathname.split('/').pop());
  var v=d.createElement('div');v.id='rv';
  v.innerHTML='<div class="rvh"><button type="button">✕ 返回</button><b></b><a download>下载</a></div><iframe></iframe>';
  v.querySelector('b').textContent=name;
  v.querySelector('a').href=a.href;v.querySelector('a').setAttribute('download',name);
  v.querySelector('iframe').src=a.href;
  function close(){v.remove();d.removeEventListener('keydown',esc)}
  function esc(ev){if(ev.key==='Escape')close()}
  v.querySelector('button').onclick=close;d.addEventListener('keydown',esc);
  d.body.appendChild(v);
});
var hd=d.querySelector('header'),mn=d.getElementById('main'),last=0;
function hide(on){
  if(on&&de.classList.contains('sbo'))on=false;
  if(on===de.classList.contains('hh'))return;
  if(on)de.style.setProperty('--hh',(hd.offsetHeight-parseFloat(getComputedStyle(hd).paddingTop))+'px');
  de.classList.toggle('hh',on);
}
if(hd&&mn)mn.addEventListener('scroll',function(){
  var y=mn.scrollTop,dy=y-last;
  if(y<40)hide(false);else if(dy>10)hide(true);else if(dy<-10)hide(false);
  if(Math.abs(dy)>10||y<40)last=y;
},{passive:true});
var nbh=d.querySelectorAll('iframe.nb-html');
function nbfit(){nbh.forEach(function(f){try{f.style.height=(f.contentDocument.documentElement.scrollHeight+4)+'px'}catch(_){}})}
nbh.forEach(function(f){f.addEventListener('load',nbfit)});if(nbh.length){nbfit();window.addEventListener('resize',nbfit)}
var bd=d.getElementById('bd');if(bd)bd.onclick=function(){de.classList.remove('sbo')};
var wr=d.getElementById('wr');
if(wr)wr.onclick=function(){put('mds.wrap',de.classList.toggle('cwrap')?'1':'0')};
if(!sb)return;
var tree=sb.querySelector('.tree');
var open;try{open=new Set(JSON.parse(get('mds.open')||'[]'))}catch(_){open=new Set()}
function save(){put('mds.open',JSON.stringify(Array.from(open)))}
function load(det){
  if(det.dataset.st)return;
  for(var c=det.firstElementChild;c;c=c.nextElementSibling)if(c.tagName==='UL'){det.dataset.st='ok';return}
  det.dataset.st='ld';
  var ph=d.createElement('div');ph.className='ph';ph.textContent='加载中…';det.appendChild(ph);
  fetch(det.dataset.p+'?tree',{credentials:'same-origin'}).then(function(r){
    if(!r.ok)throw new Error(r.status);return r.text()
  }).then(function(h){
    ph.remove();det.insertAdjacentHTML('beforeend',h);det.dataset.st='ok';restore(det);
  }).catch(function(){
    delete det.dataset.st;ph.className='ph err';ph.textContent='加载失败，点此重试';
    ph.onclick=function(){ph.remove();load(det)};
  });
}
function restore(scope){
  scope.querySelectorAll('details[data-p]').forEach(function(x){
    if(x.open){open.add(x.dataset.p);load(x)}else if(open.has(x.dataset.p))x.open=true;
  });
  save();
}
tree.addEventListener('toggle',function(e){
  var t=e.target;if(t.tagName!=='DETAILS'||!t.dataset.p)return;
  if(t.open){open.add(t.dataset.p);load(t)}else open.delete(t.dataset.p);
  save();
},true);
var ca=d.getElementById('ca');
if(ca)ca.onclick=function(){
  tree.querySelectorAll('details[open]').forEach(function(x){if(!x.classList.contains('root'))x.open=false});
  open.clear();tree.querySelectorAll('details.root[open]').forEach(function(x){open.add(x.dataset.p)});save();
};
restore(tree);
var y=+(sessionStorage.getItem('mds.sc')||0);if(y)tree.scrollTop=y;
var cur=tree.querySelector('.cur');
if(cur){var r=cur.getBoundingClientRect(),t=tree.getBoundingClientRect();
  if(r.top<t.top||r.bottom>t.bottom)cur.scrollIntoView({block:'center'})}
window.addEventListener('pagehide',function(){try{sessionStorage.setItem('mds.sc',tree.scrollTop)}catch(_){}});
var rs=d.getElementById('sbr');
if(rs)rs.addEventListener('pointerdown',function(e){
  e.preventDefault();rs.setPointerCapture(e.pointerId);rs.classList.add('on');var w=sb.offsetWidth;
  function mv(ev){w=Math.max(160,Math.min(640,ev.clientX-sb.getBoundingClientRect().left));de.style.setProperty('--sbw',w+'px')}
  function up(){rs.classList.remove('on');rs.removeEventListener('pointermove',mv);rs.removeEventListener('pointerup',up);put('mds.w',Math.round(w))}
  rs.addEventListener('pointermove',mv);rs.addEventListener('pointerup',up);
});
})();
"""


def page(title: str, crumbs: str, body: str, sidebar: str = "", wide: bool = False) -> bytes:
    toggle = '<button class="ib" id="tg" title="显示/隐藏目录栏">☰</button>' if sidebar else ""
    side = (
        f'<aside id="sb"><div class="sbh"><span>资源管理器</span>'
        f'<button class="ib" id="ca" title="全部折叠">⊟</button></div>'
        f'<nav class="tree">{sidebar}</nav><div id="sbr"></div></aside><div id="bd"></div>'
        if sidebar else ""
    )
    doc = f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{html.escape(APP_TITLE)}">
<meta name="color-scheme" content="light dark">
<title>{html.escape(title)}</title>
<script>{HEAD_JS}</script>
<style>{BASE_CSS}{PYG_CSS}</style>
</head><body>
<header>{toggle}<div class="cr">{crumbs}</div><button class="ib fz" id="fzd" title="缩小字体（双击复位）">A−</button><button class="ib fz" id="fzu" title="放大字体（双击复位）">A+</button></header>
<div class="wrap">{side}<div id="main"><main{' class="wide"' if wide else ''}>{body}</main></div></div>
<script>{BODY_JS}</script>
</body></html>"""
    return doc.encode("utf-8")


PYG_CSS = _pygments_css()


# ---------------------------------------------------------------- 小工具

def quote_path(s: str) -> str:
    return urllib.parse.quote(s, safe="/")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def file_href(name: str) -> str:
    """目录页、目录树里文件的相对链接：图片打开带侧栏的预览页，其他直接打开（PDF 默认就是预览页）"""
    return quote_path(name) + ("?view" if Path(name).suffix.lower() in IMAGE_EXT else "")


def build_id(*files: str) -> str:
    """代码指纹，放进 ETag：改了任何一个文件重启后，浏览器不会再拿旧页面（304）"""
    h = hashlib.sha1()
    for f in files:
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:10]


def stat_tag(st) -> str:
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


# ---------------------------------------------------------------- 页面片段

RAW_ATTRS = 'target="_blank" rel="noopener" data-raw'  # 前端拦下来在页内浮层打开


def _fbar(name: str, info: str, buttons: str) -> str:
    return (f'<div class="fbar"><b>{html.escape(name)}</b><span>{info}</span>'
            f'<span class="sp"></span>{buttons}</div>')


def _raw_btn(name: str, label: str) -> str:
    return f'<a class="bt" href="{quote_path(name)}?raw" {RAW_ATTRS}>{label}</a>'


def image_content(name: str, size: int) -> str:
    return (_fbar(name, human_size(size), _raw_btn(name, "原图"))
            + f'<div class="imgv"><img src="{quote_path(name)}?raw" alt="{html.escape(name)}"></div>')


def markdown_content(md: MarkdownIt, name: str, text: str) -> tuple[str, str]:
    """返回 (页面标题, 正文 HTML)"""
    body, toc, first = render_markdown(md, text)
    link = f'<p class="note"><a href="{quote_path(name)}?raw" {RAW_ATTRS}>查看原文</a></p>'
    return first or Path(name).stem, toc + body + link


def code_content(name: str, text: str, size: int) -> str:
    code, nlines = render_code(Path(name), text)
    btns = '<button id="wr" title="长行自动换行">换行</button>' + _raw_btn(name, "原文")
    return _fbar(name, f"{nlines} 行 · {human_size(size)}", btns) + code


PdfSource = Union[Path, bytes]


def _open_pdf(src: PdfSource):
    if isinstance(src, (bytes, bytearray)):
        return pymupdf.open(stream=src, filetype="pdf")
    return pymupdf.open(src)


def pdf_content(name: str, size: int, src: PdfSource) -> str:
    bar = lambda info: _fbar(name, info + human_size(size), _raw_btn(name, "原文件"))  # noqa: E731
    try:
        with _open_pdf(src) as doc:
            if doc.needs_pass:
                return bar("") + '<p class="empty">PDF 有密码，请点「原文件」用浏览器打开</p>'
            sizes = [(p.rect.width, p.rect.height) for p in doc]
    except Exception as e:  # noqa: BLE001
        return bar("") + f'<p class="empty">无法解析 PDF：{html.escape(str(e))}</p>'
    q = quote_path(name)
    imgs = "".join(
        f'<img src="{q}?page={i}" width="{round(w * PDF_SCALE)}" height="{round(h * PDF_SCALE)}" '
        f'loading="{"eager" if i <= 2 else "lazy"}" alt="第 {i} 页" id="p{i}">'
        for i, (w, h) in enumerate(sizes, 1)
    )
    return bar(f"{len(sizes)} 页 · ") + f'<div class="pdfv">{imgs}</div>'


def pdf_page_jpeg(src: PdfSource, i: int) -> bytes:
    """第 i 页（从 1 开始）渲染成 JPEG；页码越界抛 IndexError"""
    with _open_pdf(src) as doc:
        if not 1 <= i <= doc.page_count:
            raise IndexError(i)
        pix = doc[i - 1].get_pixmap(matrix=pymupdf.Matrix(PDF_SCALE, PDF_SCALE), alpha=False)
        return pix.tobytes("jpeg", jpg_quality=82)


# ---- Jupyter notebook

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# HTML 输出放进 sandbox iframe：不给 allow-scripts，里面的脚本不会执行；
# 给 allow-same-origin 只是为了让外层读到内容高度、自动撑开
NB_HTML_STYLE = ("<style>body{margin:0;padding:6px;font:13px/1.5 -apple-system,BlinkMacSystemFont,"
                 "'PingFang SC',sans-serif;color:#24292f;background:#fff}table{border-collapse:collapse}"
                 "th,td{border:1px solid #e4e2dc;padding:3px 8px;text-align:right}"
                 "thead th{background:#f3f1ec}</style>")


def _nb_text(x) -> str:
    return "".join(x) if isinstance(x, list) else (x or "")


def _nb_output(md: MarkdownIt, o: dict) -> str:
    t = o.get("output_type")
    if t == "stream":
        cls = ' class="nb-err"' if o.get("name") == "stderr" else ""
        return f"<pre{cls}>{html.escape(ANSI_RE.sub('', _nb_text(o.get('text'))))}</pre>"
    if t in ("error", "pyerr"):
        tb = "\n".join(o.get("traceback") or [f"{o.get('ename', '')}: {o.get('evalue', '')}"])
        return f'<pre class="nb-err">{html.escape(ANSI_RE.sub("", tb))}</pre>'
    # execute_result / display_data；v3 格式的数据直接挂在 output 上
    data = o.get("data") or {
        {"png": "image/png", "jpeg": "image/jpeg", "svg": "image/svg+xml", "html": "text/html",
         "text": "text/plain"}.get(k, k): v for k, v in o.items()}
    for mime in ("image/png", "image/jpeg", "image/gif"):
        if mime in data:
            b64 = re.sub(r"\s", "", _nb_text(data[mime]))
            return f'<img src="data:{mime};base64,{b64}" alt="">'
    if "image/svg+xml" in data:
        b64 = base64.b64encode(_nb_text(data["image/svg+xml"]).encode("utf-8")).decode()
        return f'<img src="data:image/svg+xml;base64,{b64}" alt="">'
    if "text/html" in data:
        doc = NB_HTML_STYLE + _nb_text(data["text/html"])
        return f'<iframe class="nb-html" sandbox="allow-same-origin" srcdoc="{html.escape(doc, quote=True)}"></iframe>'
    if "text/markdown" in data:
        return f'<div class="nb-md">{render_markdown(md, _nb_text(data["text/markdown"]))[0]}</div>'
    if "text/plain" in data:
        return f"<pre>{html.escape(ANSI_RE.sub('', _nb_text(data['text/plain'])))}</pre>"
    return ""


def notebook_content(md: MarkdownIt, name: str, data: bytes, size: int) -> tuple[str, str]:
    """返回 (页面标题, 正文 HTML)"""
    btn = _raw_btn(name, "原文")
    try:
        nb = json.loads(data)
        cells = nb.get("cells")
        if cells is None:  # nbformat 3
            cells = [c for w in nb.get("worksheets", []) for c in w.get("cells", [])]
    except (ValueError, AttributeError) as e:
        return name, _fbar(name, human_size(size), btn) + f'<p class="empty">无法解析 notebook：{html.escape(str(e))}</p>'
    meta = nb.get("metadata") or {}
    lang = ((meta.get("language_info") or {}).get("name")
            or (meta.get("kernelspec") or {}).get("language") or "python")

    out, title, n_code = [], None, 0
    for c in cells:
        t = c.get("cell_type")
        src = _nb_text(c.get("source", c.get("input")))
        if t == "markdown":
            # 单元里粘贴的图片：![](attachment:x.png) → data URI
            for fn, mimes in (c.get("attachments") or {}).items():
                for mime, b64 in mimes.items():
                    if mime.startswith("image/"):
                        src = src.replace(f"attachment:{fn}", f"data:{mime};base64,{_nb_text(b64)}")
                        break
            body, _, first = render_markdown(md, src)
            title = title or first
            out.append(f'<div class="nb-md">{body}</div>')
        elif t in ("code", "heading"):
            if t == "heading":  # nbformat 3 的标题单元
                body, _, first = render_markdown(md, "#" * c.get("level", 1) + " " + src)
                title = title or first
                out.append(f'<div class="nb-md">{body}</div>')
                continue
            n_code += 1
            hl = _highlight(src, lang, None) or html.escape(src)
            n = c.get("execution_count", c.get("prompt_number"))
            outs = "".join(_nb_output(md, o) for o in c.get("outputs") or [])
            out.append(
                f'<div class="nb-code"><div class="nb-in"><span class="nb-p">[{n if n is not None else " "}]</span>'
                f'<pre><code>{hl}</code></pre></div>' + (f'<div class="nb-out">{outs}</div>' if outs else "") + "</div>")
        elif src.strip():
            out.append(f'<pre class="nb-raw">{html.escape(src)}</pre>')
    info = f"{len(cells)} 个单元 · {n_code} 段代码 · {human_size(size)}"
    return title or Path(name).stem, _fbar(name, info, btn) + "".join(out)


def file_icon(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ("📝" if ext in MD_EXT else "🖼️" if ext in IMAGE_EXT else "📕" if ext in PDF_EXT
            else "📓" if ext in NB_EXT else "📄")


def listing(entries) -> str:
    """目录页的文件列表。entries: [(名字, 是否目录, 右侧说明)]，链接相对当前目录"""
    rows = []
    for n, is_dir, meta in entries:
        if is_dir:
            ic, cls, href = "📁", "dir", quote_path(n) + "/"
        else:
            ic, href = file_icon(n), file_href(n)
            cls = "md" if Path(n).suffix.lower() in MD_EXT else "file"
        rows.append(f'<li class="{cls}"><a href="{href}"><span class="ic">{ic}</span>'
                    f'<span class="nm">{html.escape(n)}</span><span class="meta">{meta}</span></a></li>')
    return f'<ul class="listing">{"".join(rows)}</ul>' if rows else '<p class="empty">空目录</p>'


def tree_ul(entries, base: str, cur: str | None = None, open_name: str | None = None,
            open_inner: str = "") -> str:
    """侧栏目录树的一层。

    entries: [(名字, 是否目录)]；base: 这一层目录的 URL（以 / 结尾）；
    cur: 这一层里当前打开的项；open_name / open_inner: 要预先展开的子目录和它的内容。
    """
    items = []
    for i, (n, is_dir) in enumerate(entries):
        if i >= TREE_LIMIT:
            items.append(f'<li><a class="more" href="{base}"><i class="fi"></i>'
                         f'<span class="nm">… 还有 {len(entries) - i} 项</span></a></li>')
            break
        cls = ["cur"] if n == cur else []
        if n.startswith("."):
            cls.append("dot")
        nm = f'<span class="nm" title="{html.escape(n)}">{html.escape(n)}</span>'
        if is_dir:
            href = base + quote_path(n) + "/"
            if n == open_name:
                det = f'<details data-p="{href}" open><summary>{nm}</summary>{open_inner}</details>'
            else:
                det = f'<details data-p="{href}"><summary>{nm}</summary></details>'
            items.append(f'<li class="{" ".join(cls)}">{det}</li>')
        else:
            kind = FILE_KIND.get(Path(n).suffix.lower(), "")
            if kind:
                cls.append("k-" + kind)
            items.append(f'<li class="{" ".join(cls)}"><a href="{base}{file_href(n)}">'
                         f'<i class="fi"></i>{nm}</a></li>')
    return f"<ul>{''.join(items)}</ul>" if items else '<ul><li class="ph">（空）</li></ul>'


def tree_root(label: str, href: str, inner: str | None, is_cur: bool = False) -> str:
    """侧栏最外层的根节点；inner 为 None 时折叠，展开时再按需加载"""
    nm = f'<span class="nm">{html.escape(label)}</span>'
    cls = ' class="cur"' if is_cur else ""
    if inner is None:
        return f'<li><details class="root" data-p="{href}"><summary>{nm}</summary></details></li>'
    return f'<li{cls}><details class="root" data-p="{href}" open><summary>{nm}</summary>{inner}</details></li>'


def link_list(items) -> str:
    """链接列表。items: [(图标, 文字, 绝对链接, 右侧说明)]"""
    rows = "".join(
        f'<li><a href="{href}"><span class="ic">{ic}</span><span class="nm">{html.escape(label)}</span>'
        f'<span class="meta">{html.escape(meta)}</span></a></li>'
        for ic, label, href, meta in items)
    return f'<ul class="listing">{rows}</ul>' if rows else ""


def welcome(title: str, intro: str, placeholder: str, sections, *, value: str = "",
            error: str = "", tips=None) -> str:
    """欢迎页正文：标题、说明、地址输入框（GET /?u=…）、若干「小标题 + 链接列表」、使用提示。

    sections: [(小标题, link_list 的 items)]；tips: [HTML 片段]
    """
    err = f'<p class="wl-err">{html.escape(error)}</p>' if error else ""
    parts = [
        f"<h1>{html.escape(title)}</h1><p class='note'>{intro}</p>",
        '<form class="wl-form" action="/" method="get">'
        f'<input name="u" value="{html.escape(value)}" placeholder="{html.escape(placeholder)}" '
        'autocapitalize="off" autocorrect="off" spellcheck="false" required><button>打开</button></form>',
        err,
    ]
    for head, items in sections:
        if items:
            parts.append(f'<p class="wl-h">{html.escape(head)}</p>{link_list(items)}')
    if tips:
        parts.append('<p class="wl-h">小提示</p><ul class="wl-tips">'
                     + "".join(f"<li>{t}</li>" for t in tips) + "</ul>")
    return "".join(parts)


def crumbs(items, home: bool = True) -> str:
    """面包屑。items: [(文字, 链接)]，最后一项加粗不带链接"""
    out = ['<a href="/">⌂</a>'] if home else []
    for i, (name, href) in enumerate(items):
        if i == len(items) - 1:
            out.append(f"<b>{html.escape(name)}</b>")
        else:
            out.append(f'<a href="{href}">{html.escape(name)}</a>')
    return " / ".join(out)


# ---------------------------------------------------------------- HTTP

class BaseHandler(BaseHTTPRequestHandler):
    server_version = f"view/{VERSION}"
    protocol_version = "HTTP/1.1"

    verbose = False
    build = ""          # 由应用设为 build_id(...)
    md: MarkdownIt

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str, *, head=False, etag=None,
              compress=True, extra: dict | None = None):
        headers = {"Content-Type": ctype, "Cache-Control": "no-cache"}
        if etag:
            headers["ETag"] = etag
        if compress and len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        headers.update(extra or {})
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _html(self, body: bytes, head=False, etag=None, status=200):
        self._send(status, body, "text/html; charset=utf-8", head=head, etag=etag)

    def _etag(self, *parts) -> str:
        return '"' + "-".join([VERSION, self.build] + [str(p) for p in parts]) + '"'

    def _not_modified(self, etag: str) -> bool:
        inm = self.headers.get("If-None-Match")
        if inm and etag in [t.strip() for t in inm.split(",")]:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _redirect(self, location: str):
        # 用 302 + no-store：301 会被浏览器永久缓存，配置一改，旧跳转就再也改不回来
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, status: int, msg: str, head=False):
        body = page(f"{status}", '<a href="/">首页</a>',
                    f"<h1>{status}</h1><p class='empty'>{html.escape(msg)}</p>")
        self._html(body, head=head, status=status)

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_GET(self):
        self._dispatch(head=False)

    def _dispatch(self, head: bool):
        try:
            self._route(head)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 弱网下客户端中途断开很正常，忽略
        except Exception as e:  # noqa: BLE001
            try:
                self._error(500, f"{type(e).__name__}: {e}", head)
            except Exception:
                pass

    def _route(self, head: bool):
        raise NotImplementedError

    def _raw(self, name: str, data: bytes, head: bool, etag: str, as_text: bool):
        """原样返回文件内容。文本统一按 text/plain，避免 HTML/JS 在本站执行"""
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if as_text:
            ctype = "text/plain; charset=utf-8"
        extra = {"X-Content-Type-Options": "nosniff"}
        if ctype == "image/svg+xml":
            extra["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        compress = ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml")
        self._send(200, data, ctype, head=head, etag=etag, compress=compress, extra=extra)

    def serve_file(self, name: str, size: int, read: Callable[[], bytes], query: dict, *,
                   tag: str, crumbs_html: str, sidebar: str, head: bool,
                   pdf_src: Callable[[], PdfSource] | None = None):
        """按类型展示一个文件。

        read() 取文件全部内容（只在需要时调用）；tag 标识文件版本，用于 ETag；
        pdf_src() 给 PyMuPDF 的来源，默认用 read()（本地文件可以直接给路径，省一次读）。
        """
        ext = Path(name).suffix.lower()
        raw, view = "raw" in query, "view" in query
        is_md, is_img = ext in MD_EXT, ext in IMAGE_EXT
        is_pdf = ext in PDF_EXT and pymupdf is not None
        pdf_src = pdf_src or read

        if is_pdf and "page" in query and not raw:
            try:
                i = int(query["page"][0])
            except ValueError:
                return self._error(400, "页码不对", head)
            etag = self._etag(tag, f"p{i}")
            if self._not_modified(etag):
                return
            try:
                img = pdf_page_jpeg(pdf_src(), i)
            except IndexError:
                return self._error(404, "没有这一页", head)
            return self._send(200, img, "image/jpeg", head=head, etag=etag, compress=False)

        etag = self._etag(tag, "r" if raw else "v" if view else "")
        if self._not_modified(etag):
            return

        if not raw:
            if is_img and view:
                return self._html(page(name, crumbs_html, image_content(name, size), sidebar, wide=True),
                                  head, etag)
            if ext in NB_EXT and size <= MAX_NB_BYTES:
                title, content = notebook_content(self.md, name, read(), size)
                return self._html(page(title, crumbs_html, content, sidebar), head, etag)
            if is_pdf:
                return self._html(page(name, crumbs_html, pdf_content(name, size, pdf_src()), sidebar,
                                       wide=True), head, etag)
            maybe_text = size <= MAX_TEXT_BYTES and not is_img and (
                is_md or text_by_name(name) or not binary_by_name(name))
            if maybe_text:
                data = read()
                if is_md or text_by_name(name) or is_text(data):
                    text = data.decode("utf-8", errors="replace")
                    if is_md:
                        title, content = markdown_content(self.md, name, text)
                        return self._html(page(title, crumbs_html, content, sidebar), head, etag)
                    return self._html(page(name, crumbs_html, code_content(name, text, size), sidebar,
                                           wide=True), head, etag)
                return self._raw(name, data, head, etag, as_text=False)

        # 原始文件：图片、PDF 原文件、二进制、?raw
        data = read()
        as_text = not is_img and (is_md or text_by_name(name)
                                  or (not binary_by_name(name) and is_text(data)))
        return self._raw(name, data, head, etag, as_text)


def run(handler, host: str, port: int, banner: list[str]):
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        sys.exit(f"无法监听 {host}:{port}：{e}\n（端口被占用，或这个地址不属于本机的任何网卡）")
    httpd.daemon_threads = True
    for line in banner:
        print(line, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
