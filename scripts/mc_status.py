#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`mc-status` — read-only progress report for a resume workdir (PRD-001 §4.6).

Usage:
    python -m scripts.mc_status --workdir path/to/workdir --stages video,audio,md
    python -m scripts.mc_status --workdir path/to/workdir --stages video,audio --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from scripts.common.resume.status import build_status_report, format_status_report


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mc-status",
        description="Read-only progress report for a resume-pipeline workdir.",
    )
    p.add_argument("--workdir", required=True, type=Path, help="Pipeline workdir.")
    p.add_argument(
        "--stages",
        required=True,
        help="Comma-separated stage names in order, e.g. video,audio,md",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    workdir: Path = args.workdir

    if not workdir.exists() or not workdir.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    report = build_status_report(workdir, stages=stages)

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_status_report(report, workdir=workdir))
    return 0


if __name__ == "__main__":               # pragma: no cover - CLI entry
    sys.exit(main())
