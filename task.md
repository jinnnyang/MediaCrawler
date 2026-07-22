---
kind: task
version: 1
last_updated: 2026-07-22T01:20:00+00:00
last_verified: 2026-07-22T01:20:00+00:00
last_agent: hermes-devops
last_writer: hand-off
session_id: sess-mc-maint-20260722
status: in-progress
---

# Current Tasks

Session goal: routine maintenance sweep on the MediaCrawler fork — sync upstream, tidy working tree, prove out the Douyin incremental-crawl fixes.

- `[x]` Discard local uncommitted changes in `config/base_config.py` (three runtime-only knobs) — done, then re-applied for this session's run (see below).
- `[x]` Merge `upstream/main` into local `upstream` branch (fast-forward, 2 doc-only commits: `84907f0` + `04da7f3`). NOT yet pushed to `origin/upstream`.
- `[x]` Verify Douyin test environment: uv sync clean (90 pkgs), Playwright 1.61 chromium present, CDP Chrome path exists, `browser_data/cdp_dy_user_data_dir` retains login.
- `[x]` Fix creator-mode `--crawler_max_notes_count N` no-op: added `max_notes` early-stop to `DouYinClient.get_all_user_aweme_posts` and threaded it through `DouYinCrawler.get_creators_and_videos`. Verified on smoke run — 5 awemes processed then `reached max_notes=5, stopping early`.
- `[x]` Update `config/dy_config.py::DY_CREATOR_ID_LIST` to correct 小金财经 URL after discovering the old sec_user_id had drifted to a different creator.
- `[x]` Root-cause the `httpx brotli DecodingError` on first run: `PYTHONPATH` leak from Hermes desktop pointed `uv run` at the wrong venv. Documented in context.md; workaround = `unset PYTHONPATH` per session.
- `[x]` Verify incremental skip-HTTP branch: run against 小金财经 with 5-cap; got 1 skip hit on `7658338456867425871`, 4 new downloads, jsonl written.

- `[/]` Pending user decision on wrap-up: commit strategy for the 4 modified files (`base_config.py` runtime knobs vs `dy_config.py` creator URL vs `client.py`/`core.py` real code fix).
- `[ ]` OPTIONAL follow-up: full unbounded creator run to measure real skip-HTTP hit rate (~200 existing dirs → expect ~200/(200+new) ratio).
- `[ ]` OPTIONAL follow-up: harden `dy_client.get_aweme_media` against mid-stream CDN drops (saw one `peer closed connection without sending complete message body` during smoke run — inner retry masked it, but worth confirming retry policy).
- `[ ]` OPTIONAL follow-up: push local `upstream` branch to `origin/upstream` once user is comfortable.

> [!] All follow-ups assume next session again `unset PYTHONPATH` before `uv run main.py …`, or ideally wraps this into a helper script.
