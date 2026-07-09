# -*- coding: utf-8 -*-
"""Tests for scripts.mc_status CLI wrapper."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _make_workdir_with_manifest(tmp_path: Path):
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"aweme_id": "111"}) + "\n"
        + json.dumps({"aweme_id": "222"}) + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_cli_prints_human_readable_report(tmp_path: Path, capsys):
    wd = _make_workdir_with_manifest(tmp_path)
    from scripts import mc_status

    exit_code = mc_status.main([
        "--workdir", str(wd),
        "--stages", "video,audio,md",
    ])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Manifest items: 2" in out
    assert "video" in out and "0/2" in out


def test_cli_json_mode_emits_machine_readable(tmp_path: Path, capsys):
    wd = _make_workdir_with_manifest(tmp_path)
    from scripts import mc_status

    exit_code = mc_status.main([
        "--workdir", str(wd),
        "--stages", "video,audio",
        "--json",
    ])
    out = capsys.readouterr().out
    assert exit_code == 0
    data = json.loads(out)
    assert data["total_items"] == 2
    assert data["per_stage"] == {"video": 0, "audio": 0}


def test_cli_missing_workdir_errors_out(tmp_path: Path, capsys):
    from scripts import mc_status

    exit_code = mc_status.main([
        "--workdir", str(tmp_path / "does-not-exist"),
        "--stages", "video",
    ])
    assert exit_code != 0
