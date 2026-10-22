#!/usr/bin/env python3
"""
localview —— 把本机的 Markdown / 代码 / PDF / notebook 目录变成能用浏览器阅读的网站。

左边是类似 code-server 的目录树（文件夹可展开/折叠，按需加载），右边阅读
Markdown、带行号和语法高亮的代码、图片，以及 PDF（服务端逐页渲染成图片）。
渲染和界面在 viewcore.py，这里只负责读本地磁盘。

为什么适合弱网：每个页面都是一次普通的 HTTP 请求，服务端渲染好 HTML 直接返回，
没有 WebSocket、没有长连接，也不依赖任何外部 CDN。网络抖动最多是这一次刷新失败，
重新点一下就好。页面带 ETag，没改过的文件再次打开只返回 304，流量很小。
目录树的子目录在展开时才请求一小段 HTML，展开状态记在浏览器本地。

依赖由 uv 管理（见同目录 pyproject.toml / uv.lock）：
    uv sync

运行（不带参数时共享当前目录，只监听本机 127.0.0.1:8090）：
    uv run localview.py ~/notes                          # 共享一个目录
    uv run localview.py notes=~/notes code=~/src         # 共享多个目录
    uv run localview.py ~/notes --host 0.0.0.0           # 让局域网 / VPN 里的手机、平板访问

目录参数可写成「名称=路径」，也可以只写路径（名称取文件夹名）。
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import viewcore as V
from viewcore import human_size, quote_path


def list_dir(d: Path, show_all: bool, keep: str | None = None):
    """返回 ([(名字, 是否目录, stat)], 隐藏数)。keep 是即使隐藏也要保留的项（当前路径上的）。"""
    rows = []
    hidden = 0
    for e in os.scandir(d):
        if e.name != keep and (e.name in V.SKIP_NAMES or (not show_all and e.name.startswith("."))):
            hidden += 1
            continue
        try:
            is_dir = e.is_dir()
            st = e.stat()
        except OSError:
            continue
        rows.append((e.name, is_dir, st))
    rows.sort(key=lambda r: (not r[1], r[0].casefold()))
    return rows, hidden


RECENT_EXT = V.MD_EXT | V.PDF_EXT | V.NB_EXT
RECENT_N = 8            # 首页列出最近修改的文件数
RECENT_SCAN_MAX = 20000  # 扫描上限：目录太大时宁可不全，也不能拖慢首页
RECENT_DEPTH = 6
RECENT_TTL = 60


def recent_files(roots: dict[str, Path]) -> list[tuple[float, str, str]]:
    """共享目录里最近修改的 Markdown / PDF / notebook：[(mtime, 根名, 相对路径)]"""
    found: list[tuple[float, str, str]] = []
    seen = 0
    for name, root in roots.items():
        stack = [(root, 0)]
        while stack and seen < RECENT_SCAN_MAX:
            d, depth = stack.pop()
            try:
                it = list(os.scandir(d))
            except OSError:
                continue
            for e in it:
                seen += 1
                if e.name.startswith(".") or e.name in V.SKIP_NAMES:
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if depth < RECENT_DEPTH:
                            stack.append((Path(e.path), depth + 1))
                    elif Path(e.name).suffix.lower() in RECENT_EXT:
                        found.append((e.stat().st_mtime, name, str(Path(e.path).relative_to(root))))
                except OSError:
                    continue
    found.sort(reverse=True)
    return found[:RECENT_N]


class Handler(V.BaseHandler):
    server_version = f"localview/{V.VERSION}"

    roots: dict[str, Path] = {}
    follow_symlinks = False
    _recent: tuple[float, list] = (0.0, [])
    _recent_lock = threading.Lock()

    # ---- 小工具

    def _chain_stats(self, root_name: str, dir_parts: list[str]) -> list:
        """侧栏会列出这些目录的内容，它们变了页面也要变"""
        d = self.roots[root_name]
        out = []
        for p in [None] + dir_parts:
            if p is not None:
                d = d / p
            try:
                out.append(d.stat())
            except OSError:
                break
        return out

    def _stat_etag(self, *stats, extra: str = "") -> str:
        return self._etag(*[V.stat_tag(s) for s in stats], extra)

    def _crumbs(self, root_name: str, rel_parts: list[str], file: str | None = None) -> str:
        href = f"/{quote_path(root_name)}/"
        items = [(root_name, href)]
        for p in rel_parts:
            href += quote_path(p) + "/"
            items.append((p, href))
        if file:
            items.append((file, ""))
        return V.crumbs(items)

    # ---- 目录树

    def _tree_ul(self, root_name: str, rel_parts: list[str], d: Path,
                 chain: list[str], cur: list[str] | None) -> str:
        """列出 d 的子项；chain 是还要继续往下展开的路径，cur 是当前打开的文件/目录"""
        try:
            entries, _ = list_dir(d, False, keep=chain[0] if chain else None)
        except OSError:
            return '<ul><li class="ph">无法读取</li></ul>'
        base = f"/{quote_path(root_name)}/" + "".join(quote_path(p) + "/" for p in rel_parts)
        cur_name = cur[len(rel_parts)] if cur and len(cur) == len(rel_parts) + 1 and \
            cur[:len(rel_parts)] == rel_parts else None
        open_name = chain[0] if chain else None
        inner = ""
        if open_name and any(n == open_name and is_dir for n, is_dir, _ in entries):
            inner = self._tree_ul(root_name, rel_parts + [open_name], d / open_name, chain[1:], cur)
        return V.tree_ul([(n, is_dir) for n, is_dir, _ in entries], base, cur_name, open_name, inner)

    def _sidebar(self, root_name: str | None, dir_parts: list[str], cur: list[str] | None) -> str:
        out = []
        for n, p in self.roots.items():
            inner = self._tree_ul(n, [], p, dir_parts, cur) if n == root_name else None
            out.append(V.tree_root(n, f"/{quote_path(n)}/", inner, is_cur=(n == root_name and cur == [])))
        return f"<ul>{''.join(out)}</ul>"

    # ---- 路由

    def _route(self, head: bool):
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        path = urllib.parse.unquote(split.path)

        if path in ("", "/"):
            if "u" in query:
                u = query["u"][0]
                dest = self._resolve_input(u)
                if dest:
                    return self._redirect(dest)
                return self._html(self._home(u, "没找到这个路径：要在共享目录里面，可以填本机路径、相对路径或本站网址"), head)
            return self._html(self._home(), head)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon", head=head)

        name, _, rel = path.lstrip("/").partition("/")
        root = self.roots.get(name)
        if root is None:
            # 旧书签 / 浏览器缓存的旧跳转（共享目录改名后）：只共享一个目录时直接带过去
            if len(self.roots) == 1:
                return self._redirect(f"/{quote_path(next(iter(self.roots)))}/")
            return self._error(404, "没有这个目录，请从首页进入", head)
        if "/" not in path.lstrip("/"):
            return self._redirect(f"/{quote_path(name)}/")

        rel_parts = [p for p in rel.split("/") if p]
        if any(p in ("..", ".") for p in rel_parts):
            return self._error(403, "路径不合法", head)
        target = root.joinpath(*rel_parts)
        if not target.exists():
            return self._error(404, "文件不存在", head)
        real = target.resolve()
        if not self.follow_symlinks and real != root and root not in real.parents:
            return self._error(403, "符号链接指向了共享目录之外（启动时加 --follow-symlinks 可放行）", head)

        if real.is_dir():
            if "tree" in query:
                return self._tree_fragment(name, rel_parts, real, head)
            if not path.endswith("/"):
                return self._redirect(quote_path(path) + "/")
            return self._dir(name, rel_parts, real, head, show_all="all" in query)
        return self._file(name, rel_parts, real, query, head)

    # ---- 页面

    @staticmethod
    def _tilde(p: Path) -> str:
        home = str(Path.home())
        s = str(p)
        return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s

    def _url_for(self, name: str, rel: Path, is_dir: bool) -> str:
        parts = [p for p in rel.parts if p not in ("", ".")]
        return f"/{quote_path(name)}/" + "/".join(quote_path(p) for p in parts) + ("/" if is_dir and parts else "")

    def _resolve_input(self, u: str) -> str | None:
        """把输入框里的内容变成本站地址：本机路径（绝对 / ~ 开头）、本站网址、相对路径都行"""
        u = u.strip().strip('"').strip("'")
        if not u:
            return None
        if re.match(r"^https?://", u):  # 本站网址：只取路径部分
            u = urllib.parse.unquote(urllib.parse.urlsplit(u).path)
        cands: list[tuple[str, Path]] = []
        p = Path(os.path.expanduser(u))
        if p.is_absolute():
            real = p.resolve()
            for n, root in self.roots.items():
                if real == root or root in real.parents:
                    cands.append((n, real.relative_to(root)))
            # 本站网址的路径（/zl/a/b）也是绝对形式
            first, _, rest = u.lstrip("/").partition("/")
            if first in self.roots:
                cands.append((first, Path(rest)))
        else:
            first, _, rest = u.partition("/")
            if first in self.roots:
                cands.append((first, Path(rest)))
            cands += [(n, Path(u)) for n in self.roots]
        for n, rel in cands:
            if ".." in rel.parts:
                continue
            target = self.roots[n] / rel
            if target.exists():
                return self._url_for(n, rel, target.is_dir())
        return None

    def _recent_cached(self) -> list:
        with self._recent_lock:
            t, items = Handler._recent
            if time.time() - t < RECENT_TTL:
                return items
        items = recent_files(self.roots)
        with self._recent_lock:
            Handler._recent = (time.time(), items)
        return items

    def _home(self, value: str = "", error: str = "") -> bytes:
        roots = [("📁", n, f"/{quote_path(n)}/", self._tilde(p)) for n, p in self.roots.items()]
        recent = []
        for mtime, n, rel in self._recent_cached():
            when = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
            label = f"{n}/{rel}" if len(self.roots) > 1 else rel
            recent.append((V.file_icon(rel), label, self._url_for(n, Path(rel), False), when))
        first = next(iter(self.roots.values()))
        body = V.welcome(
            "本地文件阅读器",
            "输入本机路径或共享目录里的相对路径，直接打开文件或文件夹；也可以从左边的目录树开始浏览。",
            f"{self._tilde(first)}/… 或 子目录/文件.md",
            [("共享目录", roots), ("最近修改", recent)],
            value=value, error=error,
            tips=[f"支持 <code>{html.escape(self._tilde(first))}/notes/a.md</code> 这样的本机路径、"
                  "<code>notes/a.md</code> 这样的相对路径，以及本站网址。",
                  "Mac 上在访达里选中文件按 <code>⌥⌘C</code> 可以拷贝完整路径。",
                  "Markdown、代码、PDF、Jupyter notebook、图片都能直接看；A− / A+ 调字号。"])
        return V.page("localview", "<b>⌂</b>", body, sidebar=self._sidebar(None, [], None))

    def _tree_fragment(self, name: str, rel_parts: list[str], d: Path, head: bool):
        try:
            etag = self._stat_etag(d.stat(), extra="t")
        except OSError:
            return self._error(404, "目录不存在", head)
        if self._not_modified(etag):
            return
        self._html(self._tree_ul(name, rel_parts, d, [], None).encode("utf-8"), head, etag)

    def _dir(self, name: str, rel_parts: list[str], d: Path, head: bool, show_all: bool):
        try:
            entries, hidden = list_dir(d, show_all)
        except PermissionError:
            return self._error(403, "没有权限读取这个目录", head)

        readme = next((d / n for n in V.README_NAMES if (d / n).is_file()), None)
        stats = self._chain_stats(name, rel_parts) + ([readme.stat()] if readme else [])
        etag = self._stat_etag(*stats, extra="a" if show_all else "")
        if self._not_modified(etag):
            return

        rows = []
        for n, is_dir, st in entries:
            when = datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            rows.append((n, is_dir, when if is_dir else f"{human_size(st.st_size)} · {when}"))
        body = V.listing(rows)
        if hidden and not show_all:
            body += f'<p class="note"><a href="?all">显示 {hidden} 个隐藏项</a></p>'
        elif show_all:
            body += '<p class="note"><a href="?">隐藏点开头的文件</a></p>'

        if readme:
            try:
                html_body, _, _ = V.render_markdown(self.md, readme.read_text("utf-8", errors="replace"))
                body += f'<div class="readme"><p class="note">📝 {html.escape(readme.name)}</p>{html_body}</div>'
            except OSError:
                pass

        title = rel_parts[-1] if rel_parts else name
        side = self._sidebar(name, rel_parts, rel_parts)
        self._html(V.page(title, self._crumbs(name, rel_parts), body, side), head, etag)

    def _file(self, name: str, rel_parts: list[str], f: Path, query: dict, head: bool):
        try:
            st = f.stat()
        except OSError:
            return self._error(404, "文件不存在", head)

        def read() -> bytes:
            return f.read_bytes()

        try:
            # 侧栏列出了上层目录，目录变了页面也要变
            tag = "-".join(V.stat_tag(s) for s in [st] + self._chain_stats(name, rel_parts[:-1]))
            self.serve_file(
                f.name, st.st_size, read, query, tag=tag, head=head, pdf_src=lambda: f,
                crumbs_html=self._crumbs(name, rel_parts[:-1], f.name),
                sidebar=self._sidebar(name, rel_parts[:-1], rel_parts),
            )
        except PermissionError:
            return self._error(403, "没有权限读取这个文件", head)


# ---------------------------------------------------------------- 启动

def parse_roots(specs: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for spec in specs or ["."]:
        if "=" in spec and not Path(spec).exists():
            n, _, p = spec.partition("=")
        else:
            p = spec
            n = Path(os.path.expanduser(p)).resolve().name.lstrip(".") or "root"
        path = Path(os.path.expanduser(p)).resolve()
        if not path.is_dir():
            sys.exit(f"不是目录：{p}")
        base, i = n, 2
        while n in roots:
            n, i = f"{base}{i}", i + 1
        roots[n] = path
    return roots


def main():
    ap = argparse.ArgumentParser(description="用浏览器阅读本地 Markdown / 代码 / PDF 目录")
    ap.add_argument("roots", nargs="*", help="要共享的目录，可写成 名称=路径（默认当前目录）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机；局域网访问用 0.0.0.0 或本机网卡地址）")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--follow-symlinks", action="store_true", help="允许符号链接指向共享目录之外")
    ap.add_argument("--allow-html", action="store_true", help="渲染 Markdown 里的原始 HTML")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args()

    Handler.roots = parse_roots(args.roots)
    Handler.follow_symlinks = args.follow_symlinks
    Handler.verbose = args.verbose
    Handler.md = V.build_markdown(args.allow_html)
    Handler.build = V.build_id(V.__file__, __file__)

    banner = [f"localview {V.VERSION}  →  http://{args.host}:{args.port}/"]
    banner += [f"  /{n}/  →  {p}" for n, p in Handler.roots.items()]
    V.APP_TITLE = "localview"
    V.run(Handler, args.host, args.port, banner)


if __name__ == "__main__":
    main()
