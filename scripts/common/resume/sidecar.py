# -*- coding: utf-8 -*-
"""`.done` sidecar for stage-level idempotency (PRD-001 §4.1.1).

Design invariants:

- A stage is "done" iff (product exists) AND (sidecar exists) AND
  (`sanity_check == "ok"`). See `is_stage_done`.
- Sidecar path convention: `{product.parent}/.{product.name}.done`
  (dot-prefixed so it hides in typical `ls` and file explorers).
- `sha256` is **archival metadata only** (Q_new_4, v1.1) — it is written
  once and NEVER re-computed for skip decisions. Do NOT add a
  "recompute-and-compare" path in `is_stage_done` — that would nullify
  the whole optimization for multi-GB videos.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.common.resume.atomic import _atomic_write_json


class SanityCheckError(Exception):
    """Raised by a stage `fn` when its output fails sanity check.

    The pipeline catches this and routes the item to `failed/{id}.json`
    (see §4.4). Distinct from generic exceptions so the pipeline can
    tag `error_type=SanityCheckError` for triage.
    """


def _sidecar_path(product: Path) -> Path:
    """Return the sidecar path for a product file. Dot-prefixed to hide."""
    return product.parent / f".{product.name}.done"


def _sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streaming sha256 — used only at write time (§4.1.1 Q_new_4)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_done_sidecar(
    product: Path,
    *,
    stage: str,
    sanity_check: str = "ok",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the `.done` sidecar next to `product`.

    Args:
        product:      the stage output file (must already exist).
        stage:        stage name (e.g. "video", "audio", "transcript", "md").
        sanity_check: "ok" iff the product passed its sanity check.
        extra:        stage-specific fields to merge into the sidecar
                      (e.g. `{"duration_ms": 1234}`).

    Returns:
        Path to the written sidecar file.
    """
    if not product.exists():
        raise FileNotFoundError(f"Cannot write sidecar for missing product: {product}")

    payload: dict[str, Any] = {
        "stage": stage,
        "sanity_check": sanity_check,
        "size": product.stat().st_size,
        "sha256": _sha256_file(product),
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if extra:
        # extra overrides above only where explicitly provided
        payload.update(extra)

    dst = _sidecar_path(product)
    _atomic_write_json(dst, payload)
    return dst


def read_done_sidecar(product: Path) -> dict[str, Any] | None:
    """Read the sidecar for `product`. Returns None if missing or corrupt.

    Callers that want to know WHY it's missing should check `_sidecar_path`
    themselves; this function collapses "missing" and "corrupt" into None
    because both mean "not done, re-run the stage".
    """
    path = _sidecar_path(product)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_stage_done(product: Path) -> bool:
    """Sole idempotency oracle for the pipeline (§4.1.1).

    Returns True iff:
      1. `product` exists on disk, AND
      2. sidecar exists, is valid JSON, AND
      3. `sanity_check == "ok"`.

    Q_new_4 (v1.1): DO NOT compare sha256 here. The sidecar's sha256 field
    is archival metadata for out-of-band forensics; recomputing it on every
    skip check would defeat the whole point of the fast-path (multi-GB
    videos would re-hash on every resume).
    """
    if not product.exists():
        return False
    data = read_done_sidecar(product)
    if data is None:
        return False
    return data.get("sanity_check") == "ok"
