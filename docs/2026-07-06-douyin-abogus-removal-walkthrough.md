# 抖音 a_bogus 移除改造 — Walkthrough & Findings

**日期**：2026-07-06
**分支**：`feat/remove-abogus`
**背景**：MediaCrawler 抖音爬虫在 2026 年因抖音风控体系变更导致完全失效。本次改造将其从"URL 签名（a_bogus/msToken/X-Bogus）"路径完全迁移到"设备指纹（uifid）+ 稳定会话 ID（webid）"路径。

---

## Part 1 · Findings（关键发现）

### 1.1 抖音风控 2026 年的换代

通过对真实 Firefox 152 已登录浏览器的 XHR 抓包（29 参数键 + hook 埋点），确认：

| 项目 | 旧值（截至 2024-12） | 新值（实测 2026-07） |
|---|---|---|
| URL 签名参数 | `a_bogus`（必带）+ `msToken`（必带）+ `X-Bogus` | **全部不需要** |
| 签名 SDK | `window.bdms.init._v` + `window.byted_acrawler.sign` | 已废弃，仅剩 `frontierSign / setTTWid` 等配置类方法 |
| 核心设备指纹 | 无 | **`uifid`（172 字符）— 服务端下发的持久指纹** |
| 会话 ID | `webid`（每次可以随机） | **`webid` 需保持会话稳定**（否则被判为爬虫） |
| 登录判活 cookie | `LOGIN_STATUS=1` | `login_time`（时间戳）+ `localStorage.HasUserLogin` |
| Cookie 层风控 | `sessionid` + 少量 | `bd_ticket_guard_*` + `s_v_web_id` + `passport_csrf_token`（多层） |

### 1.2 决定性证据

在真实浏览器中触发 crawler 会打的目标接口，观察签名字段：

```
/aweme/v1/web/general/search/single/  → sign: NONE, tok: 0, uifid: true
/aweme/v1/web/general/search/stream/  → sign: NONE, tok: 0, uifid: true
/aweme/v1/web/hot/search/list/        → sign: NONE, tok: 0, uifid: true
/aweme/v1/web/history/write/          → sign: NONE, tok: 0, uifid: true
/aweme/v1/web/search/sug/             → sign: NONE, tok: 0, uifid: true
```

`sign: NONE` 表示 URL query 里没有任何签名字段。所有接口共享同一批 29 个参数，无一含 `a_bogus / msToken / X-Bogus / _signature`。

### 1.3 影响面确认

用 GitNexus `impact` 工具对 5 个核心符号做上游依赖分析：

| 符号 | 直接调用者 | 跨平台污染 | 风险 |
|---|---|---|---|
| `__process_req_params` | douyin/client.py 内部 | 无 | LOW（模块内） |
| `get_a_bogus` | client.py:__process_req_params × 1 | 无 | LOW |
| `get_web_id` | client.py:__process_req_params × 1 | 无 | LOW |
| `pong` | core.py:start × 1 | 无 | LOW |
| `check_login_state` | login.py:login_by_qrcode × 1 | 无 | LOW |

`affected_modules` 全部落在 Douyin 域内，**跨平台污染为零**。

---

## Part 2 · Walkthrough（改造过程）

### 阶段 A · 侦察（Reconnaissance）

**目标**：确认真实浏览器打抖音 API 时到底带哪些签名字段。

1. 用 CDP 接管一个真实 Firefox（登录状态），注入 XHR hook：
   ```js
   window._dyHookInstalled = true;
   const _fetch = window.fetch; var log = [];
   window.fetch = function(url, opt) {
     const s = (typeof url === 'string' ? url : url.url) || '';
     log.push({url: s.slice(0, 200),
               has_a_bogus: /a_bogus=/.test(s),
               has_msToken: /msToken=/.test(s),
               has_X_Bogus: /X-Bogus=/.test(s),
               uifid: /uifid=/.test(s)});
     return _fetch.apply(this, arguments);
   };
   ```
2. 在页面上手动搜"编程"、点视频、看评论，触发目标接口。
3. `filter(/general\/search|aweme\/detail|comment\/list/)` 过滤日志，得到最小样本。
4. 用同一钩子检查 `window.bdms.init._v` 是否还存在（结论：已 undefined）。

### 阶段 B · 影响面分析（GitNexus）

对每个待改的符号跑 `impact({target, direction: "upstream"})`，确认：

- 没有跨平台调用者（例如 `get_a_bogus` 只被 douyin 模块用）
- `__process_req_params` 虽是 CRITICAL 集中点，但**下游全在 Douyin 模块内**
- 用 `search_files` 补充对 GitNexus 图漏抓 async 调用的情况（例如 `client.py:108,120,121` 实际有调用，但 GitNexus 展开为 0 直接调用者）

### 阶段 C · 分支切出

```bash
git switch -c feat/remove-abogus
```

保留 main 稳定，随时可回滚。

### 阶段 D · 代码改造（commit `567b6be`）

改动集中在两个文件、55 行增 / 30 行减。

#### D.1 `client.py::DouYinClient.__init__` — 新增稳定 webid

真实浏览器中 `webid` 在会话内保持稳定。原实现每次请求随机（`get_web_id()`），是强烈的 bot 特征。改为一次生成、贯穿整个爬虫实例：

```python
# 会话生命周期稳定 webid
self._stable_webid = self._gen_stable_webid()

@staticmethod
def _gen_stable_webid() -> str:
    import random as _r
    return str(_r.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999))
```

#### D.2 `client.py::__process_req_params` — 主战场

**删除项**：
- `a_bogus` 拼装（`get_a_bogus(uri, query_string, post_data, ...)`）
- `msToken` URL 参数（`local_storage.get("xmst")`）
- 硬编码假指纹（`MacIntel / Chrome 125.0.0.0 / 2560x1440`）
- `effective_type / round_trip_time` 等已废弃字段
- `get_web_id()` 每次随机调用

**新增/改动**：
- `uifid` 参数：从 cookie 读 `UIFID / UIFID_TEMP`
- `webid` 参数：改用 `self._stable_webid`
- 浏览器指纹（`browser_platform / browser_name / browser_version / engine_name / engine_version / os_name / os_version / cpu_core_num / device_memory / screen_width / screen_height`）**全部改为 `page.evaluate()` 从真实浏览器动态读取**
- 新增真实浏览器都携带的参数：`version_name / pc_libra_divert / support_h265 / support_dash`

#### D.3 `client.py::pong` + `login.py::check_login_state` — 登录判活

抖音已废弃 `LOGIN_STATUS` cookie，改用 `login_time`（登录时间戳）标记。两处均添加兜底：

```python
return bool(cookie_dict.get("login_time")) or cookie_dict.get("LOGIN_STATUS") == "1"
```

### 阶段 E · 静态验证（ad-hoc）

在 `~/AppData/Local/Temp/hermes-verify-*.py` 写临时脚本，用 AST 精确校验：

| 检查项 | 方法 | 结果 |
|---|---|---|
| 语法解析 | `ast.parse()` | ✅ |
| import 成功 | `import media_platform.douyin.client` | ✅ |
| `DouYinClient()` 构造 | mock playwright_page + cookie_dict | ✅ |
| `_stable_webid` 是 19 位数字 | 正则 | ✅ |
| 同一实例的 `_stable_webid` 稳定 | 二次访问对比 | ✅ |
| `__process_req_params` 无 `get_a_bogus` 调用 | AST 遍历 `ast.Call` 节点 | ✅ |
| `__process_req_params` 无 `get_web_id` 调用 | AST 遍历 | ✅ |
| `__process_req_params` 无 `"msToken"` 字符串常量 | AST 遍历 `ast.Constant` | ✅ |
| `__process_req_params` 无 `"MacIntel" / "10.15.7"` | AST 遍历 | ✅ |
| `__process_req_params` 有 `"uifid"` 字符串 | AST 遍历 | ✅ |
| `__process_req_params` 使用 `page.evaluate()` | AST 遍历 | ✅ |
| `__process_req_params` webid 引用 `self._stable_webid` | 源码 substring | ✅ |
| `pong / check_login_state` 有 `login_time` 兜底 | substring | ✅ |

**17/17 通过**（注意：仅静态验证，不代表端到端可用）。

### 阶段 F · CDP 端到端真机验证

**F.1 配置**：把 `config/base_config.py::CUSTOM_BROWSER_PATH` 指向本地 Chrome：

```python
CUSTOM_BROWSER_PATH = r"C:\Symbols\Chrome\App\Chrome-bin\chrome.exe"
```

**F.2 启动 CDP Chrome**（隔离 profile，不动日常 Chrome）：

```bash
"C:/Symbols/Chrome/App/Chrome-bin/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="C:/Users/jinnn/.cdp-chrome-profile" \
  --no-first-run --no-default-browser-check \
  "https://www.douyin.com/"
```

**F.3 用户手动扫码登录**（一次即可，后续 profile 复用）。

**F.4 跑爬虫**：

```bash
uv run python main.py --platform dy --lt cookie --type search --keywords "编程"
```

**F.5 结果**：

| 阶段 | 结果 |
|---|---|
| CDP 连接 | ✅ `Successfully connected to existing browser` |
| Stealth 注入 | ✅ `libs/stealth.min.js` |
| 登录判活 | ✅ 走 `HasUserLogin` 路径通过 |
| **搜索接口调用** | ✅ `/aweme/v1/web/general/search/single/` 返回 200 |
| **数据入库** | ✅ 14 条 `aweme` 存入 `data/douyin/jsonl/search_contents_2026-07-06.jsonl` |
| **字段完整性** | ✅ 标题 / 作者 / 点赞 / 评论数 / 视频 URL / 封面 URL / 下载 URL 均可用 |
| 评论接口 | ⚠️ 14/14 全部 `account blocked`（`response.text` 为空）— **独立遗留问题** |

**F.6 页面视觉状态验证**（`Page.captureScreenshot` via CDP 底层）：

- 抖音"精选"页正常渲染，视频瀑布流正常
- 用户已登录状态保持（右上角头像、消息 60+ 未读）
- **无验证码、无滑块、无 blocked 弹窗、无红字异常**
- 唯一提示：中间小灰字"服务超时，重新拉取数据"（评论接口失败副作用）

### 阶段 G · Checkpoint 提交

```
01fb6e6  config(douyin): 添加 CUSTOM_BROWSER_PATH 指向本地 Chrome 用于 CDP 接管
567b6be  feat(douyin): 适配 2026 抖音风控新规——移除 a_bogus/msToken 拼装，切到 uifid+稳定 webid
```

---

## Part 3 · 交付物

### 3.1 抓到的真数据（14 条）

保存在：`data/douyin/jsonl/search_contents_2026-07-06.jsonl`

| # | 点赞 | 评论 | 作者 | 标题（截断） |
|---|---|---|---|---|
| 1 | 741,437 | 3,482 | 木***码 | Claude Code 零基础终极教程！ |
| 2 | 1,785,021 | 104,171 | 北***学 | 《实用Python程序设计》（一）主讲人：北京大学 郭炜 |
| 3 | 87,277 | 1,614 | 英***来 | 古法程序员的癫疯时刻…… |
| 4 | 114,041 | 1,411 | 大***大 | Vibe Coding教程：对AI说话就做出软件 |
| 5 | 4,787 | 124 | 林***呀 | Python爬虫50分钟快速入门 动画教学【2026版】 |
| 6 | 54,781 | 1,296 | P***💕 | 0基础学习Python，一条龙全套保姆级详细教程 |
| 7 | 3,773 | 102 | G***宝 | 一口气学会AI编程！3个月10万字超详细教程 |
| 8 | 1,021 | 82 | B***s | 听我一句劝，2026 年千万别再傻傻去学写代码 |
| 9 | 408 | 37 | 阿***员 | 在这个浮躁的AI时代，我们该做什么？ |
| 10 | 27,259 | 748 | 英***程 | 投产比远超KMP！ #数据结构和算法 |
| 11 | 59,638 | 1,056 | 技***虾 | AI编程新王Codex详细攻略 |
| 12 | 5,045 | 1,132 | 织***码 | 编程简史：仓颉-最爱Shift键的语言 |
| 13 | 1,800 | 20 | 0***球 | 代码能力差怎么练，哪里找项目做？ |
| 14 | 20,857 | 921 | 银***齐 | 普通人，自学编程，5个必备步骤 |

### 3.2 代码改动

- `feat/remove-abogus` 分支两个 commit（`567b6be`, `01fb6e6`）
- 3 files, 56 insertions(+), 31 deletions(-)

### 3.3 保留但未清理（技术债，等下阶段）

- `libs/douyin.js`（435 行 a_bogus JS 算法）— 保留作存档，未来若抖音风控回滚可秒恢复
- `media_platform/douyin/help.py` 的 `get_a_bogus / get_web_id / douyin_sign_obj` 三兄弟
- `pyproject.toml` 的 `pyexecjs` 依赖

---

## Part 4 · 遗留问题 & 下阶段

### 4.1 评论接口 blocked

**现象**：14/14 视频 `get_comments` 全部返回空 body，触发 `client.py:154` 的 `account blocked` 兜底逻辑。

**猜测原因（未验证）**：
1. `/aweme/v1/web/comment/list/` 有独立于搜索的风控策略（比搜索严）
2. 缺 Referer 头（爬虫的 `playwright_page` 停在 `/jingxuan`，不在视频详情页）
3. 需要一个额外 header（`Bogus-Sign / X-Rpc-Session-ID` 之类），本次抓包没触发到评论请求所以不知道

**下阶段计划**：在 Chrome 里手动打开一个视频，滚动加载评论，抓真实评论请求的完整 headers/params，对比爬虫的失败请求找差异。

### 4.2 CDP 多 tab 问题

爬虫启动时会新开一个抖音 tab（判断"已有 tab"的时机不够精细），跑完不关。属于 `tools/cdp_browser.py::CDPBrowserManager` 的小 bug，不影响功能但会累积僵尸 tab。

### 4.3 死代码清理

搜索接口验证通过后，`libs/douyin.js` + `help.py` 里 a_bogus 相关函数 + `pyexecjs` 依赖已确认无用。等评论接口也验证不需要 a_bogus 后再统一 PR 清理。

### 4.4 与上游同步策略

- `origin/main`（jinnnyang/MediaCrawler）：日常开发
- `upstream/main`（NanmiCoder/MediaCrawler）：定期 rebase 拉取新特性
- 本次 `feat/remove-abogus` 是纯本地 fork 特性，**不建议 PR 到 upstream**（上游未必接受删签名的方向）

---

## Part 5 · 关键教训

1. **风控迁移会整体换代**：a_bogus → uifid 不是"多一个参数"，是"整套签名体系被替换"。改造要连带浏览器指纹 + cookie + 登录判活一起动，光删 a_bogus 不够。

2. **稳定性比随机性更重要**：`webid` 每次随机是强 bot 特征。真实浏览器一辈子只有一个 webid，爬虫也要模拟这个稳定性。

3. **动态读浏览器指纹比硬编码强**：`browser_version=125.0.0.0` 硬编码 + Firefox 实际版本 152.0，抖音一秒识破。改成 `page.evaluate("navigator.userAgent")` 自动跟随实际浏览器。

4. **GitNexus 影响面分析救了我们**：在动 `__process_req_params`（CRITICAL 级）之前先确认它的 `affected_modules` 只在 Douyin 内，避免了意外污染 xhs / bilibili / weibo 等其他平台。

5. **不要过早清理死代码**：`libs/douyin.js` 435 行 JS 保留为存档，若抖音风控回滚（历史上抖音改过几次策略）可以秒恢复。清理留到"多个接口都验证通过 + 观察一段时间"之后。

6. **CDP 接管 + 隔离 profile 是最佳组合**：不动用户日常浏览器，独立 `--user-data-dir` 存爬虫专用登录态；日后每次跑不用重新扫码。

---

## Part 6 · 后续 · 评论接口 blocked 修复（2026-07-09）

### 6.1 症状

搜索接口 14 条视频落盘正常，但评论 `/aweme/v1/web/comment/list/` 全部 blocked：

```
2026-07-09 11:43:06 ERROR - [DouYinCrawler.get_comments] aweme_id: 7645206239718247714 get comments failed, error: account blocked
2026-07-09 11:43:06 ERROR - request params incrr, response.text:
```

`response.text == ""` 触发爬虫的兜底 blocked 分支。抖音的实际响应：`HTTP 200` + `content-length: 0` + `bd-ticket-guard-result: 1101`。

### 6.2 抓包 diff（不盲改）

写了两个 CDP 探针**对比真实浏览器 vs 爬虫**（保留在 `scripts/probe_comment_*.py` 作诊断工具）：

- `probe_comment_request.py` — JS 层 hook `window.fetch` / `XHR`，看 fetch init 视角
- `probe_comment_full_headers.py` — CDP `Network.requestWillBeSentExtraInfo`，看 Chrome 发到线上的**完整头**

发现两处差异：

**差异 1：8 个 URL 参数缺失**（评论接口敏感，搜索接口不校验）
```
cut_version, pc_img_format, effective_type, downlink, round_trip_time,
insert_ids, whale_cut_token, rcFT
```

**差异 2：5 个 `bd-ticket-guard-*` 请求头缺失**（根因）
```
bd-ticket-guard-client-data: <base64 P-256 签名>
bd-ticket-guard-ree-public-key: <ECDH 公钥>
bd-ticket-guard-version: 2
bd-ticket-guard-web-sign-type: 1
bd-ticket-guard-web-version: 2
```

解码 `bd-ticket-guard-client-data`：
```json
{
  "ts_sign": "ts.2.34a96f1b50ffec43...",
  "req_content": "ticket,path,timestamp",
  "req_sign": "RLN8Hdf7fGtcNkv/Qx3T81NznM3huJ5fJTRTEIlYGLk=",
  "timestamp": 1783568805
}
```

这是抖音的 **bd-ticket-guard（bdtg）** 机制 —— 浏览器用**本地生成的 P-256 私钥**对 `ticket + path + timestamp` 签名，服务端用之前 handshake 得到的 `ree-public-key` 验证。私钥在浏览器 IndexedDB 里，签名逻辑封装在抖音 SDK 内。

### 6.3 修复方案对比

| 方案 | 说明 | 工作量 | 稳定性 |
|---|---|---|---|
| A | **让 Chrome 帮我们签名** — 用 playwright 页面的 `fetch()` 走浏览器通道 | ⭐ 低 | ⭐⭐⭐ 高 |
| B | 抓一批真实签名 rotate | ⭐ 极低 | ❌ 差（timestamp 过期就废）|
| C | Python 复现 P-256 签名算法 | ⭐⭐⭐⭐⭐ 极高 | ⭐⭐ 中（抖音会改）|

**选 A** —— 既然爬虫已经在 CDP 接管的 Chrome 里跑 playwright，直接让 Chrome 发请求。

### 6.4 实现

新增 `media_platform/douyin/client.py::_browser_fetch_json()` 方法：

```python
async def _browser_fetch_json(self, method, url, params=None, headers=None):
    """委托给 playwright 页面的 fetch API，Chrome 自动加 bd-ticket-guard-* 头。"""
    # 转成同源相对路径，合并 params 到 querystring
    # 只保留少量业务头（uifid, accept），Cookie/UA/Referer 让 Chrome 自己填
    result = await self.playwright_page.evaluate("""
        async (args) => {
            const resp = await fetch(args.url, {
                method: args.method,
                headers: args.headers,
                credentials: 'include'
            });
            return { status: resp.status, text: await resp.text() };
        }
    """, {"url": rel, "method": method, "headers": browser_headers})
    return json.loads(result["text"])
```

在 `request()` 里检测 `/comment/list` → 走这条通道；其他接口仍走 httpx。

同时**保留** URL 参数补齐（8 个新参数 + `headers["uifid"]`）—— 服务端会同时校验多层，冗余点安全。

### 6.5 端到端验证

```
2026-07-09 11:55:22 [DouYinCrawler.start] Douyin Crawler finished
data/douyin/jsonl/search_contents_2026-07-09.jsonl: 14 行 (视频)
data/douyin/jsonl/search_comments_2026-07-09.jsonl: 135 行 (评论)
```

零 blocked，14 个视频全部拿到评论。

### 6.6 Part 5 教训的补充

7. **抖音的风控是分层的**：搜索接口只查 uifid，评论接口加了一层 bd-ticket-guard 客户端签名。**不能"改一个接口，全平台通"**，每类接口都要抓包 diff。

8. **抓包 diff 优先于盲改**：如果一开始直接猜是 Referer / URL 编码问题去改，会浪费半天。用 CDP 网络域抓包 → diff → 定位，30 分钟拿到根因。

9. **"让浏览器自己签名"是逆向的通用捷径**：任何走 CDP + playwright 的爬虫，遇到复杂签名（P-256、WBI、msToken...），先想能不能把 fetch 委托给浏览器上下文，往往能省下 10 倍工作量。

10. **`response.text == ""` 不一定是账号 blocked**：可能是签名校验失败、bd-ticket-guard-result 报错、频控（都会静默返回空 body）。需要抓响应头才能区分。

---

**End of Walkthrough.**
