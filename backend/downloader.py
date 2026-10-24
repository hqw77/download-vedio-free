"""yt-dlp 封装: 解析, 格式整理, 下载, 获取直链."""

import os
import re
import shutil
import yt_dlp
from typing import Optional


def _find_ffmpeg_path() -> Optional[str]:
    """查找 ffmpeg 路径, 找不到返回 None."""
    # 系统 PATH
    if shutil.which("ffmpeg"):
        return os.path.dirname(shutil.which("ffmpeg"))
    try:
        # static_ffmpeg 兜底
        import static_ffmpeg
        paths = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        return os.path.dirname(paths[0])
    except Exception:
        return None


class VideoDownloader:
    """yt-dlp 封装层."""

    DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")  # 下载目录

    def __init__(self):
        # 创建下载目录
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        # 查找 ffmpeg
        self.ffmpeg_path = _find_ffmpeg_path()
        self.has_ffmpeg = self.ffmpeg_path is not None

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """替换文件名中的非法字符."""
        return re.sub(r'[\\/*?:"<>|]', "_", name)

    @staticmethod
    def _format_filesize(size: Optional[int]) -> str:
        """格式化文件大小."""
        if not size:
            return "未知大小"
        if size < 1024 * 1024:
            return f"{size / 1024:.0f}KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.2f}GB"

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        """格式化时长."""
        if not seconds:
            return "00:00"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def parse_video(self, url: str) -> dict:
        """解析视频信息, 不下载文件."""
        # 配置: 静默, 完整解析, 不解析播放列表
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 只提取信息
            info = ydl.extract_info(url, download=False)

        # 解析失败
        if not info:
            raise ValueError("无法解析该链接")

        # 整理格式列表
        formats = self._extract_formats(info)
        # 平台名
        platform = info.get("extractor", info.get("extractor_key", "Unknown"))

        return {
            # 视频基础信息
            "id": info.get("id", ""),
            "title": info.get("title", "未知标题"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "duration_string": self._format_duration(info.get("duration")),
            "uploader": info.get("uploader", info.get("channel", "未知")),
            "platform": platform,
            "view_count": info.get("view_count"),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description") or "")[:200],
            # 格式和字幕
            "formats": formats,
            "subtitles": list(info.get("subtitles", {}).keys()),
            "automatic_captions": list(info.get("automatic_captions", {}).keys())[:5],
        }

    def _extract_formats(self, info: dict) -> list:
        """整理 yt-dlp 返回的格式列表."""
        raw_formats = info.get("formats", [])
        if not raw_formats:
            return []

        seen = set()
        results = []

        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            ext = f.get("ext", "mp4")

            # 是否含视频/音频
            has_video = vcodec and vcodec != "none"
            has_audio = acodec and acodec != "none"

            # 跳过纯音频
            if not has_video:
                continue

            # 分辨率和大小
            resolution = f"{f.get('width', '?')}x{height}" if height else "未知"
            filesize = f.get("filesize") or f.get("filesize_approx")
            size_label = self._format_filesize(filesize)

            # 生成展示文案和去重键
            if has_audio:
                label = f"{height}p {ext.upper()} ({size_label})"
                key = (height, ext, "av")
            else:
                label = f"{height}p {ext.upper()} (仅视频, {size_label})"
                key = (height, ext, "v")

            # 去重
            if key in seen:
                continue
            seen.add(key)

            # 保存格式
            results.append({
                "format_id": f.get("format_id", ""),
                "ext": ext,
                "resolution": resolution,
                "height": height or 0,
                "filesize": filesize,
                "filesize_approx": filesize,
                "vcodec": vcodec,
                "acodec": acodec if has_audio else None,
                "has_audio": has_audio,
                "label": label,
            })

        # 按清晰度从高到低
        results.sort(key=lambda x: x["height"], reverse=True)

        # 没有带音频的格式时, 合成一个合并选项
        if not any(r["has_audio"] for r in results) and results:
            best_video = results[0]
            merged = {
                **best_video,
                "format_id": f"bestvideo+bestaudio/best",
                "label": f"{best_video['height']}p 最佳 (视频+音频合并)",
                "has_audio": True,
                "acodec": "merged",
            }
            results.insert(0, merged)

        # 最多返回 15 条
        return results[:15]

    def download_video(self, url: str, format_id: str) -> dict:
        """下载视频, 返回文件信息."""
        # 没有 ffmpeg 时, 合并格式降级为 best
        if not self.has_ffmpeg and "+" in format_id:
            format_id = "best"

        # 下载配置
        ydl_opts = {
            "format": format_id,
            "outtmpl": os.path.join(self.DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        # 有 ffmpeg 时指定位置和合并格式
        if self.has_ffmpeg:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path
            ydl_opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 开始下载
            info = ydl.extract_info(url, download=True)

        # 下载失败
        if not info:
            raise ValueError("下载失败")

        # 拼接预期文件名
        title = self._sanitize_filename(info.get("title", "video"))
        ext = info.get("ext", "mp4")
        filename = f"{title}.{ext}"
        filepath = os.path.join(self.DOWNLOAD_DIR, filename)

        # 文件不在预期位置时, 用 yt-dlp 的文件名或按标题查找
        if not os.path.exists(filepath):
            prepared = ydl.prepare_filename(info)
            if os.path.exists(prepared):
                filepath = prepared
                filename = os.path.basename(prepared)
            else:
                for f in os.listdir(self.DOWNLOAD_DIR):
                    if title in f:
                        filepath = os.path.join(self.DOWNLOAD_DIR, f)
                        filename = f
                        break

        return {
            "filepath": filepath,
            "filename": filename,
            "title": info.get("title", "video"),
            "ext": ext,
        }

    def get_direct_url(self, url: str, format_id: str) -> dict:
        """获取视频直链."""
        # 解析配置
        ydl_opts = {
            "format": format_id,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # 只提取信息
            info = ydl.extract_info(url, download=False)

        # 解析失败
        if not info:
            raise ValueError("无法获取直链")

        # 优先取顶层 url
        direct_url = info.get("url")
        if not direct_url:
            # 合并格式取第一个子格式
            requested = info.get("requested_formats")
            if requested and len(requested) > 0:
                direct_url = requested[0].get("url")

        # 没有直链
        if not direct_url:
            raise ValueError("该视频不支持直链下载，请使用服务端下载模式")

        return {
            "direct_url": direct_url,
            "ext": info.get("ext", "mp4"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "title": info.get("title", "video"),
        }
