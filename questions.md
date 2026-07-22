---
kind: questions
version: 1
last_updated: '2026-07-22T01:22:06+00:00'
last_verified: 2026-07-22 01:20:00+00:00
last_agent: hermes-devops
last_writer: hand-off
session_id: sess-mc-maint-20260722
status: in-progress
---

# Questions

> [!NOTE]
> `## Open` = active questions awaiting input. `## Closed` = archived, permanent.
> `<!-- resolved -->` on an Open entry → next hand-off moves it to Closed.

## Open

### Q1 · Commit strategy for the four modified files at hand-off time

Currently unstaged:
- `config/base_config.py` — runtime-local knobs (Chrome path, `CDP_CONNECT_EXISTING`, `ENABLE_GET_MEIDAS`) that shouldn't be tracked.
- `config/dy_config.py` — real config fix (correct 小金财经 URL).
- `media_platform/douyin/client.py` — real code fix (`max_notes` early stop in `get_all_user_aweme_posts`).
- `media_platform/douyin/core.py` — thread `max_notes=config.CRAWLER_MAX_NOTES_COUNT` at the call site.

Options for the next session (or the user on wrap-up):

- A. Split-commit: one commit for the real code fix (client + core), one for the dy_config URL, and discard the base_config runtime knobs.
- B. Same as A, but move base_config runtime knobs into `.env` / local overlay so future sessions don't have to re-apply them by hand.
- C. Leave everything unstaged and revisit next session.

### Q2 · `origin/upstream` push

Local `upstream` branch is 2 commits ahead of `origin/upstream` (both doc-only fast-forwards from NanmiCoder). Push?

### Q3 · CDN mid-stream drops in `dy_client.get_aweme_media`

Saw one `peer closed connection without sending complete message body (received 4094939 bytes, expected 19361398)` in the smoke run. The next retry recovered the file, but this warrants a look at the retry policy in `client.py` before it silently fails on a larger corpus (retries hard-coded? exponential? bounded?).

## Closed

- None.
