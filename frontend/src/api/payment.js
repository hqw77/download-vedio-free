/**
 * 支付相关请求.
 */

import axios from 'axios'
import { getToken } from './auth'

/** 生成带 token 的请求头. */
function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** 创建 Stripe Checkout 会话. */
export async function createCheckoutSession(planType = 'monthly') {
  const res = await axios.post(
    '/api/payment/create-checkout',
    { plan_type: planType },
    { headers: authHeaders() }
  )
  return res.data.data
}

/** 获取当前用户订单. */
export async function getOrders() {
  const res = await axios.get('/api/payment/orders', { headers: authHeaders() })
  return res.data.data
}
