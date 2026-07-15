# -*- coding: utf-8 -*-
"""JSONL manifest with cursor-based resumable fetch (PRD-001 §4.3).

## Q_new_1 (v1.1) — read/write split

The two functions look symmetric but have very different contracts:

- `_read_manifest_ids_safe(path) -> (seen, valid_lines, has_corruption)`
  **Pure read.** MUST NOT modify the file on disk. `mc_status` and other
  read-only tools call this directly and expect the file to be untouched.

- `_truncate_manifest_to_valid(path, valid_lines)` **Explicit write.**
  Only called from `fetch_manifest` after seeing `has_corruption=True`.
  Never call this from a read path.

This asymmetry is intentional and enforced by tests
(see test_read_manifest_ids_safe_stops_at_first_corrupt_line).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Union

from scripts.common.resume.atomic import _atomic_write_json, _atomic_write_text

_logger = logging.getLogger(__name__)

# Fetcher contract: given a cursor (opaque), return (page_items, next_cursor).
# next_cursor is None when the server signals end-of-list. May be sync or async.
FetchPageFn = Callable[
    [Any],
    Union[
        tuple[list[dict], Any],
        Awaitable[tuple[list[dict], Any]],
    ],
]

_CURSOR_INIT: dict[str, Any] = {
    "max_cursor": 0,
    "has_more": True,
    "fetched_count": 0,
}


def _read_manifest_ids_safe(
    manifest_file: Path,
    *,
    id_field: str = "aweme_id",
) -> tuple[set[str], list[str], bool]:
    """Pure-read parser for manifest.jsonl.

    Returns:
        (seen_ids, valid_lines_raw, has_corruption)

    Behaviour on `JSONDecodeError`: stop reading immediately (crash-induced
    corruption is by construction only ever at the tail of an append-only
    JSONL file). Set `has_corruption=True` and return the good prefix. The
    caller decides whether to repair the file — this function never does.
    """
    seen: set[str] = set()
    valid_lines: list[str] = []
    has_corruption = False

    if not manifest_file.exists():
        return seen, valid_lines, has_corruption

    with open(manifest_file, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                _logger.warning(
                    "Manifest %s has corrupt line (likely from crash): %r. "
                    "Stopping read here; caller must decide whether to repair.",
                    manifest_file,
                    line[:120],
                )
                has_corruption = True
                break
            seen.add(item[id_field])
            valid_lines.append(line)

    return seen, valid_lines, has_corruption


def _truncate_manifest_to_valid(manifest_file: Path, valid_lines: list[str]) -> None:
    """Atomically rewrite `manifest_file` to just the valid lines.

    Only `fetch_manifest` (or an explicit repair CLI) should call this.
    Read-only tools such as `mc_status` MUST NOT.
    """
    body = "\n".join(valid_lines)
    if body:
        body += "\n"
    _atomic_write_text(manifest_file, body)
    _logger.warning(
        "Manifest %s truncated to %d valid lines (corrupt tail dropped).",
        manifest_file,
        len(valid_lines),
    )


def append_manifest_line(manifest_file: Path, item: dict) -> None:
    """Append one JSONL record. NOT atomic across a crash mid-line — that
    is intentional and handled by the tail-truncation recovery in
    `_read_manifest_ids_safe` + `_truncate_manifest_to_valid`.
    """
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _load_cursor_state(cursor_file: Path) -> dict[str, Any]:
    if not cursor_file.exists():
        return dict(_CURSOR_INIT)
    try:
        state = json.loads(cursor_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning("Cursor file %s unreadable (%s); starting fresh.", cursor_file, e)
        return dict(_CURSOR_INIT)
    # forward-compat: fill missing keys with defaults
    for k, v in _CURSOR_INIT.items():
        state.setdefault(k, v)
    return state


async def _call_fetcher(fetch_page_fn: FetchPageFn, cursor: Any):
    result = fetch_page_fn(cursor)
    if inspect.isawaitable(result):
        result = await result
    return result


async def fetch_manifest(
    workdir: Path,
    fetch_page_fn: FetchPageFn,
    *,
    id_field: str = "aweme_id",
) -> Path:
    """Fetch a paginated manifest into `workdir/manifest.jsonl` with cursor
    resume support and duplicate protection (§4.3).

    - Idempotent: safe to re-run after a crash. Cursor lives in
      `manifest.cursor.json`; already-fetched IDs live in `manifest.jsonl`.
    - Corruption-tolerant: crash mid-append leaves a half-line at tail.
      This function detects it (via `_read_manifest_ids_safe`) and repairs
      it (via `_truncate_manifest_to_valid`) exactly once before continuing.
    - Fetcher may be sync or async; both are accepted.

    Returns the manifest.jsonl path.
    """
    manifest_file = workdir / "manifest.jsonl"
    cursor_file = workdir / "manifest.cursor.json"
    workdir.mkdir(parents=True, exist_ok=True)

    state = _load_cursor_state(cursor_file)
    if not state["has_more"]:
        return manifest_file

    seen, valid_lines, has_corruption = _read_manifest_ids_safe(
        manifest_file, id_field=id_field
    )
    if has_corruption:
        # Q_new_1: only the write-path is allowed to repair.
        _truncate_manifest_to_valid(manifest_file, valid_lines)

    while state["has_more"]:
        page, next_cursor = await _call_fetcher(fetch_page_fn, state["max_cursor"])
        new_items = [it for it in page if it[id_field] not in seen]
        for it in new_items:
            append_manifest_line(manifest_file, it)
            seen.add(it[id_field])
        state["max_cursor"] = next_cursor
        state["has_more"] = next_cursor is not None
        state["fetched_count"] = len(seen)
        _atomic_write_json(cursor_file, state)

    return manifest_file
