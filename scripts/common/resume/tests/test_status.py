# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.status — PRD-001 §4.6."""
import json
from pathlib import Path

import pytest

from scripts.common.resume import status, sidecar, failure


def _plant_product(workdir: Path, stage: str, item_id: str, sanity: str = "ok"):
    p = workdir / stage / f"{item_id}.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    sidecar.write_done_sidecar(p, stage=stage, sanity_check=sanity)
    return p


def test_stage_counts_from_manifest_and_sidecars(tmp_path: Path):
    # manifest of 5 items
    (tmp_path / "manifest.jsonl").write_text(
        "\n".join(json.dumps({"aweme_id": f"id{i}"}) for i in range(5)) + "\n",
        encoding="utf-8",
    )
    # 3 items done for stage=video
    for i in range(3):
        _plant_product(tmp_path, "video", f"id{i}")
    # 2 items done for stage=audio
    for i in range(2):
        _plant_product(tmp_path, "audio", f"id{i}")

    report = status.build_status_report(tmp_path, stages=["video", "audio", "md"])

    assert report.total_items == 5
    assert report.per_stage["video"] == 3
    assert report.per_stage["audio"] == 2
    assert report.per_stage["md"] == 0
    assert report.failed_count == 0
    assert report.failed_by_stage == {}


def test_stage_counts_only_counts_ok_sidecars(tmp_path: Path):
    """Products with sanity_check != 'ok' don't count as done (§4.1.1)."""
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"aweme_id": "111"}) + "\n" + json.dumps({"aweme_id": "222"}) + "\n",
        encoding="utf-8",
    )
    _plant_product(tmp_path, "video", "111", sanity="ok")
    _plant_product(tmp_path, "video", "222", sanity="too_short")

    report = status.build_status_report(tmp_path, stages=["video"])
    assert report.per_stage["video"] == 1


def test_status_report_counts_failed_items_by_stage(tmp_path: Path):
    (tmp_path / "manifest.jsonl").write_text(
        "\n".join(json.dumps({"aweme_id": f"id{i}"}) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    failure.record_failure("id0", RuntimeError("x"), "audio", tmp_path)
    failure.record_failure("id1", RuntimeError("y"), "transcript", tmp_path)
    failure.record_failure("id2", RuntimeError("z"), "audio", tmp_path)

    report = status.build_status_report(tmp_path, stages=["video", "audio", "transcript"])
    assert report.failed_count == 3
    assert report.failed_by_stage == {"audio": 2, "transcript": 1}


def test_status_report_surfaces_active_lock_holder(tmp_path: Path):
    """If a live pipeline is holding the workdir lock, status shows the holder."""
    from scripts.common.resume.lock import workdir_lock
    (tmp_path / "manifest.jsonl").write_text("", encoding="utf-8")

    with workdir_lock(tmp_path):
        report = status.build_status_report(tmp_path, stages=["video"])
        assert report.lock_holder is not None
        assert "pid" in report.lock_holder

    # lock released → no holder
    report2 = status.build_status_report(tmp_path, stages=["video"])
    assert report2.lock_holder is None


def test_status_report_handles_missing_manifest_as_zero_items(tmp_path: Path):
    report = status.build_status_report(tmp_path, stages=["video"])
    assert report.total_items == 0
    assert report.per_stage["video"] == 0


def test_format_status_report_is_human_readable(tmp_path: Path):
    (tmp_path / "manifest.jsonl").write_text(
        "\n".join(json.dumps({"aweme_id": f"id{i}"}) for i in range(5)) + "\n",
        encoding="utf-8",
    )
    for i in range(3):
        _plant_product(tmp_path, "video", f"id{i}")
    failure.record_failure("id4", RuntimeError("nope"), "audio", tmp_path)

    report = status.build_status_report(tmp_path, stages=["video", "audio"])
    text = status.format_status_report(report, workdir=tmp_path)

    # Expect an at-a-glance line showing total + per-stage progress.
    assert "5" in text                       # total items
    assert "video" in text and "3/5" in text
    assert "audio" in text and "0/5" in text
    assert "failed" in text.lower()
    assert "1" in text                       # failed count
