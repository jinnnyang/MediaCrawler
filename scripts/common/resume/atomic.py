# -*- coding: utf-8 -*-
"""Atomic file writes (PRD-001 §4.2).

All writes go through tmp + rename so a crash mid-write leaves either the
old content or the new content, never a half-written file.

Also provides `_sweep_orphan_tmp` to clean up `*.tmp` residues left by a
previous crashed run (called by the pipeline at startup).
"""
import json
import logging
from pathlib import Path

import aiofiles
import httpx

_logger = logging.getLogger(__name__)


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Write `data` to `dst` atomically: write to `dst.tmp`, then rename.

    Creates parent directories as needed. `Path.replace()` is atomic on both
    POSIX and Windows (uses `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`).
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    tmp.replace(dst)


def _atomic_write_text(dst: Path, text: str) -> None:
    """Atomic UTF-8 text write. See `_atomic_write_bytes`."""
    _atomic_write_bytes(dst, text.encode("utf-8"))


def _atomic_write_json(dst: Path, obj) -> None:
    """Atomic JSON write with indent=2 and non-ASCII passthrough.

    Suitable for small metadata (sidecar, cursor, failed/{id}.json).
    Per §6.1.1: pure-CPU serialize + small write → keep sync.
    """
    payload = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    _atomic_write_text(dst, payload + "\n")


def _sweep_orphan_tmp(workdir: Path) -> int:
    """Remove stale `*.tmp` files under workdir (residue from a crashed run).

    Per §4.2: called by pipeline at startup. Returns count of files removed.
    """
    removed = 0
    for tmp in workdir.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
            _logger.warning("Swept orphan tmp: %s", tmp)
        except OSError as e:
            _logger.error("Failed to sweep %s: %s", tmp, e)
    return removed


async def _atomic_stream_download(
    url: str,
    dst: Path,
    *,
    chunk_size: int = 1 << 15,          # 32 KiB
    timeout: float = 60.0,
    headers: dict | None = None,
) -> None:
    """Download `url` to `dst` atomically via streaming + tmp + rename.

    Per §6.1.1: native-async I/O (httpx.stream) + large file → use aiofiles.
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size):
                    await f.write(chunk)

    tmp.replace(dst)
