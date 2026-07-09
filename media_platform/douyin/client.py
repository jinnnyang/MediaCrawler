# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/client.py
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

import asyncio
import copy
import json
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Dict, Union, Optional

import httpx
from playwright.async_api import BrowserContext

from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils
from tools.httpx_util import make_async_client
from var import request_keyword_var

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import *
from .field import *
from .help import *


class DouYinClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If the crawl media option is turned on, Douyin’s short videos will require a longer timeout.
        proxy=None,
        *,
        headers: Dict,
        playwright_page: Optional[Page],
        cookie_dict: Dict,
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://www.douyin.com"
        self.cookie_urls = [
            "https://douyin.com",
            self._host,
            "https://creator.douyin.com",
            "https://douhot.douyin.com",
            "https://live.douyin.com",
        ]
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        # 稳定的 19 位 webid：一次会话内固定，模拟真实浏览器的稳定设备 ID（不再每次请求随机）
        self._stable_webid = self._gen_stable_webid()
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    @staticmethod
    def _gen_stable_webid() -> str:
        """Generate a stable 19-digit web ID for the entire session lifetime.

        Real Douyin browsers use a persistent 19-digit webid across all API requests.
        Generating a fresh one per request (as the old get_web_id() did) is a strong
        bot fingerprint. Freeze it at client construction instead.
        """
        import random as _r
        return str(_r.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999))

    async def __process_req_params(
        self,
        uri: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        request_method="GET",
    ):

        if not params:
            return
        headers = headers or self.headers

        # 2026 更新：抖音已放弃 URL 层签名（a_bogus / msToken / X-Bogus）
        # 风控完全下沉到 uifid（服务端下发的持久设备指纹）+ webid（稳定会话 ID）+ cookie 里的 bd_ticket_guard_* / s_v_web_id
        # 参考：实测 /aweme/v1/web/general/search/single/ 等接口的真实浏览器请求均已不带 a_bogus
        # 因此这里删除 a_bogus 拼装，改为对齐真实浏览器指纹参数
        common_params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "170400",
            "version_name": "17.4.0",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "pc_libra_divert": "Windows",
            "support_h265": "1",
            "support_dash": "1",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            # 浏览器指纹：从 playwright 页面动态读取真实值，避免与实际浏览器不一致
            "browser_platform": await self.playwright_page.evaluate("() => navigator.platform"),
            "browser_name": await self.playwright_page.evaluate(
                "() => (navigator.userAgent.match(/(Firefox|Chrome|Safari|Edge)/) || ['Chrome'])[0]"
            ),
            "browser_version": await self.playwright_page.evaluate(
                "() => { const m = navigator.userAgent.match(/(Firefox|Chrome|Safari|Edge)\\/([\\d.]+)/); return m ? m[2] : '125.0.0.0'; }"
            ),
            "browser_online": "true",
            "engine_name": await self.playwright_page.evaluate(
                "() => navigator.userAgent.includes('Firefox') ? 'Gecko' : (navigator.userAgent.includes('WebKit') ? 'Blink' : 'Blink')"
            ),
            "engine_version": await self.playwright_page.evaluate(
                "() => { const m = navigator.userAgent.match(/(Firefox|Chrome|Safari|Edge)\\/([\\d.]+)/); return m ? m[2] : '125.0.0.0'; }"
            ),
            "os_name": await self.playwright_page.evaluate(
                "() => { const p = navigator.platform; if (p.includes('Win')) return 'Windows'; if (p.includes('Mac')) return 'Mac OS'; if (p.includes('Linux')) return 'Linux'; return 'Windows'; }"
            ),
            "os_version": await self.playwright_page.evaluate(
                "() => { const m = navigator.userAgent.match(/(Windows NT|Mac OS X|Linux) ([\\d_.]+)/); return m ? m[2].replace(/_/g, '.') : '10'; }"
            ),
            "cpu_core_num": str(await self.playwright_page.evaluate("() => navigator.hardwareConcurrency || 8")),
            "device_memory": str(await self.playwright_page.evaluate("() => navigator.deviceMemory || ''")),
            "platform": "PC",
            "screen_width": str(await self.playwright_page.evaluate("() => screen.width")),
            "screen_height": str(await self.playwright_page.evaluate("() => screen.height")),
            # 稳定设备指纹：从 cookie 里读服务端下发的 UIFID + s_v_web_id，替代原来每次随机的 get_web_id()
            # UIFID 是抖音 2024 新增的核心设备指纹，实测所有 web API 都带此参数
            "uifid": self.cookie_dict.get("UIFID") or self.cookie_dict.get("UIFID_TEMP") or "",
            "webid": self._stable_webid,
            # 2026-07-06 更新：评论接口 /aweme/v1/web/comment/list 对以下参数敏感，
            # 真实浏览器都会带；缺失时会触发 blocked。搜索接口不校验但带上也无害。
            "cut_version": "1",
            "pc_img_format": "webp",
            "effective_type": "4g",
            "downlink": "10",
            "round_trip_time": "50",
            "insert_ids": "",
            "whale_cut_token": "",
            "rcFT": "",
        }
        params.update(common_params)

        # 2026-07-06 关键发现：抖音评论接口除了 URL query 里的 uifid，
        # 还校验 HTTP 请求头 uifid。这是评论接口 blocked 的根因。
        # 参考实测：真实浏览器 XHR 显式设置 `uifid` header，服务端严格校验。
        uifid_val = self.cookie_dict.get("UIFID") or self.cookie_dict.get("UIFID_TEMP") or ""
        if uifid_val:
            headers["uifid"] = uifid_val

    async def request(self, method, url, **kwargs):
        # Check whether the proxy has expired before each request
        await self._refresh_proxy_if_expired()

        # 2026-07-09：评论接口（/comment/list, /comment/list/reply）需要 bd-ticket-guard-* 客户端签名头
        # （抖音服务端会做 P-256 椭圆曲线签名校验，Python 侧复现工作量极大）
        # 解决方案：把 fetch 委托给已注入到 playwright 页面的浏览器上下文，
        # Chrome 会自动加 bd-ticket-guard-client-data / ree-public-key 等头。
        # 参考：hermes-verify-plan-a-browser-fetch.py 已实测通过。
        if "/comment/list" in url and self.playwright_page is not None:
            return await self._browser_fetch_json(method, url, kwargs.get("params"), kwargs.get("headers"))

        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)
        try:
            if response.text == "" or response.text == "blocked":
                utils.logger.error(f"request params incrr, response.text: {response.text}")
                raise Exception("account blocked")
            return response.json()
        except Exception as e:
            raise DataFetchError(f"{e}, {response.text}")

    async def _browser_fetch_json(self, method: str, url: str,
                                   params: Optional[Dict] = None,
                                   headers: Optional[Dict] = None) -> Dict:
        """Delegate a request to the playwright page's fetch API.

        Chrome auto-populates bd-ticket-guard-* signature headers here,
        which抖音 evaluates server-side. Response is parsed as JSON.
        Falls back to raising DataFetchError on empty body (blocked).
        """
        import urllib.parse as _up
        # Convert absolute host URL into a same-origin path (so browser treats it as same-site)
        parsed = _up.urlparse(url)
        rel = parsed.path
        # Merge params into query string
        query_pairs = _up.parse_qsl(parsed.query, keep_blank_values=True)
        if params:
            for k, v in params.items():
                query_pairs.append((k, "" if v is None else str(v)))
        if query_pairs:
            rel = rel + "?" + _up.urlencode(query_pairs, doseq=True)

        # Only pass headers that are meaningful for browser fetch (Chrome adds the rest)
        # Skip Cookie/User-Agent/Host/Origin/Referer/Content-Type — browser fills them.
        browser_headers: Dict[str, str] = {"accept": "application/json, text/plain, */*"}
        if headers:
            for k, v in headers.items():
                if k.lower() in ("uifid",):
                    browser_headers[k] = v

        js = """
        async (args) => {
            try {
                const resp = await fetch(args.url, {
                    method: args.method,
                    headers: args.headers,
                    credentials: 'include'
                });
                const text = await resp.text();
                return { status: resp.status, text: text };
            } catch (e) {
                return { error: String(e) };
            }
        }
        """
        result = await self.playwright_page.evaluate(js, {
            "url": rel,
            "method": method,
            "headers": browser_headers,
        })
        if result.get("error"):
            raise DataFetchError(f"browser fetch error: {result['error']}")
        text = result.get("text", "")
        if not text or text == "blocked":
            utils.logger.error(f"[browser_fetch_json] empty/blocked. url={rel[:200]}")
            raise DataFetchError(f"account blocked, response.text: {text!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise DataFetchError(f"JSON decode failed: {e}, body={text[:200]!r}")

    async def get(self, uri: str, params: Optional[Dict] = None, headers: Optional[Dict] = None):
        """
        GET请求
        """
        await self.__process_req_params(uri, params, headers)
        headers = headers or self.headers
        return await self.request(method="GET", url=f"{self._host}{uri}", params=params, headers=headers)

    async def post(self, uri: str, data: dict, headers: Optional[Dict] = None):
        await self.__process_req_params(uri, data, headers)
        headers = headers or self.headers
        return await self.request(method="POST", url=f"{self._host}{uri}", data=data, headers=headers)

    async def pong(self, browser_context: BrowserContext) -> bool:
        # 优先认 localStorage.HasUserLogin（实测最可靠）
        local_storage = await self.playwright_page.evaluate("() => window.localStorage")
        if local_storage.get("HasUserLogin", "") == "1":
            return True

        # 2026 更新：抖音已废弃 LOGIN_STATUS cookie，改用 login_time 时间戳标记登录态
        _, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=self.cookie_urls,
        )
        return bool(cookie_dict.get("login_time")) or cookie_dict.get("LOGIN_STATUS") == "1"

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def search_info_by_keyword(
        self,
        keyword: str,
        offset: int = 0,
        search_channel: SearchChannelType = SearchChannelType.GENERAL,
        sort_type: SearchSortType = SearchSortType.GENERAL,
        publish_time: PublishTimeType = PublishTimeType.UNLIMITED,
        search_id: str = "",
    ):
        """
        DouYin Web Search API
        :param keyword:
        :param offset:
        :param search_channel:
        :param sort_type:
        :param publish_time: ·
        :param search_id: ·
        :return:
        """
        query_params = {
            'search_channel': search_channel.value,
            'enable_history': '1',
            'keyword': keyword,
            'search_source': 'tab_search',
            'query_correct_type': '1',
            'is_filter_search': '0',
            'from_group_id': '7378810571505847586',
            'offset': offset,
            'count': '15',
            'need_filter_settings': '1',
            'list_type': 'multi',
            'search_id': search_id,
        }
        if sort_type.value != SearchSortType.GENERAL.value or publish_time.value != PublishTimeType.UNLIMITED.value:
            query_params["filter_selected"] = json.dumps({"sort_type": str(sort_type.value), "publish_time": str(publish_time.value)})
            query_params["is_filter_search"] = 1
            query_params["search_source"] = "tab_search"
        referer_url = f"https://www.douyin.com/search/{keyword}?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get("/aweme/v1/web/general/search/single/", query_params, headers=headers)

    async def get_video_by_id(self, aweme_id: str) -> Any:
        """
        DouYin Video Detail API
        :param aweme_id:
        :return:
        """
        params = {"aweme_id": aweme_id}
        headers = copy.copy(self.headers)
        del headers["Origin"]
        res = await self.get("/aweme/v1/web/aweme/detail/", params, headers)
        return res.get("aweme_detail", {})

    async def get_aweme_comments(self, aweme_id: str, cursor: int = 0):
        """get note comments

        """
        uri = "/aweme/v1/web/comment/list/"
        params = {"aweme_id": aweme_id, "cursor": cursor, "count": 20, "item_type": 0}
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_sub_comments(self, aweme_id: str, comment_id: str, cursor: int = 0):
        """
            获取子评论
        """
        uri = "/aweme/v1/web/comment/list/reply/"
        params = {
            'comment_id': comment_id,
            "cursor": cursor,
            "count": 20,
            "item_type": 0,
            "item_id": aweme_id,
        }
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_aweme_all_comments(
        self,
        aweme_id: str,
        crawl_interval: float = 1.0,
        is_fetch_sub_comments=False,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ):
        """
        获取帖子的所有评论，包括子评论
        :param aweme_id: 帖子ID
        :param crawl_interval: 抓取间隔
        :param is_fetch_sub_comments: 是否抓取子评论
        :param callback: 回调函数，用于处理抓取到的评论
        :param max_count: 一次帖子爬取的最大评论数量
        :return: 评论列表
        """
        result = []
        comments_has_more = 1
        comments_cursor = 0
        while comments_has_more and len(result) < max_count:
            comments_res = await self.get_aweme_comments(aweme_id, comments_cursor)
            comments_has_more = comments_res.get("has_more", 0)
            comments_cursor = comments_res.get("cursor", 0)
            comments = comments_res.get("comments", [])
            if not comments:
                continue
            if len(result) + len(comments) > max_count:
                comments = comments[:max_count - len(result)]
            result.extend(comments)
            if callback:  # If there is a callback function, execute the callback function
                await callback(aweme_id, comments)

            await asyncio.sleep(crawl_interval)
            if not is_fetch_sub_comments:
                continue
            # Get secondary reviews
            for comment in comments:
                reply_comment_total = comment.get("reply_comment_total")

                if reply_comment_total > 0:
                    comment_id = comment.get("cid")
                    sub_comments_has_more = 1
                    sub_comments_cursor = 0

                    while sub_comments_has_more:
                        sub_comments_res = await self.get_sub_comments(aweme_id, comment_id, sub_comments_cursor)
                        sub_comments_has_more = sub_comments_res.get("has_more", 0)
                        sub_comments_cursor = sub_comments_res.get("cursor", 0)
                        sub_comments = sub_comments_res.get("comments", [])

                        if not sub_comments:
                            continue
                        result.extend(sub_comments)
                        if callback:  # If there is a callback function, execute the callback function
                            await callback(aweme_id, sub_comments)
                        await asyncio.sleep(crawl_interval)
        return result

    async def get_user_info(self, sec_user_id: str):
        uri = "/aweme/v1/web/user/profile/other/"
        params = {
            "sec_user_id": sec_user_id,
            "publish_video_strategy_type": 2,
            "personal_center_strategy": 1,
        }
        return await self.get(uri, params)

    async def get_user_aweme_posts(self, sec_user_id: str, max_cursor: str = "") -> Dict:
        uri = "/aweme/v1/web/aweme/post/"
        params = {
            "sec_user_id": sec_user_id,
            "count": 18,
            "max_cursor": max_cursor,
            "locate_query": "false",
            "publish_video_strategy_type": 2,
        }
        return await self.get(uri, params)

    async def get_all_user_aweme_posts(self, sec_user_id: str, callback: Optional[Callable] = None):
        posts_has_more = 1
        max_cursor = ""
        result = []
        while posts_has_more == 1:
            aweme_post_res = await self.get_user_aweme_posts(sec_user_id, max_cursor)
            posts_has_more = aweme_post_res.get("has_more", 0)
            max_cursor = aweme_post_res.get("max_cursor")
            aweme_list = aweme_post_res.get("aweme_list") if aweme_post_res.get("aweme_list") else []
            utils.logger.info(f"[DouYinClient.get_all_user_aweme_posts] get sec_user_id:{sec_user_id} video len : {len(aweme_list)}")
            if callback:
                await callback(aweme_list)
            result.extend(aweme_list)
        return result

    async def get_aweme_media(self, url: str) -> Union[bytes, None]:
        async with make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", url, timeout=self.timeout, follow_redirects=True)
                response.raise_for_status()
                if not response.reason_phrase == "OK":
                    utils.logger.error(f"[DouYinClient.get_aweme_media] request {url} err, res:{response.text}")
                    return None
                else:
                    return response.content
            except httpx.HTTPError as exc:  # some wrong when call httpx.request method, such as connection error, client error, server error or response status code is not 2xx
                utils.logger.error(f"[DouYinClient.get_aweme_media] {exc.__class__.__name__} for {exc.request.url} - {exc}")  # Keep the original exception type name for developers to debug
                return None

    async def resolve_short_url(self, short_url: str) -> str:
        """
        解析抖音短链接,获取重定向后的真实URL
        Args:
            short_url: 短链接,如 https://v.douyin.com/iF12345ABC/
        Returns:
            重定向后的完整URL
        """
        async with make_async_client(proxy=self.proxy, follow_redirects=False) as client:
            try:
                utils.logger.info(f"[DouYinClient.resolve_short_url] Resolving short URL: {short_url}")
                response = await client.get(short_url, timeout=10)

                # Short links usually return a 302 redirect
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get("Location", "")
                    utils.logger.info(f"[DouYinClient.resolve_short_url] Resolved to: {redirect_url}")
                    return redirect_url
                else:
                    utils.logger.warning(f"[DouYinClient.resolve_short_url] Unexpected status code: {response.status_code}")
                    return ""
            except Exception as e:
                utils.logger.error(f"[DouYinClient.resolve_short_url] Failed to resolve short URL: {e}")
                return ""
