"""FastAPI 入口.

负责:
- 加载 .env
- 创建 downloader / douyin_parser 单例
- 注册生命周期, CORS, 请求模型
- 提供解析, 下载, 直链, 缩略图代理接口
- 挂载 auth / summarize / payment 路由
"""

import os
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import unquote  # 未使用

from dotenv import load_dotenv

# 先加载 .env, 后面的模块才能读到环境变量
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from downloader import VideoDownloader
from douyin import DouyinParser, is_douyin_url
from database import init_db


# ------------------------------------------------------------
# 1. 全局单例
# ------------------------------------------------------------
# 整个进程共用一份
downloader = VideoDownloader()
douyin_parser = DouyinParser(download_dir=downloader.DOWNLOAD_DIR)


# ------------------------------------------------------------
# 2. 应用生命周期
# ------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表, 退出时清空下载目录."""
    # 启动
    init_db()
    yield
    # 退出
    download_dir = downloader.DOWNLOAD_DIR
    if os.path.exists(download_dir):
        for f in os.listdir(download_dir):
            try:
                os.remove(os.path.join(download_dir, f))
            except OSError:
                pass


# ------------------------------------------------------------
# 3. FastAPI 实例与 CORS
# ------------------------------------------------------------
app = FastAPI(
    title="万能视频下载器 API",
    description="基于 yt-dlp 的万能视频下载服务，支持 1800+ 平台",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许跨域, 开发期前端 5173 访问后端 8000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------
# 4. 请求模型
# ------------------------------------------------------------
class ParseRequest(BaseModel):
    """解析请求: 视频链接."""
    url: str


class DownloadRequest(BaseModel):
    """下载请求: 视频链接 + 格式 ID."""
    url: str
    # 默认格式: 最佳视频 + 最佳音频
    format_id: str = "bestvideo+bestaudio/best"


# ------------------------------------------------------------
# 5. 视频相关接口
# ------------------------------------------------------------
@app.get("/api/health")
async def health_check():
    """健康检查."""
    return {"status": "ok", "message": "万能视频下载器服务运行中"}


@app.post("/api/parse")
async def parse_video(req: ParseRequest):
    """解析视频信息.

    抖音走专用解析器, 其他平台走 yt-dlp.
    """
    try:
        loop = asyncio.get_event_loop()
        # 同步解析放到线程池, 避免阻塞事件循环
        if is_douyin_url(req.url):
            result = await loop.run_in_executor(None, douyin_parser.parse, req.url)
        else:
            result = await loop.run_in_executor(None, downloader.parse_video, req.url)
        return {"success": True, "data": result}
    except Exception as e:
        # 解析失败返回 400
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"解析失败: {str(e)}"
        })


@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """服务端下载视频, 返回文件流."""
    try:
        loop = asyncio.get_event_loop()
        # 先下载到本地
        if is_douyin_url(req.url):
            result = await loop.run_in_executor(None, douyin_parser.download, req.url)
        else:
            result = await loop.run_in_executor(
                None, downloader.download_video, req.url, req.format_id
            )
        filepath = result["filepath"]
        if not os.path.exists(filepath):
            raise HTTPException(status_code=500, detail="下载的文件不存在")

        # 以附件形式返回文件
        return FileResponse(
            path=filepath,
            filename=result["filename"],
            media_type="application/octet-stream",
        )
    except HTTPException:
        # 保留上面的 500 状态
        raise
    except Exception as e:
        # 下载失败返回 400
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"下载失败: {str(e)}"
        })


@app.post("/api/direct-url")
async def get_direct_url(req: DownloadRequest):
    """获取视频直链."""
    try:
        loop = asyncio.get_event_loop()
        # 调 yt-dlp 拿直链
        result = await loop.run_in_executor(
            None, downloader.get_direct_url, req.url, req.format_id
        )
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"获取直链失败: {str(e)}"
        })


@app.get("/api/proxy/thumbnail")
async def proxy_thumbnail(url: str = Query(..., description="缩略图URL")):
    """代理获取缩略图, 绕过防盗链."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # 请求图片, 带上 Referer
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": url,
            })
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg")
            # 返回图片, 缓存一天
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        # 加载失败返回 502
        raise HTTPException(status_code=502, detail="缩略图加载失败")


# ------------------------------------------------------------
# 6. 挂载子路由
# ------------------------------------------------------------
# 放在底部导入, 避免循环依赖
from api_summarize import router as summarize_router
from api_auth import router as auth_router
from api_payment import router as payment_router

app.include_router(summarize_router)   # /api/summarize, /api/chat
app.include_router(auth_router)        # /api/auth/*
app.include_router(payment_router)     # /api/payment/*


# ------------------------------------------------------------
# 7. 本地启动
# ------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # 监听 8000 端口
    uvicorn.run(app, host="0.0.0.0", port=8000)
