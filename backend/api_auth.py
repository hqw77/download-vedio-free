"""认证接口: 注册, 登录, 当前用户."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import (
    create_token,
    get_current_user,
    hash_password,
    validate_email,
    validate_password,
    verify_password,
)
from database import create_user, get_user_by_email

router = APIRouter(prefix="/api/auth", tags=["auth"])  # 认证路由


class RegisterRequest(BaseModel):
    """注册请求."""
    email: str
    password: str


class LoginRequest(BaseModel):
    """登录请求."""
    email: str
    password: str


def _build_user_response(user: dict) -> dict:
    """把用户记录转成前端字段, 并判断 VIP 是否有效."""
    is_vip = False
    vip_expire_at = None
    # 判断 VIP 是否过期
    if user.get("is_vip") and user.get("vip_expire_at"):
        try:
            expire = datetime.fromisoformat(user["vip_expire_at"])
            is_vip = expire > datetime.now(timezone.utc)
            vip_expire_at = user["vip_expire_at"]
        except ValueError:
            pass

    return {
        "id": user["id"],
        "email": user["email"],
        "is_vip": is_vip,
        "vip_expire_at": vip_expire_at,
    }


@router.post("/register")
async def register(req: RegisterRequest):
    """注册: 校验, 查重, 创建用户, 签发 token."""
    # 校验邮箱
    if not validate_email(req.email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    # 校验密码
    err = validate_password(req.password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # 邮箱已注册
    if get_user_by_email(req.email):
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    # 创建用户
    hashed = hash_password(req.password)
    user = create_user(req.email, hashed)
    # 签发 token
    token = create_token(user["id"], req.email)

    return {
        "success": True,
        "data": {
            "token": token,
            "user": {"id": user["id"], "email": req.email, "is_vip": False, "vip_expire_at": None},
        },
    }


@router.post("/login")
async def login(req: LoginRequest):
    """登录: 查用户, 校验密码, 签发 token."""
    user = get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=400, detail="邮箱或密码错误")

    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="邮箱或密码错误")

    token = create_token(user["id"], user["email"])
    return {
        "success": True,
        "data": {
            "token": token,
            "user": _build_user_response(user),
        },
    }


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """返回当前登录用户."""
    return {
        "success": True,
        "data": _build_user_response(user),
    }
