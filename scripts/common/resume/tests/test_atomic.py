# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.atomic — PRD-001 §4.2."""
import json
from pathlib import Path

import pytest

from scripts.common.resume import atomic


def test_atomic_write_bytes_creates_file_with_expected_content(tmp_path: Path):
    dst = tmp_path / "sub" / "data.bin"       # parent doesn't exist yet
    payload = b"\x00\x01\x02hello\xff"

    atomic._atomic_write_bytes(dst, payload)

    assert dst.exists()
    assert dst.read_bytes() == payload


def test_atomic_write_bytes_crash_before_rename_leaves_no_dst_file(
    tmp_path: Path, monkeypatch
):
    """If the process dies AFTER writing tmp but BEFORE rename, dst must not exist.

    Simulates §7.3 'SIGTERM in tmp write, before rename' case.
    """
    import pathlib

    dst = tmp_path / "half.bin"
    real_replace = pathlib.Path.replace

    def boom(self, target):
        raise KeyboardInterrupt("simulated SIGTERM between write and rename")

    monkeypatch.setattr(pathlib.Path, "replace", boom)
    try:
        atomic._atomic_write_bytes(dst, b"data-that-should-not-land")
    except KeyboardInterrupt:
        pass
    monkeypatch.setattr(pathlib.Path, "replace", real_replace)

    # dst must not exist (rename never happened)
    assert not dst.exists()
    # a .tmp residue is acceptable; it will be cleaned by _sweep_orphan_tmp on next run
    tmp_residue = dst.with_suffix(dst.suffix + ".tmp")
    assert tmp_residue.exists(), "tmp file should be the only residue"


def test_atomic_write_bytes_overwrites_existing_dst(tmp_path: Path):
    dst = tmp_path / "data.bin"
    dst.write_bytes(b"old")

    atomic._atomic_write_bytes(dst, b"new")

    assert dst.read_bytes() == b"new"


def test_atomic_write_json_serializes_with_utf8_and_indent(tmp_path: Path):
    dst = tmp_path / "meta.json"

    atomic._atomic_write_json(dst, {"item": "抖音", "count": 42, "nested": {"k": True}})

    raw = dst.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed == {"item": "抖音", "count": 42, "nested": {"k": True}}
    assert "抖音" in raw, "should keep Chinese chars raw (ensure_ascii=False)"
    assert "\n" in raw, "should be indented for human readability"


def test_atomic_write_text_writes_utf8_string(tmp_path: Path):
    dst = tmp_path / "notes.txt"

    atomic._atomic_write_text(dst, "第一行\n第二行\n")

    assert dst.read_text(encoding="utf-8") == "第一行\n第二行\n"


def test_sweep_orphan_tmp_removes_stale_tmp_files(tmp_path: Path):
    # residue from previous crashed run
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "1234.mp4.tmp").write_bytes(b"partial")
    (tmp_path / "video" / "5678.mp4").write_bytes(b"finished")  # completed
    (tmp_path / "audio").mkdir()
    (tmp_path / "audio" / "abc.mp3.tmp").write_bytes(b"partial")

    removed = atomic._sweep_orphan_tmp(tmp_path)

    assert removed == 2
    assert not (tmp_path / "video" / "1234.mp4.tmp").exists()
    assert not (tmp_path / "audio" / "abc.mp3.tmp").exists()
    assert (tmp_path / "video" / "5678.mp4").exists(), "completed files untouched"


import pytest


@pytest.mark.asyncio
async def test_atomic_stream_download_writes_full_body_atomically(tmp_path: Path, monkeypatch):
    """Stream download must write via tmp+rename and match the source bytes."""
    dst = tmp_path / "video" / "1234.mp4"
    expected = b"MP4" + b"\x00" * 8192 + b"END"

    # Fake httpx.AsyncClient.stream context returning our bytes in chunks
    class _FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks
            self.status_code = 200

        async def aiter_bytes(self, chunk_size=None):
            for c in self._chunks:
                yield c

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def raise_for_status(self):
            pass

    class _FakeClient:
        def stream(self, method, url, **kwargs):
            # yield in 3 chunks to exercise the async-for loop
            return _FakeResponse([expected[:3], expected[3:3 + 8192], expected[3 + 8192:]])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", lambda *a, **kw: _FakeClient())

    await atomic._atomic_stream_download("https://fake/video.mp4", dst)

    assert dst.exists()
    assert dst.read_bytes() == expected
    # no tmp residue
    assert not dst.with_suffix(dst.suffix + ".tmp").exists()
