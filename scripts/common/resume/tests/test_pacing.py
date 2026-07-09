# -*- coding: utf-8 -*-
"""Tests for scripts.common.resume.pacing — PRD-001 §4.5 & v1.0 E1."""
import random
from datetime import datetime, time

import pytest

from scripts.common.resume import pacing


def test_pacing_config_derives_all_from_single_pace_knob():
    """§4.5: single --pace-seconds P knob derives everything else via ratios."""
    cfg = pacing.PacingConfig(pace_seconds=5.0)

    # jitter bounds are ±the sigma-clipped normal range
    assert cfg.pace_seconds == 5.0
    assert cfg.min_delay == pytest.approx(2.5)   # 0.5 * pace
    assert cfg.max_delay == pytest.approx(10.0)  # 2.0 * pace
    assert cfg.sigma == pytest.approx(5.0 / 3)   # pace / 3

    # long-pause behavior scales with pace so users don't tune two knobs
    assert cfg.long_pause_every_n > 0
    assert cfg.long_pause_seconds > cfg.max_delay, "long pause > single jitter"


def test_pacing_config_rejects_non_positive_pace():
    with pytest.raises(ValueError):
        pacing.PacingConfig(pace_seconds=0)
    with pytest.raises(ValueError):
        pacing.PacingConfig(pace_seconds=-1)


def test_next_delay_stays_within_min_max_bounds():
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    rng = random.Random(42)

    samples = [pacing.next_delay(cfg, rng) for _ in range(500)]

    for s in samples:
        assert cfg.min_delay <= s <= cfg.max_delay, (
            f"delay {s:.3f} outside [{cfg.min_delay}, {cfg.max_delay}]"
        )
    # sanity: mean should be close to pace_seconds (loose ±20% tolerance
    # because tails are clipped, so mean is a hair below pace)
    mean = sum(samples) / len(samples)
    assert abs(mean - cfg.pace_seconds) / cfg.pace_seconds < 0.2


def test_next_delay_is_deterministic_with_seeded_rng():
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    r1 = random.Random(123)
    r2 = random.Random(123)
    seq1 = [pacing.next_delay(cfg, r1) for _ in range(20)]
    seq2 = [pacing.next_delay(cfg, r2) for _ in range(20)]
    assert seq1 == seq2


# ─── is_within_hours: v1.0 E1 (跨午夜 window) ────────────────────────────

def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2026, 7, 9, h, m, 0)


@pytest.mark.parametrize("start,end,now_h,expected", [
    # normal daytime window (9-18)
    (time(9, 0), time(18, 0), 8, False),   # before window
    (time(9, 0), time(18, 0), 9, True),    # at start (inclusive)
    (time(9, 0), time(18, 0), 12, True),   # middle
    (time(9, 0), time(18, 0), 17, True),   # last minute
    (time(9, 0), time(18, 0), 18, False),  # at end (exclusive)
    (time(9, 0), time(18, 0), 22, False),  # after
])
def test_is_within_hours_same_day_window(start, end, now_h, expected):
    assert pacing.is_within_hours(_dt(now_h), start, end) is expected


@pytest.mark.parametrize("now_h,expected", [
    # overnight window 22:00 → 06:00 (crosses midnight — v1.0 E1)
    (21, False),   # just before
    (22, True),    # start
    (23, True),    # before midnight
    (0, True),     # midnight
    (3, True),     # deep night
    (5, True),     # last hour
    (6, False),    # end (exclusive)
    (12, False),   # daytime
])
def test_is_within_hours_overnight_window_v1_e1(now_h, expected):
    """v1.0 E1: 22:00-06:00 window must be True across midnight."""
    assert pacing.is_within_hours(_dt(now_h), time(22, 0), time(6, 0)) is expected


def test_is_within_hours_same_start_and_end_means_always_true():
    """Corner: 00:00-00:00 is treated as 'always on' (no restriction)."""
    for h in (0, 6, 12, 18, 23):
        assert pacing.is_within_hours(_dt(h), time(0, 0), time(0, 0)) is True


def test_sleep_seconds_until_within_hours_zero_when_already_inside():
    now = _dt(12, 0)
    assert pacing.sleep_seconds_until_within_hours(now, time(9, 0), time(18, 0)) == 0


def test_sleep_seconds_until_within_hours_computes_gap_same_day():
    """At 07:00, window is 09:00-18:00 → sleep 2h = 7200s."""
    now = _dt(7, 0)
    secs = pacing.sleep_seconds_until_within_hours(now, time(9, 0), time(18, 0))
    assert secs == 2 * 3600


def test_sleep_seconds_until_within_hours_overnight_window():
    """At 07:00, overnight window 22:00-06:00 → sleep 15h to reach 22:00."""
    now = _dt(7, 0)
    secs = pacing.sleep_seconds_until_within_hours(now, time(22, 0), time(6, 0))
    assert secs == 15 * 3600


def test_sleep_seconds_until_within_hours_overnight_before_end_returns_zero():
    """At 03:00, overnight window 22:00-06:00 is already active → 0s."""
    now = _dt(3, 0)
    assert pacing.sleep_seconds_until_within_hours(now, time(22, 0), time(6, 0)) == 0


def test_sleep_seconds_until_within_hours_after_daily_window_wraps_to_next_day():
    """At 20:00, daytime window 09:00-18:00 is done → sleep to tomorrow 09:00 = 13h."""
    now = _dt(20, 0)
    secs = pacing.sleep_seconds_until_within_hours(now, time(9, 0), time(18, 0))
    assert secs == 13 * 3600


# ─── apply_pacing (async) — the loop-level façade ──────────────────────

@pytest.mark.asyncio
async def test_apply_pacing_calls_asyncio_sleep_with_next_delay(monkeypatch):
    """Regular step (not divisible by long_pause_every_n): short jitter sleep."""
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    rng = random.Random(42)

    await pacing.apply_pacing(
        cfg,
        step_index=3,           # not a long-pause tick
        rng=rng,
        now=_dt(12, 0),
        work_hours=(time(9, 0), time(18, 0)),
    )

    assert len(slept) == 1
    assert cfg.min_delay <= slept[0] <= cfg.max_delay


@pytest.mark.asyncio
async def test_apply_pacing_uses_long_pause_when_step_is_multiple(monkeypatch):
    """Every long_pause_every_n steps: long_pause_seconds sleep instead."""
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    rng = random.Random(0)

    await pacing.apply_pacing(
        cfg,
        step_index=cfg.long_pause_every_n,   # exactly on a long-pause tick
        rng=rng,
        now=_dt(12, 0),
        work_hours=(time(9, 0), time(18, 0)),
    )

    assert len(slept) == 1
    # long pause is ~pace*10, well outside jitter range
    assert slept[0] > cfg.max_delay


@pytest.mark.asyncio
async def test_apply_pacing_blocks_until_work_hours_open(monkeypatch):
    """Outside window: first sleep = seconds until start; then normal jitter."""
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    rng = random.Random(0)

    # 07:00, window 09:00-18:00 → wait 2h, THEN normal jitter
    await pacing.apply_pacing(
        cfg,
        step_index=1,
        rng=rng,
        now=_dt(7, 0),
        work_hours=(time(9, 0), time(18, 0)),
    )

    assert len(slept) == 2
    assert slept[0] == 2 * 3600
    assert cfg.min_delay <= slept[1] <= cfg.max_delay


@pytest.mark.asyncio
async def test_apply_pacing_no_work_hours_means_no_gating(monkeypatch):
    cfg = pacing.PacingConfig(pace_seconds=5.0)
    slept = []
    monkeypatch.setattr("asyncio.sleep", lambda s: slept.append(s) or _noop())

    async def _noop():
        return

    monkeypatch.setattr("asyncio.sleep", lambda s: _record(slept, s))

    async def _record(bucket, s):
        bucket.append(s)
    # simpler: use a real async stub
    async def _fake_sleep(secs):
        slept.append(secs)
    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    slept.clear()

    await pacing.apply_pacing(
        cfg,
        step_index=1,
        rng=random.Random(0),
        now=_dt(3, 0),        # would be outside daytime hours
        work_hours=None,      # but no gating requested
    )
    assert len(slept) == 1                        # only jitter, no gate wait
