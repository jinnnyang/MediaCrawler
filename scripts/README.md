# Douyin 调试 / 抓包脚本

这些脚本用于**排查抖音 web API 风控变化**（新增/更换的签名头、参数、Cookie 校验等）。全部通过 CDP 连到已经**跑起来的 Chrome（`--remote-debugging-port=9222`）**，不启动新浏览器实例。

## 前置条件

1. 一个 Chrome 实例已经用 CDP 参数启动，并已登录抖音：
   ```bash
   chrome.exe --remote-debugging-port=9222 --user-data-dir=C:/Users/jinnn/.cdp-chrome-profile https://www.douyin.com/
   ```
2. 至少打开一个 `douyin.com` 标签页（不管什么页面）。

## 脚本清单

### `probe_comment_request.py`（JS 层 fetch/XHR 钩子）

**目的**：在抖音标签页的 JS 上下文里 hook `window.fetch` / `XMLHttpRequest`，捕获所有 `/comment/list` 请求的 fetch init 视角（URL、method、显式设置的 headers、request body、response）。

**用法**：
```bash
uv run python scripts/probe_comment_request.py
```
然后在浏览器里手动滚评论/点视频触发请求；脚本装完 hook 后立即返回。用 `dump_comment_log.py` 拉结果。

**局限**：只看到**JS 代码显式传给 fetch/XHR 的 headers**（Chrome 自动附加的 Cookie / User-Agent / Referer / bd-ticket-guard-* / Sec-Fetch-* **都看不到**）。要看这些用 `probe_comment_full_headers.py`。

### `probe_comment_full_headers.py`（网络层完整头抓包）

**目的**：用 CDP `Network.requestWillBeSentExtraInfo` 事件抓 Chrome **实际发到线上的完整请求头**，能看到 fetch init 里看不到的 Chrome 默认头（Cookie、User-Agent、bd-ticket-guard-client-data 等）。

**用法**：
```bash
uv run python scripts/probe_comment_full_headers.py
```
脚本会监听 45 秒；期间手动在浏览器里滚评论/点视频触发请求；结果保存到 `data/real-comment-full-headers.json`。

**关键场景**：排查签名头、时间戳签名、客户端证书签名（如 bd-ticket-guard-*）等风控项。

### `dump_comment_log.py`（拉取 JS 钩子累积的日志）

**目的**：把 `probe_comment_request.py` 装的 hook 累积在 `window._commentLog` 里的记录 dump 出来。

**用法**：先运行 `probe_comment_request.py` 装钩子 → 在浏览器里滚评论 → 跑 `uv run python scripts/dump_comment_log.py`。

## 什么时候用这些脚本

- 抖音突然新增签名参数（如 a_bogus/msToken/bd-ticket-guard/...）导致爬虫 blocked
- 爬虫的 HTTP 请求头与真实浏览器不一致，需要对齐
- 想搞清楚某个接口需要什么 Cookie 才能通过风控

## 排查流程模板

1. 跑 `probe_comment_full_headers.py`（45s 窗口，用户滚一下）
2. 对比真实请求 vs 爬虫构造请求（URL 参数、请求头、Cookie）
3. 找到差异 → 打补丁 → 重跑爬虫端到端验证
