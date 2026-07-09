# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.sidecar — PRD-001 §4.1.1."""
import json
from pathlib import Path

import pytest

from scripts.common.resume import sidecar


def test_write_done_sidecar_creates_hidden_dot_file_next_to_product(tmp_path: Path):
    product = tmp_path / "video" / "1234.mp4"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP4" + b"\x00" * 1024)

    sidecar_path = sidecar.write_done_sidecar(
        product,
        stage="video",
        sanity_check="ok",
        extra={"duration_ms": 1234},
    )

    # naming convention: {stage}/{item_id}.mp4 → {stage}/.{item_id}.mp4.done
    assert sidecar_path == product.parent / f".{product.name}.done"
    assert sidecar_path.exists()

    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert data["stage"] == "video"
    assert data["sanity_check"] == "ok"
    assert data["size"] == len(b"MP4" + b"\x00" * 1024)
    assert "sha256" in data and len(data["sha256"]) == 64
    assert data["duration_ms"] == 1234
    assert "written_at" in data


def test_is_stage_done_true_when_product_and_sidecar_ok(tmp_path: Path):
    product = tmp_path / "audio" / "abc.mp3"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP3 data")
    sidecar.write_done_sidecar(product, stage="audio")

    assert sidecar.is_stage_done(product) is True


def test_is_stage_done_false_when_product_missing(tmp_path: Path):
    product = tmp_path / "audio" / "abc.mp3"
    # no product, no sidecar
    assert sidecar.is_stage_done(product) is False


def test_is_stage_done_false_when_sidecar_missing(tmp_path: Path):
    """Crash between rename and sidecar write → treat as not done."""
    product = tmp_path / "audio" / "abc.mp3"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP3 data")
    # no sidecar written
    assert sidecar.is_stage_done(product) is False


def test_is_stage_done_false_when_sanity_check_not_ok(tmp_path: Path):
    """A bad product with a sidecar marked 'failed' must NOT be treated as done."""
    product = tmp_path / "audio" / "abc.mp3"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP3")
    sidecar.write_done_sidecar(product, stage="audio", sanity_check="too_short")

    assert sidecar.is_stage_done(product) is False


def test_is_stage_done_ignores_sha256_mismatch_by_design(tmp_path: Path):
    """Q_new_4 (v1.1): sha256 is archival only. Even if product bytes change
    after sidecar was written, is_stage_done must still return True as long
    as sanity_check == 'ok'. This prevents future maintainers from adding a
    'recompute sha256 and compare' path that would nullify the optimization.
    """
    product = tmp_path / "audio" / "abc.mp3"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP3 original")
    sidecar.write_done_sidecar(product, stage="audio")

    # tamper with product bytes after sidecar was written
    product.write_bytes(b"TAMPERED - different content, different sha256")

    assert sidecar.is_stage_done(product) is True, (
        "Q_new_4: sha256 must not be re-computed at skip check time"
    )


def test_is_stage_done_false_when_sidecar_is_corrupt_json(tmp_path: Path):
    product = tmp_path / "audio" / "abc.mp3"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"MP3")
    (product.parent / f".{product.name}.done").write_text("{not-json")

    assert sidecar.is_stage_done(product) is False


def test_read_done_sidecar_returns_dict(tmp_path: Path):
    product = tmp_path / "video" / "xxx.mp4"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"data")
    sidecar.write_done_sidecar(product, stage="video", extra={"foo": "bar"})

    data = sidecar.read_done_sidecar(product)
    assert data is not None
    assert data["stage"] == "video"
    assert data["foo"] == "bar"


def test_read_done_sidecar_returns_none_when_missing(tmp_path: Path):
    product = tmp_path / "nothing.mp4"
    assert sidecar.read_done_sidecar(product) is None


def test_sanity_check_error_is_exception():
    assert issubclass(sidecar.SanityCheckError, Exception)
