<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'

// ---- Connection & Navigation ----
const activeTab = ref('overview')
const baseUrl = ref(localStorage.getItem('aim_base') || window.location.origin)
const apiKey = ref(localStorage.getItem('aim_key') || '')
const showApiKey = ref(false)
const autoRefresh = ref(true)
const windowSec = ref(900)
const connState = reactive({ ok: false, role: 'guest', text: '未连接', backend: 'browser', paused: false })
const toastMsg = ref('')
let toastTimer = null
let pollTimer = null

function notify(msg) {
  toastMsg.value = msg
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 3200)
}

function cleanBase() {
  return (baseUrl.value || window.location.origin).replace(/\/+$/, '')
}

function saveCreds() {
  localStorage.setItem('aim_base', cleanBase())
  localStorage.setItem('aim_key', apiKey.value.trim())
}

function authHeaders() {
  const h = { 'Content-Type': 'application/json' }
  const k = apiKey.value.trim()
  if (k) {
    h['Authorization'] = 'Bearer ' + k
    h['X-API-Key'] = k
  }
  return h
}

async function apiFetch(path, opts = {}) {
  const url = cleanBase() + path
  const headers = { ...authHeaders(), ...(opts.headers || {}) }
  return fetch(url, { ...opts, headers })
}

// ---- Telemetry & State ----
const models = ref(['Gemini5.0', 'google-ai-mode'])
const selectedModel = ref('Gemini5.0')
const metrics = reactive({
  now: 0,
  uptime_s: 0,
  tokenizer: 'tiktoken/cl100k_base',
  totals: { req: 0, ok: 0, err: 0, rej: 0, tin: 0, tout: 0, auth_fail: 0 },
  rates: { tps_10s: 0, tps_60s: 0, tps_window: 0, tps_peak_1h: 0, tokens_per_min: 0 },
  latency: { n: 0, avg: null, p50: null, p95: null, p99: null },
  success_rate: null,
  unique_ips: 0,
  series: { step: 5, cols: [], points: [] },
  ips: [],
  users: {},
  recent: [],
  kinds: {},
  endpoints: {},
  process: { pid: null, rss_mb: null, threads: null },
  pool: {
    backend: 'browser',
    paused: false,
    workers: 0,
    busy: 0,
    idle: 0,
    queued: 0,
    queue_limit: 200,
    max_workers: 2,
    min_workers: 0,
    total_tasks: 0,
    rejected: 0,
    users: {},
    slots: [],
  },
  soak: null,
})

const liveTasks = ref([])
const recentFilter = ref('all')
const recentSearch = ref('')

// ---- Admin Keys & Settings ----
const keysList = ref([])
const revealedKeys = reactive({})
const newKeyForm = reactive({ user_id: '', max_concurrent: 2, queue_limit: 20, note: '' })
const settingsData = reactive({
  backend: 'browser',
  min_workers: 0,
  max_workers: 2,
  queue_limit: 200,
  idle_ttl: 300,
  answer_timeout: 90,
  goto_timeout: 40,
  ready_quiet_s: 1.5,
  max_requests_per_identity: 50,
  block_streak_limit: 2,
  tenant_rotate_identity: true,
  block_assets: true,
  metrics_preview_chars: 80,
  mock_latency_str: '1.5-4.0',
  mock_fail_rate: 0.02,
  mock_answer_chars_str: '200-1200',
  mock_max_workers: 32,
  capacity: { cap: 2, cpus: 2, free_mb: null },
})

// ---- Soak Runner ----
const soakForm = reactive({ concurrency: 4, duration_s: 60, max_requests: 0, think_ms: 50 })

// ---- Chat Playground ----
const chatLog = ref([
  {
    role: 'assistant',
    content: '欢迎使用 Gemini5.0 代理控制台！默认每次调用走无状态直连 goto（兼容所有 OpenAI 客户端与工具调用），可在此直接测试或复制调用代码。',
    meta: null,
  },
])
const chatInput = ref('')
const chatFresh = ref(true)
const chatUser = ref('')
const chatSending = ref(false)
const showCodeHelper = ref(false)
const chatBoxRef = ref(null)

// ---- Formatting Helpers ----
function fmtNum(n) {
  n = Number(n || 0)
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'k'
  return String(n)
}

function fmtDuration(sec) {
  sec = Math.round(Number(sec || 0))
  if (sec < 60) return sec + 's'
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + (sec % 60) + 's'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return `${h}h ${m}m`
}

function fmtTime(ts) {
  if (!ts) return '-'
  return new Date(ts * 1000).toTimeString().slice(0, 8)
}

function fmtAgo(ts) {
  if (!ts) return '-'
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return s + ' 秒前'
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前'
  return Math.floor(s / 3600) + ' 小时前'
}

function maskKey(k, uid) {
  if (!k) return '(无密钥)'
  if (revealedKeys[uid]) return k
  if (k.length <= 12) return k.slice(0, 4) + '••••'
  return k.slice(0, 8) + '••••••••' + k.slice(-4)
}

async function copyText(text, label = '已复制到剪贴板') {
  try {
    await navigator.clipboard.writeText(text)
    notify(label)
  } catch {
    notify('复制失败，请手动复制')
  }
}

// ---- Chart Computed ----
const chartData = computed(() => {
  const pts = metrics.series?.points || []
  const W = 860
  const H = 180
  const padX = 14
  const padY = 14
  if (!pts.length) return { bars: [], tokLine: '', tokArea: '', maxReq: 0, maxTok: 0 }
  const maxReq = Math.max(1, ...pts.map((p) => p[1] || 0))
  const maxTok = Math.max(1, ...pts.map((p) => (p[5] || 0) + (p[6] || 0)))
  const bw = Math.max(2.5, (W - padX * 2) / pts.length)
  const bars = []
  const tokPts = []
  pts.forEach((p, idx) => {
    const x = padX + idx * bw
    const ok = p[2] || 0
    const err = p[3] || 0
    const rej = p[4] || 0
    const tok = (p[5] || 0) + (p[6] || 0)
    const availH = H - padY * 2
    const hOk = (ok / maxReq) * availH
    const hRej = (rej / maxReq) * availH
    const hErr = (err / maxReq) * availH
    let y = H - padY
    if (hOk > 0) {
      y -= hOk
      bars.push({ x, y, w: Math.max(1.8, bw - 1.2), h: hOk, color: '#10b981', title: `${fmtTime(p[0])} 成功:${ok}` })
    }
    if (hRej > 0) {
      y -= hRej
      bars.push({ x, y, w: Math.max(1.8, bw - 1.2), h: hRej, color: '#f59e0b', title: `${fmtTime(p[0])} 拒绝:${rej}` })
    }
    if (hErr > 0) {
      y -= hErr
      bars.push({ x, y, w: Math.max(1.8, bw - 1.2), h: hErr, color: '#ef4444', title: `${fmtTime(p[0])} 错误:${err}` })
    }
    const cx = +(x + bw / 2).toFixed(1)
    const cy = +(H - padY - (tok / maxTok) * availH).toFixed(1)
    tokPts.push(`${cx},${cy}`)
  })
  const tokLine = tokPts.join(' ')
  const firstX = padX + bw / 2
  const lastX = padX + (pts.length - 1) * bw + bw / 2
  const tokArea = tokPts.length > 1 ? `${firstX},${H - padY} ${tokLine} ${lastX},${H - padY}` : ''
  return { bars, tokLine, tokArea, maxReq, maxTok }
})

const mergedTenants = computed(() => {
  const pUsers = metrics.pool?.users || {}
  const mUsers = metrics.users || {}
  const uids = Array.from(new Set([...Object.keys(pUsers), ...Object.keys(mUsers)]))
  return uids.map((uid) => {
    const pu = pUsers[uid] || {}
    const mu = mUsers[uid] || {}
    return {
      user_id: uid,
      running: pu.running || 0,
      queued: pu.queued || 0,
      max_concurrent: pu.max_concurrent || 0,
      queue_limit: pu.queue_limit || 0,
      ok: mu.ok || 0,
      err: mu.err || 0,
      rej: mu.rej || pu.rejected || 0,
      tin: mu.tin || 0,
      tout: mu.tout || 0,
      last: mu.last || pu.last || 0,
    }
  })
})

const filteredRecent = computed(() => {
  const q = recentSearch.value.trim().toLowerCase()
  return (metrics.recent || []).filter((r) => {
    if (recentFilter.value !== 'all' && r.cls !== recentFilter.value) return false
    if (!q) return true
    return (
      (r.user || '').toLowerCase().includes(q) ||
      (r.ip || '').toLowerCase().includes(q) ||
      (r.kind || '').toLowerCase().includes(q) ||
      (r.preview || '').toLowerCase().includes(q) ||
      (r.error || '').toLowerCase().includes(q)
    )
  })
})

const curlSnippet = computed(() => {
  const k = apiKey.value.trim()
  const authLine = k ? ` \\\n  -H "Authorization: Bearer ${k}"` : ''
  return `curl -X POST ${cleanBase()}/v1/chat/completions \\
  -H "Content-Type: application/json"${authLine} \\
  -d '{
    "model": "${selectedModel.value}",
    "messages": [{"role": "user", "content": "用一句话介绍 FastAPI"}]
  }'`
})

// ---- Data Fetching ----
async function connect() {
  saveCreds()
  connState.text = '连接中…'
  try {
    const r = await apiFetch('/v1/models')
    if (r.status === 401) {
      connState.ok = false
      connState.text = '密钥无效 (401)'
      notify('API Key 校验失败 (401)')
      return
    }
    if (r.ok) {
      const j = await r.json()
      models.value = (j.data || []).map((m) => m.id)
      if (models.value.length) selectedModel.value = models.value[0]
    }
    await refreshTelemetry()
    await loadAdminSettings()
  } catch (e) {
    connState.ok = false
    connState.text = '连接失败: ' + e.message
  }
}

async function refreshTelemetry() {
  try {
    const mr = await apiFetch(`/admin/metrics?window=${windowSec.value}&top_ips=100&recent=120`)
    if (mr.ok) {
      const d = await mr.json()
      Object.assign(metrics, d)
      connState.ok = true
      connState.role = 'admin'
      connState.backend = d.pool?.backend || 'browser'
      connState.paused = !!d.pool?.paused
      connState.text = `已连接 · 管理员 (${connState.backend})`
      const tr = await apiFetch('/admin/tasks')
      if (tr.ok) {
        const tj = await tr.json()
        liveTasks.value = tj.tasks || []
      }
      return
    }
    // Fallback for standard tenant key
    const sr = await apiFetch('/stats')
    if (sr.ok) {
      const s = await sr.json()
      metrics.pool = { ...metrics.pool, ...s }
      connState.ok = true
      connState.role = 'tenant'
      connState.backend = s.backend || 'browser'
      connState.paused = !!s.paused
      connState.text = `已连接 · 租户 (${s.scope || 'default'})`
    }
  } catch (_) {}
}

async function loadAdminSettings() {
  try {
    const r = await apiFetch('/admin/settings')
    if (!r.ok) return
    const j = await r.json()
    keysList.value = j.keys || []
    const s = j.settings || {}
    Object.assign(settingsData, s)
    if (Array.isArray(s.mock_latency)) {
      settingsData.mock_latency_str = `${s.mock_latency[0]}-${s.mock_latency[1]}`
    }
    if (Array.isArray(s.mock_answer_chars)) {
      settingsData.mock_answer_chars_str = `${s.mock_answer_chars[0]}-${s.mock_answer_chars[1]}`
    }
  } catch (_) {}
}

// ---- Pool & Admin Actions ----
async function bootstrapAdmin() {
  saveCreds()
  try {
    const r = await apiFetch('/admin/bootstrap', { method: 'POST' })
    const j = await r.json()
    if (!r.ok) {
      notify(j.detail || '管理员密钥已存在')
      return
    }
    apiKey.value = j.api_key
    saveCreds()
    await copyText(j.api_key, '已生成并复制管理员密钥！')
    await connect()
  } catch (e) {
    notify('生成失败: ' + e.message)
  }
}

async function togglePause() {
  const path = connState.paused ? '/admin/pool/resume' : '/admin/pool/pause'
  const r = await apiFetch(path, { method: 'POST' })
  if (r.ok) {
    notify(connState.paused ? '已恢复接单' : '已暂停接单')
    await refreshTelemetry()
  }
}

async function warmWorker() {
  const r = await apiFetch('/warm?n=1', { method: 'POST' })
  if (r.ok) {
    const j = await r.json()
    notify(`已预热 +${j.spawned} 个实例（当前 ${j.workers}）`)
    await refreshTelemetry()
  }
}

async function clearQueue() {
  if (!confirm('确定清空当前所有排队中的任务？')) return
  const r = await apiFetch('/admin/pool/clear', { method: 'POST' })
  if (r.ok) {
    const j = await r.json()
    notify(`已清空 ${j.cleared} 个排队任务`)
    await refreshTelemetry()
  }
}

async function resetMetrics() {
  if (!confirm('确定清空所有统计计数？')) return
  const r = await apiFetch('/admin/metrics/reset', { method: 'POST' })
  if (r.ok) {
    notify('统计已重置')
    await refreshTelemetry()
  }
}

async function cancelTask(tid) {
  const r = await apiFetch(`/tasks/${tid}`, { method: 'DELETE' })
  if (r.ok) {
    notify(`任务 ${tid} 已取消`)
    await refreshTelemetry()
  }
}

// ---- Key Management ----
async function createKey() {
  const body = {
    user_id: newKeyForm.user_id.trim() || null,
    max_concurrent: Number(newKeyForm.max_concurrent) || 2,
    queue_limit: Number(newKeyForm.queue_limit) || 20,
    note: newKeyForm.note.trim() || null,
  }
  const r = await apiFetch('/admin/keys', { method: 'POST', body: JSON.stringify(body) })
  const j = await r.json()
  if (!r.ok) {
    notify('创建失败: ' + (j.detail || r.status))
    return
  }
  newKeyForm.user_id = ''
  newKeyForm.note = ''
  revealedKeys[j.user_id] = true
  notify(`已创建租户 ${j.user_id}`)
  await loadAdminSettings()
  await refreshTelemetry()
}

async function updateKeyField(k, patch) {
  const r = await apiFetch(`/admin/keys/${encodeURIComponent(k.user_id)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
  if (r.ok) {
    notify(`已更新租户 ${k.user_id}`)
    await loadAdminSettings()
    await refreshTelemetry()
  }
}

async function rotateKey(k) {
  if (!confirm(`确定重置租户 ${k.user_id} 的密钥？旧密钥将立即失效。`)) return
  const r = await apiFetch(`/admin/keys/${encodeURIComponent(k.user_id)}/rotate`, { method: 'POST' })
  if (r.ok) {
    revealedKeys[k.user_id] = true
    notify(`已轮换 ${k.user_id} 密钥`)
    await loadAdminSettings()
  }
}

async function deleteKey(k) {
  if (!confirm(`确定删除租户 ${k.user_id} ？`)) return
  const r = await apiFetch(`/admin/keys/${encodeURIComponent(k.user_id)}`, { method: 'DELETE' })
  const j = await r.json()
  if (!r.ok) {
    notify('删除失败: ' + (j.detail || r.status))
    return
  }
  notify(`已删除租户 ${k.user_id}`)
  await loadAdminSettings()
  await refreshTelemetry()
}

// ---- Save Runtime Settings ----
async function saveSettings() {
  const body = {
    backend: settingsData.backend,
    min_workers: Number(settingsData.min_workers),
    max_workers: Number(settingsData.max_workers),
    queue_limit: Number(settingsData.queue_limit),
    idle_ttl: Number(settingsData.idle_ttl),
    answer_timeout: Number(settingsData.answer_timeout),
    goto_timeout: Number(settingsData.goto_timeout),
    ready_quiet_s: Number(settingsData.ready_quiet_s),
    max_requests_per_identity: Number(settingsData.max_requests_per_identity),
    block_streak_limit: Number(settingsData.block_streak_limit),
    tenant_rotate_identity: !!settingsData.tenant_rotate_identity,
    block_assets: !!settingsData.block_assets,
    metrics_preview_chars: Number(settingsData.metrics_preview_chars),
    mock_latency: settingsData.mock_latency_str,
    mock_fail_rate: Number(settingsData.mock_fail_rate),
    mock_answer_chars: settingsData.mock_answer_chars_str,
    mock_max_workers: Number(settingsData.mock_max_workers),
  }
  const r = await apiFetch('/admin/settings', { method: 'POST', body: JSON.stringify(body) })
  if (r.ok) {
    notify('设置已保存并热生效')
    await loadAdminSettings()
    await refreshTelemetry()
  } else {
    notify('保存失败: ' + r.status)
  }
}

// ---- Soak Test ----
async function startSoak() {
  const r = await apiFetch('/admin/soak/start', {
    method: 'POST',
    body: JSON.stringify({
      concurrency: Number(soakForm.concurrency) || 4,
      duration_s: Number(soakForm.duration_s) || 60,
      max_requests: Number(soakForm.max_requests) || 0,
      think_ms: Number(soakForm.think_ms) || 0,
    }),
  })
  const j = await r.json()
  if (r.ok) {
    metrics.soak = j
    notify('冒烟压测已启动')
  } else {
    notify('启动失败: ' + (j.detail || r.status))
  }
}

async function stopSoak() {
  const r = await apiFetch('/admin/soak/stop', { method: 'POST' })
  if (r.ok) {
    metrics.soak = await r.json()
    notify('冒烟压测已停止')
  }
}

// ---- Chat Playground ----
async function sendChat() {
  const q = chatInput.value.trim()
  if (!q || chatSending.value) return
  chatInput.value = ''
  chatLog.value.push({ role: 'user', content: q })
  const pending = reactive({ role: 'assistant', content: '正在思考并获取 AI Mode 回答…', meta: null, loading: true })
  chatLog.value.push(pending)
  chatSending.value = true
  await nextTick()
  if (chatBoxRef.value) chatBoxRef.value.scrollTop = chatBoxRef.value.scrollHeight

  const t0 = performance.now()
  try {
    const payload = {
      model: selectedModel.value,
      messages: [{ role: 'user', content: q }],
      fresh: chatFresh.value,
    }
    if (chatUser.value.trim()) payload.user = chatUser.value.trim()
    const r = await apiFetch('/v1/chat/completions', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    const j = await r.json()
    if (!r.ok) {
      throw new Error((j.error && j.error.message) || `HTTP ${r.status}`)
    }
    const text = j.choices?.[0]?.message?.content || '(空回答)'
    const g = j.gemini_proxy || {}
    const u = j.usage || {}
    pending.content = text
    pending.loading = false
    pending.meta = {
      sec: ((performance.now() - t0) / 1000).toFixed(2),
      queue_s: g.queue_s ?? 0,
      serve_s: g.serve_s ?? 0,
      mode: g.mode || 'goto',
      slot: g.slot || '-',
      solve: g.solve || 0,
      tokens: `${u.prompt_tokens || 0} + ${u.completion_tokens || 0} = ${u.total_tokens || 0}`,
      user_id: g.user_id || 'public',
    }
  } catch (e) {
    pending.content = '请求失败：' + e.message
    pending.loading = false
  } finally {
    chatSending.value = false
    await nextTick()
    if (chatBoxRef.value) chatBoxRef.value.scrollTop = chatBoxRef.value.scrollHeight
    refreshTelemetry()
  }
}

function onChatKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendChat()
  }
}

onMounted(() => {
  connect()
  pollTimer = setInterval(() => {
    if (autoRefresh.value && document.visibilityState === 'visible') {
      refreshTelemetry()
    }
  }, 2500)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="app-shell">
    <!-- Left Sidebar -->
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-logo">G5</div>
        <div>
          <div class="brand-title">Gemini5.0 Proxy</div>
          <div class="brand-sub">Vue 3 · OpenAI 协议网关</div>
        </div>
      </div>

      <nav class="nav-list">
        <button class="nav-btn" :class="{ active: activeTab === 'overview' }" @click="activeTab = 'overview'">
          <span>📊 实时监控大盘</span>
          <span class="nav-badge">{{ metrics.pool.busy }}/{{ metrics.pool.workers }}</span>
        </button>
        <button class="nav-btn" :class="{ active: activeTab === 'playground' }" @click="activeTab = 'playground'">
          <span>💬 AI 对话测试台</span>
          <span class="nav-badge">OpenAI</span>
        </button>
        <button class="nav-btn" :class="{ active: activeTab === 'traffic' }" @click="activeTab = 'traffic'">
          <span>🛡️ 流量与来源审计</span>
          <span class="nav-badge">{{ metrics.unique_ips }} IP</span>
        </button>
        <button class="nav-btn" :class="{ active: activeTab === 'keys' }" @click="activeTab = 'keys'">
          <span>🔑 租户与密钥管理</span>
          <span class="nav-badge">{{ keysList.length }}</span>
        </button>
        <button class="nav-btn" :class="{ active: activeTab === 'settings' }" @click="activeTab = 'settings'">
          <span>⚡ 压测与系统设置</span>
          <span class="nav-badge">{{ metrics.pool.backend }}</span>
        </button>
      </nav>

      <div class="sidebar-footer">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span>状态</span>
          <span class="pill" :class="connState.ok ? 'ok' : 'err'">
            <span class="dot" :class="connState.ok ? 'ok' : 'err'"></span>
            {{ connState.ok ? (connState.paused ? '已暂停' : '运行中') : '离线' }}
          </span>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>进程内存</span>
          <b class="mono">{{ metrics.process?.rss_mb ?? '-' }} MB</b>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>整机可用内存</span>
          <b class="mono">{{ metrics.process?.avail_mem_mb ?? '-' }} MB</b>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>活跃线程</span>
          <b class="mono">{{ metrics.process?.threads ?? '-' }}</b>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>运行时长</span>
          <b class="mono">{{ fmtDuration(metrics.uptime_s) }}</b>
        </div>
        <a
          href="https://ys666.site"
          target="_blank"
          rel="noopener"
          style="margin-top:6px;padding:7px 10px;border-radius:8px;background:linear-gradient(135deg,rgba(99,102,241,0.22),rgba(56,189,248,0.16));border:1px solid rgba(99,102,241,0.4);color:#e2e8f0;text-decoration:none;display:flex;align-items:center;justify-content:space-between;font-weight:600;font-size:12px"
        >
          <span>💎 跃数AI赞助</span>
          <span class="mono" style="color:#38bdf8">ys666.site ↗</span>
        </a>
      </div>
    </aside>

    <!-- Right Main Area -->
    <div class="main-area">
      <!-- Sticky Topbar -->
      <header class="topbar">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="color:var(--text-muted);font-size:12px">服务地址</span>
          <input v-model="baseUrl" style="width:210px" placeholder="http://38.244.6.87:8080" @keyup.enter="connect" />
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <span style="color:var(--text-muted);font-size:12px">API Key</span>
          <input
            v-model="apiKey"
            :type="showApiKey ? 'text' : 'password'"
            style="width:200px"
            placeholder="可选：管理员或租户密钥"
            @keyup.enter="connect"
          />
          <button class="btn ghost sm" type="button" @click="showApiKey = !showApiKey">
            {{ showApiKey ? '隐藏' : '显示' }}
          </button>
        </div>
        <button class="btn" type="button" @click="connect">连接</button>
        <button class="btn ghost" type="button" @click="refreshTelemetry">刷新</button>
        <button class="btn ghost" type="button" @click="bootstrapAdmin">生成管理员密钥</button>

        <label style="display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--text-muted);cursor:pointer">
          <input v-model="autoRefresh" type="checkbox" /> 自动刷新
        </label>

        <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
          <span class="pill" :class="connState.ok ? 'ok' : 'err'">{{ connState.text }}</span>
        </div>
      </header>

      <div class="content-wrap">
        <!-- ==================== TAB 1: OVERVIEW ==================== -->
        <template v-if="activeTab === 'overview'">
          <!-- KPI Row -->
          <div class="kpi-grid">
            <div class="kpi-card">
              <div class="kpi-label">
                <span>实例池负载 ({{ metrics.pool.backend }})</span>
                <span class="pill info">上限 {{ metrics.pool.max_workers }}</span>
              </div>
              <div class="kpi-value mono">{{ metrics.pool.busy }} / {{ metrics.pool.workers }}</div>
              <div class="progress">
                <div
                  class="progress-bar"
                  :style="{ width: Math.min(100, ((metrics.pool.busy || 0) / Math.max(1, metrics.pool.max_workers)) * 100) + '%' }"
                ></div>
              </div>
              <div class="kpi-sub">空闲 {{ metrics.pool.idle }} · 最小保活 {{ metrics.pool.min_workers }}</div>
            </div>

            <div class="kpi-card">
              <div class="kpi-label">
                <span>公平队列水位</span>
                <span class="pill" :class="metrics.pool.queued > 0 ? 'warn' : 'ok'">
                  {{ metrics.pool.queued > 0 ? '排队中' : '畅通' }}
                </span>
              </div>
              <div class="kpi-value mono">{{ metrics.pool.queued }} / {{ metrics.pool.queue_limit }}</div>
              <div class="progress">
                <div
                  class="progress-bar"
                  :style="{ width: Math.min(100, ((metrics.pool.queued || 0) / Math.max(1, metrics.pool.queue_limit)) * 100) + '%' }"
                ></div>
              </div>
              <div class="kpi-sub">累计接单 {{ metrics.pool.total_tasks }} · 拒绝 {{ metrics.pool.rejected }}</div>
            </div>

            <div class="kpi-card">
              <div class="kpi-label">
                <span>实时吞吐 (TPS)</span>
                <span class="pill ok">1h峰 {{ metrics.rates?.tps_peak_1h ?? 0 }}/s</span>
              </div>
              <div class="kpi-value mono">
                {{ metrics.rates?.tps_10s ?? 0 }} <span style="font-size:14px;color:var(--text-muted)">req/s</span>
              </div>
              <div class="kpi-sub">60s 均值 {{ metrics.rates?.tps_60s ?? 0 }}/s · 窗口 {{ metrics.rates?.tps_window ?? 0 }}/s</div>
            </div>

            <div class="kpi-card">
              <div class="kpi-label">
                <span>Token 吞吐速率</span>
                <span class="pill info">{{ metrics.tokenizer }}</span>
              </div>
              <div class="kpi-value mono">
                {{ fmtNum(metrics.rates?.tokens_per_min || 0) }} <span style="font-size:14px;color:var(--text-muted)">/min</span>
              </div>
              <div class="kpi-sub">
                累计入 {{ fmtNum(metrics.totals?.tin) }} · 出 {{ fmtNum(metrics.totals?.tout) }}
              </div>
            </div>

            <div class="kpi-card">
              <div class="kpi-label">
                <span>分位响应延迟</span>
                <span class="pill ok">样本 {{ metrics.latency?.n || 0 }}</span>
              </div>
              <div class="kpi-value mono">
                {{ metrics.latency?.p50 ?? '-' }}s
                <span style="font-size:13px;color:var(--text-muted)">/ P95 {{ metrics.latency?.p95 ?? '-' }}s</span>
              </div>
              <div class="kpi-sub">均值 {{ metrics.latency?.avg ?? '-' }}s · P99 {{ metrics.latency?.p99 ?? '-' }}s</div>
            </div>

            <div class="kpi-card">
              <div class="kpi-label">
                <span>窗口成功率 & 资源</span>
                <span class="pill" :class="(metrics.success_rate ?? 100) >= 95 ? 'ok' : 'warn'">
                  {{ metrics.success_rate ?? 100 }}%
                </span>
              </div>
              <div class="kpi-value mono">{{ metrics.process?.rss_mb ?? '-' }} MB</div>
              <div class="kpi-sub">
                成功 {{ metrics.totals?.ok }} · 错 {{ metrics.totals?.err }} · 拒 {{ metrics.totals?.rej }}
              </div>
            </div>
          </div>

          <!-- Trend Chart Panel -->
          <div class="panel">
            <div class="panel-header">
              <div>
                <h3 class="panel-title">📈 请求吞吐与 Token 实时曲线</h3>
                <div style="font-size:12px;color:var(--text-muted);margin-top:2px">
                  柱状图：成功(绿) / 限流拒绝(黄) / 错误(红) · 折线：每桶 Token 消耗（步长 {{ metrics.series?.step || 5 }}s）
                </div>
              </div>
              <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <select v-model.number="windowSec" @change="refreshTelemetry">
                  <option :value="300">最近 5 分钟</option>
                  <option :value="900">最近 15 分钟</option>
                  <option :value="3600">最近 1 小时</option>
                  <option :value="21600">最近 6 小时</option>
                  <option :value="86400">最近 24 小时</option>
                </select>
                <button class="btn ghost sm" type="button" @click="togglePause">
                  {{ connState.paused ? '▶ 恢复接单' : '⏸ 暂停接单' }}
                </button>
                <button class="btn ghost sm" type="button" @click="warmWorker">🔥 预热 +1</button>
                <button class="btn ghost sm" type="button" @click="clearQueue">🧹 清空排队</button>
                <button class="btn ghost sm" type="button" @click="resetMetrics">🔄 重置统计</button>
              </div>
            </div>

            <div style="background:var(--bg-base);border:1px solid var(--border);border-radius:10px;padding:12px">
              <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:6px">
                <span>峰值请求：{{ chartData.maxReq }} req/桶</span>
                <span>峰值 Token：{{ fmtNum(chartData.maxTok) }} tok/桶</span>
              </div>
              <svg viewBox="0 0 860 180" style="width:100%;height:180px;display:block" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="tokGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.28" />
                    <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0" />
                  </linearGradient>
                </defs>
                <polygon v-if="chartData.tokArea" :points="chartData.tokArea" fill="url(#tokGrad)" />
                <rect
                  v-for="(b, i) in chartData.bars"
                  :key="i"
                  :x="b.x"
                  :y="b.y"
                  :width="b.w"
                  :height="b.h"
                  :fill="b.color"
                  rx="1.5"
                >
                  <title>{{ b.title }}</title>
                </rect>
                <polyline
                  v-if="chartData.tokLine"
                  fill="none"
                  stroke="#38bdf8"
                  stroke-width="2"
                  :points="chartData.tokLine"
                />
                <text
                  v-if="!chartData.bars.length && !chartData.tokLine"
                  x="430"
                  y="95"
                  fill="#64748b"
                  text-anchor="middle"
                  font-size="13"
                >
                  暂无窗口流量数据，可在「对话测试台」或「压测」发起请求
                </text>
              </svg>
            </div>
          </div>

          <!-- Live Worker Slots Panel -->
          <div class="panel">
            <div class="panel-header">
              <h3 class="panel-title">🖥️ Worker 实例槽位实时状态 ({{ metrics.pool.slots?.length || 0 }})</h3>
              <span style="font-size:12px;color:var(--text-muted)">
                按需自动扩容至 {{ metrics.pool.max_workers }} 个实例，空闲超时自动回收
              </span>
            </div>
            <div v-if="metrics.pool.slots?.length" class="slot-grid">
              <div
                v-for="s in metrics.pool.slots"
                :key="s.sid"
                class="slot-card"
                :class="{ busy: s.busy }"
              >
                <div style="display:flex;justify-content:space-between;align-items:center">
                  <b class="mono">Slot #{{ s.sid }}</b>
                  <span class="pill" :class="s.busy ? 'info' : 'ok'">
                    <span class="dot" :class="s.busy ? 'warn' : 'ok'"></span>
                    {{ s.busy ? '执行中' : `空闲 ${s.idle_s ?? 0}s` }}
                  </span>
                </div>
                <div style="font-size:12px;color:var(--text-muted);display:flex;justify-content:space-between">
                  <span>当前租户: <b>{{ s.user || s.session_user || '-' }}</b></span>
                  <span>已服务: <b class="mono">{{ s.served }}</b> 次</span>
                </div>
                <div v-if="s.seq" style="font-size:12px;color:var(--text-muted);display:flex;justify-content:space-between">
                  <span>身份代次: <b class="mono">#{{ s.seq }}</b></span>
                  <span v-if="s.task">任务: <code class="mono">{{ s.task.slice(0, 8) }}</code></span>
                </div>
              </div>
            </div>
            <div v-else style="color:var(--text-muted);font-size:13px;padding:12px 0">
              当前无常驻实例（MIN_WORKERS={{ metrics.pool.min_workers }}），收到首个请求或点击「预热 +1」时将立即启动。
            </div>
          </div>
        </template>

        <!-- ==================== TAB 2: AI PLAYGROUND ==================== -->
        <template v-if="activeTab === 'playground'">
          <div class="panel">
            <div class="panel-header">
              <div>
                <h3 class="panel-title">💬 Google AI Mode 在线对话测试台</h3>
                <div style="font-size:12px;color:var(--text-muted)">
                  通过 OpenAI 兼容接口 <code>/v1/chat/completions</code> 实时提问，支持同会话连续追问（followup）
                </div>
              </div>
              <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <label style="display:flex;align-items:center;gap:5px;font-size:12.5px;color:var(--text-muted);cursor:pointer">
                  <input v-model="chatFresh" type="checkbox" /> 强制新开页面 (fresh)
                </label>
                <input
                  v-model="chatUser"
                  style="width:140px"
                  placeholder="代租户发 (可选)"
                  title="管理员密钥可指定租户 user_id"
                />
                <button class="btn ghost sm" type="button" @click="showCodeHelper = !showCodeHelper">
                  {{ showCodeHelper ? '隐藏调用代码' : '📋 查看调用代码' }}
                </button>
                <button class="btn ghost sm" type="button" @click="chatLog = []">清空对话</button>
              </div>
            </div>

            <div v-if="showCodeHelper" style="margin-bottom:14px;background:var(--bg-base);border:1px solid var(--border);border-radius:10px;padding:12px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <span style="font-size:12px;color:var(--text-muted)">cURL / OpenAI SDK 调用示例</span>
                <button class="btn ghost sm" type="button" @click="copyText(curlSnippet)">复制 cURL</button>
              </div>
              <pre style="margin:0;font-size:12px;overflow-x:auto;color:#a5b4fc">{{ curlSnippet }}</pre>
            </div>

            <div ref="chatBoxRef" class="chat-box">
              <div v-for="(m, idx) in chatLog" :key="idx" class="chat-msg" :class="m.role">
                <div>{{ m.content }}</div>
                <div v-if="m.meta" class="chat-meta">
                  <span class="pill ok">{{ m.meta.mode }}</span>
                  <span>耗时 <b>{{ m.meta.sec }}s</b> (排队 {{ m.meta.queue_s }}s / 服务 {{ m.meta.serve_s }}s)</span>
                  <span>Slot <b>#{{ m.meta.slot }}</b></span>
                  <span>Token <b>{{ m.meta.tokens }}</b></span>
                  <span>租户 <b>{{ m.meta.user_id }}</b></span>
                  <span v-if="m.meta.solve">打码 <b>{{ m.meta.solve }}</b></span>
                  <button
                    class="btn ghost sm"
                    style="margin-left:auto;padding:2px 7px"
                    type="button"
                    @click="copyText(m.content)"
                  >
                    复制回答
                  </button>
                </div>
              </div>
            </div>

            <form style="display:flex;gap:10px;margin-top:12px" @submit.prevent="sendChat">
              <textarea
                v-model="chatInput"
                rows="2"
                style="flex:1;resize:vertical"
                placeholder="输入问题，按 Enter 发送，Shift + Enter 换行…"
                @keydown="onChatKeydown"
              ></textarea>
              <button class="btn" type="submit" :disabled="chatSending" style="padding:0 22px">
                {{ chatSending ? '请求中…' : '发送提问' }}
              </button>
            </form>
          </div>
        </template>

        <!-- ==================== TAB 3: TRAFFIC & AUDIT ==================== -->
        <template v-if="activeTab === 'traffic'">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px">
            <!-- Tenant Quota & Usage -->
            <div class="panel">
              <div class="panel-header">
                <h3 class="panel-title">👥 租户配额与累计用量</h3>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>租户</th>
                      <th>在跑 / 排队</th>
                      <th>配额 (并发/队)</th>
                      <th>成功 / 错 / 拒</th>
                      <th>累计 Token (入 / 出)</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="u in mergedTenants" :key="u.user_id">
                      <td><b>{{ u.user_id }}</b></td>
                      <td class="mono">{{ u.running }} / {{ u.queued }}</td>
                      <td class="mono">{{ u.max_concurrent }} / {{ u.queue_limit }}</td>
                      <td class="mono">
                        <span style="color:var(--success)">{{ u.ok }}</span> /
                        <span style="color:var(--danger)">{{ u.err }}</span> /
                        <span style="color:var(--warning)">{{ u.rej }}</span>
                      </td>
                      <td class="mono">{{ fmtNum(u.tin) }} / {{ fmtNum(u.tout) }}</td>
                    </tr>
                    <tr v-if="!mergedTenants.length">
                      <td colspan="5" style="color:var(--text-muted);text-align:center">暂无租户数据</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- Source IP Audit -->
            <div class="panel">
              <div class="panel-header">
                <h3 class="panel-title">🌐 来源 IP 审计 ({{ metrics.ips?.length || 0 }})</h3>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>来源 IP</th>
                      <th>关联租户</th>
                      <th>成功/错/拒</th>
                      <th>鉴权失败</th>
                      <th>Token</th>
                      <th>最近活跃</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="ip in metrics.ips" :key="ip.ip">
                      <td><code class="mono">{{ ip.ip }}</code></td>
                      <td>{{ (ip.users || []).join(', ') || '-' }}</td>
                      <td class="mono">{{ ip.ok }} / {{ ip.err }} / {{ ip.rej }}</td>
                      <td>
                        <span class="pill" :class="ip.auth_fail > 0 ? 'err' : 'ok'">{{ ip.auth_fail || 0 }}</span>
                      </td>
                      <td class="mono">{{ fmtNum((ip.tin || 0) + (ip.tout || 0)) }}</td>
                      <td style="color:var(--text-muted)">{{ fmtAgo(ip.last) }}</td>
                    </tr>
                    <tr v-if="!metrics.ips?.length">
                      <td colspan="6" style="color:var(--text-muted);text-align:center">暂无来源 IP 记录</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <!-- Active Tasks -->
          <div v-if="liveTasks.length" class="panel">
            <div class="panel-header">
              <h3 class="panel-title">⏳ 当前在跑与排队任务 ({{ liveTasks.length }})</h3>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>任务 ID</th>
                    <th>状态</th>
                    <th>租户</th>
                    <th>Slot</th>
                    <th>来源 IP</th>
                    <th>已等待/执行</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="t in liveTasks" :key="t.id">
                    <td><code class="mono">{{ t.id }}</code></td>
                    <td><span class="pill info">{{ t.state }}</span></td>
                    <td><b>{{ t.user_id }}</b></td>
                    <td class="mono">{{ t.slot ? '#' + t.slot : '-' }}</td>
                    <td><code class="mono">{{ t.ip || '-' }}</code></td>
                    <td class="mono">{{ t.age_s }}s</td>
                    <td>
                      <button class="btn danger sm" type="button" @click="cancelTask(t.id)">取消任务</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Recent Requests Table -->
          <div class="panel">
            <div class="panel-header">
              <h3 class="panel-title">📜 最近请求明细 ({{ filteredRecent.length }})</h3>
              <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                <select v-model="recentFilter">
                  <option value="all">全部状态</option>
                  <option value="ok">仅成功 (OK)</option>
                  <option value="rej">仅限流/拒绝</option>
                  <option value="err">仅错误</option>
                </select>
                <input v-model="recentSearch" placeholder="搜索租户 / IP / 摘要…" style="width:200px" />
              </div>
            </div>
            <div class="table-wrap" style="max-height:420px;overflow-y:auto">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>状态</th>
                    <th>租户</th>
                    <th>来源 IP</th>
                    <th>接口</th>
                    <th>耗时 (总 / 排 / 服)</th>
                    <th>Token (入 / 出)</th>
                    <th>问题摘要 / 错误信息</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(r, idx) in filteredRecent" :key="idx">
                    <td class="mono">{{ fmtTime(r.ts) }}</td>
                    <td><span class="pill" :class="r.cls">{{ r.kind }}</span></td>
                    <td><b>{{ r.user }}</b></td>
                    <td><code class="mono">{{ r.ip }}</code></td>
                    <td><code class="mono">{{ r.endpoint }}</code></td>
                    <td class="mono">{{ r.total_s ?? '-' }}s ({{ r.queue_s ?? '-' }} / {{ r.serve_s ?? '-' }})</td>
                    <td class="mono">{{ r.tin || 0 }} / {{ r.tout || 0 }}</td>
                    <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                      <span v-if="r.error" style="color:#f87171">{{ r.error }}</span>
                      <span v-else>{{ r.preview || '-' }}</span>
                    </td>
                  </tr>
                  <tr v-if="!filteredRecent.length">
                    <td colspan="8" style="color:var(--text-muted);text-align:center">暂无匹配的请求记录</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>

        <!-- ==================== TAB 4: TENANTS & KEYS ==================== -->
        <template v-if="activeTab === 'keys'">
          <div class="panel">
            <div class="panel-header">
              <div>
                <h3 class="panel-title">🔑 租户配额与 API 密钥管理</h3>
                <div style="font-size:12px;color:var(--text-muted)">
                  每个租户拥有独立密钥、并发上限（在跑+排队）与排队上限，超限立即返回 BUSY，互不挤占
                </div>
              </div>
            </div>

            <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:16px;padding:14px;background:var(--bg-base);border:1px solid var(--border);border-radius:10px">
              <label class="field-label">
                <span>租户用户名 (可留空自动生成)</span>
                <input v-model="newKeyForm.user_id" placeholder="如 alice" style="width:150px" />
              </label>
              <label class="field-label">
                <span>并发上限</span>
                <input v-model.number="newKeyForm.max_concurrent" type="number" min="1" style="width:95px" />
              </label>
              <label class="field-label">
                <span>排队上限</span>
                <input v-model.number="newKeyForm.queue_limit" type="number" min="1" style="width:95px" />
              </label>
              <label class="field-label" style="flex:1;min-width:160px">
                <span>备注</span>
                <input v-model="newKeyForm.note" placeholder="业务线 / 用途说明" />
              </label>
              <button class="btn" type="button" @click="createKey">+ 创建租户密钥</button>
            </div>

            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>租户 ID</th>
                    <th>API 密钥</th>
                    <th>并发上限</th>
                    <th>排队上限</th>
                    <th>状态</th>
                    <th>备注</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="k in keysList" :key="k.user_id">
                    <td><b>{{ k.user_id }}</b></td>
                    <td>
                      <div style="display:flex;align-items:center;gap:6px">
                        <code class="mono">{{ maskKey(k.key, k.user_id) }}</code>
                        <button
                          class="btn ghost sm"
                          type="button"
                          @click="revealedKeys[k.user_id] = !revealedKeys[k.user_id]"
                        >
                          {{ revealedKeys[k.user_id] ? '隐藏' : '查看' }}
                        </button>
                        <button class="btn ghost sm" type="button" @click="copyText(k.key, `已复制 ${k.user_id} 密钥`)">
                          复制
                        </button>
                      </div>
                    </td>
                    <td>
                      <input
                        v-model.number="k.max_concurrent"
                        type="number"
                        min="1"
                        style="width:75px"
                        @change="updateKeyField(k, { max_concurrent: k.max_concurrent })"
                      />
                    </td>
                    <td>
                      <input
                        v-model.number="k.queue_limit"
                        type="number"
                        min="1"
                        style="width:75px"
                        @change="updateKeyField(k, { queue_limit: k.queue_limit })"
                      />
                    </td>
                    <td>
                      <span class="pill" :class="k.disabled ? 'err' : 'ok'">
                        {{ k.disabled ? '已禁用' : '启用中' }}
                      </span>
                    </td>
                    <td>
                      <input
                        v-model="k.note"
                        placeholder="添加备注"
                        style="width:140px"
                        @change="updateKeyField(k, { note: k.note })"
                      />
                    </td>
                    <td>
                      <div style="display:flex;gap:6px">
                        <button
                          class="btn ghost sm"
                          type="button"
                          @click="updateKeyField(k, { disabled: !k.disabled })"
                        >
                          {{ k.disabled ? '启用' : '禁用' }}
                        </button>
                        <button class="btn ghost sm" type="button" @click="rotateKey(k)">轮换密钥</button>
                        <button class="btn danger sm" type="button" @click="deleteKey(k)">删除</button>
                      </div>
                    </td>
                  </tr>
                  <tr v-if="!keysList.length">
                    <td colspan="7" style="color:var(--text-muted);text-align:center">
                      当前未配置租户密钥（免鉴权 public 模式），可在上方创建密钥或点击顶栏「生成管理员密钥」
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>

        <!-- ==================== TAB 5: SOAK & SETTINGS ==================== -->
        <template v-if="activeTab === 'settings'">
          <!-- Soak Test Panel -->
          <div class="panel">
            <div class="panel-header">
              <div>
                <h3 class="panel-title">🧪 无外网 Mock 冒烟与并发压测 (SoakRunner)</h3>
                <div style="font-size:12px;color:var(--text-muted)">
                  当前主池为真实浏览器模式时，自动启动隔离的 Mock 池压测调度内核与公平队列，不消耗打码与外网资源
                </div>
              </div>
              <div style="display:flex;gap:8px">
                <button class="btn" type="button" :disabled="metrics.soak?.running" @click="startSoak">
                  ▶ 启动冒烟压测
                </button>
                <button class="btn ghost" type="button" :disabled="!metrics.soak?.running" @click="stopSoak">
                  ⏹ 停止压测
                </button>
              </div>
            </div>

            <div class="form-grid" style="margin-bottom:14px">
              <label class="field-label">
                <span>压测并发线程数</span>
                <input v-model.number="soakForm.concurrency" type="number" min="1" max="64" />
              </label>
              <label class="field-label">
                <span>持续时长 (秒)</span>
                <input v-model.number="soakForm.duration_s" type="number" min="5" max="86400" />
              </label>
              <label class="field-label">
                <span>总请求上限 (0 为按时长)</span>
                <input v-model.number="soakForm.max_requests" type="number" min="0" />
              </label>
              <label class="field-label">
                <span>单线程思考间隔 (毫秒)</span>
                <input v-model.number="soakForm.think_ms" type="number" min="0" />
              </label>
            </div>

            <div
              v-if="metrics.soak && metrics.soak.started"
              style="padding:14px;background:var(--bg-base);border:1px solid var(--border);border-radius:10px;display:flex;flex-wrap:wrap;gap:18px;align-items:center"
            >
              <div>
                <span style="color:var(--text-muted);font-size:12px">运行状态</span>
                <div>
                  <span class="pill" :class="metrics.soak.running ? 'info' : 'ok'">
                    {{ metrics.soak.running ? '压测进行中' : '已完成' }} ({{ metrics.soak.mode }})
                  </span>
                </div>
              </div>
              <div>
                <span style="color:var(--text-muted);font-size:12px">已耗时 / 吞吐</span>
                <div class="mono"><b>{{ metrics.soak.elapsed_s }}s · {{ metrics.soak.tps }} req/s</b></div>
              </div>
              <div>
                <span style="color:var(--text-muted);font-size:12px">完成计数 (总 / OK / 错 / 拒)</span>
                <div class="mono">
                  <b>{{ metrics.soak.sent }}</b> ({{ metrics.soak.ok }} / {{ metrics.soak.err }} / {{ metrics.soak.rej }})
                </div>
              </div>
              <div>
                <span style="color:var(--text-muted);font-size:12px">分位延迟 (P50 / P95 / P99)</span>
                <div class="mono">
                  <b>{{ metrics.soak.latency?.p50 ?? '-' }} / {{ metrics.soak.latency?.p95 ?? '-' }} / {{ metrics.soak.latency?.p99 ?? '-' }}s</b>
                </div>
              </div>
              <div>
                <span style="color:var(--text-muted);font-size:12px">内存变化 (起 / 现 / 峰 / 增)</span>
                <div class="mono">
                  <b>
                    {{ metrics.soak.memory?.start_mb ?? '-' }} / {{ metrics.soak.memory?.now_mb ?? '-' }} /
                    {{ metrics.soak.memory?.peak_mb ?? '-' }} / {{ metrics.soak.memory?.delta_mb ?? 0 }} MB
                  </b>
                </div>
              </div>
              <div>
                <span style="color:var(--text-muted);font-size:12px">不变量校验</span>
                <div>
                  <span class="pill" :class="metrics.soak.invariants?.counts_match ? 'ok' : 'err'">
                    {{ metrics.soak.invariants?.counts_match ? '✓ 计数一致' : '✗ 计数异常' }}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <!-- Runtime Settings Panel -->
          <div class="panel">
            <div class="panel-header">
              <div>
                <h3 class="panel-title">⚙️ 运行时参数热配置</h3>
                <div style="font-size:12px;color:var(--text-muted)">
                  机器探测容量上限：<b>{{ settingsData.capacity?.cap ?? 8 }}</b> 个浏览器实例（CPU {{ settingsData.capacity?.cpus ?? 2 }} 核 I/O 并发放大，单实例瘦身 ~280MB，内置 350MB 动态内存背压防 OOM）
                </div>
              </div>
              <button class="btn" type="button" @click="saveSettings">💾 保存并立即生效</button>
            </div>

            <div class="form-grid">
              <label class="field-label">
                <span>执行后端模式 (热切换)</span>
                <select v-model="settingsData.backend">
                  <option value="browser">browser (真实 Chrome 浏览器)</option>
                  <option value="mock">mock (本地模拟，不出网)</option>
                </select>
              </label>
              <label class="field-label">
                <span>最少常驻实例 (min_workers)</span>
                <input v-model.number="settingsData.min_workers" type="number" min="0" />
              </label>
              <label class="field-label">
                <span>最大扩容实例 (max_workers)</span>
                <input v-model.number="settingsData.max_workers" type="number" min="1" />
              </label>
              <label class="field-label">
                <span>全局队列容量 (queue_limit)</span>
                <input v-model.number="settingsData.queue_limit" type="number" min="1" />
              </label>
              <label class="field-label">
                <span>空闲实例回收秒数 (idle_ttl)</span>
                <input v-model.number="settingsData.idle_ttl" type="number" min="10" />
              </label>
              <label class="field-label">
                <span>等待回答超时秒数 (answer_timeout)</span>
                <input v-model.number="settingsData.answer_timeout" type="number" min="10" />
              </label>
              <label class="field-label">
                <span>页面打开超时秒数 (goto_timeout)</span>
                <input v-model.number="settingsData.goto_timeout" type="number" min="5" />
              </label>
              <label class="field-label">
                <span>回答停稳判定秒数 (ready_quiet_s)</span>
                <input v-model.number="settingsData.ready_quiet_s" type="number" step="0.1" min="0.2" />
              </label>
              <label class="field-label">
                <span>单身份最大请求次数</span>
                <input v-model.number="settingsData.max_requests_per_identity" type="number" min="1" />
              </label>
              <label class="field-label">
                <span>连续被拦轮换阈值</span>
                <input v-model.number="settingsData.block_streak_limit" type="number" min="1" />
              </label>
              <label class="field-label">
                <span>统计保留问题预览字数</span>
                <input v-model.number="settingsData.metrics_preview_chars" type="number" min="0" />
              </label>
              <label class="field-label">
                <span>Mock 延迟区间 (秒)</span>
                <input v-model="settingsData.mock_latency_str" placeholder="1.5-4.0" />
              </label>
              <label class="field-label">
                <span>Mock 模拟失败率 (0~1)</span>
                <input v-model.number="settingsData.mock_fail_rate" type="number" step="0.01" min="0" max="1" />
              </label>
              <label class="field-label">
                <span>Mock 回答字数区间</span>
                <input v-model="settingsData.mock_answer_chars_str" placeholder="200-1200" />
              </label>
            </div>

            <div style="display:flex;gap:22px;margin-top:14px">
              <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                <input v-model="settingsData.tenant_rotate_identity" type="checkbox" />
                <span>切换租户时重建全新浏览器指纹与 Cookie</span>
              </label>
              <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                <input v-model="settingsData.block_assets" type="checkbox" />
                <span>拦截图片/字体/媒体资源以节省内存与带宽</span>
              </label>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div v-if="toastMsg" class="toast">{{ toastMsg }}</div>
  </div>
</template>
