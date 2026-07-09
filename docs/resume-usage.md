# resume 使用手册

> `scripts/common/resume/` — 断点续传 + 反风控通用工具包
> 实现 PRD-001 v1.1 · 全部走 TDD · 115 tests green

## 1. 五分钟速览

```python
import asyncio
from pathlib import Path
from scripts.common.resume.pipeline import Stage, run_pipeline
from scripts.common.resume.sidecar import write_done_sidecar

def _prod(workdir, item): return workdir / "fetch" / f"{item['aweme_id']}.bin"

async def fetch(workdir, item, product):
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_bytes(b"...content...")
    # sidecar 可选：pipeline 会自动补一个默认 .done sidecar，
    # 如果你想加自定义 extra 就自己写。
    write_done_sidecar(product, stage="fetch", extra={"size": product.stat().st_size})

asyncio.run(run_pipeline(
    workdir=Path("./workdir"),
    items=[{"aweme_id": "111"}, {"aweme_id": "222"}],
    stages=[Stage(name="fetch", product_path=_prod, run=fetch)],
    pace_seconds=5.0,       # 单旋钮限流
))
```

跑一次 Ctrl+C 后直接**再跑一次**——已完成的 stage 自动跳过，未完成的从头。

## 2. 数据契约（在 workdir 里能看到什么）

```
<workdir>/
├── .lock.acquire            # filelock 排他文件（OS 级）
├── .lock                    # 当前 holder 元数据（pid + start_time + workdir）
├── manifest.jsonl           # 抓取到的 items（每行一个 JSON）
├── <stage>/                 # e.g. video/, audio/, md/
│   ├── item001.bin          # 产物
│   └── .item001.bin.done    # sidecar：sanity_check='ok' 时视为 stage 已完成
└── failed/
    ├── item002.json         # 当前失败记录
    └── .attempt-N/          # 历次归档，永不覆写
        └── item002.json
```

**核心不变式**（TDD 强制）：
- `.done` sidecar 里 `sanity_check == "ok"` **是** stage 已完成的唯一判据（Q_new_4）
- `sha256` 只归档、不参与 skip 判定
- 所有产物写入都是 `tmp+rename` 原子操作，中断不产生半写文件

## 3. 单旋钮限流

```python
# 只调 pace_seconds 一个参数，其余全部按行为学比例自动推导
await run_pipeline(
    workdir=wd, items=items, stages=stages,
    pace_seconds=5.0,                        # ← 唯一旋钮
    work_hours=(time(22, 0), time(6, 0)),    # 可选：跨午夜工作窗口（v1.0 E1）
)
```

`PacingConfig` 从 `pace_seconds=P` 推导：

| 参数                  | 值               | 含义                              |
|----------------------|------------------|-----------------------------------|
| `min_delay`          | `0.5 * P`        | jitter 下界                        |
| `max_delay`          | `2.0 * P`        | jitter 上界                        |
| `sigma`              | `P / 3`          | 正态分布 σ                          |
| `long_pause_every_n` | `max(5, 25·5/P)` | 每 N 步一次长停（P 越大越稀疏）      |
| `long_pause_seconds` | `10 * P`         | 长停时长                            |

`work_hours=(start, end)`：
- `start == end` → always on（无门控）
- `start < end` → 当日窗口 `[start, end)`
- `start > end` → **跨午夜**（例如 22:00–06:00），逻辑上是 `t >= start OR t < end`

## 4. 关键 API

### `Stage`
```python
@dataclass(frozen=True)
class Stage:
    name: str                                        # 编译期确定的 stage 标识（Q_new_5）
    product_path: Callable[[Path, dict], Path]       # 从 item 推产物路径
    run: Callable[[Path, dict, Path], Awaitable[None]]
```

`run` 一旦抛异常 → 通过 `Stage.name`（不是 traceback 解析）路由到 `failed/{id}.json`；同 item 的后续 stage 跳过，其它 item 继续执行。

### `run_pipeline(workdir, items, stages, pace_seconds, work_hours=None, rng=None, now_fn=datetime.now)`

主循环。启动时：
1. 抢 `workdir_lock` → 已被占则立刻 `WorkdirLockedError`
2. `_sweep_orphan_tmp(workdir)` 清理上次崩溃留下的 `*.tmp`
3. 逐 item：
   - 若 `failed/{id}.json` 存在 → skip（`--retry-failed` 流程会先 archive 再重跑）
   - 逐 stage：若 `is_stage_done(product)` → skip；否则跑 `run`
   - 任意 stage 抛异常 → `record_failure(id, exc, stage=stage.name, workdir)` 后跳到下一 item
4. item 之间调 `apply_pacing(cfg, step_index=..., rng=..., now=..., work_hours=...)`

返回 `PipelineResult(processed, skipped, failed, per_stage_skips)`。

### `failure.archive_failed_for_retry(item_id, workdir) -> Path | None`

调用位置：`--retry-failed` 之前。把 `failed/{id}.json` 原子搬到 `failed/.attempt-N/`（N 自动递增，永不覆写）→ 下一次 `run_pipeline` 就会重跑这个 item。

### `failure.should_retry(item_id, workdir, *, max_retries) -> bool`

CLI 判断"这个 item 是否值得再试"（比对 `.attempt-N/` 数量）。

## 5. CLI

```bash
# 只读进度报告
python -m scripts.mc_status --workdir ./wd --stages video,audio,md
python -m scripts.mc_status --workdir ./wd --stages video,audio --json

# 最小 3-stage demo（fetch → probe → render）
python -m scripts.demo_resume.run --workdir ./demo-wd --items 5
python -m scripts.demo_resume.run --workdir ./demo-wd --items 5 --crash-at 4
python -m scripts.demo_resume.run --workdir ./demo-wd --items 5 --retry-failed
```

## 6. 编写自己的 stage 的 checklist

- [ ] 用 `product_path` 严格返回**该 stage 的产物文件**（不是目录）
- [ ] `run(workdir, item, product)` 里：所有 I/O 走 `atomic._atomic_write_bytes/json/text`
- [ ] 网络下载：`atomic._atomic_stream_download(url, dst, client)`（async, aiofiles+httpx）
- [ ] 完成 sanity check 后调 `sidecar.write_done_sidecar(product, stage=..., sanity_check=...)`；
  失败但产物已经在磁盘上时写 `sanity_check="reason"`（不为 `"ok"`）→ 下次会重做
- [ ] Sanity check 抛 `SanityCheckError`：pipeline 会自动路由到 `failed/`

## 7. 崩溃与恢复语义（§7.3）

已用 25 个 fuzz 测试覆盖：

- **任意 tick 中断**（下载中 / 写完还没 sidecar / 刚写完 sidecar）后 **不变式**：
  - `sidecar.is_stage_done(p) == True ⇒ p 的字节等于最终 payload`
  - workdir 里没有残留 `*.tmp`
- **重跑**总能让所有 item × 所有 stage 到达 done 状态

## 8. FAQ

**Q：一台机器能同时跑两个 pipeline 吗？**
A：**同一 workdir 不行**（`WorkdirLockedError`）；不同 workdir 完全并行。

**Q：手动删了 `.done` sidecar 会怎样？**
A：pipeline 视为该 stage 未完成 → 下次重跑。这也是"强制重跑某个 stage"的正规操作。

**Q：`sha256` 字段有用吗？**
A：仅归档（Q_new_4）。任何一致性判断都以 sidecar 的 `sanity_check` 字段为准，避免"哈希算错就把好产物判死"。

**Q：`work_hours` 会中断进行中的 item 吗？**
A：不会。门控在 item 之间生效（`apply_pacing` 在 item 结束、下一 item 开始之前调用）。
