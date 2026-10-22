#!/usr/bin/env python3
"""
ghview —— 用和 localview 一样的界面阅读 GitHub 公开仓库，不下载仓库。

把 github.com 换成本服务地址即可：
    https://github.com/karpathy/nanoGPT             →  http://<host>:8091/karpathy/nanoGPT
    https://github.com/o/r/blob/main/docs/a.md      →  http://<host>:8091/o/r/blob/main/docs/a.md

每次点击都只是转发：
  - 文件树：GitHub git trees API（recursive=1），每个仓库 + 分支一次请求拿到整棵树，内存缓存 TREE_TTL 秒；
    目录页、侧栏、判断路径是文件还是目录都从这份缓存里读。
  - 文件内容：raw.githubusercontent.com（不占 API 限额），小 LRU 缓存，PDF 翻页不用重复下载。
  - 渲染和界面：viewcore.py（与 localview 共用）。

未登录的 GitHub API 每小时只有 60 次，设置环境变量 GITHUB_TOKEN 可提到 5000 次。
运行：
    uv run ghview.py                                     # 默认只监听本机 127.0.0.1:8091
    GITHUB_TOKEN=xxx uv run ghview.py --host 0.0.0.0     # 对外提供服务（建议放在 Caddy / Nginx 后面）
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

import viewcore as V
from viewcore import human_size, quote_path

TREE_TTL = 300          # 文件树缓存秒数：仓库更新后最多这么久能看到
RAW_TTL = 300
RAW_CACHE_BYTES = 200 * 1024 * 1024
MAX_FETCH_BYTES = 40 * 1024 * 1024   # 超过这个大小的文件不代理，给 GitHub 原始链接
UA = "ghview (+viewcore)"
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class GHError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- GitHub 访问

def _get(url: str, *, api: bool, etag: str | None = None, limit: int = MAX_FETCH_BYTES):
    """返回 (status, headers, body)；304 时 body 为 None"""
    h = {"User-Agent": UA}
    if api:
        h["Accept"] = "application/vnd.github+json"
        tok = os.environ.get("GITHUB_TOKEN")
        if tok:
            h["Authorization"] = f"Bearer {tok}"
    if etag:
        h["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise GHError(413, "文件太大")
            return r.status, r.headers, body
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, e.headers, None
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(e.headers.get("X-RateLimit-Reset") or 0)
            wait = max(0, reset - int(time.time())) // 60 + 1
            raise GHError(429, f"GitHub API 限额用完，约 {wait} 分钟后恢复（服务端设置 GITHUB_TOKEN 可提高限额）")
        if e.code == 404:
            raise GHError(404, "GitHub 上没有找到（仓库不存在、是私有仓库，或分支/路径不对）")
        raise GHError(502, f"GitHub 返回 {e.code}")
    except urllib.error.URLError as e:
        raise GHError(502, f"连不上 GitHub：{e.reason}")


class Repo:
    """一个仓库某个 ref 的文件树索引"""

    def __init__(self, owner: str, repo: str, ref: str, data: dict):
        self.owner, self.repo, self.ref = owner, repo, ref
        self.sha = data["sha"]
        self.truncated = data.get("truncated", False)
        self.dirs: dict[str, list[tuple[str, bool, int]]] = {"": []}
        self.files: dict[str, int] = {}
        for it in data["tree"]:
            p, t = it["path"], it["type"]
            parent, _, name = p.rpartition("/")
            if t == "tree":
                self.dirs.setdefault(p, [])
                self.dirs.setdefault(parent, []).append((name, True, 0))
            elif t == "blob":
                self.files[p] = it.get("size", 0)
                self.dirs.setdefault(parent, []).append((name, False, it.get("size", 0)))
        for v in self.dirs.values():
            v.sort(key=lambda e: (not e[1], e[0].casefold()))

    @property
    def base(self) -> str:
        return f"/{quote_path(self.owner)}/{quote_path(self.repo)}"

    def url(self, path: str, is_dir: bool) -> str:
        kind = "tree" if is_dir else "blob"
        p = "/".join(quote_path(x) for x in path.split("/")) if path else ""
        return f"{self.base}/{kind}/{quote_path(self.ref)}/{p}{'/' if is_dir and p else ''}"

    def raw_url(self, path: str) -> str:
        return (f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{quote_path(self.ref)}/"
                + "/".join(quote_path(x) for x in path.split("/")))


class GitHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.api_cache: dict[str, tuple[float, str | None, dict]] = {}
        self.raw_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.raw_size = 0

    def _api(self, path: str, ttl: int) -> dict:
        url = "https://api.github.com" + path
        with self.lock:
            hit = self.api_cache.get(url)
        if hit and time.time() - hit[0] < ttl:
            return hit[2]
        # 过期后带 ETag 去问：没变化时 GitHub 返回 304，不占限额
        status, headers, body = _get(url, api=True, etag=hit[1] if hit else None, limit=64 * 1024 * 1024)
        data = hit[2] if status == 304 and hit else json.loads(body)
        with self.lock:
            self.api_cache[url] = (time.time(), headers.get("ETag"), data)
        return data

    def default_branch(self, owner: str, repo: str) -> str:
        return self._api(f"/repos/{owner}/{repo}", 3600)["default_branch"]

    def repo(self, owner: str, repo: str, ref: str) -> Repo:
        key = ("repo", owner, repo, ref)
        miss = ("miss",) + key
        with self.lock:
            m = self.api_cache.get(miss)
        if m and time.time() - m[0] < TREE_TTL:  # 不存在的 ref 也缓存，免得反复消耗限额
            raise GHError(404, "没有这个分支")
        try:
            data = self._api(f"/repos/{owner}/{repo}/git/trees/{quote_path(ref)}?recursive=1", TREE_TTL)
        except GHError as e:
            if e.status == 404:
                with self.lock:
                    self.api_cache[miss] = (time.time(), None, {})
            raise
        with self.lock:
            r = self.api_cache.get(key)
            if r and r[2].sha == data["sha"]:
                return r[2]
        r = Repo(owner, repo, ref, data)
        with self.lock:
            self.api_cache[key] = (0, None, r)
        return r

    def raw(self, r: Repo, path: str) -> bytes:
        key = f"{r.owner}/{r.repo}@{r.sha}:{path}"
        with self.lock:
            hit = self.raw_cache.get(key)
            if hit and time.time() - hit[0] < RAW_TTL:
                self.raw_cache.move_to_end(key)
                return hit[1]
        _, _, body = _get(r.raw_url(path), api=False)
        with self.lock:
            if key not in self.raw_cache:
                self.raw_cache[key] = (time.time(), body)
                self.raw_size += len(body)
            while self.raw_size > RAW_CACHE_BYTES and self.raw_cache:
                _, (_, old) = self.raw_cache.popitem(last=False)
                self.raw_size -= len(old)
        return body


GH = GitHub()


# ---------------------------------------------------------------- 页面

def from_github_url(u: str) -> str | None:
    """各种形式的 GitHub 地址 → 本站路径"""
    u = re.sub(r"^(?:https?:/+)?(?:www\.)?github\.com/", "", u.strip()).strip("/")
    u = re.sub(r"\.git$", "", u)
    parts = u.split("/")
    if len(parts) < 2 or not all(NAME_RE.match(p) for p in parts[:2]):
        return None
    return "/" + "/".join(quote_path(p) for p in parts) + ("/" if len(parts) <= 2 else "")


class Handler(V.BaseHandler):
    server_version = f"ghview/{V.VERSION}"

    def _route(self, head: bool):
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        path = urllib.parse.unquote(split.path)

        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon", head=head)
        if path in ("", "/"):
            if "u" in query:
                u = query["u"][0]
                dest = from_github_url(u)
                if dest:
                    return self._redirect(dest)
                return self._html(self._home(u, "看不懂这个地址，请填 GitHub 仓库或文件的网址，例如 owner/repo"), head)
            return self._html(self._home(), head)

        # 直接粘贴整个 GitHub 地址：/https://github.com/o/r/...
        m = re.match(r"^/+(?:https?:/+)?(?:www\.)?github\.com(/.*)$", path)
        if m:
            return self._redirect(from_github_url(m.group(1)) or "/")

        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or not all(NAME_RE.match(p) for p in parts[:2]):
            return self._error(404, "地址格式：/owner/repo/…", head)
        if any(p in ("..", ".") for p in parts):
            return self._error(403, "路径不合法", head)
        owner, repo = parts[0], parts[1]
        try:
            if len(parts) <= 3 or parts[2] not in ("tree", "blob", "raw"):
                ref = GH.default_branch(owner, repo)
                return self._redirect(f"/{owner}/{repo}/tree/{quote_path(ref)}/")
            r, rel = self._resolve(owner, repo, parts[3:])
            if rel in r.dirs:
                if "tree" in query:
                    return self._tree_fragment(r, rel, head)
                if not path.endswith("/"):
                    return self._redirect(r.url(rel, True))
                return self._dir(r, rel, head)
            if rel in r.files:
                if parts[2] == "raw":
                    query["raw"] = [""]
                return self._file(r, rel, query, head)
            if r.truncated:
                return self._error(404, "仓库太大，GitHub 只返回了部分文件树，这个路径不在里面", head)
            return self._error(404, "仓库里没有这个文件", head)
        except GHError as e:
            return self._error(e.status, str(e), head)

    def _resolve(self, owner: str, repo: str, rest: list[str]) -> tuple[Repo, str]:
        """rest = ref + 路径。分支名可能带 /（如 feature/x），从短到长试"""
        last = None
        for n in range(1, min(len(rest), 4) + 1):
            try:
                return GH.repo(owner, repo, "/".join(rest[:n])), "/".join(rest[n:])
            except GHError as e:
                if e.status != 404:
                    raise
                last = e
        raise last or GHError(404, "没有这个分支")

    # ---- 首页

    def _home(self, value: str = "", error: str = "") -> bytes:
        ex = [("karpathy/nanoGPT", "README · 代码 · notebook"),
              ("karpathy/nanoGPT/blob/master/scaling_laws.ipynb", "Jupyter notebook"),
              ("mml-book/mml-book.github.io/blob/master/book/mml-book.pdf", "417 页 PDF"),
              ("jakevdp/PythonDataScienceHandbook", "pandas / numpy 教程")]
        body = V.welcome(
            "GitHub 仓库阅读器",
            "粘贴 GitHub 仓库或文件地址；也可以直接把网址里的 <code>github.com</code> 换成本站地址。",
            "https://github.com/owner/repo", [("示例", [("📦", e, "/" + e, m) for e, m in ex])],
            value=value, error=error,
            tips=["支持 <code>owner/repo</code>、<code>github.com/owner/repo/tree/分支/目录</code>、"
                  "<code>…/blob/分支/文件</code> 等各种写法。",
                  "PDF 逐页渲染，notebook 显示图表和表格，A− / A+ 调字号。"])
        return V.page("ghview", "<b>⌂</b>", body)

    # ---- 面包屑 / 目录树

    def _crumbs(self, r: Repo, rel: str, file: bool) -> str:
        segs = rel.split("/") if rel else []
        dir_segs = segs[:-1] if file else segs
        items = [(f"{r.owner}/{r.repo}", r.url("", True))]
        for i, s in enumerate(dir_segs):
            items.append((s, r.url("/".join(dir_segs[: i + 1]), True)))
        if file:
            items.append((segs[-1], ""))
        gh = f"https://github.com/{r.owner}/{r.repo}/{'blob' if file else 'tree'}/{r.ref}/{rel}"
        return (V.crumbs(items) + f' <span style="opacity:.6">@{html.escape(r.ref)}</span>'
                f' · <a href="{html.escape(gh)}" target="_blank" rel="noopener">GitHub↗</a>')

    def _tree_ul(self, r: Repo, d: str, chain: list[str], cur: str | None) -> str:
        entries = [(n, is_dir) for n, is_dir, _ in r.dirs.get(d, []) if n not in V.SKIP_NAMES]
        prefix = f"{d}/" if d else ""
        cur_name = cur[len(prefix):] if cur and cur.startswith(prefix) and "/" not in cur[len(prefix):] else None
        open_name = chain[0] if chain else None
        inner = self._tree_ul(r, prefix + open_name, chain[1:], cur) if open_name else ""
        return V.tree_ul(entries, r.url(d, True), cur_name, open_name, inner)

    def _sidebar(self, r: Repo, dir_rel: str, cur: str | None) -> str:
        chain = dir_rel.split("/") if dir_rel else []
        inner = self._tree_ul(r, "", chain, cur)
        return f"<ul>{V.tree_root(r.repo, r.url('', True), inner, is_cur=(cur == ''))}</ul>"

    def _tree_fragment(self, r: Repo, rel: str, head: bool):
        etag = self._etag(r.sha, "t", rel)
        if self._not_modified(etag):
            return
        self._html(self._tree_ul(r, rel, [], None).encode("utf-8"), head, etag)

    # ---- 目录 / 文件

    def _dir(self, r: Repo, rel: str, head: bool):
        etag = self._etag(r.sha, "d", rel)
        if self._not_modified(etag):
            return
        entries = r.dirs.get(rel, [])
        body = V.listing([(n, is_dir, "" if is_dir else human_size(size)) for n, is_dir, size in entries])
        if r.truncated:
            body += '<p class="note">⚠️ 仓库太大，GitHub 只返回了部分文件树。</p>'

        names = {n for n, is_dir, _ in entries if not is_dir}
        readme = next((n for n in V.README_NAMES if n in names), None) or next(
            (n for n in sorted(names) if n.lower().startswith("readme") and Path(n).suffix.lower() in V.MD_EXT),
            None)
        if readme:
            try:
                text = GH.raw(r, f"{rel}/{readme}" if rel else readme).decode("utf-8", errors="replace")
                html_body, _, _ = V.render_markdown(self.md, text)
                body += f'<div class="readme"><p class="note">📝 {html.escape(readme)}</p>{html_body}</div>'
            except GHError:
                pass
        title = f"{r.owner}/{r.repo}" + (f"/{rel}" if rel else "")
        self._html(V.page(title, self._crumbs(r, rel, False), body, self._sidebar(r, rel, rel)), head, etag)

    def _file(self, r: Repo, rel: str, query: dict, head: bool):
        size = r.files[rel]
        if size > MAX_FETCH_BYTES:
            return self._redirect(r.raw_url(rel))
        self.serve_file(
            rel.rsplit("/", 1)[-1], size, lambda: GH.raw(r, rel), query,
            tag=f"{r.sha}-{quote_path(rel)}", head=head,
            crumbs_html=self._crumbs(r, rel, True),
            sidebar=self._sidebar(r, rel.rpartition("/")[0], rel),
        )


def main():
    ap = argparse.ArgumentParser(description="用 localview 的界面阅读 GitHub 公开仓库")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机）")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args()

    V.APP_TITLE = "ghview"
    Handler.verbose = args.verbose
    Handler.md = V.build_markdown(False)  # 仓库内容不可信：不渲染原始 HTML，避免 XSS
    Handler.build = V.build_id(V.__file__, __file__)
    tok = "已设置" if os.environ.get("GITHUB_TOKEN") else "未设置（API 每小时 60 次）"
    V.run(Handler, args.host, args.port,
          [f"ghview {V.VERSION}  →  http://{args.host}:{args.port}/   GITHUB_TOKEN {tok}"])


if __name__ == "__main__":
    main()
