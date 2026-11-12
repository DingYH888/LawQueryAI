<script setup>
import { onMounted, watch, ref, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useChatStore } from '../stores/chat'
import ChatSidebar from '../components/chat/ChatSidebar.vue'
import ChatMessage from '../components/chat/ChatMessage.vue'
import ChatInput from '../components/chat/ChatInput.vue'

const route = useRoute()
const router = useRouter()
const chat = useChatStore()
const messagesEnd = ref(null)
const showSidebar = ref(true)

function scrollToBottom(smooth = false) {
  nextTick(() => {
    messagesEnd.value?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto' })
  })
}

// 加载会话列表；store 已在初始化时从 sessionStorage 恢复缓存，
// 因此这里只做服务端列表/历史的校准，不会清空已有会话状态
onMounted(() => {
  chat.loadSessions()
  const sessionId = route.params.sessionId
  if (sessionId) {
    chat.loadMessages(sessionId)
  }
})

// 路由参数变化（浏览器前进/后退、直接访问 /chat/:id）时同步 store
watch(
  () => route.params.sessionId,
  (newVal, oldVal) => {
    if (newVal) {
      if (newVal !== chat.currentSessionId) chat.loadMessages(newVal)
    } else if (oldVal && chat.currentSessionId) {
      // 从某个会话地址回到 /chat：视为新建对话（旧会话缓存保留）
      chat.newSession()
    }
  }
)

// store 的当前会话变化时反向同步 URL，保证刷新/分享链接能回到同一会话
watch(
  () => chat.currentSessionId,
  (sessionId) => {
    const urlSessionId = route.params.sessionId
    if (sessionId && urlSessionId !== sessionId) {
      router.replace(`/chat/${sessionId}`)
    } else if (!sessionId && urlSessionId) {
      router.replace('/chat')
    }
  }
)

// 自动滚动到底部：消息条数变化（切换会话、新消息）
watch(
  () => chat.messages.length,
  () => scrollToBottom(true)
)

// 自动滚动到底部：正在流式生成时内容持续变化
watch(
  () => chat.messages[chat.messages.length - 1]?.content || '',
  () => scrollToBottom(false)
)
</script>

<template>
  <div class="flex h-[calc(100vh-3.5rem)]">
    <!-- 侧边栏（移动端可收起） -->
    <div v-show="showSidebar" class="hidden md:block">
      <ChatSidebar />
    </div>
    <!-- 移动端侧边栏切换 -->
    <button
      @click="showSidebar = !showSidebar"
      class="md:hidden fixed bottom-20 left-4 z-10 w-10 h-10 rounded-full shadow-lg flex items-center justify-center text-sm cursor-pointer"
      :style="{ backgroundColor: 'var(--primary)', color: '#fff' }"
    >
      {{ showSidebar ? '✕' : '☰' }}
    </button>

    <!-- 右半部分：聊天区 -->
    <div class="flex-1 flex flex-col">
      <!-- 消息列表 -->
      <div class="flex-1 overflow-y-auto" :style="{ backgroundColor: 'var(--bg)' }">
        <!-- 空状态 -->
        <div
          v-if="chat.messages.length === 0"
          class="flex flex-col items-center justify-center h-full text-center px-4"
        >
          <div class="text-5xl mb-4">⚖️</div>
          <h3 class="text-lg font-semibold mb-2" :style="{ color: 'var(--text)' }">法律咨询助手</h3>
          <p class="text-sm max-w-md" :style="{ color: 'var(--text-secondary)' }">
            您好！我是您的 AI 法律助手，可以帮您解答法律问题、分析案情、提供法律建议。请告诉我您遇到的法律问题。
          </p>
        </div>

        <!-- 消息列表 -->
        <div v-else class="max-w-4xl mx-auto">
          <ChatMessage
            v-for="(msg, i) in chat.messages"
            :key="msg.id || i"
            :message="msg"
            :streaming="chat.isStreaming && i === chat.messages.length - 1"
          />
          <div ref="messagesEnd"></div>
        </div>
      </div>

      <!-- 输入区 -->
      <ChatInput />
    </div>
  </div>
</template>
