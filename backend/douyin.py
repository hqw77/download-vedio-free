"""抖音视频解析与下载.

流程: 短链重定向 -> 提取 video_id -> 公开 API / 分享页 -> 无水印播放地址 -> 下载.
对外提供:
- is_douyin_url(url): 判断是否抖音链接
- DouyinParser.parse(url): 解析视频信息
- DouyinParser.download(url): 下载视频
依赖: requests.
"""

import base64
import json
import hashlib
import os
import re
import time
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger("douyin")

DEFAULT_HEADERS = {  # 默认请求头
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.douyin.com/",
}

MOBILE_HEADERS = {  # 移动端请求头, 用于分享页
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)  # 从分享文案中提取 URL


def is_douyin_url(url: str) -> bool:
    """判断 URL 是否属于抖音域名."""
    douyin_domains = [
        "douyin.com", "iesdouyin.com", "v.douyin.com",
        "www.douyin.com", "m.douyin.com",
    ]
    try:
        # 取域名
        host = urlparse(url).netloc.lower()
        # 匹配抖音域名
        return any(d in host for d in douyin_domains)
    except Exception:
        return False


class DouyinParser:
    """抖音视频解析器, 无需 Cookie."""

    API_URL = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"  # 公开视频信息接口

    def __init__(self, download_dir: str = "downloads"):
        """初始化下载目录, 请求会话, 超时和重试次数."""
        # 下载目录
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # 复用连接
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        # (连接超时, 读取超时)
        self.timeout = (10, 30)
        # 最大重试次数
        self.max_retries = 3

    def parse(self, url: str) -> dict:
        """解析抖音视频信息, 返回统一格式."""
        # 1. 从分享文案中提取 URL
        share_url = self._extract_url(url)
        # 2. 跟随短链重定向
        resolved_url = self._resolve_redirect(share_url)
        # 3. 提取视频 ID
        video_id = self._extract_video_id(resolved_url)

        # 4. 获取视频信息
        item_info = self._fetch_item_info(video_id, resolved_url)
        # 5. 转成统一格式
        return self._build_result(item_info, video_id)

    def download(self, url: str, mode: str = "video") -> dict:
        """下载抖音视频到本地, 返回文件信息."""
        # 解析视频信息
        share_url = self._extract_url(url)
        resolved_url = self._resolve_redirect(share_url)
        video_id = self._extract_video_id(resolved_url)

        item_info = self._fetch_item_info(video_id, resolved_url)
        # 获取播放地址
        media_url = self._get_media_url(item_info, mode)
        title = item_info.get("desc") or f"douyin_{video_id}"
        # 清理标题中的非法字符
        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t#@]', "_", title).strip("_. ")[:60]
        safe_title = re.sub(r'_+', '_', safe_title)
        if not safe_title:
            safe_title = f"douyin_{video_id}"

        # 视频/音频后缀
        ext = ".mp4" if mode == "video" else ".mp3"
        filename = f"{safe_title}{ext}"
        filepath = self.download_dir / filename

        # 下载文件
        self._download_file(media_url, filepath)

        return {
            "filepath": str(filepath),
            "filename": filename,
            "title": title,
            "ext": ext.lstrip("."),
        }

    def _extract_url(self, text: str) -> str:
        """从文本中提取第一个 URL."""
        match = _URL_PATTERN.search(text)
        if not match:
            raise ValueError("未找到有效的抖音链接")
        # 去掉首尾引号
        candidate = match.group(0).strip().strip('"').strip("'")
        # 去掉末尾标点
        return candidate.rstrip(").,;!?")

    def _resolve_redirect(self, share_url: str) -> str:
        """跟随短链重定向, 返回最终 URL."""
        for attempt in range(self.max_retries):
            try:
                # 请求短链
                resp = self.session.get(
                    share_url, timeout=self.timeout,
                    allow_redirects=True, headers=DEFAULT_HEADERS,
                )
                resp.raise_for_status()
                # 返回重定向后的地址
                return resp.url
            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"链接解析失败: {e}")
                # 指数退避
                time.sleep(1 * (2 ** attempt))
        raise ValueError("链接解析失败")

    def _extract_video_id(self, url: str) -> str:
        """从 URL 中提取视频 ID."""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        # 从查询参数取
        for key in ("modal_id", "item_ids", "group_id", "aweme_id"):
            values = query.get(key)
            if values:
                match = re.search(r"(\d{8,24})", values[0])
                if match:
                    return match.group(1)

        # 从路径取
        for pattern in (r"/video/(\d{8,24})", r"/note/(\d{8,24})", r"/(\d{8,24})(?:/|$)"):
            match = re.search(pattern, parsed.path)
            if match:
                return match.group(1)

        # 从整串数字兜底
        fallback = re.search(r"(\d{15,24})", url)
        if fallback:
            return fallback.group(1)

        raise ValueError("无法从链接中提取视频ID")

    def _fetch_item_info(self, video_id: str, resolved_url: str) -> dict:
        """获取视频元数据: 先公开 API, 失败再解析分享页."""
        try:
            return self._fetch_via_api(video_id)
        except Exception as e:
            # 公开 API 失败, 走分享页兜底
            logger.warning("公开API获取失败(%s)，尝试分享页解析", e)
            return self._fetch_via_share_page(video_id, resolved_url)

    def _fetch_via_api(self, video_id: str) -> dict:
        """通过公开 API 获取视频信息."""
        # 请求参数
        params = {"item_ids": video_id}
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    self.API_URL, params=params, timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("item_list") or []
                # 返回第一条
                if items:
                    return items[0]
                raise ValueError("API 返回空数据")
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                # 指数退避
                time.sleep(1 * (2 ** attempt))
        raise ValueError("API 请求失败")

    def _fetch_via_share_page(self, video_id: str, resolved_url: str) -> dict:
        """从分享页 HTML 中提取视频信息."""
        parsed = urlparse(resolved_url)
        # 构造分享页地址
        if "iesdouyin.com" in (parsed.netloc or ""):
            share_url = resolved_url
        else:
            share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"

        # 请求页面
        resp = self.session.get(share_url, headers=MOBILE_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        html = resp.text or ""

        # 遇到 WAF 验证
        if "Please wait..." in html and "wci=" in html and "cs=" in html:
            html = self._solve_waf_and_retry(html, share_url)

        # 提取页面中的路由数据
        router_data = self._extract_router_data(html)
        if not router_data:
            raise ValueError("无法从分享页提取数据")

        # 遍历 loaderData 找 item_list
        loader_data = router_data.get("loaderData", {})
        for node in loader_data.values():
            if not isinstance(node, dict):
                continue
            video_info_res = node.get("videoInfoRes", {})
            if not isinstance(video_info_res, dict):
                continue
            item_list = video_info_res.get("item_list", [])
            if item_list and isinstance(item_list[0], dict):
                return item_list[0]

        raise ValueError("分享页中未找到视频信息")

    def _solve_waf_and_retry(self, html: str, page_url: str) -> str:
        """处理 WAF 挑战, 写入 cookie 后重试."""
        # 提取 cookie 名和挑战数据
        match = re.search(r'wci="([^"]+)"\s*,\s*cs="([^"]+)"', html)
        if not match:
            return html

        cookie_name, challenge_blob = match.groups()
        try:
            # 解码挑战数据
            decoded = self._decode_b64(challenge_blob).decode("utf-8")
            challenge_data = json.loads(decoded)
            prefix = self._decode_b64(challenge_data["v"]["a"])
            expected = self._decode_b64(challenge_data["v"]["c"]).hex()
        except (KeyError, ValueError):
            return html

        # 搜索满足哈希的 candidate
        for candidate in range(1_000_001):
            digest = hashlib.sha256(prefix + str(candidate).encode()).hexdigest()
            if digest == expected:
                challenge_data["d"] = base64.b64encode(
                    str(candidate).encode()
                ).decode()
                cookie_val = base64.b64encode(
                    json.dumps(challenge_data, separators=(",", ":")).encode()
                ).decode()
                domain = urlparse(page_url).hostname or "www.iesdouyin.com"
                # 写入 cookie 并重试
                self.session.cookies.set(cookie_name, cookie_val, domain=domain, path="/")
                resp = self.session.get(page_url, headers=MOBILE_HEADERS, timeout=self.timeout)
                return resp.text or ""

        return html

    @staticmethod
    def _decode_b64(value: str) -> bytes:
        """解码 base64url."""
        normalized = value.replace("-", "+").replace("_", "/")
        # 补齐 padding
        normalized += "=" * (-len(normalized) % 4)
        return base64.b64decode(normalized)

    def _extract_router_data(self, html: str) -> dict:
        """从 HTML 中提取 window._ROUTER_DATA 的 JSON."""
        # 找到标记
        marker = "window._ROUTER_DATA = "
        start = html.find(marker)
        if start < 0:
            return {}

        idx = start + len(marker)
        # 跳过空白
        while idx < len(html) and html[idx].isspace():
            idx += 1
        if idx >= len(html) or html[idx] != "{":
            return {}

        # 括号匹配
        depth = 0
        in_str = False
        escaped = False
        for cursor in range(idx, len(html)):
            ch = html[cursor]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    # 解析 JSON
                    try:
                        return json.loads(html[idx: cursor + 1])
                    except ValueError:
                        return {}
        return {}

    def _get_media_url(self, item_info: dict, mode: str = "video") -> str:
        """获取无水印播放地址或音频地址."""
        if mode == "video":
            # 视频播放地址
            play_urls = (
                item_info.get("video", {})
                .get("play_addr", {})
                .get("url_list", [])
            )
            if not play_urls:
                raise ValueError("未找到视频播放地址")
            # 把 playwm 换成 play
            return play_urls[0].replace("playwm", "play")

        if mode == "audio":
            # 音频地址
            music = item_info.get("music", {})
            audio_urls = music.get("play_url", {}).get("url_list", [])
            if not audio_urls:
                raise ValueError("未找到音频地址")
            return audio_urls[0]

        raise ValueError(f"不支持的模式: {mode}")

    def _build_result(self, item_info: dict, video_id: str) -> dict:
        """把抖音 item_info 转成统一格式."""
        # 标题/作者/统计
        title = item_info.get("desc") or f"抖音视频_{video_id}"
        author = item_info.get("author", {})
        stats = item_info.get("statistics", {})

        # 播放地址/封面/时长
        video_info = item_info.get("video", {})
        play_urls = video_info.get("play_addr", {}).get("url_list", [])
        cover_urls = video_info.get("cover", {}).get("url_list", [])
        duration = video_info.get("duration", 0)
        duration_sec = duration // 1000 if duration > 1000 else duration

        # 格式列表
        formats = []
        if play_urls:
            clean_url = play_urls[0].replace("playwm", "play")
            width = video_info.get("width", 0)
            height = video_info.get("height", 0)
            formats.append({
                "format_id": "douyin_nowm",
                "ext": "mp4",
                "resolution": f"{width}x{height}" if width and height else "原始",
                "height": height or 720,
                "filesize": None,
                "filesize_approx": None,
                "vcodec": "h264",
                "acodec": "aac",
                "has_audio": True,
                "label": f"无水印 MP4 ({height}p)" if height else "无水印 MP4 (原始画质)",
                "_direct_url": clean_url,
            })

        # 返回统一结构
        return {
            "id": video_id,
            "title": title,
            "thumbnail": cover_urls[0] if cover_urls else "",
            "duration": duration_sec,
            "duration_string": self._fmt_duration(duration_sec),
            "uploader": author.get("nickname", "抖音用户"),
            "platform": "抖音",
            "view_count": stats.get("play_count") or stats.get("digg_count"),
            "upload_date": "",
            "description": title[:200],
            "formats": formats,
            "subtitles": [],
            "automatic_captions": [],
        }

    @staticmethod
    def _fmt_duration(seconds: Optional[int]) -> str:
        """格式化时长."""
        if not seconds:
            return "00:00"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _download_file(self, url: str, filepath: Path, chunk_size: int = 64 * 1024):
        """流式下载文件, 失败重试."""
        for attempt in range(self.max_retries):
            try:
                # 流式请求
                resp = self.session.get(
                    url, stream=True, timeout=self.timeout, allow_redirects=True,
                )
                resp.raise_for_status()

                # 先写 .part 文件
                temp_path = filepath.with_suffix(filepath.suffix + ".part")
                with temp_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                # 完成后重命名
                temp_path.replace(filepath)
                return
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"文件下载失败: {e}")
                # 指数退避
                time.sleep(1 * (2 ** attempt))
