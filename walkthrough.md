---
kind: walkthrough
version: 1
last_updated: '2026-07-22T01:22:06+00:00'
last_verified: 2026-07-22 01:20:00+00:00
last_agent: hermes-devops
last_writer: hand-off
session_id: sess-mc-maint-20260722
status: in-progress
---

# Living Work Memory & Walkthrough

> [!NOTE]
> Entry header format: `## YYYY-MM-DD — <slug>`. Lifecycle: `<!-- keep -->` or keyword `lesson`/`surprise`/`decision`/`invariant` → KEEP. `<!-- resolved -->` → CLEAR next hand-off. Age > 30d untagged + unreferenced → STALE.

## History of Active Entries

## 2026-07-22 — Douyin creator-mode maintenance sweep (decision + surprise + invariant)

**Scope.** Routine maintenance: sync upstream, clean working tree, exercise the two Douyin incremental fixes (`b4f5a62` list-endpoint bypass, `96dcd25` skip-HTTP-when-mp4-exists) on a real creator.

**Actions.**
1. Discarded three uncommitted knobs in `config/base_config.py` (`CUSTOM_BROWSER_PATH`, `CDP_CONNECT_EXISTING`, `ENABLE_GET_MEIDAS`) that are runtime-local and don't belong in tracked config.
2. Fast-forwarded local `upstream` branch to `upstream/main` (`076dcba..04da7f3`, +2 doc-only commits: Bloome sponsor table + Readme update). Not yet pushed to `origin/upstream` — awaiting user OK.
3. Environment audit: uv sync frozen clean (90 pkgs). Playwright 1.61 with 4 chromium versions cached. `.venv/Scripts/python.exe = 3.11.11`. CDP Chrome at `C:\Programs\Chrome\146.0.7680.165\...` exists. `browser_data/cdp_dy_user_data_dir` = 358 MB (persistent Douyin login).
4. Re-applied the three base_config runtime knobs for this session and set `CRAWLER_TYPE`/`PLATFORM` via CLI (`--platform dy --type creator`).

**Surprises.**
- **`--crawler_max_notes_count N` is a no-op in creator mode.** `DouYinClient.get_all_user_aweme_posts` walks `while has_more == 1` with no cap. Fixed: added `max_notes: Optional[int]` param that truncates the current page's list to fit the remaining budget and breaks the loop once `len(result) >= max_notes`. Threaded `config.CRAWLER_MAX_NOTES_COUNT` through from `get_creators_and_videos`. Also added `List` to `typing` imports.
- **Douyin sec_user_id drift.** The `sec_user_id` stored in `DY_CREATOR_ID_LIST` (`MS4wLjABAAAATJPY7...vqE`) was 小金财经's at the time of the original 07-15 download but today resolves to a different creator (阿江-Relakkes / MediaCrawler author). We got 24 videos into an unwanted download before the mismatch was spotted (aweme titles about GPT-5.5/Claude Opus etc. — not financial). Killed the run, deleted the 24 new dirs, dropped `data/douyin/jsonl/creator_contents_2026-07-22.jsonl`. User supplied the correct 小金财经 URL.
- **`httpx.DecodingError: brotli: decoder process called with data when 'can_accept_more_data()' is False`** on first fixed run. Root cause: Hermes desktop terminal exports `PYTHONPATH=…\hermes-agent;…\venv\Lib\site-packages`, which `uv run` inherits. Python resolved `httpx` from Hermes' venv (0.28.1 there too, but paired with a `brotlicffi` that trips on Douyin's specific brotli framing). Workaround: `unset PYTHONPATH` before `uv run main.py`. Verified via `uv run python -c "import httpx; print(httpx.__file__)"` before/after unset — resolves from `.venv/Lib/site-packages/httpx` when clean.

**Decision — commit strategy (deferred to Step 4).**
Modified files at hand-off time:
- `config/base_config.py` — runtime-local knobs, should be discarded before commit (or the two-commit split moved to `.env` / local overlay).
- `config/dy_config.py` — correct 小金财经 URL, worth committing.
- `media_platform/douyin/client.py` — real code fix (`max_notes` early stop). Worth committing.
- `media_platform/douyin/core.py` — wiring for `max_notes`. Worth committing.

**Verification of the two incremental fixes on real traffic.**
Ran `main.py --platform dy --type creator --lt cookie --crawler_max_notes_count 5 --get_comment no` against the correct 小金财经. Log confirms:
- `[DouYinClient.get_all_user_aweme_posts] get sec_user_id:MS4wLjABAAAA1IaCXu... video len : 5` — early-stop working (would have been 18 without the fix).
- `[DouYinCrawler.get_aweme_video] mp4 already downloaded, skip HTTP: data\douyin\videos\7658338456867425871\video.mp4` — skip-HTTP branch hit for one older aweme.
- 4 new mp4s written for content published 07-19 through 07-22; jsonl metadata rows appended to `data/douyin/jsonl/creator_contents_2026-07-22.jsonl`.
- `[DouYinClient.get_all_user_aweme_posts] reached max_notes=5, stopping early` — clean exit before the second page.

Non-fatal noise: one CDN `peer closed connection without sending complete message body (received 4094939 bytes, expected 19361398)` during a video download. The inner retry recovered on the next attempt; worth verifying the retry policy in `dy_client.get_aweme_media` before trusting it silently.

**Files touched this session.**
- `config/base_config.py` — 3 lines (runtime knobs, currently unstaged, discardable).
- `config/dy_config.py` — DY_CREATOR_ID_LIST replaced with the correct 小金财经 URL.
- `media_platform/douyin/client.py` — signature change: `get_all_user_aweme_posts(..., max_notes=None)` + early-stop loop; `typing.List` added.
- `media_platform/douyin/core.py` — threads `max_notes=config.CRAWLER_MAX_NOTES_COUNT` at the call site with a paragraph-length comment.
- `data/douyin/videos/` — cleaned 24 mis-attributed dirs; net +3 correct new videos vs baseline 200.
- `data/douyin/jsonl/creator_contents_2026-07-22.jsonl` — deleted the mis-attributed file; regenerated fresh with 5-cap 小金财经 run.
- Local `upstream` branch fast-forwarded to `04da7f3` (not pushed).
