# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.manifest — PRD-001 §4.3 & Q_new_1."""
import json
from pathlib import Path

import pytest

from scripts.common.resume import manifest


def _write_lines(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(l + "\n" for l in lines), encoding="utf-8")


def test_read_manifest_ids_safe_empty_when_file_absent(tmp_path: Path):
    seen, valid, corrupt = manifest._read_manifest_ids_safe(tmp_path / "nope.jsonl")
    assert seen == set()
    assert valid == []
    assert corrupt is False


def test_read_manifest_ids_safe_parses_clean_file(tmp_path: Path):
    m = tmp_path / "manifest.jsonl"
    _write_lines(m, [
        json.dumps({"aweme_id": "111", "title": "a"}),
        json.dumps({"aweme_id": "222", "title": "b"}),
        json.dumps({"aweme_id": "333", "title": "c"}),
    ])

    seen, valid, corrupt = manifest._read_manifest_ids_safe(m)
    assert seen == {"111", "222", "333"}
    assert len(valid) == 3
    assert corrupt is False


def test_read_manifest_ids_safe_stops_at_first_corrupt_line(tmp_path: Path):
    """Q_new_1: crash-induced trailing corruption must be detected but the
    function itself must NOT touch the file."""
    m = tmp_path / "manifest.jsonl"
    m.write_text(
        json.dumps({"aweme_id": "111"}) + "\n"
        + json.dumps({"aweme_id": "222"}) + "\n"
        + '{"aweme_id": "333", "trunc',                # half-written line
        encoding="utf-8",
    )
    original_size = m.stat().st_size

    seen, valid, corrupt = manifest._read_manifest_ids_safe(m)

    assert seen == {"111", "222"}
    assert len(valid) == 2
    assert corrupt is True
    # side-effect invariant: pure read must NOT mutate the file
    assert m.stat().st_size == original_size, (
        "_read_manifest_ids_safe is a pure read — Q_new_1 forbids side-effects"
    )


def test_read_manifest_ids_safe_skips_blank_lines(tmp_path: Path):
    m = tmp_path / "manifest.jsonl"
    m.write_text(
        json.dumps({"aweme_id": "111"}) + "\n"
        + "\n"
        + "   \n"
        + json.dumps({"aweme_id": "222"}) + "\n",
        encoding="utf-8",
    )
    seen, valid, corrupt = manifest._read_manifest_ids_safe(m)
    assert seen == {"111", "222"}
    assert corrupt is False


def test_truncate_manifest_to_valid_rewrites_file_atomically(tmp_path: Path):
    m = tmp_path / "manifest.jsonl"
    m.write_text("orig-corrupt-content", encoding="utf-8")

    valid_lines = [
        json.dumps({"aweme_id": "111"}),
        json.dumps({"aweme_id": "222"}),
    ]
    manifest._truncate_manifest_to_valid(m, valid_lines)

    # file rewritten to exactly the valid lines
    body = m.read_text(encoding="utf-8")
    lines = [l for l in body.split("\n") if l]
    assert lines == valid_lines
    # no tmp residue
    assert not m.with_suffix(m.suffix + ".tmp").exists()


def test_append_manifest_line_is_utf8_and_ends_with_newline(tmp_path: Path):
    m = tmp_path / "manifest.jsonl"
    manifest.append_manifest_line(m, {"aweme_id": "111", "title": "抖音测试"})
    manifest.append_manifest_line(m, {"aweme_id": "222"})

    body = m.read_text(encoding="utf-8")
    assert body.endswith("\n")
    lines = [json.loads(l) for l in body.splitlines()]
    assert lines[0]["title"] == "抖音测试"
    assert [x["aweme_id"] for x in lines] == ["111", "222"]


import pytest


def _make_fake_fetcher(pages):
    """Build a sync fetcher returning `pages`, each `(items, next_cursor)`."""
    calls = []

    def _fetch(cursor):
        calls.append(cursor)
        return pages.pop(0)

    return _fetch, calls


@pytest.mark.asyncio
async def test_fetch_manifest_walks_all_pages_and_persists_cursor(tmp_path: Path):
    fetcher, calls = _make_fake_fetcher([
        ([{"aweme_id": "1"}, {"aweme_id": "2"}], "c1"),
        ([{"aweme_id": "3"}], "c2"),
        ([], None),                                     # end-of-list
    ])

    m_path = await manifest.fetch_manifest(tmp_path, fetcher)

    seen, valid, corrupt = manifest._read_manifest_ids_safe(m_path)
    assert seen == {"1", "2", "3"}
    assert corrupt is False
    assert calls == [0, "c1", "c2"], "cursor persisted across pages from state file"
    cursor_state = json.loads((tmp_path / "manifest.cursor.json").read_text(encoding="utf-8"))
    assert cursor_state["has_more"] is False


@pytest.mark.asyncio
async def test_fetch_manifest_resumes_from_saved_cursor(tmp_path: Path):
    """Second run starts from persisted cursor, not from scratch."""
    # simulate first run stopped after page-1
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"aweme_id": "1"}) + "\n" + json.dumps({"aweme_id": "2"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.cursor.json").write_text(
        json.dumps({"max_cursor": "c1", "has_more": True, "fetched_count": 2}),
        encoding="utf-8",
    )

    fetcher, calls = _make_fake_fetcher([
        ([{"aweme_id": "3"}], None),
    ])

    await manifest.fetch_manifest(tmp_path, fetcher)

    assert calls == ["c1"], "resume must use saved cursor, not restart at 0"
    seen, _, _ = manifest._read_manifest_ids_safe(tmp_path / "manifest.jsonl")
    assert seen == {"1", "2", "3"}


@pytest.mark.asyncio
async def test_fetch_manifest_dedupes_by_id_across_pages(tmp_path: Path):
    """If server returns overlapping page contents, do not duplicate."""
    fetcher, _ = _make_fake_fetcher([
        ([{"aweme_id": "1"}, {"aweme_id": "2"}], "c1"),
        ([{"aweme_id": "2"}, {"aweme_id": "3"}], None),   # 2 repeats
    ])

    await manifest.fetch_manifest(tmp_path, fetcher)

    seen, valid, _ = manifest._read_manifest_ids_safe(tmp_path / "manifest.jsonl")
    assert seen == {"1", "2", "3"}
    assert len(valid) == 3, "duplicate 2 must not be appended twice"


@pytest.mark.asyncio
async def test_fetch_manifest_repairs_corrupt_tail_before_appending(tmp_path: Path):
    """Q_new_1: fetch_manifest is the write-path that IS allowed to truncate
    a corrupt manifest; verify it does so exactly once before continuing."""
    m = tmp_path / "manifest.jsonl"
    m.write_text(
        json.dumps({"aweme_id": "1"}) + "\n"
        + '{"aweme_id": "2", "trunc',
        encoding="utf-8",
    )
    (tmp_path / "manifest.cursor.json").write_text(
        json.dumps({"max_cursor": "c0", "has_more": True, "fetched_count": 1}),
        encoding="utf-8",
    )

    fetcher, _ = _make_fake_fetcher([
        ([{"aweme_id": "2"}, {"aweme_id": "3"}], None),  # re-fetch 2 and add 3
    ])

    await manifest.fetch_manifest(tmp_path, fetcher)

    seen, valid, corrupt = manifest._read_manifest_ids_safe(m)
    assert seen == {"1", "2", "3"}
    assert corrupt is False, "corrupt tail was repaired"
    assert len(valid) == 3


@pytest.mark.asyncio
async def test_fetch_manifest_noop_when_all_pages_fetched(tmp_path: Path):
    """When cursor says has_more=False, don't call fetcher at all."""
    (tmp_path / "manifest.cursor.json").write_text(
        json.dumps({"max_cursor": None, "has_more": False, "fetched_count": 42}),
        encoding="utf-8",
    )

    def _fetch(cursor):
        raise AssertionError("fetcher must not be called when has_more=False")

    await manifest.fetch_manifest(tmp_path, _fetch)
