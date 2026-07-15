# -*- coding: utf-8 -*-
"""Single-process workdir lock (PRD-001 §4.7 / Q6).

Guarantees only one live pipeline run per workdir. Uses `filelock` for the
cross-platform OS-level exclusive lock and writes human-readable metadata
(pid, start_time, workdir) into a companion file for debugging (`who's
holding this workdir?`).

Two files by design:
- `.lock.acquire`  — owned by `filelock` (empty, holds OS-level flock)
- `.lock`          — human-readable JSON metadata

We keep them separate because filelock manipulates the lock file at
release/teardown time, and mixing metadata writes with those semantics
gets subtle (esp. on Windows where the lock file can be removed).
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock, Timeout

from scripts.common.resume.atomic import _atomic_write_json

_logger = logging.getLogger(__name__)


class WorkdirLockedError(RuntimeError):
    """Raised when the workdir is already locked by another live process."""


def _lock_paths(workdir: Path) -> tuple[Path, Path]:
    """Return (acquire_file, metadata_file)."""
    return workdir / ".lock.acquire", workdir / ".lock"


def read_lock_info(workdir: Path) -> dict[str, Any] | None:
    """Read metadata about the current lock holder, or None if:
    - the workdir has no `.lock` file
    - the `.lock` file is corrupt / unreadable
    """
    _, meta_file = _lock_paths(workdir)
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@contextmanager
def workdir_lock(
    workdir: Path,
    *,
    timeout: float = 0.0,
) -> Iterator[dict[str, Any]]:
    """Acquire the single-process workdir lock or raise `WorkdirLockedError`.

    - `timeout=0.0` (default) fails fast when the lock is held.
    - On acquire, writes `{pid, start_time, workdir}` to `.lock` for
      external observability (mc_status will surface this info).
    - On release, deletes `.lock` metadata but keeps `.lock.acquire`
      (filelock manages that file's lifecycle).

    Yields the metadata dict for the current holder.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    acquire_file, meta_file = _lock_paths(workdir)

    lk = FileLock(str(acquire_file), timeout=timeout)
    try:
        lk.acquire(timeout=timeout)
    except Timeout:
        holder = read_lock_info(workdir) or {"pid": "?", "start_time": "?"}
        raise WorkdirLockedError(
            f"Workdir {workdir} is locked by pid={holder.get('pid')} "
            f"(started at {holder.get('start_time')}). "
            f"If that process is gone, remove {meta_file} and retry."
        ) from None

    info = {
        "pid": os.getpid(),
        "start_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workdir": str(workdir.resolve()),
    }
    _atomic_write_json(meta_file, info)
    _logger.debug("Acquired workdir lock: %s", info)

    try:
        yield info
    finally:
        try:
            meta_file.unlink(missing_ok=True)
        except OSError as e:
            _logger.warning("Failed to remove %s: %s", meta_file, e)
        lk.release()
