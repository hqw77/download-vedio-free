/**
 * 视频解析和下载请求.
 */

import axios from 'axios'

// axios 实例: /api 前缀, 120 秒超时
const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

/** 解析视频信息. */
export async function parseVideo(url) {
  const { data } = await api.post('/parse', { url })
  return data
}

/** 获取视频直链. */
export async function getDirectUrl(url, formatId) {
  const { data } = await api.post('/direct-url', { url, format_id: formatId })
  return data
}

/** 服务端下载地址. */
export function getDownloadUrl() {
  return '/api/download'
}

/** 服务端下载视频, 返回 blob 响应. */
export async function downloadViaServer(url, formatId) {
  const response = await api.post(
    '/download',
    { url, format_id: formatId },
    { responseType: 'blob', timeout: 600000 }
  )
  return response
}
