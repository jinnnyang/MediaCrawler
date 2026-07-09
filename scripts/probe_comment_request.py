"""Install an XHR/fetch hook via CDP into the current Douyin tab.

Captures ONLY comment-related requests (`/comment/list` and `/comment/list/reply`).
Records: full URL, method, request headers, request body, response status.
Stores into window._commentLog for later retrieval.
"""
import asyncio
import json
import urllib.request

from websockets.client import connect  # type: ignore


HOOK_JS = r"""
(() => {
  // Allow forced re-install by clearing the flag (we call this with force=true below)
  if (window._commentHookInstalled && !window._commentHookForce) return "already installed";
  window._commentHookForce = false;
  window._commentHookInstalled = true;
  window._commentLog = [];

  const recordFetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = (init && init.method) || (typeof input === 'object' && input.method) || 'GET';
    if (!/\/aweme\/v1\/web\/comment\/list/.test(url)) return null;
    const entry = {
      source: 'fetch',
      url: url,
      method: method,
      req_headers: {},
      req_body: (init && init.body) ? String(init.body) : null,
      ts: Date.now(),
      status: null,
      resp_body_preview: null,
      error: null
    };
    // Serialize request headers
    try {
      if (init && init.headers) {
        if (init.headers instanceof Headers) {
          init.headers.forEach((v, k) => entry.req_headers[k] = v);
        } else {
          Object.assign(entry.req_headers, init.headers);
        }
      }
      // Also grab default Cookie/UA from the browser (Chrome adds these to fetch)
      if (typeof input === 'object' && input.headers) {
        input.headers.forEach((v, k) => entry.req_headers[k] = v);
      }
    } catch (e) {}
    window._commentLog.push(entry);   // record IMMEDIATELY on send
    console.log('[comment-hook] SEND', method, url.slice(0, 120));
    return entry;
  };

  const _fetch = window.fetch;
  window.fetch = async function(input, init) {
    const entry = recordFetch(input, init);
    try {
      const resp = await _fetch.apply(this, arguments);
      if (entry) {
        entry.status = resp.status;
        entry.resp_headers = {};
        try { resp.headers.forEach((v, k) => entry.resp_headers[k] = v); } catch (e) {}
        try {
          const clone = resp.clone();
          entry.resp_body_preview = (await clone.text()).slice(0, 500);
        } catch (e) { entry.resp_body_preview = '<clone-failed>'; }
      }
      return resp;
    } catch (e) {
      if (entry) entry.error = String(e);
      throw e;
    }
  };

  // XHR hook
  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  const _setHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function(method, url) {
    this._method = method;
    this._url = url;
    this._headers = {};
    return _open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(k, v) {
    if (this._headers) this._headers[k] = v;
    return _setHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    const url = this._url || '';
    if (/\/aweme\/v1\/web\/comment\/list/.test(url)) {
      const entry = {
        source: 'xhr',
        url: url,
        method: this._method,
        req_headers: this._headers || {},
        req_body: body ? String(body) : null,
        ts: Date.now(),
        status: null,
        resp_body_preview: null,
        error: null
      };
      window._commentLog.push(entry);
      console.log('[comment-hook][xhr] SEND', this._method, url.slice(0, 120));
      const _this = this;
      this.addEventListener('load', () => {
        entry.status = _this.status;
        entry.resp_body_preview = (_this.responseText || '').slice(0, 500);
      });
      this.addEventListener('error', () => { entry.error = 'xhr-error'; });
      this.addEventListener('abort', () => { entry.error = 'xhr-abort'; });
    }
    return _send.apply(this, arguments);
  };

  return "installed";
})();
"""


async def send_cdp(ws, method, params=None, msg_id=1):
    await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == msg_id:
            return msg


async def main():
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json").read())
    pages = [t for t in targets if t["type"] == "page" and "douyin.com" in t.get("url", "")]
    if not pages:
        print("NO DOUYIN PAGE — please navigate to douyin.com first")
        return
    page = pages[0]
    print(f"Installing hook on: {page['title']!r}")
    print(f"  URL: {page['url']}")

    async with connect(page["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024) as ws:
        # First flip the force-reinstall flag so the new hook overwrites any old one
        await send_cdp(ws, "Runtime.evaluate", {
            "expression": "window._commentHookForce = true; 'ok';",
            "returnByValue": True,
        }, msg_id=0)
        # Then evaluate the hook
        resp = await send_cdp(ws, "Runtime.evaluate", {
            "expression": HOOK_JS,
            "returnByValue": True,
        }, msg_id=1)
        result = resp.get("result", {}).get("result", {})
        print(f"  install status: {result.get('value')!r}")


asyncio.run(main())
