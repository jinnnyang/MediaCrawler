# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/scripts/dump_comment_log.py
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

"""Dump captured comment requests from window._commentLog via CDP."""
import asyncio
import json
import urllib.request
from pathlib import Path

from websockets.client import connect  # type: ignore


async def send_cdp(ws, method, params, msg_id):
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg


async def main():
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
    pages = [t for t in targets if t["type"] == "page" and "douyin.com" in t.get("url", "")]
    if not pages:
        print("no douyin tab")
        return
    page = pages[0]
    print(f"dumping from: {page['title']!r} @ {page['url']}\n")

    async with connect(page["webSocketDebuggerUrl"], max_size=100 * 1024 * 1024) as ws:
        resp = await send_cdp(ws, "Runtime.evaluate", {
            "expression": "JSON.stringify(window._commentLog || [])",
            "returnByValue": True,
        }, msg_id=1)

    raw_json = resp["result"]["result"]["value"]
    log = json.loads(raw_json)
    # Drop synthetic probe requests
    log = [e for e in log if "_probe=1" not in e.get("url", "") and "probe_synthetic" not in e.get("url", "")]

    print(f"Captured {len(log)} comment request(s)\n" + "="*80)
    for i, e in enumerate(log, 1):
        print(f"\n--- [{i}] {e['method']} {e['source']}  status={e.get('status')!r}  error={e.get('error')!r}")
        print(f"URL: {e['url'][:400]}")
        if e.get("req_body"):
            print(f"BODY: {e['req_body'][:400]}")
        print(f"REQ HEADERS ({len(e.get('req_headers', {}))}):")
        for k, v in (e.get("req_headers") or {}).items():
            print(f"  {k}: {str(v)[:200]}")
        rb = e.get("resp_body_preview")
        if rb:
            print(f"RESP PREVIEW: {rb[:300]!r}")

    # Save full dump to file for later diff
    out = Path(r"C:\Users\jinnn\Documents\MediaCrawler\data\real-comment-requests.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] full dump -> {out}")


asyncio.run(main())
