# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.lock — PRD-001 §4.7 (Q6)."""
import json
import os
import time
from pathlib import Path

import pytest

from scripts.common.resume import lock


def test_acquire_creates_lock_file_with_pid_and_start_time(tmp_path: Path):
    with lock.workdir_lock(tmp_path) as info:
        lock_file = tmp_path / ".lock"
        assert lock_file.exists()
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        assert data["start_time"] == info["start_time"]
        assert isinstance(data["start_time"], str)  # ISO format

    # lock released on exit; the lock file may remain (filelock semantics)
    # but should be re-acquirable
    with lock.workdir_lock(tmp_path):
        pass


def test_acquire_raises_when_held_by_other_process(tmp_path: Path):
    """Two concurrent lock attempts on the same workdir: second must fail
    fast rather than hang waiting."""
    with lock.workdir_lock(tmp_path):
        with pytest.raises(lock.WorkdirLockedError) as ei:
            with lock.workdir_lock(tmp_path, timeout=0.1):
                pass
        # error message includes the holder's pid and workdir
        msg = str(ei.value)
        assert str(os.getpid()) in msg
        assert str(tmp_path.name) in msg or str(tmp_path) in msg


def test_lock_file_contains_workdir_path_for_debugging(tmp_path: Path):
    with lock.workdir_lock(tmp_path):
        data = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
        # useful for grep-ing "who's holding .lock in this repo"
        assert "workdir" in data
        assert Path(data["workdir"]).resolve() == tmp_path.resolve()


def test_read_lock_info_returns_none_if_no_lock(tmp_path: Path):
    assert lock.read_lock_info(tmp_path) is None


def test_read_lock_info_returns_dict_while_held(tmp_path: Path):
    with lock.workdir_lock(tmp_path):
        info = lock.read_lock_info(tmp_path)
        assert info is not None
        assert info["pid"] == os.getpid()


def test_read_lock_info_tolerates_corrupt_lockfile(tmp_path: Path):
    (tmp_path / ".lock").write_text("{not-json", encoding="utf-8")
    assert lock.read_lock_info(tmp_path) is None
