# -*- coding: utf-8 -*-
"""Anti-detection pacing (PRD-001 §4.5).

## Single-knob design

Users only tune `--pace-seconds P`. All other parameters (jitter bounds,
long-pause frequency, long-pause length, work hours) derive from `P` via
fixed behavioral ratios, so tuning stays intuitive:

- `P=2`   fast burst pace (~2s per item)
- `P=5`   default human pace
- `P=30`  low-and-slow / overnight friendly

Ratios were picked from human-behavior heuristics:
- jitter is normal-distributed with σ = P/3, clipped to [P/2, 2P]
- long pause every 25 items × (P/5)   (fewer pauses when pace is slow)
- long pause length is 10× a single normal delay
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PacingConfig:
    """Derived pacing parameters. Only `pace_seconds` is a real input."""
    pace_seconds: float
    # derived — never set by callers directly
    min_delay: float = field(init=False)
    max_delay: float = field(init=False)
    sigma: float = field(init=False)
    long_pause_every_n: int = field(init=False)
    long_pause_seconds: float = field(init=False)

    def __post_init__(self):
        if self.pace_seconds <= 0:
            raise ValueError(f"pace_seconds must be > 0, got {self.pace_seconds}")
        # frozen dataclass — set fields via object.__setattr__
        p = float(self.pace_seconds)
        object.__setattr__(self, "min_delay", p * 0.5)
        object.__setattr__(self, "max_delay", p * 2.0)
        object.__setattr__(self, "sigma", p / 3.0)
        # every ~25 items at pace=5s → every ~5 items at pace=25s (rarer as pace grows)
        object.__setattr__(self, "long_pause_every_n", max(5, int(round(25 * (5.0 / p)))))
        object.__setattr__(self, "long_pause_seconds", p * 10.0)


def next_delay(cfg: PacingConfig, rng: random.Random) -> float:
    """Normal-distributed delay clipped to [min_delay, max_delay].

    Deterministic given a seeded `rng` → allows reproducible mocked runs
    (needed by §7.3 SIGTERM fuzz tests).
    """
    raw = rng.normalvariate(cfg.pace_seconds, cfg.sigma)
    return max(cfg.min_delay, min(cfg.max_delay, raw))


# ─── work-hours window (v1.0 E1: cross-midnight support) ────────────────

def is_within_hours(now: datetime, start: time, end: time) -> bool:
    """Return True iff `now.time()` is inside the window `[start, end)`.

    Behaviors (v1.0 E1):
      - `start == end`: treated as "always on" (no restriction).
      - `start < end`:  same-day window. True iff start <= t < end.
      - `start > end`:  crosses midnight (e.g. 22:00-06:00).
        True iff t >= start OR t < end.
    """
    t = now.time()
    if start == end:
        return True
    if start < end:
        return start <= t < end
    # cross-midnight window
    return t >= start or t < end


def sleep_seconds_until_within_hours(
    now: datetime, start: time, end: time
) -> int:
    """Seconds to sleep until the next `[start, end)` window opens.

    Returns 0 if `now` is already inside the window (including cross-midnight
    windows already active). If the window is "always on" (start == end),
    also returns 0.
    """
    if is_within_hours(now, start, end):
        return 0

    # Not in window → next open is today's `start` if still in the future,
    # otherwise tomorrow's `start`. Works for both same-day and cross-midnight
    # windows: when outside a cross-midnight window, we are strictly between
    # `end` (morning) and `start` (evening) of the SAME calendar day.
    today_start = datetime.combine(now.date(), start)
    if today_start > now:
        target = today_start
    else:
        target = datetime.combine(now.date() + timedelta(days=1), start)
    return int((target - now).total_seconds())


# ─── async façade for the pipeline main loop ───────────────────────────

async def apply_pacing(
    cfg: PacingConfig,
    *,
    step_index: int,
    rng: random.Random,
    now: datetime,
    work_hours: Optional[tuple[time, time]] = None,
) -> None:
    """Sleep between items, respecting long-pause cadence and work hours.

    Call order per item:
      1. If `work_hours` is given AND we're outside it: sleep until it opens.
      2. If `step_index % long_pause_every_n == 0` (and step_index > 0):
         sleep `long_pause_seconds` (fixed, no jitter — deliberate rest tick).
      3. Otherwise: sleep `next_delay(cfg, rng)`.

    `now` and `rng` are injected so tests can pin behavior; production
    call sites pass `datetime.now()` and a module-level `random.Random()`.
    """
    if work_hours is not None:
        gate_wait = sleep_seconds_until_within_hours(now, work_hours[0], work_hours[1])
        if gate_wait > 0:
            _logger.info(
                "Outside work-hours %s-%s; sleeping %ds",
                work_hours[0], work_hours[1], gate_wait,
            )
            await asyncio.sleep(gate_wait)

    if step_index > 0 and step_index % cfg.long_pause_every_n == 0:
        _logger.info("Long pause at step=%d: %.1fs", step_index, cfg.long_pause_seconds)
        await asyncio.sleep(cfg.long_pause_seconds)
    else:
        await asyncio.sleep(next_delay(cfg, rng))
