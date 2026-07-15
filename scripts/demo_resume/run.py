#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""demo_resume runner. See scripts/demo_resume/__init__.py for usage."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import time as dtime
from pathlib import Path

from scripts.common.resume import failure as failure_mod
from scripts.common.resume.pipeline import Stage, run_pipeline
from scripts.common.resume.sidecar import write_done_sidecar


_call_counter = {"n": 0}


def _tick_and_maybe_crash(crash_at: int | None) -> None:
    _call_counter["n"] += 1
    if crash_at is not None and _call_counter["n"] == crash_at:
        raise RuntimeError(f"demo crash on stage.run call #{crash_at}")


def _prod_fetch(w: Path, i: dict) -> Path:  return w / "fetch"  / f"{i['aweme_id']}.bin"
def _prod_probe(w: Path, i: dict) -> Path:  return w / "probe"  / f"{i['aweme_id']}.json"
def _prod_render(w: Path, i: dict) -> Path: return w / "render" / f"{i['aweme_id']}.md"


def _build_stages(crash_at: int | None) -> list[Stage]:
    async def fetch(w: Path, i: dict, p: Path):
        _tick_and_maybe_crash(crash_at)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"<video-blob id={i['aweme_id']}>".encode())
        write_done_sidecar(p, stage="fetch", extra={"size": p.stat().st_size})

    async def probe(w: Path, i: dict, p: Path):
        _tick_and_maybe_crash(crash_at)
        src = _prod_fetch(w, i)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"id": i["aweme_id"], "src_size": src.stat().st_size}, indent=2),
            encoding="utf-8",
        )
        write_done_sidecar(p, stage="probe")

    async def render(w: Path, i: dict, p: Path):
        _tick_and_maybe_crash(crash_at)
        meta = json.loads(_prod_probe(w, i).read_text(encoding="utf-8"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"# {i['aweme_id']}\n\nsrc_size = {meta['src_size']} bytes\n",
            encoding="utf-8",
        )
        write_done_sidecar(p, stage="render")

    return [
        Stage(name="fetch",  product_path=_prod_fetch,  run=fetch),
        Stage(name="probe",  product_path=_prod_probe,  run=probe),
        Stage(name="render", product_path=_prod_render, run=render),
    ]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="demo_resume")
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--items", type=int, default=5, help="Number of demo items.")
    p.add_argument("--crash-at", type=int, default=None,
                   help="Raise RuntimeError on the N-th stage.run() call.")
    p.add_argument("--pace-seconds", type=float, default=0.05)
    p.add_argument("--retry-failed", action="store_true",
                   help="Archive existing failed/ entries and re-run them.")
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args.workdir.mkdir(parents=True, exist_ok=True)
    items = [{"aweme_id": f"item{i:03d}"} for i in range(args.items)]

    if args.retry_failed:
        failed_dir = args.workdir / "failed"
        if failed_dir.exists():
            for entry in list(failed_dir.glob("*.json")):
                item_id = entry.stem
                archived = failure_mod.archive_failed_for_retry(item_id, args.workdir)
                print(f"retry-failed: {item_id} archived={archived}")

    result = await run_pipeline(
        workdir=args.workdir,
        items=items,
        stages=_build_stages(args.crash_at),
        pace_seconds=args.pace_seconds,
    )
    print(f"processed={result.processed}  skipped={result.skipped}  failed={result.failed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_amain(args))


if __name__ == "__main__":                # pragma: no cover
    sys.exit(main())
