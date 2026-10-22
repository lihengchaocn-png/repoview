# 更新记录

## 1.3.2 — 欢迎页

- **localview 首页**：打开 `/` 不再直接跳进共享目录，而是显示欢迎页。
  - 地址输入框：可以填本机路径（`~/notes/a.md`、绝对路径，带引号也行）、共享目录里的相对路径（`sub/a.md` 或 `共享名/sub/a.md`）或本站网址，回车直接打开文件或文件夹。不在共享目录里的路径会提示，不会越界。
  - 共享目录：列出所有共享目录及其本机路径。
  - 最近修改：列出最近修改的 8 个 Markdown / PDF / notebook，可以直接点开。扫描跳过隐藏目录和 `node_modules` 等，最多 6 层、2 万项，结果缓存 60 秒。
  - 文件页的面包屑开头总是显示 ⌂，可以随时回到首页。
- **ghview 首页**：改用同一套欢迎页组件，示例增加 notebook、PDF 和 pandas 教程。看不懂的地址会提示，不再静默回到首页。
- `viewcore` 新增 `welcome()`、`link_list()` 和 `.wl-*` 样式，两个应用共用。

## 1.3.1 — Jupyter notebook 阅读

加在 `viewcore.py`（`notebook_content()`，由 `serve_file()` 调用），localview 和 ghview 都能用。

- **Markdown 单元**：按 Markdown 渲染，单元里粘贴的图片（`attachment:`）转成 data URI 显示。页面标题取第一个标题。
- **代码单元**：按 notebook 声明的语言高亮（默认 Python），执行序号 `[n]` 放在代码框右上角。
- **输出**：
  - 文本 / stderr / 报错：去掉终端颜色码，自动换行，报错标红。
  - 图片：png / jpeg / gif / svg 直接内嵌。
  - Markdown 输出：按 Markdown 渲染。
  - HTML 输出（如 pandas 表格）：放进 `sandbox="allow-same-origin"` 的 iframe。不给 `allow-scripts`，里面的脚本不会执行；`allow-same-origin` 只用来让外层读取内容高度、自动撑开。
- 兼容 nbformat 3（`worksheets`、`input`、`prompt_number`、标题单元）。
- 大小上限 30MB（notebook 常内嵌图片，普通文本上限是 2MB）。`?raw` 仍按纯文本返回 JSON。目录页和目录树里显示 📓 图标。
- 测试：`karpathy/nanoGPT` 的 `scaling_laws.ipynb`（matplotlib 图）、`jakevdp/PythonDataScienceHandbook` 的 pandas 章节（DataFrame 表格）。

## 1.3 — 拆分为 viewcore / localview / ghview

- **公共部分抽成 `viewcore.py`**：
  - `serve_file()`：统一决定一个文件按 Markdown / 代码 / 图片预览 / PDF 逐页 / notebook / 原文件哪种方式返回。数据来源由调用方传 `read()` 回调，本地读磁盘，GitHub 读 raw。
  - `pdf_content()` / `pdf_page_jpeg()` 同时支持文件路径和内存字节；`image_content()`、`markdown_content()`、`code_content()` 负责页面片段。
  - `tree_ul()` / `tree_root()` / `listing()` / `crumbs()`：目录树、目录列表、面包屑的 HTML。
  - `BaseHandler`：`_send`、`_etag`（版本 + 代码指纹）、`_not_modified`、`_redirect`（302）、`_error`、`_raw`（文本一律 `text/plain` + `nosniff`，SVG 带 CSP）、请求分发。
  - `build_id()`（代码指纹）和 `run()`（启动服务）。
- **`mdserve.py` 改名为 `localview.py`**：只保留本地目录相关的部分（共享目录、`list_dir`、符号链接检查、隐藏文件、目录 ETag）。
- **`ghview.py` 改为基于 `viewcore`**：只保留 GitHub 访问、缓存和路由。
- 两个应用的 ETag 都带上 `viewcore.py` 和自身文件的指纹，改哪个文件都会让页面失效。添加到主屏幕时的默认名字分别是「Markdown」和「GitHub」。
- 默认只监听 `127.0.0.1`；localview 不带参数时共享当前目录。

## 1.2.1 — ghview：GitHub 仓库阅读器

用同一套界面阅读 GitHub 公开仓库，**不下载仓库**，每次点击都转发到 GitHub。

- **地址**：与 GitHub 一致，把 `github.com` 换成本站即可：`/owner/repo`、`/owner/repo/tree/<ref>/<dir>/`、`/owner/repo/blob/<ref>/<file>`。首页可以粘贴任意 GitHub 地址，也支持 `/https://github.com/...` 这种直接拼接。
- **文件树**：每个仓库 + 分支调一次 git trees API（`recursive=1`）拿整棵树，缓存 5 分钟；过期后带 ETag 重新验证，没变化不占限额。目录页、侧栏懒加载、判断路径是文件还是目录都从这份缓存读。打开一个仓库约用 2 次 API（默认分支 + 文件树）。
- **文件内容**：走 `raw.githubusercontent.com`，不占 API 限额。按 commit sha 做 200MB 的 LRU 缓存，PDF 翻页不重复下载。超过 40MB 的文件直接跳到 GitHub 原始链接。
- **安全**：仓库内容不可信。Markdown 原始 HTML 不渲染；原文按 `text/plain` + `nosniff` 返回；SVG 带 CSP 禁止脚本；owner/repo 名白名单校验，路径禁止 `..`。
- **分支名带 `/`**（如 `feature/x`）：从短到长试，最多 4 段；不存在的 ref 也缓存，避免反复消耗限额。
- 测试：`karpathy/nanoGPT`（README 内图片、代码、侧栏）；`mml-book/mml-book.github.io` 的 417 页 / 16.8MB PDF，首次打开约 7 秒（从 GitHub 下载原文件），之后每页约 0.1 秒。

## 1.2

### PDF 查看

- 点开 `.pdf` 默认进入带侧栏的预览页，顶部显示页数、大小和「原文件」按钮。
- 服务端用 PyMuPDF 把每页渲染成 JPEG（`?page=N`，2 倍分辨率，质量 82）。前两页立即加载，其余懒加载；`<img>` 带宽高，滚动时版面不跳。
- 每页单独带 ETag，看过的页再次打开只返回 304。
- 这样做是因为 iPad / iPhone Safari 在 iframe / embed 里只显示 PDF 第一页；渲染成图片后所有页都能看，也不用下载几 MB 的 pdf.js。
- 代价：页面上的文字不能选中复制，需要时点「原文件」。
- 有密码或解析失败的 PDF 会给出提示，引导打开原文件。没装 pymupdf 时直接返回原文件。

### 字体放大 / 缩小

- 顶栏右侧 **A−** / **A+**，每次 ±10%，范围 70%–200%；双击任一按钮复位到 100%。
- 只缩放正文区（正文、代码、目录、front matter 表、PDF 页面宽度），顶栏和目录树不变。
- 比例存在 `localStorage`，页面渲染前就在 `<head>` 里恢复，不闪烁。按钮设了 `touch-action: manipulation`，连点不会触发双击缩放。

### 顶栏滚动时自动隐藏

- 往下滚时顶栏收起，只留顶部安全区那一条；往上滑一下或回到顶部时重新出现。手机侧栏打开时不隐藏。
- 实现：`.wrap` 用负 `margin-bottom` 盖住顶栏，顶栏没有移出页面，iPhone 状态栏下方的安全区保持不变。

### 原文件在页内浮层打开

- 在 Safari「添加到主屏幕 / 程序坞」的独立 App 窗口里没有返回按钮，原文件一旦在窗口里打开就退不回来，`target="_blank"` 也没用。
- 「原文件」「原图」「原文」「查看原文」点击时不跳转，在当前页弹出全屏查看层（iframe 内嵌原文件），「✕ 返回」或 Esc 关闭，右上角可「下载」。没有 JS 时退回新标签打开；按住 ⌘ / Ctrl / Shift 点击仍按浏览器默认处理。

### 修复

- **跳转被浏览器永久缓存**：首页原来用 301 跳到唯一的共享目录，换了共享目录后浏览器仍跳到旧地址。所有跳转改为 302 + `Cache-Control: no-store`；访问不存在的根目录时，只共享一个目录就直接带过去。
- **改了代码后浏览器仍显示旧页面**：ETag 只看文件状态，代码更新后仍返回 304。ETag 加入代码指纹，代码一改所有页面自动失效。

### 其他

- 依赖改由 uv 管理（`pyproject.toml` / `uv.lock`）。`pymupdf` 锁在 `<2`：2.0 会连带安装 onnxruntime、numpy 等大包，本项目用不上。代码保持 Python 3.9 兼容。

## 1.1 及以前

- 类 code-server 的可折叠目录树（按需加载、展开状态本地记忆、可拖宽、可隐藏）。
- Markdown 渲染（目录、front matter、脚注、任务列表）；带行号和语法高亮的代码查看（可切换换行）；图片预览。
- ETag / 304 与 gzip；亮暗色自适应。
