# -*- coding: utf-8 -*-
"""Read-only status reporter for a resume workdir (PRD-001 §4.6).

Consumed by `scripts/mc_status.py` (CLI) and can also be called from
other scripts to programmatically check progress. All operations here
are strictly read-only per Q_new_1 (v1.1): they MUST NOT touch files,
especially manifest.jsonl.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.common.resume.lock import read_lock_info
from scripts.common.resume.manifest import _read_manifest_ids_safe
from scripts.common.resume.sidecar import is_stage_done

_logger = logging.getLogger(__name__)


@dataclass
class StatusReport:
    total_items: int
    per_stage: dict[str, int] = field(default_factory=dict)
    failed_count: int = 0
    failed_by_stage: dict[str, int] = field(default_factory=dict)
    lock_holder: dict[str, Any] | None = None
    manifest_corrupt: bool = False


def build_status_report(workdir: Path, *, stages: list[str]) -> StatusReport:
    """Scan `workdir` and count items done per stage plus failures.

    Read-only: does NOT truncate a corrupt manifest — just flags it so the
    CLI can advise the user to re-run `fetch_manifest` (Q_new_1).
    """
    manifest = workdir / "manifest.jsonl"
    seen, _valid_lines, corrupt = _read_manifest_ids_safe(manifest)
    total = len(seen)

    per_stage: dict[str, int] = {}
    for stage in stages:
        n = 0
        for item_id in seen:
            product = workdir / stage / f"{item_id}.bin"
            # Also support common video extensions used by real pipelines;
            # if the caller's product path differs, they can subclass this
            # module — status.py is a convenience, not the source of truth.
            if not product.exists():
                for ext in (".mp4", ".mp3", ".wav", ".md", ".json", ".txt"):
                    candidate = workdir / stage / f"{item_id}{ext}"
                    if candidate.exists():
                        product = candidate
                        break
            if is_stage_done(product):
                n += 1
        per_stage[stage] = n

    failed_by_stage: dict[str, int] = {}
    failed_count = 0
    failed_dir = workdir / "failed"
    if failed_dir.exists():
        for entry in failed_dir.iterdir():
            if entry.is_file() and entry.suffix == ".json":
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    stage = data.get("stage", "?")
                    failed_by_stage[stage] = failed_by_stage.get(stage, 0) + 1
                    failed_count += 1
                except (json.JSONDecodeError, OSError):
                    # corrupt failure record — count it under '?'
                    failed_by_stage["?"] = failed_by_stage.get("?", 0) + 1
                    failed_count += 1

    return StatusReport(
        total_items=total,
        per_stage=per_stage,
        failed_count=failed_count,
        failed_by_stage=failed_by_stage,
        lock_holder=read_lock_info(workdir),
        manifest_corrupt=corrupt,
    )


def format_status_report(report: StatusReport, workdir: Path) -> str:
    """Render a StatusReport as a compact, human-friendly text block."""
    lines = [
        f"Workdir: {workdir}",
        f"Manifest items: {report.total_items}",
    ]
    if report.manifest_corrupt:
        lines.append("  ⚠  manifest has corrupt trailing lines — run fetch to repair")

    lines.append("Progress:")
    for stage, n in report.per_stage.items():
        lines.append(f"  {stage:>12}: {n}/{report.total_items}")

    if report.failed_count:
        lines.append(f"Failed: {report.failed_count}")
        for stage, n in report.failed_by_stage.items():
            lines.append(f"  {stage:>12}: {n}")
    else:
        lines.append("Failed: 0")

    if report.lock_holder is not None:
        lines.append(
            f"Live run: pid={report.lock_holder.get('pid')} "
            f"since {report.lock_holder.get('start_time')}"
        )

    return "\n".join(lines)
