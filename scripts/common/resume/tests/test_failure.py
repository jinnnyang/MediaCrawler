# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.failure — PRD-001 §4.4 & Q_new_5."""
import json
import re
from pathlib import Path

import pytest

from scripts.common.resume import failure


def test_record_failure_writes_json_with_explicit_stage(tmp_path: Path):
    """Q_new_5 (v1.1): stage MUST come from caller, never inferred from traceback."""
    err = RuntimeError("http 500 from ARK API")

    failure.record_failure(
        item_id="1234567890",
        err=err,
        stage="transcript",
        workdir=tmp_path,
    )

    fail_file = tmp_path / "failed" / "1234567890.json"
    assert fail_file.exists()
    data = json.loads(fail_file.read_text(encoding="utf-8"))
    assert data["item_id"] == "1234567890"
    assert data["stage"] == "transcript"
    assert data["error_type"] == "RuntimeError"
    assert "http 500" in data["error"]
    assert data["attempt"] == 1
    assert "failed_at" in data
    # traceback presence (may be empty string if not in an except block)
    assert "traceback" in data


def test_record_failure_within_except_captures_traceback(tmp_path: Path):
    try:
        raise ValueError("boom")
    except ValueError as e:
        failure.record_failure("id_1", e, "video", tmp_path)

    data = json.loads((tmp_path / "failed" / "id_1.json").read_text(encoding="utf-8"))
    assert "ValueError" in data["traceback"]
    assert "boom" in data["traceback"]


def test_is_in_failed_true_when_failed_json_exists(tmp_path: Path):
    failure.record_failure("id_x", RuntimeError("x"), "video", tmp_path)
    assert failure.is_in_failed("id_x", tmp_path) is True


def test_is_in_failed_false_when_only_attempt_archives_exist(tmp_path: Path):
    """After archiving for retry, failed/{id}.json is gone → should
    NOT count as 'in failed' anymore (that's the whole point of
    --retry-failed archiving)."""
    (tmp_path / "failed" / ".attempt-1").mkdir(parents=True)
    (tmp_path / "failed" / ".attempt-1" / "id_x.json").write_text("{}")

    assert failure.is_in_failed("id_x", tmp_path) is False


def test_archive_failed_for_retry_moves_json_to_attempt_dir(tmp_path: Path):
    """§4.4.2: --retry-failed must archive current failed/{id}.json to
    failed/.attempt-N/ so history is preserved across retry rounds."""
    failure.record_failure("id_a", RuntimeError("boom"), "audio", tmp_path)
    assert failure.is_in_failed("id_a", tmp_path) is True

    archived = failure.archive_failed_for_retry("id_a", tmp_path)

    # top-level file gone; attempt-1 archive exists
    assert not (tmp_path / "failed" / "id_a.json").exists()
    assert archived == tmp_path / "failed" / ".attempt-1" / "id_a.json"
    assert archived.exists()
    # subsequent runs treat as NOT in failed → open to retry
    assert failure.is_in_failed("id_a", tmp_path) is False


def test_archive_failed_for_retry_increments_attempt_number(tmp_path: Path):
    # attempt 1
    failure.record_failure("id_b", RuntimeError("e1"), "video", tmp_path)
    a1 = failure.archive_failed_for_retry("id_b", tmp_path)
    assert a1.parent.name == ".attempt-1"

    # attempt 2 (after another failed retry)
    failure.record_failure("id_b", RuntimeError("e2"), "video", tmp_path)
    data = json.loads((tmp_path / "failed" / "id_b.json").read_text(encoding="utf-8"))
    assert data["attempt"] == 2, "attempt counter reads prior .attempt-N/ archives"

    a2 = failure.archive_failed_for_retry("id_b", tmp_path)
    assert a2.parent.name == ".attempt-2"

    # attempt 3
    failure.record_failure("id_b", RuntimeError("e3"), "video", tmp_path)
    data3 = json.loads((tmp_path / "failed" / "id_b.json").read_text(encoding="utf-8"))
    assert data3["attempt"] == 3


def test_archive_failed_for_retry_noop_when_no_failed_file(tmp_path: Path):
    """Should not raise; just return None."""
    result = failure.archive_failed_for_retry("id_ghost", tmp_path)
    assert result is None


def test_should_retry_respects_max_retries(tmp_path: Path):
    """--max-retries N caps how many attempts we'll do."""
    # simulate 3 prior attempts already archived
    for n in range(1, 4):
        d = tmp_path / "failed" / f".attempt-{n}"
        d.mkdir(parents=True)
        (d / "id_c.json").write_text("{}")

    assert failure.should_retry("id_c", tmp_path, max_retries=3) is False
    assert failure.should_retry("id_c", tmp_path, max_retries=5) is True
    # brand-new item always retries (no prior attempts)
    assert failure.should_retry("id_fresh", tmp_path, max_retries=3) is True
