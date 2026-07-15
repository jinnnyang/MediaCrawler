# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/scripts/probe_comment_full_headers.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""Capture ALL request headers Chrome sends for /comment/list via CDP Network domain.

Fetch init only shows headers set by JS. Chrome adds many default headers
(Cookie, User-Agent, Sec-Fetch-*, bd-ticket-guard-*, etc.). Use CDP
Network.requestWillBeSentExtraInfo to see the final headers on the wire.

Prereq: user has an open douyin video tab with comments loaded.
Run this BEFORE scrolling for more comments — it will listen for 30s,
then dump all captured /comment/list requests with FULL headers.
"""
import asyncio
import json
import urllib.request
from pathlib import Path

from websockets.client import connect  # type: ignore


async def main():
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
    pages = [t for t in targets if t["type"] == "page" and "douyin.com" in t.get("url", "")]
    if not pages:
        print("no douyin tab")
        return
    page = pages[0]
    print(f"listening on: {page['title']!r} @ {page['url']}\n")

    captured = {}  # requestId -> {url, headers}

    async with connect(page["webSocketDebuggerUrl"], max_size=200 * 1024 * 1024) as ws:
        # Enable Network domain
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        # Drain until response for id=1
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == 1:
                break

        print(">>> Network domain enabled. Now scroll the comment area, or click a new video with comments. <<<")
        print(">>> Listening for 45 seconds... <<<\n")

        try:
            end_time = asyncio.get_event_loop().time() + 45
            while asyncio.get_event_loop().time() < end_time:
                remaining = end_time - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method == "Network.requestWillBeSentExtraInfo":
                    rid = params.get("requestId")
                    headers = params.get("headers", {})
                    if rid not in captured:
                        captured[rid] = {"headers": headers}
                    else:
                        captured[rid]["headers"] = headers
                elif method == "Network.requestWillBeSent":
                    rid = params.get("requestId")
                    url = params.get("request", {}).get("url", "")
                    if "/comment/list" in url:
                        if rid not in captured:
                            captured[rid] = {}
                        captured[rid]["url"] = url
                        captured[rid]["method"] = params.get("request", {}).get("method")
                        captured[rid]["referrer"] = params.get("request", {}).get("headers", {}).get("Referer") or params.get("documentURL")
        except Exception as e:
            print(f"listen error: {e}")

    # Filter to only comment requests
    comment_reqs = {k: v for k, v in captured.items() if "url" in v}
    print(f"\n[done] captured {len(comment_reqs)} /comment/list request(s)\n" + "=" * 80)

    for i, (rid, info) in enumerate(comment_reqs.items(), 1):
        print(f"\n--- [{i}] {info.get('method')} {info.get('url', '?')[:400]}")
        print(f"referrer: {info.get('referrer')!r}")
        print(f"FULL HEADERS ({len(info.get('headers', {}))}):")
        for k, v in sorted(info.get("headers", {}).items()):
            v_str = str(v)
            if len(v_str) > 200:
                v_str = v_str[:120] + f"...[+{len(v_str) - 120} chars]"
            print(f"  {k}: {v_str}")

    out = Path(r"C:\Users\jinnn\Documents\MediaCrawler\data\real-comment-full-headers.json")
    out.write_text(json.dumps(comment_reqs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] {out}")


asyncio.run(main())
