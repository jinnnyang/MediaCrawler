# -*- coding: utf-8 -*-
"""Per-item failure isolation (PRD-001 §4.4).

Each item's failure is recorded into `failed/{item_id}.json` and does NOT
block other items. On `--retry-failed`, the pipeline moves the file to
`failed/.attempt-N/` before re-running the stages (§4.4.2), so history is
preserved across retry rounds.

Q_new_5 (v1.1): `stage` is ALWAYS passed explicitly by the caller. Never
infer it from the exception object or the traceback — that logic is
fragile under decorators/inline/lambdas and was explicitly retired.
"""
from __future__ import annotations

import json
import logging
import re
import traceback as _tb
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.common.resume.atomic import _atomic_write_json

_logger = logging.getLogger(__name__)


def _failed_dir(workdir: Path) -> Path:
    return workdir / "failed"


def _failed_file(item_id: str, workdir: Path) -> Path:
    return _failed_dir(workdir) / f"{item_id}.json"


_ATTEMPT_DIR_RE = re.compile(r"^\.attempt-(\d+)$")


def _count_prior_attempts(item_id: str, workdir: Path) -> int:
    """Count how many `.attempt-N/` archives contain this item_id."""
    fd = _failed_dir(workdir)
    if not fd.exists():
        return 0
    n = 0
    for entry in fd.iterdir():
        if entry.is_dir() and _ATTEMPT_DIR_RE.match(entry.name):
            if (entry / f"{item_id}.json").exists():
                n += 1
    return n


def record_failure(
    item_id: str,
    err: BaseException,
    stage: str,
    workdir: Path,
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write `failed/{item_id}.json` capturing the failure metadata.

    Q_new_5: `stage` is REQUIRED and comes from the caller — do not attempt
    to derive it from `err` or the current traceback.
    """
    tb_str = _tb.format_exc()
    # inside an active except block, format_exc includes the traceback;
    # outside, it returns "NoneType: None\n" — we normalize that to "".
    if tb_str.strip().startswith("NoneType"):
        tb_str = ""

    payload: dict[str, Any] = {
        "item_id": item_id,
        "stage": stage,
        "error": str(err),
        "error_type": type(err).__name__,
        "traceback": tb_str,
        "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempt": _count_prior_attempts(item_id, workdir) + 1,
    }
    if extra:
        payload.update(extra)

    dst = _failed_file(item_id, workdir)
    _atomic_write_json(dst, payload)
    _logger.warning(
        "Recorded failure for %s at stage=%s (attempt=%d): %s",
        item_id, stage, payload["attempt"], err,
    )
    return dst


def is_in_failed(item_id: str, workdir: Path) -> bool:
    """True iff `failed/{item_id}.json` exists (top-level, NOT `.attempt-N/`)."""
    return _failed_file(item_id, workdir).exists()


def archive_failed_for_retry(item_id: str, workdir: Path) -> Path | None:
    """Move `failed/{id}.json` into `failed/.attempt-N/` (§4.4.2).

    Called by `--retry-failed` BEFORE re-running stages so:
      - `is_in_failed` returns False → item is eligible for retry
      - history is preserved (never overwrite prior failure records)

    Returns the archive path, or None if there was nothing to archive.
    """
    src = _failed_file(item_id, workdir)
    if not src.exists():
        return None

    n = _count_prior_attempts(item_id, workdir) + 1
    dst_dir = _failed_dir(workdir) / f".attempt-{n}"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{item_id}.json"
    src.replace(dst)                          # atomic move (same fs)
    _logger.info("Archived failed/%s.json → %s", item_id, dst.relative_to(workdir))
    return dst


def should_retry(item_id: str, workdir: Path, *, max_retries: int) -> bool:
    """True iff prior attempts < max_retries.

    Attempts are counted from `.attempt-N/` archives; a brand-new item has
    zero prior attempts and always retries (up to the cap).
    """
    return _count_prior_attempts(item_id, workdir) < max_retries
