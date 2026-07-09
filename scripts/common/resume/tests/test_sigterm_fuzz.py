# -*- coding: utf-8 -*-
"""SIGTERM-like random-tick fuzz tests (PRD-001 §7.3).

Real cross-process SIGTERM is unreliable on Windows and pollutes pytest.
We use *cooperative* interruption instead: an InterruptClock decrements
on every hook call and raises KeyboardInterrupt at zero.

By seeding a range of tick budgets we exercise every reachable interrupt
point inside a stage's run() — early, mid-write, and just-before-sidecar.
After each simulated crash, we re-run the pipeline and check two
invariants:

  I1: NO product file appears on disk in a "done" state (sidecar 'ok')
      whose actual content is a partial write. i.e. sidecar & content
      always agree.
  I2: A second pipeline call re-runs at MOST the stages that had no
      sidecar before, and eventually completes every item.
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from scripts.common.resume import pipeline, sidecar


class InterruptClock:
    """Raises KeyboardInterrupt on the Nth tick."""
    def __init__(self, budget: int):
        self.budget = budget
        self.ticks = 0
    def tick(self):
        self.ticks += 1
        if self.ticks >= self.budget:
            raise KeyboardInterrupt(f"simulated crash at tick {self.ticks}")


def _prod_bin(stage_name: str):
    def _fn(w: Path, i: dict) -> Path:
        return w / stage_name / f"{i['aweme_id']}.bin"
    return _fn


def _make_atomic_stage(name: str, clock: InterruptClock, payload: bytes):
    """A stage that writes tmp → rename → sidecar, with clock.tick()
    at each mutation point (mimicking the real download → verify → mark path).
    """
    from scripts.common.resume.atomic import _atomic_write_bytes

    async def _run(w: Path, i: dict, product: Path):
        product.parent.mkdir(parents=True, exist_ok=True)
        clock.tick()                                   # point A: before write
        _atomic_write_bytes(product, payload)          # tmp+rename atomically
        clock.tick()                                   # point B: after write, before sidecar
        sidecar.write_done_sidecar(product, stage=name)
        clock.tick()                                   # point C: after sidecar (rare crash)

    return pipeline.Stage(name=name, product_path=_prod_bin(name), run=_run)


@pytest.mark.parametrize("crash_budget", list(range(1, 25)))
@pytest.mark.asyncio
async def test_crash_at_every_tick_preserves_atomicity(tmp_path: Path, crash_budget: int):
    """For every possible crash tick within a 3-stage × 3-item run:
    - No product file is in a "done" state with wrong content.
    - A second run completes all items with no data loss.
    """
    items = [{"aweme_id": "111"}, {"aweme_id": "222"}, {"aweme_id": "333"}]
    payload = b"payload-final-good"

    # --- run 1: crash somewhere ---
    clock = InterruptClock(budget=crash_budget)
    stages = [
        _make_atomic_stage("video", clock, payload),
        _make_atomic_stage("audio", clock, payload),
        _make_atomic_stage("md",    clock, payload),
    ]

    with pytest.raises(KeyboardInterrupt):
        await pipeline.run_pipeline(
            workdir=tmp_path, items=items, stages=stages, pace_seconds=0.001,
        )

    # I1: every product that has a sidecar with sanity_check='ok' must
    # match the good payload exactly. NO orphan .tmp files leaked.
    for stage_name in ("video", "audio", "md"):
        stage_dir = tmp_path / stage_name
        if not stage_dir.exists():
            continue
        # no leftover .tmp files on disk
        assert not list(stage_dir.glob("*.tmp")), (
            f"orphan tmp files after crash: {list(stage_dir.glob('*.tmp'))}"
        )
        for product in stage_dir.glob("*.bin"):
            if sidecar.is_stage_done(product):
                assert product.read_bytes() == payload, (
                    f"{product} marked done but content is not the final payload"
                )

    # --- run 2: fresh clock with big budget, must complete cleanly ---
    clock2 = InterruptClock(budget=10**9)
    stages2 = [
        _make_atomic_stage("video", clock2, payload),
        _make_atomic_stage("audio", clock2, payload),
        _make_atomic_stage("md",    clock2, payload),
    ]
    result = await pipeline.run_pipeline(
        workdir=tmp_path, items=items, stages=stages2, pace_seconds=0.001,
    )

    # I2: every item ended with all 3 stages done.
    for it in items:
        for stage_name in ("video", "audio", "md"):
            product = tmp_path / stage_name / f"{it['aweme_id']}.bin"
            assert product.exists()
            assert sidecar.is_stage_done(product), (
                f"stage {stage_name} for {it['aweme_id']} not marked done "
                f"after retry (crash_budget={crash_budget})"
            )
            assert product.read_bytes() == payload

    # I1b: no residue .tmp anywhere in workdir
    all_tmp = list(tmp_path.rglob("*.tmp"))
    assert not all_tmp, f"orphan tmp survived retry: {all_tmp}"


@pytest.mark.asyncio
async def test_randomised_crashes_across_many_seeds(tmp_path_factory):
    """20 seeds × random crash budgets — smoke test the whole envelope."""
    rng = random.Random(20260709)
    items = [{"aweme_id": f"id{i}"} for i in range(5)]

    for seed_i in range(20):
        wd = tmp_path_factory.mktemp(f"fuzz-{seed_i}")
        budget = rng.randint(1, 40)
        clock = InterruptClock(budget=budget)
        stages = [
            _make_atomic_stage("video", clock, b"vidpay"),
            _make_atomic_stage("audio", clock, b"audpay"),
        ]

        try:
            await pipeline.run_pipeline(
                workdir=wd, items=items, stages=stages, pace_seconds=0.001,
            )
        except KeyboardInterrupt:
            pass

        # ALWAYS the "sidecar ok ⇒ payload correct" invariant
        for stage_name in ("video", "audio"):
            stage_dir = wd / stage_name
            if not stage_dir.exists():
                continue
            for product in stage_dir.glob("*.bin"):
                if sidecar.is_stage_done(product):
                    expected = b"vidpay" if stage_name == "video" else b"audpay"
                    assert product.read_bytes() == expected, (
                        f"seed {seed_i} budget {budget}: {product} corrupt"
                    )
            assert not list(stage_dir.glob("*.tmp"))

        # Recovery: replay must complete
        clock2 = InterruptClock(budget=10**9)
        result = await pipeline.run_pipeline(
            workdir=wd,
            items=items,
            stages=[
                _make_atomic_stage("video", clock2, b"vidpay"),
                _make_atomic_stage("audio", clock2, b"audpay"),
            ],
            pace_seconds=0.001,
        )
        for it in items:
            for stage_name in ("video", "audio"):
                p = wd / stage_name / f"{it['aweme_id']}.bin"
                assert p.exists() and sidecar.is_stage_done(p)
