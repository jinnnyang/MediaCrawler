# PRD-001 · MediaCrawler 断点续传 & 文件系统状态管理

> **状态**：Draft v0.2（已评审，待实现）
> **作者**：hermes-agent 协助
> **创建日期**：2026-07-09
> **最后更新**：2026-07-09（v0.2：吸收评审意见 Q1-Q8）
> **依赖任务**：无（本 PRD 是 PRD-002 的前置）
> **关联任务**：PRD-002 单博主视频转文字流水线

## Changelog

- **v0.2**（2026-07-09）：
  - Q1: 引入 `.done` sidecar + sanity check 区分"完成"与"坏产物"
  - Q2: 移除 `Stage.dep`，统一 `fn(item_id, workdir) -> Path` 签名，依赖关系由 stage 自己读
  - Q3: 统一 failed/ 语义（默认跳过，`--retry-failed` 显式开启，历史归档到 `failed/.attempt-N/`）
  - Q4: 拆分 `run.log`（结构化 KV）vs stdout（人读进度）
  - Q5: 统一 status 入口为 `python -m scripts.mc_status`
  - Q6: 新增 §4.7 单进程锁（`filelock`）
  - Q7: 工作时间窗口 `--work-hours` 从 Non-Blocking 提升为 MVP，默认 `09:00-24:00`
  - Q8: 测试策略加"随机 tick 点 SIGTERM 模糊测试"
  - 新增 §4.8 manifest schema version 字段
  - §8 加入现有 `data/xxx/jsonl/` 存量迁移说明
- **v0.1**（2026-07-09）：初稿

---

## 1. 背景与动机

### 1.1 现状痛点

MediaCrawler 现有的爬取流程是**"跑完写盘、中断即丢"**：

- `main.py --platform dy --type search` 跑到一半被 blocked / 网络断 / Ctrl-C，**已抓的部分不会持久化**；下次启动只能整个重跑。
- 评论采集虽然逐条落 jsonl，但**没有"哪些 aweme_id 已经跑过"的记录** —— 重跑时又要把同一批视频重新过一遍风控。
- 长任务（比如 200+ 视频、每个还要下载音频 + STT）中断概率**接近 100%**，而 STT 是有成本的（时间 or 钱），最不希望重跑。

### 1.2 为什么现在做

前置任务已完成：`_browser_fetch_json` 修复了评论 blocked，说明后续要跑**大批量 + 多阶段**的任务（PRD-002 的 204 条视频转文字流水线）就是硬需求。断点续传是 PRD-002 上线的前置条件，先做好这一层地基。

### 1.3 非目标（Non-Goals）

- **不引入外部状态存储**（SQLite / Redis / 消息队列）—— 增加运维复杂度，且 MediaCrawler 是本地单机爬虫，用不上。
- **不覆盖已有的 `data/xxx/jsonl/*.jsonl` 写入逻辑** —— 那是最终产物；断点续传管的是"过程状态"，两层解耦。
- **不做分布式并发**（多进程 / 多机器） —— 当前场景单机足够。

---

## 2. 设计哲学：文件系统即状态

**核心理念**：**每个业务阶段的产物文件存在 = 该阶段已完成**。

对比方案：

| 方案 | 优点 | 缺点 |
|---|---|---|
| SQLite 状态表 | 结构化查询、支持复杂过滤 | 引入依赖、需要 schema 迁移、调试要 SQL |
| status.json 单文件 | 简单 | 并发写有 race、大 dict 反序列化开销 |
| **文件系统（选）** | 零依赖、`ls` 即可查进度、天然并发安全（不同 id 不同文件） | 大规模（>10 万条）时 `ls` 慢 |

MediaCrawler 场景（单任务通常 < 1000 条）完全适用文件系统方案。

---

## 3. 目录布局规范

### 3.1 通用模板

```
data/<platform>/<task_type>/<task_id>/
├── manifest.jsonl               # 任务清单：本任务要处理的所有条目（一次生成，之后只读）
├── manifest.cursor.json         # 清单爬取的翻页 checkpoint
├── <stage_1>/{item_id}.<ext>    # 阶段 1 产物（文件存在 = 阶段 1 完成）
├── <stage_2>/{item_id}.<ext>    # 阶段 2 产物
├── ...
├── failed/{item_id}.json        # 失败记录（错误 stack + 阶段 + 时间戳）
└── run.log                      # 运行日志（每次 append）
```

**示例**（PRD-002 的目录）：

```
data/dyv/user_transcribe/MS4wLj...IlIos/
├── manifest.jsonl               # 204 条视频清单
├── manifest.cursor.json
├── video/{aweme_id}.mp4         # 阶段 1：下载
├── audio/{aweme_id}.mp3         # 阶段 2：ffmpeg 提音频
├── transcript/{aweme_id}.txt    # 阶段 3：STT 转文字
├── md/{aweme_id}.md             # 阶段 4：md 组装
├── failed/{aweme_id}.json
└── run.log
```

### 3.2 命名约定

- `<platform>`：`dyv` (抖音视频) / `xhs` / `bili` / ...
- `<task_type>`：`user_transcribe` / `search_comments` / ...
- `<task_id>`：语义清晰的短标识（sec_uid、keyword_hash、UUID）
- `<item_id>`：平台内的稳定 ID（抖音 `aweme_id`）

---

## 4. 关键机制

### 4.1 幂等步骤：先看再做（Q1 修订）

#### 4.1.1 `.done` sidecar：区分"完成"与"坏产物"

**问题**：只靠产物文件存在判断"完成"，无法识别以下情况：
- STT API 静默返回空字符串 → `transcript/{id}.txt` 存在但内容 0 字节
- 视频源被删，下载得到 200 响应但 body 是错误 HTML → `video/{id}.mp4` 存在但不是 mp4
- ffmpeg 提音频超时被 kill → `audio/{id}.mp3` 是半截文件（虽然有 tmp 保护，但依然可能是 ffmpeg 自己写出了坏 mp3）

**方案**：每个 stage 完成时**同时**写产物文件 + `.done` sidecar：

```
video/
├── 7658327349150174505.mp4
└── 7658327349150174505.mp4.done      ← sidecar，内含 sanity 结果
```

sidecar 内容（JSON）：
```json
{
  "stage": "video",
  "item_id": "7658327349150174505",
  "artifact": "video/7658327349150174505.mp4",
  "size_bytes": 8412563,
  "sha256": "3f2a...",
  "duration_ms": 3217,
  "sanity_check": "ok",
  "completed_at": "2026-07-09T14:15:22"
}
```

**判定规则**：
- **完成** = 产物文件存在 **且** sidecar 存在 **且** `sanity_check == "ok"`
- 缺 sidecar 或 sanity 失败 → 视为未完成，重新跑该 stage（旧产物会被 tmp+rename 原子覆盖）

#### 4.1.2 每 stage 的 sanity check 最小要求

框架强制每个 stage fn 返回前跑一个 sanity check，不通过就抛 `StageSanityError`（走 failed/ 分支）：

| Stage | 最低 sanity |
|---|---|
| `video` | `size > 100 KB` 且能被 `ffprobe` 读出 duration |
| `audio` | `size > 10 KB` 且 duration > 1s |
| `transcript` | `len(text.strip()) > 0` 且非纯标点 |
| `md` | 含 `##` 且长度 > transcript 长度的 80% |

具体阈值在各 stage 实现里定义，框架只强制"必须有 sanity_check 字段"。

#### 4.1.3 幂等 stage 函数模板

```python
def stage_download(item_id: str, workdir: Path) -> Path:
    """Idempotent: skip if .done sidecar present, else produce + verify + write sidecar."""
    out = workdir / "video" / f"{item_id}.mp4"
    done = out.with_suffix(out.suffix + ".done")
    if done.exists() and json.loads(done.read_text())["sanity_check"] == "ok":
        return out                                # ← 短路
    # ... 实际下载逻辑（原子写）...
    _atomic_stream_download(url, out)
    _verify_video_sanity(out)                     # 不通过则抛 StageSanityError
    _write_done_sidecar(out, stage="video", extra={"duration_ms": elapsed_ms})
    return out
```

#### 4.1.4 主循环

```python
for item_id in manifest:
    if _is_stage_done(workdir / "md", item_id):
        continue                                      # 最终产物已完成 → 整条跳过
    if _is_in_failed(item_id, workdir) and not retry_failed:
        continue                                      # Q3: 默认跳过 failed
    try:
        stage_download(item_id, workdir)              # Q2: 各 stage 自己读 workdir 拿输入
        stage_extract_audio(item_id, workdir)
        stage_stt(item_id, workdir)
        stage_render_md(item_id, workdir)
    except (StageSanityError, Exception) as e:
        _record_failure(item_id, e, workdir)
```

### 4.2 原子写入（防半截文件）

**问题**：如果在写 `video/{aweme_id}.mp4` 到一半时断电 / Ctrl-C，下次启动看到文件存在 → 误判为已完成 → 后续步骤读到坏数据。

**方案**：所有产物走 tmp + rename：

```python
def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    tmp.replace(dst)            # POSIX / Windows 都是原子 rename
```

**流下载场景**（大文件不适合一次读进内存）：

```python
async def _atomic_stream_download(url: str, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.stream("GET", url) as r:
        async with aiofiles.open(tmp, "wb") as f:
            async for chunk in r.aiter_bytes(1 << 15):
                await f.write(chunk)
    tmp.replace(dst)
```

**清理孤儿 .tmp**：任务启动时先扫一遍 `**/*.tmp`，删掉（上次跑到一半的残留）。

### 4.3 清单爬取的翻页 checkpoint

**场景**：204 条视频要分 ~10 页拿，每页拿完更新 cursor：

```json
// manifest.cursor.json
{
  "max_cursor": "1783568800000",
  "has_more": true,
  "fetched_count": 90,
  "last_fetched_at": "2026-07-09T14:12:33"
}
```

**恢复逻辑**：

```python
def fetch_manifest(workdir: Path, fetch_page_fn):
    cursor_file = workdir / "manifest.cursor.json"
    manifest_file = workdir / "manifest.jsonl"

    state = _read_json_or(cursor_file, {"max_cursor": 0, "has_more": True, "fetched_count": 0})
    if not state["has_more"]:
        return manifest_file              # 全量已拉完

    seen = _read_manifest_ids(manifest_file)
    while state["has_more"]:
        page, next_cursor = fetch_page_fn(state["max_cursor"])
        new_items = [it for it in page if it["aweme_id"] not in seen]
        _append_jsonl(manifest_file, new_items)
        seen.update(it["aweme_id"] for it in new_items)
        state["max_cursor"] = next_cursor
        state["has_more"] = next_cursor is not None
        state["fetched_count"] = len(seen)
        _atomic_write_json(cursor_file, state)
    return manifest_file
```

**关键点**：先 append manifest.jsonl，再 write cursor.json。即使 append 后崩溃，下次跑靠 `seen` 集合去重也能正确恢复。

### 4.4 失败隔离（Q3 修订）

**每个 item 的失败不阻塞其他 item**：

```python
def _record_failure(item_id: str, err: Exception, workdir: Path) -> None:
    fail = workdir / "failed" / f"{item_id}.json"
    fail.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(fail, {
        "item_id": item_id,
        "error": str(err),
        "error_type": type(err).__name__,
        "traceback": traceback.format_exc(),
        "failed_at": datetime.now().isoformat(),
        "stage": _current_stage_hint(),           # 从调用栈或显式传入
        "attempt": _count_attempts(item_id, workdir) + 1,
    })
```

#### 4.4.1 语义（唯一权威版）

- **默认行为**：主循环遇到 `failed/{id}.json` 就 **skip 该 item**（不消耗 STT 额度）。
- **`--retry-failed`**：显式要求重试 failed/ 里的 item。重试前先把当前 `failed/{id}.json` 归档到 `failed/.attempt-<N>/{id}.json`，然后清空当前记录，走正常流程。
- **`--max-retries N`**（默认 `3`）：单个 item 累计失败 N 次后彻底放弃，即使 `--retry-failed` 也跳过。用户想强制再试就手动 `rm -rf failed/`。
- **`--force-retry`**：忽略 `--max-retries` 上限，把所有 failed/ 归档后从零开始（谨慎用）。

#### 4.4.2 归档目录结构

```
failed/
├── 7645206239718247714.json          # 当前失败记录
├── 7472744403937856806.json
└── .attempt-1/                        # 第 1 次重试前归档的旧失败
    ├── 7645206239718247714.json
    └── ...
```

**决策依据**：数据不可采集的 item（视频被删、账号封禁）无脑重试会浪费 STT 额度。默认跳过 + 显式重试是安全默认值。

### 4.5 分批与限流（Q7 修订：新增 work-hours 到 MVP）

- **批大小**由 CLI `--batch-size N` 控制（PRD-002 默认 10）
- **批内串行**（避免同时下载 10 个 mp4 打爆网络 / 触发风控）
- **批间 sleep**：由 `--pace-seconds P`（默认 `5.0`）单参数驱动，详见 §10.1
- **工作时间窗口**（MVP）：`--work-hours "09:00-24:00"`（默认，可用 `--work-hours ""` 关闭）
  - 主循环每次进入下一批前检查当前时间
  - 越过窗口右边界 → 计算距下一个左边界的秒数，sleep 到早晨自动继续
  - 用 `run.log` 记录 `stage=sleep kind=work_hours until=2026-07-10T09:00:00Z`
  - **不打断正在跑的 item**：只在批与批之间检查，避免半途中断留 tmp
  - 决策依据：抖音风控对夜间自动化的敏感度高，PRD-002 200+ 条必然过夜，硬编码窗口比让用户凌晨 3 点被封更好
- **进度打印**：见 §4.6.2

### 4.6 日志（Q4 修订：结构化 vs 人读拆分）

拆两个通道，各司其职：

#### 4.6.1 `run.log`（结构化，机器解析）

- `append` 模式，每次运行的行都带 ISO 时间戳
- **单行 KV 格式**（logfmt 风格），`--status` 命令和后续分析工具直接解析
- 示例：
  ```
  2026-07-09T14:15:22Z stage=download item=7658327349150174505 status=ok elapsed_ms=3217 size_bytes=8412563
  2026-07-09T14:15:26Z stage=stt      item=7658327349150174505 status=fail err_type=RateLimitError err="API rate limit"
  2026-07-09T14:15:28Z stage=sleep    kind=batch mean_s=5.0 actual_s=4.72
  2026-07-09T14:15:33Z stage=sleep    kind=long_pause actual_s=32.1 triggered_at_batch=7
  ```

#### 4.6.2 stdout（人读，进度条）

- 单行覆盖式进度条（用 `rich.progress` 或简单 `\r`）
- 每完成一条打印一行历史（`[87/204] ✓ 7658... "视频标题..." 3.2s`）
- 示例：
  ```
  [ 87/204] ✓ 7658327349150174505  "刀郎新歌翻唱..."  3.2s
  [ 88/204] ✗ 7645206239718247714  stage=stt  "audio too short"
  [ 89/204] ⋯ downloading...  ▓▓▓▓▓░░░░░  52%
  ```

**实现要点**：logger 写 `run.log`，stdout 用另一个 handler（`rich.console`），不互相污染。

### 4.7 单进程锁(Q6 新增)

**场景**：用户不小心在两个终端同时跑 `--task-id abc`，两个进程都往同一个 workdir 写 → manifest.jsonl append 交错、`.done` sidecar race、failed/ 记录互相覆盖。

**方案**：workdir 内放 `.lock` 文件，用 `filelock` 库跨平台锁：

```python
from filelock import FileLock, Timeout

lock_path = workdir / ".lock"
lock = FileLock(str(lock_path), timeout=0)      # 立刻失败，不等待
try:
    with lock:
        lock_path.write_text(json.dumps({
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(),
            "host": socket.gethostname(),
        }))
        run_pipeline(...)
finally:
    lock_path.unlink(missing_ok=True)
```

**用户可见错误**：
```
❌ Another process is already running in this workdir:
   pid: 43217
   started_at: 2026-07-09T14:12:33
   host: DESKTOP-XYZ

If that process is dead, remove the lock manually:
   rm data/dyv/user_transcribe/MS4w.../.lock
```

**依赖新增**：`filelock ~= 3.13`（纯 Python，跨平台，600 行代码，无外部依赖）。

### 4.8 Manifest schema version(新增)

`manifest.jsonl` 每行都带 `_schema_version` 字段，以后升级流水线定义可以判断旧 workdir 要不要迁移：

```json
{"_schema_version": 1, "aweme_id": "7658...", "desc": "...", "create_time": 1730000000, ...}
```

**当前版本**：`1`

**升级策略**：
- 加字段（向前兼容）→ version 不变
- 改字段名 / 语义 → version +1，加迁移函数 `migrate_v1_to_v2(item: dict) -> dict`
- 启动时读 manifest 第一行的 version，若小于当前版本，走 in-place 迁移（先备份 `manifest.jsonl.v1.bak`）

---

## 5. 用户接口

### 5.1 CLI 约定（Q3 + Q7 修订）

假设通用调用形式：

```bash
python -m scripts.<task_module> \
    --task-id <task_id> \
    --resume \                        # 默认开启：跳过已完成的 item
    --retry-failed \                  # Q3: 显式重试 failed/ 里的（旧记录归档到 .attempt-N/）
    --max-retries 3 \                 # Q3: 单个 item 最多重试次数（默认 3）
    --force-retry \                   # Q3: 忽略 --max-retries 上限（谨慎）
    --batch-size 10 \
    --pace-seconds 5.0 \              # §10.1: 单旋钮驱动所有 sleep 参数
    --work-hours "09:00-24:00" \      # Q7: 默认开启工作时间窗口，"" 关闭
    --dry-run                         # 只打印会做什么，不执行
```

### 5.2 状态查询命令（Q5：统一入口）

任务运行中或跑完后，用户想看进度 —— **唯一入口**：

```bash
python -m scripts.mc_status --workdir data/dyv/user_transcribe/MS4w.../
```

（`scripts/mc_status.py` 是 `scripts/common/resume/status.py` 的薄 CLI wrapper）

输出：

```
Manifest: 204 items
├─ video/     : 187 (91.7%)
├─ audio/     : 185 (90.7%)
├─ transcript/: 143 (70.1%)
├─ md/        : 143 (70.1%)
└─ failed/    : 3
    - 7645206239718247714 (stage=stt, err="audio too short")
    - 7472744403937856806 (stage=download, err="video removed")
    - 7571394970960104731 (stage=stt, err="API timeout")
```

---

## 6. 可复用组件设计

抽象成 `scripts/common/resume/` 通用工具，PRD-002 和以后所有类似任务复用：

```
scripts/common/resume/
├── __init__.py
├── atomic.py        # _atomic_write_bytes / _atomic_write_json / _atomic_stream_download
├── sidecar.py       # .done sidecar 读写 + sanity check 抽象（Q1 新增）
├── manifest.py      # fetch_manifest_with_cursor(...) + schema version 迁移（§4.8）
├── pipeline.py      # run_pipeline(manifest, stages, workdir, ...)  ← 主循环
├── failure.py       # _record_failure / _read_failed_ids / _archive_failed_for_retry
├── lock.py          # single-process filelock wrapper（Q6 新增）
├── pacing.py        # sleep_jitter / long_pause / work_hours 约束（§10.1）
├── status.py        # count_stage_artifacts / print_progress
└── tests/
    ├── test_atomic.py
    ├── test_sidecar.py
    ├── test_manifest.py
    ├── test_lock.py
    ├── test_pacing.py
    └── test_pipeline.py     # 用 tmpdir 模拟中断和恢复
```

### 6.1 `pipeline.run_pipeline()` 签名（Q2 修订）

```python
def run_pipeline(
    workdir: Path,
    manifest_path: Path,
    stages: list[Stage],           # [Stage("video", download_fn, "mp4"), Stage("audio", extract_fn, "mp3"), ...]
    batch_size: int = 10,
    pace_seconds: float = 5.0,     # §10.1 单旋钮
    work_hours: str = "09:00-24:00",  # Q7 MVP
    retry_failed: bool = False,
    max_retries: int = 3,
) -> RunSummary:
    """
    Runs all items in manifest through the given stages.
    Each stage is idempotent (skip if .done sidecar present with sanity_check==ok).
    Each item's failure is isolated to failed/{item_id}.json (see §4.4).
    Runs under a single-process lock (see §4.7).
    """
```

`Stage` 数据类（**Q2**：删掉 `dep` 字段。每个 stage 函数自己从 workdir 读需要的输入 —— 上一 stage 的产物、manifest.jsonl 里的元数据等。框架只管调度，不做依赖推断）：

```python
@dataclass
class Stage:
    name: str                                 # "video" / "audio" / "transcript" / "md"
    fn: Callable[[str, Path], Path]           # (item_id, workdir) -> output_path
    ext: str                                  # "mp4" / "mp3" / "txt" / "md"
    # ← 移除 dep 字段（v0.2）
    #   原因：stage_render_md 需要 transcript 文本 + manifest 里的原始元数据（标题、发布时间），
    #        这种"多输入"依赖无法用单一字符串表达。让 stage 函数自己按约定读 workdir 更清晰。
```

**stage 函数如何读输入**（约定俗成，不靠框架强制）：

```python
def stage_render_md(item_id: str, workdir: Path) -> Path:
    # 读上一 stage 产物
    transcript = (workdir / "transcript" / f"{item_id}.txt").read_text()
    # 读 manifest 里的原始元数据
    meta = _read_manifest_entry(workdir / "manifest.jsonl", item_id)
    # 组装
    md = _render_template(meta["desc"], meta["create_time"], transcript)
    _atomic_write_text(workdir / "md" / f"{item_id}.md", md)
    _verify_md_sanity(...)
    _write_done_sidecar(...)
    return workdir / "md" / f"{item_id}.md"
```

---

## 7. 测试策略（Q8 修订）

### 7.1 单元测试

- `test_atomic`: 写入过程中模拟 `KeyboardInterrupt`，确认不留半截文件；单独跑 tmp 残留清理路径
- `test_sidecar`: sidecar 存在但 `sanity_check!=ok` → 视为未完成；缺 sidecar → 视为未完成
- `test_manifest`: 模拟"翻到第 5 页时挂掉"→ 重跑 → 确认继续从第 5 页；schema v1→v2 迁移路径
- `test_lock`: 同 workdir 起第二个进程 → 立即失败 + 打印首进程 pid；首进程崩溃留下 stale lock → 手动清理后能启
- `test_pacing`: `--pace-seconds 5` 跑 100 批，验证实际 sleep 分布 mean/std 与推导公式吻合；work-hours 越界 → sleep 到明早

### 7.2 集成测试（用 tmpdir + mock）

- Stage 抛异常 → 记录 failed/，其他 item 继续跑
- Stage 已有 `.done` sidecar → 短路跳过（**关键**：不调用 fn，用 mock 计数器验证）
- Stage 有产物但缺 sidecar → 重新调用 fn（覆盖旧产物）
- 全部完成 → 二次运行不重跑任何东西（关键：确保 resume 语义正确）
- `--retry-failed`：failed/{id}.json 存在 → 归档到 `.attempt-1/`，重跑 → 若失败则 attempt 计数 = 2

### 7.3 中断时机模糊测试（Q8 新增）

**核心目标**：证明"任何时机被 SIGTERM 打断，重跑都能自愈"。

```python
def test_interrupt_at_random_ticks():
    for seed in range(50):                # 50 次随机中断
        with tmpdir() as wd:
            _spawn_pipeline(wd)
            _wait_random_seconds(random.Random(seed).uniform(0.1, 5.0))
            _kill_with_sigterm()          # 用 os.kill(pid, SIGTERM)
            _restart_pipeline(wd)         # 重跑
            _assert_final_state_consistent(wd)
```

**特别测**：
- SIGTERM 在 tmp write 中间（rename 前）→ 下次跑 tmp 被清理，产物不存在，重跑
- SIGTERM 在 rename 后 sidecar 写之前 → 产物存在但缺 sidecar，下次跑重新执行 fn
- SIGTERM 在 sidecar 写到一半（tmp）→ sidecar 也走原子写，下次跑发现缺 sidecar 重跑
- SIGTERM 在 manifest append 中途 → jsonl 可能有半行，用 `try/except json.JSONDecodeError` 跳过坏行

### 7.4 手动 QA

- 用 PRD-002 前 10 条视频跑一遍，跑到一半 Ctrl-C，重跑，观察是否续上
- 手动删除某个 `md/xxx.md`（但不删 sidecar）→ 重跑应重新生成
- 手动删除某个 `md/xxx.md.done`（但不删产物）→ 重跑应重新生成
- 手动 corrupted `video/xxx.mp4`（写入乱码）→ sanity fail → 走 failed/ 分支
- 同 workdir 起两个进程 → 后启的立即报错退出

---

## 8. 交付物

1. `scripts/common/resume/` 目录 + 单元测试（含 `sidecar.py` / `lock.py` / `pacing.py`）
2. `scripts/mc_status.py` —— 全局状态查询入口
3. `docs/resume-usage.md` 使用文档（如何写一个新任务复用这套框架）
4. 一个最小 demo（`scripts/demo_resume/`）演示 3 阶段流水线，用于验证框架
5. **迁移说明**（新增）：现有 `data/{platform}/jsonl/*.jsonl` 存量是**最终产物**，不受本 PRD 影响：
   - 新框架产出的 workdir 是 `data/{platform}/{task_type}/{task_id}/`，与旧目录**独立并存**
   - 已跑过的老任务不用迁移，也不用回填 `.done` sidecar
   - 若希望把老任务纳入 resume 管理，需手动创建 workdir 并 touch 每个 `.done`（一次性脚本，非必须）
   - 老代码路径（`store/douyin/__init__.py` 等）保持原样，不动

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Windows `rename` 目标已存在会报错 | 原子写失败 | 用 `Path.replace()`（Python 层封装了 POSIX rename 语义） |
| 大文件流下载中途断，tmp 残留 | 磁盘占用 | 启动时扫 `**/*.tmp` 清理 |
| manifest 拉取本身失败 | 无法启动主流程 | manifest 阶段独立重试；`--force-refresh-manifest` 强制重拉 |
| 用户手动删了产物文件却没删对应的 failed/ | 会永远跳过该 item | `--retry-failed` 参数明确重试 failed 里的 |

---

## 10. 未来扩展（Non-Blocking）

### 10.1 反风控（Anti-detection）时序抖动

当前 `--batch-sleep S` 是**固定秒数**，行为特征明显：批与批的间隔恒定、单条内的 stage 切换零延迟。真实用户浏览的时序具有强随机性，需要用**正态分布抖动**打散节律。

**扩展点**：

1. **批间 sleep 用正态分布**（截断防负数）
   ```python
   def sleep_jitter(mean: float, std: float, min_s: float = 0.5, max_s: float | None = None):
       """N(mean, std) 采样，截断到 [min_s, max_s]。"""
       s = random.gauss(mean, std)
       s = max(min_s, s)
       if max_s is not None:
           s = min(max_s, s)
       time.sleep(s)
   ```
   CLI 参数替换：`--batch-sleep 5` → `--batch-sleep-mean 5 --batch-sleep-std 1.5`

2. **单条内 stage 间也 sleep**（不是所有 stage 切换都是"零延迟按 Enter 键"）
   ```
   download → sleep N(0.8, 0.3)s → extract_audio → sleep N(1.2, 0.4)s → stt → ...
   ```
   每个 stage 完成后由 `pipeline.py` 统一 hook 一段抖动。

3. **偶发长停顿**（模拟"用户去泡杯茶")
   - 5% 概率触发一次 `N(30, 8)s` 的长 sleep
   - 打破"每 5-8s 稳定发一次请求"的固定节律

4. **同 batch 内 item 顺序打乱**
   - 默认按 manifest 顺序（发布时间倒序）跑，规律性强
   - 加 `--shuffle-within-batch` 选项，把每批 10 条**随机重排**再跑
   - 不影响幂等性（每条完成后单独落盘）

5. **HTTP 请求间的短抖动**
   - 下载阶段每个 chunk 之间 / 每次 API 调用之间加 `N(0.15, 0.05)s`
   - 累计小抖动效果最好，且几乎不增加总耗时

6. **工作时间窗口约束** — **已提升为 MVP，见 §4.5**（v0.2 决策）

**默认关闭 vs 默认开启**：

- 建议**默认开启** batch-sleep 的正态分布（低成本，无副作用）
- stage 间 sleep、偶发长停顿、shuffle 用 CLI 开关默认关，需要激进反风控时打开
- 长时间窗口约束默认关，仅用户明确要求时启用

**单参数控制（推荐）**：

用户只需要拨一个旋钮 `--pace-seconds P`（批间平均等待秒数，默认 `5.0`），其他参数按**行为学比例**从 P 推导：

| 参数 | 推导公式 | P=5.0 时的值 |
|---|---|---|
| `batch_sleep_mean` | `P` | 5.0 |
| `batch_sleep_std` | `P × 0.30`（30% 变异系数，接近人类间隔分布） | 1.5 |
| `batch_sleep_min` | `P × 0.30`（截断防太快） | 1.5 |
| `batch_sleep_max` | `P × 3.0`（截断防离群等太久） | 15.0 |
| `stage_sleep_mean` | `P × 0.12`（stage 间是"点击级"停顿，比批间小一个量级） | 0.6 |
| `stage_sleep_std` | `stage_sleep_mean × 0.40` | 0.24 |
| `long_pause_mean` | `P × 6.0`（模拟"泡杯茶"约 30-60s） | 30.0 |
| `long_pause_std` | `long_pause_mean × 0.25` | 7.5 |
| `long_pause_probability` | `0.05`（每 20 个 batch 触发一次，独立于 P） | 0.05 |

**语义参考**：

- `P=2.0` — 激进（约 2 分钟一批，风险较高）
- `P=5.0` — **默认**（平衡，适合 200 条量级）
- `P=10.0` — 保守（约 10 分钟一批，长任务过夜跑）
- `P=30.0` — 隐身（每半小时一批，非常稳但慢）

**高级用户可显式覆盖**：任一参数支持 `--override-batch-sleep-std 2.0` 手工覆盖推导结果，仅在需要调优时用。

**统计验证**：跑完一批后 `run.log` 里输出实际 sleep 直方图，用户可以核对分布是否合理，不至于失手把 std 调成 0。

### 10.2 其他

- **Webhook / 进度回调**：跑完后调用一个 URL 通知 —— 有需要再加。
- **多任务队列 UI**：一个简单 Web 页面看所有 workdir 的进度 —— 需求驱动再加。

---

**End of PRD-001.**
