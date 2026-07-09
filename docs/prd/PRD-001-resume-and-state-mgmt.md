# PRD-001 · MediaCrawler 断点续传 & 文件系统状态管理

> **状态**：Draft
> **作者**：hermes-agent 协助
> **创建日期**：2026-07-09
> **依赖任务**：无（本 PRD 是 PRD-002 的前置）
> **关联任务**：PRD-002 单博主视频转文字流水线

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

### 4.1 幂等步骤：先看再做

每个阶段函数的**统一签名**：

```python
def stage_download(item_id: str, workdir: Path) -> Path:
    """Idempotent: if the artifact exists, return it; otherwise produce it."""
    out = workdir / "video" / f"{item_id}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out                    # ← 短路：已完成
    # ... 实际下载逻辑 ...
    _atomic_write(out, tmp_path)      # ← 原子替换
    return out
```

**主循环**只需串起来：

```python
for item_id in manifest:
    if (workdir / "md" / f"{item_id}.md").exists():
        continue                              # 最终产物已存在 → 整条跳过
    if item_id in failed_ids:
        continue                              # 已知失败（除非用户手动清 failed/）
    try:
        video = stage_download(item_id, workdir)
        audio = stage_extract_audio(video, workdir)
        text = stage_stt(audio, workdir)
        stage_render_md(item_id, text, workdir)
    except Exception as e:
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

### 4.4 失败隔离

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
        "stage": _current_stage_hint(),   # 从调用栈或显式传入
    })
```

**重试语义**：默认跳过已在 `failed/` 里的。用户想重试就 `rm data/.../failed/{aweme_id}.json`（或整个 `failed/` 一起清）。

### 4.5 分批与限流

- **批大小**由 CLI `--batch-size N` 控制（PRD-002 默认 10）
- **批内串行**（避免同时下载 10 个 mp4 打爆网络 / 触发风控）
- **批间 sleep**：CLI `--batch-sleep S`（默认 5s），批完了 sleep 再下一批
- **进度打印**：`[87/204] ✓ 7658327349150174505 "视频标题..."` 每条一行

### 4.6 日志

- **`run.log`**：`append` 模式，每次运行的行都带时间戳；便于 tail -f 看进度
- 结构化行示例：
  ```
  2026-07-09T14:15:22 stage=download item=7658... status=ok elapsed=3.2s size=8.4MB
  2026-07-09T14:15:26 stage=stt item=7658... status=fail err="API rate limit"
  ```

---

## 5. 用户接口

### 5.1 CLI 约定

假设通用调用形式：

```bash
python -m scripts.<task_module> \
    --task-id <task_id> \
    --resume \                    # 默认开启：跳过已完成
    --retry-failed \              # 显式重试 failed/ 里的
    --batch-size 10 \
    --batch-sleep 5 \
    --dry-run                     # 只打印会做什么，不执行
```

### 5.2 状态查询命令

任务运行中或跑完后，用户想看进度：

```bash
python -m scripts.status --workdir data/dyv/user_transcribe/MS4w.../
```

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
├── manifest.py      # fetch_manifest_with_cursor(...)
├── pipeline.py      # run_pipeline(manifest, stages, workdir, ...)  ← 主循环
├── failure.py       # _record_failure / _read_failed_ids
├── status.py        # count_stage_artifacts / print_progress
└── tests/
    ├── test_atomic.py
    ├── test_manifest.py
    └── test_pipeline.py     # 用 tmpdir 模拟中断和恢复
```

### 6.1 `pipeline.run_pipeline()` 签名

```python
def run_pipeline(
    workdir: Path,
    manifest_path: Path,
    stages: list[Stage],           # [Stage("video", download_fn, "mp4"), Stage("audio", extract_fn, "mp3"), ...]
    batch_size: int = 10,
    batch_sleep: float = 5.0,
    retry_failed: bool = False,
) -> RunSummary:
    """
    Runs all items in manifest through the given stages.
    Each stage is idempotent (skip if artifact exists).
    Each item's failure is isolated to failed/{item_id}.json.
    """
```

`Stage` 数据类：

```python
@dataclass
class Stage:
    name: str                                 # "video" / "audio" / ...
    fn: Callable[[str, Path], Path]           # (item_id, workdir) -> output_path
    ext: str                                  # "mp4" / "mp3" / ...
    dep: Optional[str] = None                 # 上一阶段名（决定输入路径）
```

---

## 7. 测试策略

### 7.1 单元测试

- `test_atomic`: 写入过程中模拟 `KeyboardInterrupt`，确认不留半截文件
- `test_manifest`: 模拟"翻到第 5 页时挂掉"→ 重跑 → 确认继续从第 5 页

### 7.2 集成测试（用 tmpdir + mock）

- Stage 抛异常 → 记录 failed/，其他 item 继续跑
- Stage 已有 artifact → 短路跳过（不调用 fn）
- 全部完成 → 二次运行不重跑任何东西（关键：确保 resume 语义正确）

### 7.3 手动 QA

- 用 PRD-002 前 10 条视频跑一遍，跑到一半 Ctrl-C，重跑，观察是否续上
- 手动删除某个 `md/xxx.md`，重跑，确认只补做那一条

---

## 8. 交付物

1. `scripts/common/resume/` 目录 + 单元测试
2. `docs/resume-usage.md` 使用文档（如何写一个新任务复用这套框架）
3. 一个最小 demo（`scripts/demo_resume/`）演示 3 阶段流水线，用于验证框架

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

6. **工作时间窗口约束**（可选，激进反风控）
   - `--work-hours "09:00-23:00"`：过点自动 sleep 到明早
   - 模拟真人不 24×7 刷抖音

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
