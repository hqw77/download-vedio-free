"""AI 总结接口: /api/summarize 和 /api/chat, 使用 SSE 流式返回."""

import asyncio
import json
from collections.abc import AsyncIterable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from auth import get_optional_user
from database import check_and_increment_summary, refund_summary, FREE_DAILY_SUMMARY_LIMIT

router = APIRouter(prefix="/api", tags=["AI 总结"])  # AI 路由


class SummarizeRequest(BaseModel):
    """总结请求."""
    url: str
    language: str = "zh"


class ChatRequest(BaseModel):
    """问答请求."""
    url: str
    question: str
    subtitle_text: str = ""


def _check_summary_permission(user: dict | None):
    """检查总结权限, 返回 (是否允许, 剩余次数, 提示)."""
    # 未登录
    if not user:
        return False, 0, "请先登录后使用 AI 功能"

    # 检查并增加次数
    allowed, remaining = check_and_increment_summary(user["id"])
    # 次数用完
    if not allowed:
        return False, 0, f"今日免费 AI 使用次数已用完（每日 {FREE_DAILY_SUMMARY_LIMIT} 次），开通 VIP 可无限使用"

    return True, remaining, None


def _refund_if_needed(user: dict | None, remaining: int) -> None:
    """非 VIP 失败时退回配额."""
    if not user or remaining == -1:
        return
    try:
        refund_summary(user["id"])
    except Exception:
        pass


def _get_summarizer():
    """懒加载 VideoSummarizer."""
    from summarizer import VideoSummarizer
    if not hasattr(_get_summarizer, "_instance"):
        try:
            _get_summarizer._instance = VideoSummarizer()
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
    return _get_summarizer._instance


def _get_extractor():
    """懒加载 SubtitleExtractor."""
    from summarizer import SubtitleExtractor
    if not hasattr(_get_extractor, "_instance"):
        _get_extractor._instance = SubtitleExtractor()
    return _get_extractor._instance


@router.post("/summarize", response_class=EventSourceResponse)
async def summarize_video(req: SummarizeRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """总结视频: 提取字幕, 流式总结, 生成导图, 推送配额."""
    # 检查权限
    allowed, remaining, message = _check_summary_permission(user)
    if not allowed:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": message, "need_login": user is None, "need_vip": user is not None}, ensure_ascii=False),
            event="error",
        )
        return

    generated = False
    try:
        loop = asyncio.get_event_loop()
        # 提取字幕
        extractor = _get_extractor()
        subtitle_data = await loop.run_in_executor(
            None, extractor.extract, req.url
        )

        # 发送字幕事件
        yield ServerSentEvent(
            raw_data=json.dumps(subtitle_data, ensure_ascii=False),
            event="subtitle",
        )

        # 无字幕, 退回配额
        if not subtitle_data["has_subtitle"]:
            _refund_if_needed(user, remaining)
            yield ServerSentEvent(
                raw_data=json.dumps({"message": "该视频没有可用的字幕，无法生成总结"}, ensure_ascii=False),
                event="error",
            )
            return

        full_text = subtitle_data["full_text"]
        summarizer = _get_summarizer()

        # 流式总结, 每个 token 一个事件
        async for token in summarizer.summarize_stream(full_text, req.language):
            generated = True
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="summary")

        # 生成思维导图
        mindmap_md = await summarizer.generate_mindmap(full_text, req.language)
        yield ServerSentEvent(
            raw_data=json.dumps({"markdown": mindmap_md}, ensure_ascii=False),
            event="mindmap",
        )

        # 发送剩余次数
        quota_info = {"remaining": remaining, "limit": FREE_DAILY_SUMMARY_LIMIT}
        yield ServerSentEvent(
            raw_data=json.dumps(quota_info, ensure_ascii=False),
            event="quota",
        )

        # 结束
        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        # 还没产出总结就失败, 退回配额
        if not generated:
            _refund_if_needed(user, remaining)
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"总结失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )


@router.post("/chat", response_class=EventSourceResponse)
async def chat_with_video(req: ChatRequest, user: dict | None = Depends(get_optional_user)) -> AsyncIterable[ServerSentEvent]:
    """基于字幕回答用户问题."""
    # 检查权限
    allowed, remaining, message = _check_summary_permission(user)
    if not allowed:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": message, "need_login": user is None, "need_vip": user is not None}, ensure_ascii=False),
            event="error",
        )
        return

    generated = False
    try:
        # 没有传字幕就先提取
        if not req.subtitle_text.strip():
            loop = asyncio.get_event_loop()
            extractor = _get_extractor()
            subtitle_data = await loop.run_in_executor(
                None, extractor.extract, req.url
            )
            # 无字幕, 退回配额
            if not subtitle_data["has_subtitle"]:
                _refund_if_needed(user, remaining)
                yield ServerSentEvent(
                    raw_data=json.dumps({"message": "该视频没有可用的字幕，无法回答问题"}, ensure_ascii=False),
                    event="error",
                )
                return
            subtitle_text = subtitle_data["full_text"]
        else:
            subtitle_text = req.subtitle_text

        summarizer = _get_summarizer()
        # 流式回答
        async for token in summarizer.chat_stream(subtitle_text, req.question):
            generated = True
            yield ServerSentEvent(raw_data=json.dumps(token, ensure_ascii=False), event="answer")

        yield ServerSentEvent(raw_data="[DONE]", event="done")

    except Exception as e:
        # 还没产出回答就失败, 退回配额
        if not generated:
            _refund_if_needed(user, remaining)
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"回答失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )
