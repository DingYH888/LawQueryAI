import client from './client'

// 连接建立上限 / 数据空闲上限（毫秒）
// 后端每 10s 发一次 SSE 心跳（": ping"），因此：
// - 连响应头都拿不到超过 CONNECT_TIMEOUT → 请求确实卡死，直接报错
// - 只要还有心跳在到（即使模型首 token 要 80s+）就不判超时，避免误杀慢响应
const CONNECT_TIMEOUT = 35000
const IDLE_TIMEOUT = 120000

/** 流超时错误：与用户主动取消（AbortError）区分，store 据此给出不同提示 */
export class StreamTimeoutError extends Error {
  constructor(message = 'stream timeout') {
    super(message)
    this.name = 'StreamTimeoutError'
  }
}

export async function getSessions() {
  const { data } = await client.get('/chat/sessions')
  return data.sessions
}

export async function getMessages(sessionId, limit = 50) {
  const { data } = await client.get(`/chat/sessions/${sessionId}/messages`, { params: { limit } })
  return data.messages
}

export async function deleteSession(sessionId) {
  const { data } = await client.delete(`/chat/sessions/${sessionId}`)
  return data
}

/**
 * 流式发送消息
 * 返回一个对象 { abort, stream }
 * stream 是一个 AsyncGenerator，yield { content: string } 或 { sources: [...] } 或 { done: true }
 */
export function streamSendMessage(message, sessionId) {
  const controller = new AbortController()

  async function* generate() {
    // 响应头本身也要有上限，否则 fetch 会无限挂起（后端未起/网络黑洞）
    let headerTimedOut = false
    const headerTimer = setTimeout(() => {
      headerTimedOut = true
      controller.abort()
    }, CONNECT_TIMEOUT)

    let response
    try {
      response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('token')}`,
        },
        body: JSON.stringify({ message, session_id: sessionId || null }),
        signal: controller.signal,
      })
    } catch (e) {
      if (headerTimedOut) throw new StreamTimeoutError('connect timeout')
      throw e
    } finally {
      clearTimeout(headerTimer)
    }

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let receivedAnyData = false
    let timedOut = false

    /** 带空闲超时的单次读取：一个字节都没收到时用连接阈值，收到过心跳后放宽 */
    const readOnce = () => {
      const timeoutMs = receivedAnyData ? IDLE_TIMEOUT : CONNECT_TIMEOUT
      let timer = null
      const read = reader.read()
      // race 结束后底层 read 仍可能因 abort 而 reject，先行标记已处理避免未捕获异常
      read.catch(() => {})
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true
          controller.abort()
          reject(new StreamTimeoutError(`no data in ${timeoutMs}ms`))
        }, timeoutMs)
      })
      return Promise.race([read, timeout]).finally(() => {
        if (timer) clearTimeout(timer)
      })
    }

    try {
      while (true) {
        let result
        try {
          result = await readOnce()
        } catch (e) {
          // abort 会让 pending read 抛 AbortError，统一归因为超时
          if (timedOut) throw new StreamTimeoutError()
          throw e
        }
        const { done, value } = result
        if (done) break

        // 心跳也算「连接正常」，收到过就把单次读取阈值放宽到 IDLE_TIMEOUT
        if (value && value.length) receivedAnyData = true

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // 保留未完成的行

        for (const line of lines) {
          const trimmed = line.trim()
          // ": ping" 是后端 SSE 心跳注释帧，直接跳过
          if (!trimmed || !trimmed.startsWith('data: ')) continue
          const payload = trimmed.slice(6)
          if (payload === '[DONE]') {
            yield { done: true }
            return
          }
          try {
            yield JSON.parse(payload)
          } catch {
            // 忽略无法解析的数据
          }
        }
      }
    } finally {
      try {
        reader.releaseLock()
      } catch {
        // 已释放或流已关闭，忽略
      }
    }
  }

  return { abort: () => controller.abort(), stream: generate() }
}
