#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""demo_resume — minimal 3-stage pipeline showing all resume primitives.

Stages (all fake, no network):
  1. fetch   — write a "video" blob to <workdir>/fetch/<id>.bin
  2. probe   — write a JSON metadata sidecar to <workdir>/probe/<id>.json
  3. render  — write a markdown summary to <workdir>/render/<id>.md

Simulate crashes with `--crash-at N`: the N-th stage.run() call raises
RuntimeError. Re-run without --crash-at → resumes from the first
incomplete stage per item.

Usage:
    python -m scripts.demo_resume.run --workdir ./demo-wd --items 5
    # crash at 4th stage.run() call
    python -m scripts.demo_resume.run --workdir ./demo-wd --items 5 --crash-at 4
    # inspect
    python -m scripts.mc_status --workdir ./demo-wd --stages fetch,probe,render
    # retry the failed items (archive them first)
    python -m scripts.demo_resume.run --workdir ./demo-wd --items 5 --retry-failed
"""
from __future__ import annotations
