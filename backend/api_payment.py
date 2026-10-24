"""Stripe 支付接口: 创建 Checkout, Webhook, 订单列表."""

import os
import uuid
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import get_current_user
from database import (
    create_order,
    update_order_stripe_session,
    complete_order,
    get_user_orders,
    get_user_by_id,
)

router = APIRouter(prefix="/api/payment", tags=["payment"])  # 支付路由


def _get_config(key: str, default: str = "") -> str:
    """读取环境变量."""
    return os.getenv(key, default)


PLANS = {  # 套餐配置
    "monthly": {
        "name": "SaveAny VIP 月度会员",
        "amount": 990,
        "currency": "cny",
    },
}


class CreateCheckoutRequest(BaseModel):
    """创建支付请求."""
    plan_type: str = "monthly"


def _generate_order_no(user_id: int) -> str:
    """生成订单号."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"SA{ts}{user_id:04d}{short_uuid}"


@router.post("/create-checkout")
async def create_checkout_session(req: CreateCheckoutRequest, user: dict = Depends(get_current_user)):
    """创建 Stripe Checkout 会话."""
    # 读取配置
    secret_key = _get_config("STRIPE_SECRET_KEY")
    price_id = _get_config("STRIPE_PRICE_ID_MONTHLY")
    frontend_url = _get_config("FRONTEND_URL", "http://localhost:5173")

    # 校验配置
    if not secret_key:
        raise HTTPException(status_code=500, detail="支付服务未配置，请设置 STRIPE_SECRET_KEY")
    if not price_id:
        raise HTTPException(status_code=500, detail="套餐价格未配置，请设置 STRIPE_PRICE_ID_MONTHLY")

    # 校验套餐
    plan = PLANS.get(req.plan_type)
    if not plan:
        raise HTTPException(status_code=400, detail="无效的套餐类型")

    stripe.api_key = secret_key

    # 创建订单
    order_no = _generate_order_no(user["id"])
    create_order(
        user_id=user["id"],
        order_no=order_no,
        amount=plan["amount"],
        currency=plan["currency"],
        plan_type=req.plan_type,
    )

    try:
        # 创建 Stripe 会话
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[
                {
                    "price": price_id,
                    "quantity": 1,
                }
            ],
            mode="payment",
            success_url=f"{frontend_url}?payment=success&order_no={order_no}",
            cancel_url=f"{frontend_url}?payment=cancel&order_no={order_no}",
            client_reference_id=str(user["id"]),
            customer_email=user["email"],
            metadata={
                "order_no": order_no,
                "user_id": str(user["id"]),
                "plan_type": req.plan_type,
            },
        )

        # 保存 session id
        update_order_stripe_session(order_no, session.id)

        return {
            "success": True,
            "data": {
                "checkout_url": session.url,
                "order_no": order_no,
                "session_id": session.id,
            },
        }

    except stripe.StripeError as e:
        raise HTTPException(status_code=400, detail=f"创建支付会话失败: {str(e)}")


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """处理 Stripe Webhook."""
    # 读取原始请求体和签名
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    webhook_secret = _get_config("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        return JSONResponse(status_code=400, content={"error": "Webhook secret not configured"})

    stripe.api_key = _get_config("STRIPE_SECRET_KEY")

    try:
        # 验签
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid payload"})
    except stripe.SignatureVerificationError:
        return JSONResponse(status_code=400, content={"error": "Invalid signature"})

    # 支付完成
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        if session.get("payment_status") == "paid":
            payment_intent_id = session.get("payment_intent", "")
            result = complete_order(session["id"], payment_intent_id)
            if result:
                print(f"[Payment] Order {result['order_no']} completed successfully")
            else:
                print(f"[Payment] Session {session['id']} already processed or not found")

    # 异步支付成功
    elif event["type"] == "checkout.session.async_payment_succeeded":
        session = event["data"]["object"]
        payment_intent_id = session.get("payment_intent", "")
        complete_order(session["id"], payment_intent_id)

    return JSONResponse(status_code=200, content={"received": True})


@router.get("/orders")
async def list_orders(user: dict = Depends(get_current_user)):
    """返回当前用户的订单列表."""
    orders = get_user_orders(user["id"])
    return {
        "success": True,
        "data": [
            {
                "order_no": o["order_no"],
                "amount": o["amount"],
                "currency": o["currency"],
                "status": o["status"],
                "plan_type": o["plan_type"],
                "created_at": o["created_at"],
                "paid_at": o["paid_at"],
            }
            for o in orders
        ],
    }
