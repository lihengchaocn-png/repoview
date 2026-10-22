# repoview

`localview` 看本地目录，`ghview` 看 GitHub 仓库。

在手机、平板上舒服地阅读 Markdown、代码、PDF 和 Jupyter notebook。服务端渲染好 HTML 直接返回，不依赖外部 CDN，适合弱网。

- **localview**：把本机的目录变成可浏览的网站。
- **ghview**：阅读 GitHub 公开仓库，不下载仓库，把网址里的 `github.com` 换成本站地址即可。

两者共用 `viewcore.py` 里的渲染和界面。

## 功能

- **目录树**：类似 VS Code 的侧栏，文件夹按需加载，展开状态记在浏览器本地，可拖宽、可隐藏。
- **Markdown**：标题目录、front matter、脚注、任务列表、代码高亮。
- **代码**：行号、语法高亮（Pygments）、长行换行开关。
- **PDF**：服务端用 PyMuPDF 逐页渲染成图片并懒加载。手机 Safari 内嵌 PDF 只能显示第一页，这样所有页都能看。
- **Jupyter notebook**：Markdown 单元、代码高亮、文本 / 报错 / 图片输出。pandas 表格等 HTML 输出放在禁止脚本的沙盒 iframe 里显示。
- **图片**：预览页和原图。
- **阅读体验**：A− / A+ 调字号、往下滚自动隐藏顶栏、亮暗色自适应。添加到主屏幕后像独立 App，原文件在页内浮层打开，不会“回不去”。
- **省流量**：每个页面都是普通 HTTP 请求，带 ETag，没改过的内容只返回 304，大文件 gzip 压缩。

## 安装

需要 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/lihengchaocn-png/repoview.git
cd repoview
uv sync
```

## localview：浏览本机目录

```bash
uv run localview.py                         # 共享当前目录，http://127.0.0.1:8090/
uv run localview.py ~/notes                 # 共享指定目录
uv run localview.py notes=~/notes src=~/src # 共享多个目录（名称=路径）
uv run localview.py ~/notes --host 0.0.0.0  # 让同一局域网 / VPN 里的手机、平板访问
```

其他参数：`--port`、`--follow-symlinks`（允许符号链接指向共享目录之外）、`--allow-html`（渲染 Markdown 里的原始 HTML）、`-v`（访问日志）。

> 默认只监听本机。用 `--host 0.0.0.0` 后，同一网络里的任何人都能读到共享目录，请只在可信网络里使用。

## ghview：阅读 GitHub 仓库

```bash
uv run ghview.py                            # http://127.0.0.1:8091/
GITHUB_TOKEN=ghp_xxx uv run ghview.py       # 提高 GitHub API 限额
```

地址与 GitHub 一一对应：

```
https://github.com/karpathy/nanoGPT                   →  http://<host>:8091/karpathy/nanoGPT
https://github.com/owner/repo/blob/main/docs/a.md     →  http://<host>:8091/owner/repo/blob/main/docs/a.md
```

工作方式：
- **文件树**：每个仓库 + 分支调一次 GitHub git trees API，缓存 5 分钟。
- **文件内容**：从 `raw.githubusercontent.com` 取，不占 API 限额。

未登录时 GitHub API 每小时只有 60 次，设置 `GITHUB_TOKEN`（不需要任何权限的 token 即可）可以提到每小时 5000 次。

对公网提供服务时，建议放在 Caddy / Nginx 等反向代理后面，由它负责 HTTPS。目前还没有限流。

已知限制：
- 只支持公开仓库。
- 超大仓库的文件树会被 GitHub 截断。
- Markdown 里以 `/` 开头的绝对链接没有改写。

## 文件

| 文件 | 作用 |
|---|---|
| `viewcore.py` | 渲染（Markdown / 代码 / PDF / notebook / 图片）、CSS / JS、页面框架、HTTP 基础处理 |
| `localview.py` | 本地目录 |
| `ghview.py` | GitHub 仓库 |
| `CHANGELOG.md` | 更新记录 |
| `LICENSE` | MIT 许可证 |

依赖：`markdown-it-py`、`mdit-py-plugins`、`pygments`、`pymupdf`。代码兼容 Python 3.9+。

## 许可证

[MIT](LICENSE)
