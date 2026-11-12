import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getSessions, getMessages, deleteSession, streamSendMessage } from '../api/chat'

/**
 * 会话状态设计说明（修复「生成中切换会话后消息丢失」的问题）
 *
 * 1. 消息按会话隔离缓存：cache[key].messages，key 为服务端 session_id，
 *    或尚未拿到 session_id 时的本地草稿 key（local-draft-*）。
 *    这样「切换会话」只是切换视图指针，不会清空任何会话的消息数组。
 * 2. 流式任务绑定在发送那一刻的 cache entry 上：增量始终写入 entry.messages，
 *    而不是写入全局 messages 数组的下标，切换/新建会话不会写错地方或抛错。
 * 3. 新建对话 = 新建一个草稿 entry，旧会话（即使正在生成）原样留在缓存里，
 *    拿到 session_id 后草稿 entry 自动迁移到真实会话 id，侧边栏即可再次进入。
 * 4. 缓存写入 sessionStorage，路由跳转/组件卸载/刷新后仍可恢复（含未完成的部分回答）。
 */

const STORAGE_KEY = 'lawquery:chat-state:v1'
const DRAFT_PREFIX = 'local-draft-'

let seq = 0
function uid() {
  seq += 1
  return `${Date.now().toString(36)}-${seq}`
}

function newDraftKey() {
  return `${DRAFT_PREFIX}${uid()}`
}

function isDraftKey(key) {
  return typeof key === 'string' && key.startsWith(DRAFT_PREFIX)
}

function normalizeMessage(msg) {
  return {
    id: msg?.id || uid(),
    role: msg?.role === 'assistant' ? 'assistant' : 'user',
    content: typeof msg?.content === 'string' ? msg.content : '',
    sources: Array.isArray(msg?.sources) ? msg.sources : [],
  }
}

function createEntry(key, patch = {}) {
  return {
    sessionId: isDraftKey(key) ? null : key,
    messages: [],
    streaming: false,
    loaded: false,
    abort: null,
    ...patch,
  }
}

/**
 * 合并本地缓存与服务端历史：
 * 后端在流式结束后才补齐回答，所以「服务端条数 >= 本地条数」时以服务端为准；
 * 反之本地一定包含尚未落库的消息（刚发送的提问 / 正在生成的回答），保留本地。
 */
function mergeMessages(local, server) {
  const localList = Array.isArray(local) ? local : []
  const serverList = Array.isArray(server) ? server : []
  if (!serverList.length) return localList
  if (!localList.length) return serverList
  return serverList.length >= localList.length ? serverList : localList
}

export const useChatStore = defineStore('chat', () => {
  const sessions = ref([])
  const currentSessionId = ref(null)
  const currentDraftKey = ref(newDraftKey())
  const cache = ref({})

  // 当前视图对应的缓存 key：已绑定会话时用 session_id，否则用草稿 key
  const viewKey = computed(() => currentSessionId.value || currentDraftKey.value)
  const currentEntry = computed(() => cache.value[viewKey.value] || null)
  const messages = computed(() => currentEntry.value?.messages || [])
  const isStreaming = computed(() => !!currentEntry.value?.streaming)
  const currentSession = computed(
    () => sessions.value.find((s) => s.session_id === currentSessionId.value) || null
  )

  function ensureEntry(key) {
    if (!cache.value[key]) {
      cache.value[key] = createEntry(key)
    }
    return cache.value[key]
  }

  // ---------- 本地持久化（sessionStorage） ----------
  let persistTimer = null

  function saveNow() {
    if (persistTimer) {
      clearTimeout(persistTimer)
      persistTimer = null
    }
    try {
      const snapshot = {
        currentSessionId: currentSessionId.value,
        currentDraftKey: currentDraftKey.value,
        cache: {},
      }
      for (const [key, entry] of Object.entries(cache.value)) {
        // 空草稿不落快照，避免产生大量无意义的空会话
        if (!entry.messages.length && !entry.streaming) continue
        snapshot.cache[key] = {
          sessionId: entry.sessionId,
          loaded: entry.loaded,
          messages: entry.messages.map((m) => ({
            id: m.id,
            role: m.role,
            content: m.content,
            sources: m.sources || [],
          })),
        }
      }
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot))
    } catch {
      // 存储不可用/配额不足时忽略，不影响内存中的会话状态
    }
  }

  function schedulePersist() {
    if (persistTimer) return
    persistTimer = setTimeout(() => {
      persistTimer = null
      saveNow()
    }, 300)
  }

  function restore() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY)
      if (!raw) return
      const data = JSON.parse(raw)
      const restored = {}
      for (const [key, value] of Object.entries(data.cache || {})) {
        if (!value) continue
        restored[key] = createEntry(key, {
          // 刷新后原 SSE 连接已断开，保留已生成的内容但不再标记为生成中
          streaming: false,
          loaded: !!value.loaded,
          sessionId: value.sessionId || (isDraftKey(key) ? null : key),
          messages: Array.isArray(value.messages) ? value.messages.map(normalizeMessage) : [],
        })
      }
      cache.value = restored
      if (data.currentSessionId && restored[data.currentSessionId]) {
        currentSessionId.value = data.currentSessionId
      } else if (data.currentDraftKey && restored[data.currentDraftKey]) {
        currentDraftKey.value = data.currentDraftKey
      }
    } catch {
      // 恢复失败则忽略，回落到空状态
    }
  }

  // ---------- 会话列表 ----------
  function resolveTitle(session) {
    const entry = cache.value[session.session_id]
    const firstUser = entry?.messages?.find((m) => m.role === 'user')
    if ((!session.title || session.title === '新对话') && firstUser?.content) {
      return firstUser.content.slice(0, 30)
    }
    return session.title || '新对话'
  }

  async function loadSessions() {
    try {
      const list = await getSessions()
      sessions.value = (list || []).map((s) => ({ ...s, title: resolveTitle(s) }))
    } catch {
      // 静默处理
    }
  }

  // ---------- 服务端历史校准 ----------
  async function syncFromServer(sessionId) {
    const entry = cache.value[sessionId]
    // 正在生成中的会话以本地为准，绝不用服务端的旧数据覆盖
    if (entry?.streaming) return
    try {
      const serverMessages = (await getMessages(sessionId) || []).map(normalizeMessage)
      const target = ensureEntry(sessionId)
      if (target.streaming) return
      target.messages = mergeMessages(target.messages, serverMessages)
      target.loaded = true
      saveNow()
    } catch {
      // 静默处理，保留本地缓存
    }
  }

  async function loadMessages(sessionId) {
    if (!sessionId) return
    currentSessionId.value = sessionId
    ensureEntry(sessionId)
    await syncFromServer(sessionId)
  }

  function newSession() {
    if (!currentSessionId.value) {
      const draft = cache.value[currentDraftKey.value]
      // 当前已经在空的草稿上，无需再开新草稿
      if (draft && draft.messages.length === 0 && !draft.streaming) return
      // 离开空草稿时顺手清掉，避免缓存堆积
      if (draft && !draft.streaming) delete cache.value[currentDraftKey.value]
    }
    // 关键：只切换视图指针，旧的会话（含正在生成的回答）全部保留在 cache 中
    currentSessionId.value = null
    currentDraftKey.value = newDraftKey()
    ensureEntry(currentDraftKey.value)
    saveNow()
  }

  async function removeSession(sessionId) {
    try {
      const entry = cache.value[sessionId]
      if (entry?.abort) entry.abort()
      await deleteSession(sessionId)
      sessions.value = sessions.value.filter((s) => s.session_id !== sessionId)
      delete cache.value[sessionId]
      if (currentSessionId.value === sessionId) {
        newSession()
      }
      saveNow()
    } catch {
      // 静默处理
    }
  }

  /** 草稿会话拿到服务端 session_id 后，把缓存迁移到真实会话 id 下 */
  async function adoptSessionId(oldKey, entry, sessionId) {
    entry.sessionId = sessionId
    if (oldKey !== sessionId) {
      // session_id 由服务端新生成，缓存里不会存在同键 entry，直接迁移即可
      cache.value[sessionId] = entry
      delete cache.value[oldKey]
    }
    if (!currentSessionId.value && currentDraftKey.value === oldKey) {
      // 用户仍停留在该草稿视图上，跟随切到真实会话 id（URL 由页面同步）
      currentSessionId.value = sessionId
    }
    if (!sessions.value.find((s) => s.session_id === sessionId)) {
      await loadSessions()
    }
  }

  // ---------- 发送消息 ----------
  async function sendMessage(text) {
    const content = (text || '').trim()
    if (!content) return null

    // 绑定发送时刻的会话缓存，后续任何会话切换都不会影响这条消息的归属
    const key = viewKey.value
    const entry = ensureEntry(key)
    if (entry.streaming) return entry.sessionId

    const msgs = entry.messages
    msgs.push(normalizeMessage({ role: 'user', content }))
    msgs.push(normalizeMessage({ role: 'assistant', content: '', sources: [] }))
    const aiIdx = msgs.length - 1

    entry.streaming = true
    saveNow()

    const requestSessionId = currentSessionId.value

    try {
      const { abort, stream } = streamSendMessage(content, requestSessionId)
      entry.abort = abort

      for await (const chunk of stream) {
        if (chunk.done) break
        // 通过 entry.messages 定位，而不是全局 messages 数组下标
        const reply = entry.messages[aiIdx]
        if (!reply) break
        if (chunk.content !== undefined) reply.content += chunk.content
        if (chunk.sources) reply.sources = chunk.sources
        if (chunk.session_id && entry.sessionId !== chunk.session_id) {
          await adoptSessionId(key, entry, chunk.session_id)
        }
        schedulePersist()
      }
    } catch (e) {
      // 用户主动取消（AbortError）、流超时（StreamTimeoutError）、网络/服务端异常要给不同提示
      const reply = entry.messages[aiIdx]
      if (reply && e?.name === 'AbortError') {
        if (!reply.content) reply.content = '（已取消）'
      } else if (reply && e?.name === 'StreamTimeoutError') {
        reply.content = reply.content
          ? `${reply.content}\n\n（响应超时已中断，请重试）`
          : '响应超时，未收到回答，请重试。'
      } else if (reply && !reply.content) {
        reply.content = '抱歉，消息发送失败，请重试。'
      }
    } finally {
      // 兜底：流已结束但一个字符都没有时，不要让气泡永远停在「思考中」
      const reply = entry.messages[aiIdx]
      if (reply && !reply.content) {
        reply.content = '未收到有效回答，请重试。'
      }
      entry.streaming = false
      entry.abort = null
      saveNow()
      if (entry.sessionId) {
        // 流结束后后端才落库完整回答，校准一次以便与服务端对齐
        await syncFromServer(entry.sessionId)
      }
    }

    return entry.sessionId
  }

  /** 侧边栏用：某个会话是否正在后台生成回答（切换到别的会话后仍在继续） */
  function isSessionStreaming(sessionId) {
    return !!cache.value[sessionId]?.streaming
  }

  function cancelStream() {
    const entry = currentEntry.value
    if (!entry) return
    if (entry.abort) {
      entry.abort()
      entry.abort = null
    }
    entry.streaming = false
    saveNow()
  }

  /** 退出登录等场景：清空全部会话状态与本地快照 */
  function reset() {
    for (const entry of Object.values(cache.value)) {
      if (entry?.abort) entry.abort()
    }
    cache.value = {}
    sessions.value = []
    currentSessionId.value = null
    currentDraftKey.value = newDraftKey()
    try {
      sessionStorage.removeItem(STORAGE_KEY)
    } catch {
      // 忽略
    }
  }

  restore()

  return {
    sessions,
    currentSessionId,
    messages,
    isStreaming,
    currentSession,
    loadSessions,
    loadMessages,
    newSession,
    removeSession,
    sendMessage,
    cancelStream,
    isSessionStreaming,
    reset,
  }
})
