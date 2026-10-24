/**
 * 认证相关请求和本地登录态.
 */

import axios from 'axios'

const TOKEN_KEY = 'saveany_token' // token 存储键
const USER_KEY = 'saveany_user' // 用户信息存储键

/** 获取 token. */
export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

/** 保存 token. */
export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token)
}

/** 清除 token 和用户信息. */
export function removeToken() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 读取本地缓存的用户信息. */
export function getSavedUser() {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

/** 缓存用户信息. */
export function saveUser(user) {
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

/** 生成带 token 的请求头. */
function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** 注册, 保存 token 和用户信息. */
export async function register(email, password) {
  const res = await axios.post('/api/auth/register', { email, password })
  const { token, user } = res.data.data
  setToken(token)
  saveUser(user)
  return user
}

/** 登录, 保存 token 和用户信息. */
export async function login(email, password) {
  const res = await axios.post('/api/auth/login', { email, password })
  const { token, user } = res.data.data
  setToken(token)
  saveUser(user)
  return user
}

/** 请求当前用户信息. */
export async function fetchMe() {
  const res = await axios.get('/api/auth/me', { headers: authHeaders() })
  const user = res.data.data
  saveUser(user)
  return user
}

/** 退出登录. */
export function logout() {
  removeToken()
}

/** 是否已登录. */
export function isLoggedIn() {
  return !!getToken()
}
