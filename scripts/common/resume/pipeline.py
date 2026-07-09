# -*- coding: utf-8 -*-
"""Pipeline main loop (PRD-001 §4.1.4 & Q_new_5).

## Design contract

- Each item is processed through a fixed sequence of `Stage`s.
- Per-stage idempotency: `sidecar.is_stage_done(product)` short-circuits.
- Per-stage failure isolation: any exception (including `SanityCheckError`)
  routes THIS item into `failed/{id}.json` with `stage=<explicit stage name>`
  (Q_new_5 v1.1 — stage is passed from the loop, never inferred).
- Failed items don't retry unless caller archived them via
  `failure.archive_failed_for_retry` beforehand (`--retry-failed` flow).
- Startup:
    1. Acquire `workdir_lock` (fail-fast if another run holds it).
    2. `_sweep_orphan_tmp(workdir)` to clean up crashed-run residue.
- Between items: `pacing.apply_pacing(...)` (single-knob `pace_seconds`).

## Q_new_5 (v1.1) — compile-time stage identifiers

The stage name attached to a failure comes from `stage.name`, computed
once at Stage construction, NOT parsed from a traceback. This makes stage
attribution unambiguous under decorators, lambdas, and nested helpers.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from scripts.common.resume import failure as _failure_mod
from scripts.common.resume import pacing as _pacing_mod
from scripts.common.resume.atomic import _sweep_orphan_tmp
from scripts.common.resume.lock import workdir_lock
from scripts.common.resume.sidecar import (
    SanityCheckError,
    is_stage_done,
    write_done_sidecar,
)

_logger = logging.getLogger(__name__)


ProductPathFn = Callable[[Path, dict], Path]
StageRunFn = Callable[[Path, dict, Path], Awaitable[None]]


@dataclass(frozen=True)
class Stage:
    """One step in the per-item pipeline.

    - `name`: stage identifier used as the sidecar `stage` field AND the
      `stage=` argument to `record_failure` on error. Compile-time constant.
    - `product_path`: (workdir, item) -> Path to the stage's output file.
      The pipeline uses this both to check `is_stage_done` and to pass
      as the `product` arg to `run`.
    - `run`: async `(workdir, item, product) -> None`. Must produce
      `product` on success and MAY call `write_done_sidecar` itself —
      but if it doesn't, the pipeline writes a default sidecar on return.
    """
    name: str
    product_path: ProductPathFn
    run: StageRunFn


@dataclass
class PipelineResult:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    per_stage_skips: dict[str, int] = field(default_factory=dict)


async def run_pipeline(
    workdir: Path,
    items: Iterable[dict],
    stages: list[Stage],
    *,
    pace_seconds: float = 5.0,
    work_hours: Optional[tuple[time, time]] = None,
    rng: Optional[random.Random] = None,
    now_fn: Callable[[], datetime] = datetime.now,
) -> PipelineResult:
    """Run all `stages` for each item in `items`, resumable and isolated."""
    workdir.mkdir(parents=True, exist_ok=True)
    result = PipelineResult()
    cfg = _pacing_mod.PacingConfig(pace_seconds=pace_seconds)
    if rng is None:
        rng = random.Random()

    with workdir_lock(workdir):
        _sweep_orphan_tmp(workdir)

        for step_index, item in enumerate(items):
            item_id = item["aweme_id"]

            # skip items already in failed/{id}.json (retries go through
            # failure.archive_failed_for_retry BEFORE calling run_pipeline)
            if _failure_mod.is_in_failed(item_id, workdir):
                result.skipped += 1
                _logger.info("Skip %s: in failed/", item_id)
                continue

            item_ok = await _run_item(workdir, item, stages, result)
            if item_ok:
                result.processed += 1
            else:
                result.failed += 1

            # Between-item pacing: never fires before the FIRST item.
            if step_index + 1 < _peek_len(items):
                await _pacing_mod.apply_pacing(
                    cfg,
                    step_index=step_index + 1,
                    rng=rng,
                    now=now_fn(),
                    work_hours=work_hours,
                )

    return result


async def _run_item(
    workdir: Path,
    item: dict,
    stages: list[Stage],
    result: PipelineResult,
) -> bool:
    """Run every stage for one item. Returns True iff all stages succeeded
    or were already done. Q_new_5: stage name is `stage.name`, taken from
    the Stage object at call site — never parsed from tracebacks."""
    item_id = item["aweme_id"]
    for stage in stages:
        product = stage.product_path(workdir, item)

        if is_stage_done(product):
            result.per_stage_skips[stage.name] = (
                result.per_stage_skips.get(stage.name, 0) + 1
            )
            continue

        try:
            await stage.run(workdir, item, product)
        except SanityCheckError as e:
            _failure_mod.record_failure(item_id, e, stage.name, workdir)
            return False
        except Exception as e:                    # noqa: BLE001
            _failure_mod.record_failure(item_id, e, stage.name, workdir)
            return False

        # If run() didn't write the sidecar, write a default one now.
        # (Some stages may prefer to embed richer `extra` metadata themselves.)
        if product.exists() and not is_stage_done(product):
            write_done_sidecar(product, stage=stage.name)

    return True


def _peek_len(items) -> int:
    """Best-effort len() for pacing gate; returns a very large number for
    non-sized iterables so pacing still fires between items."""
    try:
        return len(items)
    except TypeError:
        return 2**31 - 1
