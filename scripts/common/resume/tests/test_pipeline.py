# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.pipeline — PRD-001 §4.1.4 & Q_new_5."""
import asyncio
from datetime import datetime, time
from pathlib import Path

import pytest

from scripts.common.resume import pipeline, sidecar, failure


# ─── helper: build a fake stage that records what it saw ──────────────

def _make_stage(name: str, calls: list, *, raise_exc: Exception | None = None):
    """Build a Stage whose run_fn records the item_id and optionally raises.

    Product path is `{workdir}/{name}/{item_id}.bin`.
    """
    def _product_path(workdir: Path, item: dict) -> Path:
        return workdir / name / f"{item['aweme_id']}.bin"

    async def _run(workdir: Path, item: dict, product: Path):
        calls.append((name, item["aweme_id"]))
        if raise_exc is not None:
            raise raise_exc
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes(b"ok-" + name.encode() + b"-" + item["aweme_id"].encode())

    return pipeline.Stage(name=name, product_path=_product_path, run=_run)


def _items():
    return [{"aweme_id": "111"}, {"aweme_id": "222"}, {"aweme_id": "333"}]


# ─── #1: happy path — all stages run for all items ────────────────────

@pytest.mark.asyncio
async def test_run_pipeline_executes_all_stages_for_each_item(tmp_path: Path):
    calls: list[tuple[str, str]] = []
    stages = [
        _make_stage("video", calls),
        _make_stage("audio", calls),
        _make_stage("md", calls),
    ]

    result = await pipeline.run_pipeline(
        workdir=tmp_path,
        items=_items(),
        stages=stages,
        pace_seconds=0.01,           # negligible for tests
    )

    # 3 items × 3 stages = 9 calls, in per-item order
    assert calls == [
        ("video", "111"), ("audio", "111"), ("md", "111"),
        ("video", "222"), ("audio", "222"), ("md", "222"),
        ("video", "333"), ("audio", "333"), ("md", "333"),
    ]
    assert result.processed == 3
    assert result.failed == 0
    assert result.skipped == 0


# ─── #2: idempotency — skip stage whose sidecar is 'ok' ───────────────

@pytest.mark.asyncio
async def test_run_pipeline_skips_stages_with_ok_sidecar(tmp_path: Path):
    calls: list[tuple[str, str]] = []
    stages = [_make_stage("video", calls), _make_stage("audio", calls)]

    # pretend 111.video is already done from a previous run
    prod = tmp_path / "video" / "111.bin"
    prod.parent.mkdir(parents=True)
    prod.write_bytes(b"cached")
    sidecar.write_done_sidecar(prod, stage="video")

    await pipeline.run_pipeline(
        workdir=tmp_path, items=_items(), stages=stages, pace_seconds=0.01,
    )

    # 111.video was skipped; 111.audio + rest still ran
    assert ("video", "111") not in calls, "already-done stage must not re-run"
    assert ("audio", "111") in calls
    assert ("video", "222") in calls

# ─── #3: failure isolation — one item's error must not block others ─

@pytest.mark.asyncio
async def test_run_pipeline_isolates_failures_and_records_stage_explicitly(tmp_path: Path):
    calls: list[tuple[str, str]] = []
    boom = RuntimeError("audio corrupt")

    # audio stage: fail for item 222, ok for 111 and 333
    def _audio_product(workdir: Path, item: dict) -> Path:
        return workdir / "audio" / f"{item['aweme_id']}.bin"

    async def _audio_run(workdir: Path, item: dict, product: Path):
        calls.append(("audio", item["aweme_id"]))
        if item["aweme_id"] == "222":
            raise boom
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes(b"audio")

    stages = [
        _make_stage("video", calls),
        pipeline.Stage(name="audio", product_path=_audio_product, run=_audio_run),
        _make_stage("md", calls),
    ]

    result = await pipeline.run_pipeline(
        workdir=tmp_path, items=_items(), stages=stages, pace_seconds=0.01,
    )

    # 222 stopped mid-item: no md stage for 222; but 333 kept going.
    assert ("md", "111") in calls
    assert ("md", "222") not in calls, "failed item must not proceed to later stages"
    assert ("md", "333") in calls
    assert result.processed == 2
    assert result.failed == 1

    # Q_new_5: failed/{222}.json must record stage='audio' explicitly.
    fail_data = _read_failed(tmp_path, "222")
    assert fail_data["stage"] == "audio", "Q_new_5: stage from Stage.name, not traceback"
    assert fail_data["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_run_pipeline_sanity_check_error_routes_to_failed(tmp_path: Path):
    """SanityCheckError from a stage → failed/{id}.json with error_type=SanityCheckError."""
    def _prod(w: Path, i: dict) -> Path:
        return w / "video" / f"{i['aweme_id']}.bin"

    async def _run(w: Path, i: dict, p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"too-small")
        raise sidecar.SanityCheckError("size < 1KB")

    result = await pipeline.run_pipeline(
        workdir=tmp_path,
        items=[{"aweme_id": "111"}],
        stages=[pipeline.Stage(name="video", product_path=_prod, run=_run)],
        pace_seconds=0.01,
    )
    assert result.failed == 1
    fd = _read_failed(tmp_path, "111")
    assert fd["error_type"] == "SanityCheckError"
    assert fd["stage"] == "video"


@pytest.mark.asyncio
async def test_run_pipeline_skips_items_already_in_failed(tmp_path: Path):
    """Items with existing failed/{id}.json are skipped, not retried,
    unless caller archived them first (--retry-failed flow)."""
    # pre-plant a failure for 222
    _failure_mod = __import__("scripts.common.resume.failure", fromlist=["failure"])
    _failure_mod.record_failure("222", RuntimeError("previous"), "audio", tmp_path)

    calls: list[tuple[str, str]] = []
    stages = [_make_stage("video", calls)]

    result = await pipeline.run_pipeline(
        workdir=tmp_path, items=_items(), stages=stages, pace_seconds=0.01,
    )

    assert ("video", "222") not in calls
    assert result.skipped == 1
    assert result.processed == 2


def _read_failed(workdir: Path, item_id: str) -> dict:
    import json as _json
    return _json.loads((workdir / "failed" / f"{item_id}.json").read_text(encoding="utf-8"))


# ─── #4: interrupt & resume — half-processed item resumes at right stage ─

@pytest.mark.asyncio
async def test_run_pipeline_resumes_at_next_incomplete_stage_after_crash(tmp_path: Path):
    """First run completes stage=video for all items then dies mid-audio for 222.
    Second run (fresh call) must resume such that:
      - video is skipped for everyone (sidecars exist)
      - audio re-runs starting at 222 (partial product from crash is overwritten)
      - md runs normally for everyone
    """
    calls_run1: list[tuple[str, str]] = []
    calls_run2: list[tuple[str, str]] = []

    def _prod(name):
        def inner(w: Path, i: dict):
            return w / name / f"{i['aweme_id']}.bin"
        return inner

    # RUN 1: video ok for all; audio raises on 222 mid-write
    async def _v1(w, i, p):
        calls_run1.append(("video", i["aweme_id"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"vid")

    async def _a1(w, i, p):
        calls_run1.append(("audio", i["aweme_id"]))
        if i["aweme_id"] == "222":
            raise RuntimeError("crash mid-audio")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"aud")

    async def _m1(w, i, p):
        calls_run1.append(("md", i["aweme_id"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"md")

    await pipeline.run_pipeline(
        workdir=tmp_path,
        items=_items(),
        stages=[
            pipeline.Stage("video", _prod("video"), _v1),
            pipeline.Stage("audio", _prod("audio"), _a1),
            pipeline.Stage("md",    _prod("md"),    _m1),
        ],
        pace_seconds=0.01,
    )

    # sanity: 222 landed in failed/, then 333 was still processed
    assert (tmp_path / "failed" / "222.json").exists()
    assert (tmp_path / "audio" / "111.bin").exists()
    assert (tmp_path / "audio" / "333.bin").exists()

    # RUN 2: user calls --retry-failed → we archive 222 first, then re-run
    from scripts.common.resume import failure as _f
    _f.archive_failed_for_retry("222", tmp_path)
    assert not (tmp_path / "failed" / "222.json").exists()

    async def _v2(w, i, p):
        calls_run2.append(("video", i["aweme_id"]))
    async def _a2(w, i, p):
        calls_run2.append(("audio", i["aweme_id"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"aud-retry")
    async def _m2(w, i, p):
        calls_run2.append(("md", i["aweme_id"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"md-retry")

    await pipeline.run_pipeline(
        workdir=tmp_path,
        items=_items(),
        stages=[
            pipeline.Stage("video", _prod("video"), _v2),
            pipeline.Stage("audio", _prod("audio"), _a2),
            pipeline.Stage("md",    _prod("md"),    _m2),
        ],
        pace_seconds=0.01,
    )

    # video: 100% skipped by sidecar in run 2
    assert not any(s == "video" for s, _ in calls_run2), "video sidecars → skip all"

    # audio: 111, 333 skipped (sidecar 'ok'); ONLY 222 re-executes
    audio_ids = [i for s, i in calls_run2 if s == "audio"]
    assert audio_ids == ["222"], (
        f"only 222 audio should re-run; got {audio_ids}"
    )

    # md: 111 & 333 were already done in run 1 → skipped; 222 runs fresh
    md_ids = [i for s, i in calls_run2 if s == "md"]
    assert md_ids == ["222"]

    # final state: all 3 items complete on md
    assert (tmp_path / "md" / "111.bin").exists()
    assert (tmp_path / "md" / "222.bin").exists()
    assert (tmp_path / "md" / "333.bin").exists()


# ─── #5: workdir lock — reject concurrent run ─────────────────────────

@pytest.mark.asyncio
async def test_run_pipeline_rejects_concurrent_run_on_same_workdir(tmp_path: Path):
    """Q6 (§4.7): only ONE live pipeline per workdir. Second call must fail
    fast (WorkdirLockedError), not silently corrupt state."""
    from scripts.common.resume.lock import workdir_lock, WorkdirLockedError

    # hold the lock ourselves to simulate another live process
    with workdir_lock(tmp_path):
        with pytest.raises(WorkdirLockedError):
            await pipeline.run_pipeline(
                workdir=tmp_path,
                items=[{"aweme_id": "111"}],
                stages=[_make_stage("video", [])],
                pace_seconds=0.01,
            )


# ─── #6: orphan tmp sweep runs at startup ─────────────────────────────

@pytest.mark.asyncio
async def test_run_pipeline_sweeps_orphan_tmp_at_startup(tmp_path: Path):
    """Residue *.tmp files (from a crashed prior run) must be gone after
    the pipeline starts."""
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "old.mp4.tmp").write_bytes(b"partial")

    await pipeline.run_pipeline(
        workdir=tmp_path,
        items=[{"aweme_id": "111"}],
        stages=[_make_stage("video", [])],
        pace_seconds=0.01,
    )

    assert not (tmp_path / "video" / "old.mp4.tmp").exists(), (
        "orphan tmp must be swept at pipeline startup"
    )
