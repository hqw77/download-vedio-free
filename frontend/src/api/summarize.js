/**
 * AI 视频总结 API 封装.
 * 使用 fetch + ReadableStream 处理 SSE 流式响应.
 */

import { getToken } from './auth'

/**
 * 读取 SSE 流, 按事件名调用回调.
 * callbacks 的键是事件名, 值接收 data 字符串.
 */
async function handleSSEStream(response, callbacks) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''
  let dataLines = []
  let hasData = false

  // 分发一个完整事件
  function dispatch() {
    if (hasData && currentEvent) {
      const handler = callbacks[currentEvent]
      if (handler) handler(dataLines.join('\n'))
    }
    dataLines = []
    hasData = false
    currentEvent = ''
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    // 流式解码, 保留不完整的行
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      // 空行表示一个事件结束
      if (line === '') {
        dispatch()
        continue
      }

      // 注释行
      if (line.startsWith(':')) continue

      const colonIdx = line.indexOf(':')
      if (colonIdx < 0) continue

      const field = line.slice(0, colonIdx)
      let val = line.slice(colonIdx + 1)
      if (val.startsWith(' ')) val = val.slice(1)

      // 记录事件名和数据行
      if (field === 'event') {
        currentEvent = val
      } else if (field === 'data') {
        hasData = true
        dataLines.push(val)
      }
    }
  }
  dispatch()
}

/** 生成带 token 的请求头. */
function authHeaders() {
  const token = getToken()
  const headers = { 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

/** 请求视频总结, 通过 callbacks 接收 SSE 事件. */
export async function summarizeVideo(url, language = 'zh', callbacks = {}) {
  const response = await fetch('/api/summarize', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ url, language }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  await handleSSEStream(response, callbacks)
}

/** 请求视频问答, 通过 callbacks 接收 SSE 事件. */
export async function chatWithVideo(url, question, subtitleText = '', callbacks = {}) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ url, question, subtitle_text: subtitleText }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  await handleSSEStream(response, callbacks)
}
