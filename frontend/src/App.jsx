import { useState, useRef, useEffect, useCallback } from 'react'
import './App.css'

// ── Icons (inline SVG to avoid dependencies) ─────────────────────────────────
const SendIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M22 2L11 13M22 2L15 22l-4-9-9-4 20-7z" />
  </svg>
)
const PlusIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M12 5v14M5 12h14" />
  </svg>
)
const BotIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <rect x="3" y="11" width="18" height="10" rx="2" />
    <path d="M12 11V7M9 7h6M7 15h.01M12 15h.01M17 15h.01" />
  </svg>
)
const UserIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <circle cx="12" cy="8" r="4" />
    <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
  </svg>
)
const TrashIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
    <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" />
  </svg>
)


// ── Typing indicator component ────────────────────────────────────────────────
function TypingIndicator({ status }) {
  return (
    <div className="message assistant">
      <div className="avatar"><BotIcon /></div>
      <div className="bubble typing-bubble">
        {status ? (
          <span className="status-text">{status}</span>
        ) : (
          <span className="dots">
            <span /><span /><span />
          </span>
        )}
      </div>
    </div>
  )
}


// ── Single message component ──────────────────────────────────────────────────
function Message({ msg }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`message ${isUser ? 'user' : 'assistant'}`}>
      <div className="avatar">
        {isUser ? <UserIcon /> : <BotIcon />}
      </div>
      <div className="bubble">
        {/* Render newlines as line breaks */}
        {msg.content.split('\n').map((line, i) => (
          <span key={i}>{line}{i < msg.content.split('\n').length - 1 && <br />}</span>
        ))}
      </div>
    </div>
  )
}


// ── Sidebar conversation item ─────────────────────────────────────────────────
function ConversationItem({ conv, isActive, onSelect, onDelete }) {
  return (
    <div
      className={`conv-item ${isActive ? 'active' : ''}`}
      onClick={() => onSelect(conv.id)}
    >
      <span className="conv-title">{conv.title}</span>
      <button
        className="conv-delete"
        onClick={e => { e.stopPropagation(); onDelete(conv.id) }}
        title="Delete"
      >
        <TrashIcon />
      </button>
    </div>
  )
}


// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const [conversations, setConversations] = useState([])   // list of {id, title, messages}
  const [activeId, setActiveId] = useState(null)           // current conversation id
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamStatus, setStreamStatus] = useState('')     // "Searching knowledge base..."
  const bottomRef = useRef(null)
  const textareaRef = useRef(null)

  // Active conversation derived from state
  const activeConv = conversations.find(c => c.id === activeId)
  const messages = activeConv?.messages ?? []

  // Auto scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isStreaming])

  // Auto resize textarea
  useEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
  }, [input])

  // Create a new conversation (fetches a thread_id from the backend)
  const newConversation = useCallback(async () => {
    const res = await fetch('/api/new_thread')
    const { thread_id } = await res.json()
    const conv = { id: thread_id, title: 'New conversation', messages: [] }
    setConversations(prev => [conv, ...prev])
    setActiveId(thread_id)
    setInput('')
  }, [])

  // Create first conversation on mount
  useEffect(() => {
    newConversation()
  }, [])

  // Delete a conversation
  const deleteConversation = useCallback((id) => {
    setConversations(prev => {
      const next = prev.filter(c => c.id !== id)
      if (activeId === id) {
        setActiveId(next[0]?.id ?? null)
      }
      return next
    })
  }, [activeId])

  // Update messages in a specific conversation
  const updateMessages = useCallback((convId, updater) => {
    setConversations(prev =>
      prev.map(c => c.id === convId ? { ...c, messages: updater(c.messages) } : c)
    )
  }, [])

  // Set conversation title from first message
  const setTitle = useCallback((convId, title) => {
    setConversations(prev =>
      prev.map(c => c.id === convId ? { ...c, title: title.slice(0, 40) } : c)
    )
  }, [])

  // Send a message
  const sendMessage = useCallback(async () => {
    const text = input.trim()
    if (!text || isStreaming || !activeId) return

    setInput('')
    setIsStreaming(true)
    setStreamStatus('')

    // Add user message immediately
    const userMsg = { role: 'user', content: text }
    updateMessages(activeId, msgs => {
      if (msgs.length === 0) setTitle(activeId, text)  // first message = title
      return [...msgs, userMsg]
    })

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, thread_id: activeId }),
      })

      if (!response.ok) throw new Error(`HTTP ${response.status}`)

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop()  // keep incomplete last line in buffer

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const raw = line.slice(6).trim()
          if (!raw) continue

          let data
          try { data = JSON.parse(raw) } catch { continue }

          if (data.type === 'status') {
            setStreamStatus(data.text)
          } else if (data.type === 'token') {
            setStreamStatus('')
            updateMessages(activeId, msgs => [
              ...msgs,
              { role: 'assistant', content: data.text }
            ])
          } else if (data.type === 'error') {
            updateMessages(activeId, msgs => [
              ...msgs,
              { role: 'assistant', content: `Error: ${data.text}` }
            ])
          } else if (data.type === 'done') {
            setStreamStatus('')
          }
        }
      }
    } catch (err) {
      updateMessages(activeId, msgs => [
        ...msgs,
        { role: 'assistant', content: `Connection error: ${err.message}` }
      ])
    } finally {
      setIsStreaming(false)
      setStreamStatus('')
    }
  }, [input, isStreaming, activeId, updateMessages, setTitle])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  return (
    <div className="app">
      {/* ── Sidebar ── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <span className="logo">⬡ RAG Chat</span>
          <button className="new-chat-btn" onClick={newConversation} title="New conversation">
            <PlusIcon />
          </button>
        </div>

        <div className="conv-list">
          {conversations.length === 0 && (
            <p className="conv-empty">No conversations yet</p>
          )}
          {conversations.map(conv => (
            <ConversationItem
              key={conv.id}
              conv={conv}
              isActive={conv.id === activeId}
              onSelect={setActiveId}
              onDelete={deleteConversation}
            />
          ))}
        </div>

        <div className="sidebar-footer">
          <span>Powered by Gemini + LangGraph</span>
        </div>
      </aside>

      {/* ── Main chat area ── */}
      <main className="chat-area">
        {messages.length === 0 && !isStreaming ? (
          <div className="welcome">
            <div className="welcome-icon"><BotIcon /></div>
            <h2>How can I help you?</h2>
            <p>Ask me anything about the documents in your knowledge base.</p>
          </div>
        ) : (
          <div className="messages">
            {messages.map((msg, i) => (
              <Message key={i} msg={msg} />
            ))}
            {isStreaming && <TypingIndicator status={streamStatus} />}
            <div ref={bottomRef} />
          </div>
        )}

        {/* ── Input bar ── */}
        <div className="input-bar">
          <div className="input-wrapper">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask a question… (Enter to send, Shift+Enter for new line)"
              rows={1}
              disabled={isStreaming || !activeId}
            />
            <button
              className="send-btn"
              onClick={sendMessage}
              disabled={!input.trim() || isStreaming || !activeId}
            >
              <SendIcon />
            </button>
          </div>
          <p className="input-hint">Responses come from your knowledge base only.</p>
        </div>
      </main>
    </div>
  )
}