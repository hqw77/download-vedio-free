"""字幕提取与 AI 总结: B 站/yt-dlp 字幕, DeepSeek 总结/导图/问答."""

import json
import os
import re
import tempfile
from typing import Optional

import httpx
import yt_dlp
from openai import AsyncOpenAI


def _is_bilibili_url(url: str) -> bool:
    """判断是否 B 站链接."""
    return "bilibili.com" in url or "b23.tv" in url


class SubtitleExtractor:
    """字幕提取器: 人工字幕优先, 自动字幕兜底."""

    PREFERRED_LANGS = ["zh-Hans", "zh", "zh-CN", "en", "ja", "ko"]  # 语言优先级
    SUBTITLE_FORMAT = "json3"  # 首选字幕格式

    def extract(self, url: str) -> dict:
        """提取字幕, 返回字幕数据."""
        # B 站优先
        if _is_bilibili_url(url):
            result = self._extract_bilibili(url)
            if result["has_subtitle"]:
                return result

        # 其他平台走 yt-dlp
        info = self._get_video_info(url)

        # 人工/自动字幕
        manual_subs = info.get("subtitles") or {}
        auto_subs = info.get("automatic_captions") or {}

        # 过滤弹幕
        manual_subs = {k: v for k, v in manual_subs.items() if k != "danmaku"}

        # 选最佳字幕
        lang, sub_url, sub_type = self._pick_best_subtitle(manual_subs, auto_subs)
        # 没有字幕
        if not sub_url:
            return {
                "has_subtitle": False,
                "language": "",
                "subtitle_type": "none",
                "segments": [],
                "full_text": "",
            }

        # 下载并解析字幕
        segments = self._download_and_parse(url, lang, sub_type)

        # 拼成全文
        full_text = " ".join(seg["text"] for seg in segments)

        return {
            "has_subtitle": True,
            "language": lang,
            "subtitle_type": sub_type,
            "segments": segments,
            "full_text": full_text,
        }

    def _extract_bilibili(self, url: str) -> dict:
        """B 站专用字幕提取."""
        empty = {
            "has_subtitle": False, "language": "", "subtitle_type": "none",
            "segments": [], "full_text": "",
        }
        try:
            # 解析 bvid
            bvid = self._parse_bvid(url)
            if not bvid:
                return empty

            # 请求头
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": f"https://www.bilibili.com/video/{bvid}",
            }

            # 拿 aid/cid
            view_resp = httpx.get(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                headers=headers, timeout=15,
            )
            view_data = view_resp.json().get("data", {})
            cid = view_data.get("cid")
            aid = view_data.get("aid")
            if not cid or not aid:
                return empty

            # 拿字幕列表
            dm_resp = httpx.get(
                f"https://api.bilibili.com/x/v2/dm/view?aid={aid}&oid={cid}&type=1",
                headers=headers, timeout=15,
            )
            dm_data = dm_resp.json().get("data", {})
            subtitle_list = dm_data.get("subtitle", {}).get("subtitles", [])

            if not subtitle_list:
                return empty

            # 优先中文字幕
            best = subtitle_list[0]
            for s in subtitle_list:
                lang = s.get("lan", "")
                if lang == "zh" or lang == "zh-Hans":
                    best = s
                    break

            # 判断人工/自动
            sub_type = "auto" if best.get("lan", "").startswith("ai-") else "manual"

            # 补全 URL
            sub_url = best.get("subtitle_url", "")
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            if sub_url.startswith("http://"):
                sub_url = "https://" + sub_url[7:]

            if not sub_url:
                return empty

            # 下载字幕 JSON
            sub_resp = httpx.get(sub_url, headers=headers, timeout=15)
            sub_json = sub_resp.json()
            body = sub_json.get("body", [])

            # 转成 segments
            segments = []
            for item in body:
                content = item.get("content", "").strip()
                if not content:
                    continue
                segments.append({
                    "start": round(item.get("from", 0), 2),
                    "end": round(item.get("to", 0), 2),
                    "text": content,
                })

            # 拼全文
            full_text = " ".join(seg["text"] for seg in segments)
            return {
                "has_subtitle": True,
                "language": best.get("lan", "zh"),
                "subtitle_type": sub_type,
                "segments": segments,
                "full_text": full_text,
            }
        except Exception:
            return empty

    @staticmethod
    def _parse_bvid(url: str) -> Optional[str]:
        """从 URL 中提取 BV 号."""
        m = re.search(r"(BV[a-zA-Z0-9]+)", url)
        return m.group(1) if m else None

    def _get_video_info(self, url: str) -> dict:
        """用 yt-dlp 获取视频信息."""
        # 配置: 只取字幕, 不下载
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 只提取信息
            info = ydl.extract_info(url, download=False)
        # 解析失败
        if not info:
            raise ValueError("无法解析该视频链接")
        return info

    def _pick_best_subtitle(
        self, manual_subs: dict, auto_subs: dict
    ) -> tuple[str, Optional[str], str]:
        """按语言优先级选择人工/自动字幕."""
        # 人工字幕
        for lang in self.PREFERRED_LANGS:
            if lang in manual_subs:
                formats = manual_subs[lang]
                url = self._get_format_url(formats)
                if url:
                    return lang, url, "manual"

        # 自动字幕
        for lang in self.PREFERRED_LANGS:
            if lang in auto_subs:
                formats = auto_subs[lang]
                url = self._get_format_url(formats)
                if url:
                    return lang, url, "auto"

        # 兜底: 取第一个
        if manual_subs:
            first_lang = next(iter(manual_subs))
            url = self._get_format_url(manual_subs[first_lang])
            if url:
                return first_lang, url, "manual"

        if auto_subs:
            first_lang = next(iter(auto_subs))
            url = self._get_format_url(auto_subs[first_lang])
            if url:
                return first_lang, url, "auto"

        return "", None, "none"

    @staticmethod
    def _get_format_url(formats: list) -> Optional[str]:
        """按格式优先级取字幕 URL."""
        preferred = ["json3", "srv3", "vtt", "ttml"]
        for pref in preferred:
            for fmt in formats:
                if fmt.get("ext") == pref:
                    return fmt.get("url")
        # 兜底取第一个
        return formats[0].get("url") if formats else None

    def _download_and_parse(self, url: str, lang: str, sub_type: str) -> list[dict]:
        """下载字幕文件并解析."""
        # 临时目录
        with tempfile.TemporaryDirectory() as tmp_dir:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "writesubtitles": sub_type == "manual",
                "writeautomaticsub": sub_type == "auto",
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": os.path.join(tmp_dir, "subtitle"),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 只下载字幕
                ydl.download([url])

            # 找 vtt 文件
            vtt_files = [
                f for f in os.listdir(tmp_dir) if f.endswith(".vtt")
            ]
            if not vtt_files:
                return []

            vtt_path = os.path.join(tmp_dir, vtt_files[0])
            # 解析
            return self._parse_vtt(vtt_path)

    @staticmethod
    def _parse_vtt(filepath: str) -> list[dict]:
        """解析 VTT 字幕文件."""
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        segments = []
        # 按空行分块
        blocks = re.split(r"\n\n+", content)
        time_pattern = re.compile(
            r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})"
        )

        seen_texts = set()
        for block in blocks:
            lines = block.strip().split("\n")
            time_match = None
            text_lines = []
            for line in lines:
                # 时间行
                m = time_pattern.search(line)
                if m:
                    time_match = m
                # 文本行, 去掉标签
                elif time_match and line.strip() and not line.strip().isdigit():
                    clean = re.sub(r"<[^>]+>", "", line.strip())
                    if clean:
                        text_lines.append(clean)

            if time_match and text_lines:
                text = " ".join(text_lines)
                # 去重
                if text in seen_texts:
                    continue
                seen_texts.add(text)

                # 时间转秒
                start = _time_to_seconds(time_match.group(1))
                end = _time_to_seconds(time_match.group(2))
                segments.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "text": text,
                })

        return segments


class VideoSummarizer:
    """DeepSeek 总结器: 总结, 思维导图, 问答."""

    def __init__(self):
        """初始化 DeepSeek 客户端."""
        # 读取 API Key
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 环境变量未设置")
        # 使用 DeepSeek 的 OpenAI 兼容接口
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
        )
        # 模型
        self.model = "deepseek-chat"

    async def summarize_stream(self, subtitle_text: str, language: str = "zh"):
        """流式生成总结, 逐个 token 返回."""
        # 构造 prompt
        prompt = self._build_summary_prompt(subtitle_text, language)
        # 调用模型, 开启流式
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个专业的视频内容分析助手，擅长提取关键信息并生成结构化的总结。"},
                {"role": "user", "content": prompt},
            ],
            stream=True,
            temperature=0.7,
            max_tokens=4096,
        )
        # 逐个 token 返回
        async for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def generate_mindmap(self, subtitle_text: str, language: str = "zh") -> str:
        """生成思维导图 Markdown."""
        # 构造 prompt
        prompt = self._build_mindmap_prompt(subtitle_text, language)
        # 异步调用
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个专业的思维导图生成助手，擅长将内容组织为清晰的层级结构。"},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            temperature=0.5,
            max_tokens=4096,
        )
        # 返回内容
        return response.choices[0].message.content

    async def chat_stream(self, subtitle_text: str, question: str):
        """基于字幕流式回答用户问题."""
        # 构造 prompt
        prompt = self._build_chat_prompt(subtitle_text, question)
        # 调用模型
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个视频内容问答助手。根据提供的视频字幕内容来回答用户的问题。如果问题超出视频内容范围，请诚实告知。"},
                {"role": "user", "content": prompt},
            ],
            stream=True,
            temperature=0.7,
            max_tokens=2048,
        )
        # 逐个 token 返回
        async for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    @staticmethod
    def _build_summary_prompt(subtitle_text: str, language: str) -> str:
        """构造总结 prompt."""
        # 截断字幕
        truncated = subtitle_text[:15000]
        # 语言提示
        lang_hint = "中文" if language.startswith("zh") else "与原文相同的语言"
        return f"""请对以下视频字幕内容进行深度总结分析，使用{lang_hint}输出。

要求输出格式：
## 视频概述
（用2-3句话概括视频的主题和核心内容）

## 内容大纲
（按视频内容的逻辑顺序，列出主要章节/段落，每个章节包含要点）

## 核心知识要点
（提取视频中最重要的知识点、观点或结论，用编号列表形式）

## 总结
（用1-2句话给出整体评价或一句话总结）

---
视频字幕内容：
{truncated}"""

    @staticmethod
    def _build_mindmap_prompt(subtitle_text: str, language: str) -> str:
        """构造思维导图 prompt."""
        # 截断字幕
        truncated = subtitle_text[:15000]
        # 语言提示
        lang_hint = "中文" if language.startswith("zh") else "与原文相同的语言"
        return f"""请将以下视频字幕内容整理为思维导图结构，使用{lang_hint}输出。

要求：
1. 使用 Markdown 标题层级格式（# 一级标题，## 二级标题，### 三级标题）
2. 最外层是视频主题
3. 第二层是主要章节/模块
4. 第三层是各章节的要点
5. 可以有第四层做更细的展开
6. 每个节点的文字要简洁精炼
7. 只输出 Markdown 内容，不要其他说明文字

---
视频字幕内容：
{truncated}"""

    @staticmethod
    def _build_chat_prompt(subtitle_text: str, question: str) -> str:
        """构造问答 prompt."""
        # 截断字幕
        truncated = subtitle_text[:12000]
        return f"""以下是一个视频的字幕内容，请根据这些内容回答用户的问题。

视频字幕内容：
{truncated}

---
用户问题：{question}

请基于视频内容给出准确、详细的回答。如果视频内容中没有相关信息，请诚实说明。"""


def _time_to_seconds(time_str: str) -> float:
    """把 HH:MM:SS.mmm 转成秒."""
    parts = time_str.split(":")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds
