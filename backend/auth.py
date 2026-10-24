"""认证工具: 密码哈希, JWT 签发和校验, 登录依赖."""

import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.getenv("JWT_SECRET", "saveany-dev-secret-change-in-production")  # 签名密钥
JWT_ALGORITHM = "HS256"  # 签名算法
JWT_EXPIRE_HOURS = 72  # 过期时间

security = HTTPBearer(auto_error=False)  # 读取 Authorization 头


def hash_password(password: str) -> str:
    """用 bcrypt 哈希密码."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """校验密码."""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, email: str) -> str:
    """签发 JWT."""
    # 载荷: 用户 ID, 邮箱, 过期时间, 签发时间
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """校验并解析 JWT."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        # sub 转回 int
        payload["sub"] = int(payload["sub"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的 Token")


def validate_email(email: str) -> bool:
    """校验邮箱格式."""
    return bool(re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email))


def validate_password(password: str) -> str | None:
    """校验密码长度, 通过返回 None, 否则返回错误信息."""
    if len(password) < 6:
        return "密码长度不能少于 6 位"
    if len(password) > 50:
        return "密码长度不能超过 50 位"
    return None


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """必须登录: 解析 token 并查用户."""
    if not credentials:
        raise HTTPException(status_code=401, detail="请先登录")
    payload = decode_token(credentials.credentials)
    from database import get_user_by_id
    user = get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


async def get_optional_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict | None:
    """可选登录: 有 token 就解析, 否则返回 None."""
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
        from database import get_user_by_id
        return get_user_by_id(payload["sub"])
    except HTTPException:
        return None
