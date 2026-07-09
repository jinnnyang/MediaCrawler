# -*- coding: utf-8 -*-
"""End-to-end tests for scripts.demo_resume — 3-stage happy + crash + retry."""
import json
from pathlib import Path

import pytest

from scripts.demo_resume import run as demo_run
from scripts.common.resume import sidecar


def test_demo_happy_path_produces_all_stage_products(tmp_path: Path):
    demo_run._call_counter["n"] = 0
    rc = demo_run.main([
        "--workdir", str(tmp_path),
        "--items", "3",
        "--pace-seconds", "0.001",
    ])
    assert rc == 0
    for i in range(3):
        item_id = f"item{i:03d}"
        for stage, ext in [("fetch", ".bin"), ("probe", ".json"), ("render", ".md")]:
            p = tmp_path / stage / f"{item_id}{ext}"
            assert p.exists(), f"missing {p}"
            assert sidecar.is_stage_done(p), f"{p} not marked done"


def test_demo_crash_mid_pipeline_routes_item_to_failed(tmp_path: Path):
    """--crash-at 4 fires on the 4th stage.run call (item002 fetch).
    That item should end up in failed/; item000+001 fully done."""
    demo_run._call_counter["n"] = 0
    rc = demo_run.main([
        "--workdir", str(tmp_path),
        "--items", "3",
        "--pace-seconds", "0.001",
        "--crash-at", "4",   # item000 fetch/probe/render (3) then item001 fetch fails
    ])
    assert rc == 0

    # first two items complete; third landed in failed/
    assert (tmp_path / "render" / "item000.md").exists()
    assert (tmp_path / "failed" / "item001.json").exists(), "item001 must be failed"
    # item002 was fully processed (fetch counter went past crash tick)
    # actually crash is on stage-call #4 = item001 fetch. So:
    #   item000 fetch/probe/render (calls 1,2,3) — done
    #   item001 fetch (call 4) — RAISES → failed
    #   item002 fetch/probe/render (calls 5,6,7) — done
    assert (tmp_path / "render" / "item002.md").exists()
    fail_json = json.loads((tmp_path / "failed" / "item001.json").read_text(encoding="utf-8"))
    assert fail_json["stage"] == "fetch"
    assert fail_json["error_type"] == "RuntimeError"


def test_demo_retry_failed_recovers_the_failed_item(tmp_path: Path):
    # first run crashes on item001 fetch
    demo_run._call_counter["n"] = 0
    demo_run.main([
        "--workdir", str(tmp_path),
        "--items", "3",
        "--pace-seconds", "0.001",
        "--crash-at", "4",
    ])
    assert (tmp_path / "failed" / "item001.json").exists()

    # second run with --retry-failed and NO crash — item001 should recover
    demo_run._call_counter["n"] = 0
    rc = demo_run.main([
        "--workdir", str(tmp_path),
        "--items", "3",
        "--pace-seconds", "0.001",
        "--retry-failed",
    ])
    assert rc == 0

    # every stage done for every item
    for i in range(3):
        for stage, ext in [("fetch", ".bin"), ("probe", ".json"), ("render", ".md")]:
            p = tmp_path / stage / f"item{i:03d}{ext}"
            assert p.exists() and sidecar.is_stage_done(p)

    # failed/ is empty; item001 archived to .attempt-1/
    assert not (tmp_path / "failed" / "item001.json").exists()
    assert (tmp_path / "failed" / ".attempt-1" / "item001.json").exists()
