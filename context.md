---
kind: context
version: 1
last_updated: 2026-07-22T01:20:00+00:00
last_verified: 2026-07-22T01:20:00+00:00
last_agent: hermes-devops
last_writer: hand-off
session_id: sess-mc-maint-20260722
status: in-progress
---

# Project Invariants & Context

> [!NOTE]
> This file contains invariants, credentials locations, environmental variables, and project constraints that must never break.
> This document is strictly additive-only. Add corrections as new dated entries at the bottom.
>
> **Provenance-tag every invariant** (see PROTOCOL §11a). Prefix each bullet with `[git:<sha>]`, `[user:<YYYY-MM-DD>]`, `[test:<name>]`, `[inferred:<session-id>]`, or `[unknown]`.

## Project Description
- [git:96dcd25] Fork of NanmiCoder/MediaCrawler at `github.com/jinnnyang/MediaCrawler`; `origin/main` carries local Douyin fixes + a PRD-001 resume subsystem, `origin/upstream` tracks vanilla upstream.
- [user:2026-07-22] Primary usage is Douyin creator-mode incremental crawling (video download) on Windows with CDP-driven local Chrome.

## Invariants & Rules
- [git:96dcd25] Douyin video-download idempotency has TWO layers: (1) `DouYinCrawler.get_aweme_video` checks `data/douyin/videos/<aweme_id>/video.mp4` and skips the HTTP call entirely; (2) `DouYinVideo.save_video` re-checks before writing. Both layers MUST mirror `make_save_file_name` — `{video_store_path}/{aweme_id}/{extension_file_name}`.
- [git:b4f5a62] `/aweme/v1/web/aweme/detail/` is server-side blocked for scraped clients (empty body → `DataFetchError "account blocked"`). Creator mode consumes list-endpoint (`/aweme/v1/web/aweme/post/`) aweme objects directly — do NOT reintroduce per-item detail fetch on the creator path.
- [user:2026-07-22] Douyin `sec_user_id` values can be REASSIGNED by抖音 over time. A previously verified sec_user_id may later return a completely different creator's data. Always sanity-check nickname / titles against expected content before trusting a re-run.
- [git:HEAD] Repo layout has two `test/` roots (top-level) plus a `tests/` root; do NOT collapse them without checking `conftest.py` and CI targeting.

## Environment & Build
- [user:2026-07-22] Python pinned to 3.11 (.python-version), managed via uv. Project venv is `.venv/` at repo root; `uv sync --frozen` reports `Checked 90 packages`.
- [user:2026-07-22] Playwright 1.61.0 with chromium 1161/1200/1208/1228 already downloaded under `~/AppData/Local/ms-playwright/`.
- [user:2026-07-22] CDP mode uses `C:\Programs\Chrome\146.0.7680.165\windows\amd64\App\Chrome-bin\chrome.exe`. Config default (`C:\Symbols\Chrome\App\Chrome-bin\chrome.exe`) is stale and does not exist on this machine — must be overridden in `base_config.py::CUSTOM_BROWSER_PATH` locally without committing.
- [user:2026-07-22] Persistent login state lives in `browser_data/cdp_dy_user_data_dir` (~358 MB). To reuse it, set `CDP_CONNECT_EXISTING=False` (else it connects to an already-open Chrome instead of launching one against `user_data_dir`).
- [inferred:sess-mc-maint-20260722] Hermes-desktop terminal sets `PYTHONPATH=C:\Users\jinnn\AppData\Local\hermes\hermes-agent;…\venv\Lib\site-packages` in the environment. `uv run` inherits it and Python resolves `httpx` from the Hermes venv (0.28.1 with a brotli-decoder crash on Douyin responses) instead of the project `.venv`. **Always `unset PYTHONPATH` before any `uv run main.py` in this repo.**
- [git:HEAD] `main.py --platform dy --type creator --lt cookie --crawler_max_notes_count N` — after the fix in this session, `N` genuinely caps the creator run (was a no-op before).
- [git:HEAD] Data outputs: `data/douyin/videos/<aweme_id>/video.mp4` (media) and `data/douyin/jsonl/creator_contents_YYYY-MM-DD.jsonl` (metadata, one file per calendar day of the run).

## Invariant Corrections Log
- 2026-07-22 [user:2026-07-22] `DY_CREATOR_ID_LIST` prior contents (`MS4wLjABAAAATJPY7LAlaa5X-c8uNdWkvz0jUGgpw4eeXIwu_8BhvqE`) were re-labelled "小金财经" earlier but on this date resolved to a different creator (阿江-Relakkes). The correct 小金财经 URL is now `https://www.douyin.com/user/MS4wLjABAAAA1IaCXuEoeZGeehsJczgJi8mHlIqJubxq0-ELkTIlIos`. Existing `data/douyin/videos/` 200 dirs were all downloaded under the old (still-小金财经-then) sec_user_id and remain valid.
