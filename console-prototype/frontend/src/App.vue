<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Monitor, DataLine, Operation, Refresh, ArrowRight, VideoPlay, VideoPause, Plus, Document, Connection, CircleCheck, Warning, Download, Search, Share, Files } from '@element-plus/icons-vue'
import { api, APIError, query, emptyPage, date, label, tone, deviceName, short, type Row, type Page } from './api'

const initial = location.hash.slice(1)
const page = ref(['overview', 'data', 'control', 'vpp-workbench', 'grid-workbench'].includes(initial) ? initial : 'overview')
const nav = [{ id: 'overview', title: '运行总览', subtitle: '系统与设备', icon: Monitor }, { id: 'data', title: '数据与批次', subtitle: '原文与证据', icon: DataLine }, { id: 'control', title: '实验控制', subtitle: '采集与恢复', icon: Operation }, { id: 'vpp-workbench', title: 'VPP 工作台', subtitle: '收件与验签', icon: Share }, { id: 'grid-workbench', title: 'GRID 工作台', subtitle: '批次核验', icon: Files }]
const authUser = ref<Row | null>(null), authReady = ref(false), authBusy = ref(false), authError = ref('')
const loginForm = ref({ username: '', password: '' })
const forbidden = ref(false)
const allowedPage = (id: string) => authUser.value?.role === 'grid' ? id === 'grid-workbench' : id !== 'grid-workbench'
const visibleNav = computed(() => authUser.value?.role === 'grid' ? nav.filter(item => item.id === 'grid-workbench') : nav.filter(item => item.id !== 'grid-workbench'))
const activeTitle = computed(() => nav.find(item => item.id === page.value)?.title)
const pageDescription = computed(() => ({ overview: '从设备采集到回执复核，查看每一步实际发生的结果。', data: '按编号追踪原始记录、签名包与核验依据。', control: '控制设备记录采集，查看持久化运行状态。', 'vpp-workbench': '查看 Edge 节点设备状态、VPP 收件批次与验签结果。', 'grid-workbench': '逐个核对 VPP 原始批次的本地摘要与链上摘要。' }[page.value] || ''))
function navigate(id: string) { if (!allowedPage(id)) { forbidden.value = true; return }; forbidden.value = false; page.value = id; location.hash = id; if (id === 'data') void loadTable(); if (id === 'control') void loadRuns(); if (id === 'vpp-workbench') void loadVppWorkbench(); if (id === 'grid-workbench') void loadGridWorkbench() }

function applySession(user: Row | null) {
  authUser.value = user
  if (!user) { page.value = 'login'; location.hash = 'login'; authReady.value = true; return }
  const requested = location.hash.slice(1)
  const canOpen = requested !== 'login' && allowedPage(requested)
  page.value = canOpen ? requested : user.role === 'grid' ? 'grid-workbench' : 'vpp-workbench'
  location.hash = page.value; forbidden.value = requested !== 'login' && !canOpen; authReady.value = true
}
async function checkSession() {
  authReady.value = false
  try { const result = await api<Row>('/api/auth/me'); applySession(result.user || result) }
  catch (error) { if ((error as APIError).status === 403) { authError.value = '当前账号无效或未分配 VPP / GRID 工作台角色。'; authReady.value = true; page.value = 'login' } else applySession(null) }
}
async function login() {
  if (authBusy.value) return
  authBusy.value = true; authError.value = ''
  try { const result = await api<Row>('/api/auth/login', { method: 'POST', body: loginForm.value }); applySession(result.user || result); loginForm.value.password = ''; if (authUser.value) navigate(page.value) }
  catch (error) { authError.value = (error as Error).message }
  finally { authBusy.value = false }
}
async function logout() {
  try { await api('/api/auth/logout', { method: 'POST' }) }
  catch (error) { if ((error as APIError).status !== 401) authError.value = (error as Error).message }
  applySession(null)
}

const edge = ref<Row | null>(null), edge2 = ref<Row | null>(null), gridStatus = ref<Row | null>(null), vpp = ref<Row | null>(null)
const selectedEdgeId = ref('EDGE-001')
const selectedEdge = computed(() => selectedEdgeId.value === 'EDGE-002' ? edge2.value : edge.value)
const selectedEdgeAvailable = computed(() => selectedEdgeId.value === 'EDGE-002' ? edge2Available.value : edgeAvailable.value)
const devices = ref<Row[]>([]), edgeAvailable = ref<boolean | null>(null), vppAvailable = ref<boolean | null>(null)
const edge2Available = ref<boolean | null>(null)
const lastRefresh = ref(''), devicesRefresh = ref(''), devicesError = ref(''), refreshError = ref('')
const refreshing = ref(false), operation = ref(''), operationError = ref(''), lastAction = ref<Row | null>(null)
const clock = ref(Date.now()), serverOffset = ref(0)
const countdown = computed(() => {
  if (!edge.value) return '—'
  const seconds = Math.max(0, Math.ceil((Date.parse(edge.value.windowEnd) - clock.value - serverOffset.value) / 1000))
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0')
})
const metrics = computed(() => [
  { name: '配置设备', value: edge.value?.configuredDevices, unit: '台', note: '后端设备清单' },
  { name: '本窗口产生数据的设备', value: edge.value?.currentWindowDevices, unit: '台', note: '按记录去重，不等于在线探测' },
  { name: '本窗口记录', value: edge.value?.currentWindowRecords, unit: '条', note: 'UTC 左闭右开窗口' },
  { name: '待投递批次', value: edge.value?.pendingBatches, unit: '批', note: '含等待自动重试', accent: 'amber' },
  { name: '边缘复核通过', value: edge.value?.verifiedBatches, unit: '批', note: '已核对 VPP 回执', accent: 'green' },
  { name: '拒绝批次', value: edge.value?.rejectedBatches, unit: '批', note: '保留原文与失败依据', accent: 'red' }
])

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const result = await api('/api/overview')
    edgeAvailable.value = result.edge.available; edge2Available.value = result.edge2?.available ?? false; vppAvailable.value = result.vpp.available; gridStatus.value = result.grid?.data || null
    if (result.edge.available) { edge.value = result.edge.data; serverOffset.value = Date.parse(result.edge.data.serverTime) - Date.now() }
    if (result.edge2?.available) edge2.value = result.edge2.data
    if (result.vpp.available) vpp.value = result.vpp.data
    refreshError.value = [!result.edge.available && 'EDGE-001 或其数据库不可用', result.edge2 && !result.edge2.available && 'EDGE-002 或其数据库不可用', !result.vpp.available && 'VPP 服务或其数据库不可用'].filter(Boolean).join('；')
    if (result.edge.available || result.edge2?.available || result.vpp.available) lastRefresh.value = result.checkedAt
  } catch (error) { edgeAvailable.value = false; vppAvailable.value = false; refreshError.value = (error as Error).message }
  finally { refreshing.value = false }
  try { const result = await api('/api/' + (selectedEdgeId.value === 'EDGE-002' ? 'edge2' : 'edge') + '/devices'); devices.value = result.items; devicesError.value = ''; devicesRefresh.value = new Date().toISOString() }
  catch (error) { devicesError.value = (error as Error).message }
}

const vppWorkbench = ref<Row | null>(null), vppWorkbenchError = ref(''), vppWorkbenchBusy = ref(false)
const vppEdges = computed(() => vppWorkbench.value?.edgeRoster || vppWorkbench.value?.edges || [])
const vppInbox = computed(() => vppWorkbench.value?.batches?.items || vppWorkbench.value?.receivedBatches?.items || vppWorkbench.value?.batches || vppWorkbench.value?.receivedBatches || [])
const gridDetailDrawer = ref(false)
const gridOverview = ref<Row | null>(null), gridBatches = ref<Page>(emptyPage()), gridRecords = ref<Page>(emptyPage()), gridBatchDetail = ref<Row | null>(null)
const gridBatchRecords = ref<Page>(emptyPage())
const gridVerifications = ref<Row[]>([]), gridError = ref(''), gridBusy = ref(false), gridActionBusy = ref(false)
const gridPage = ref(1), gridPageSize = 25
const gridRecordPage = ref(1), gridRecordPageSize = 25, gridFilters = ref({ vppId: '', edgeId: '', deviceId: '', packetId: '', from: '', to: '' })
const gridDateRange = ref<Date[] | null>(null)
function applyGridFilters() {
  gridPage.value = 1; gridRecordPage.value = 1
  gridFilters.value.from = gridDateRange.value?.[0]?.toISOString() || ''
  gridFilters.value.to = gridDateRange.value?.[1]?.toISOString() || ''
  void loadGridWorkbench()
}
function clearGridFilters() {
  gridFilters.value = { vppId: '', edgeId: '', deviceId: '', packetId: '', from: '', to: '' }
  gridDateRange.value = null; applyGridFilters()
}
async function loadVppWorkbench() {
  vppWorkbenchBusy.value = true; vppWorkbenchError.value = ''
  try {
    const [workbench, overview, edgeOneDevices, edgeTwoDevices] = await Promise.all([
      api<Row>('/api/vpp/workbench/overview'), api<Row>('/api/overview'),
      api<Row>('/api/edge/devices'), api<Row>('/api/edge2/devices')
    ])
    vppWorkbench.value = { ...workbench, overview, deviceGroups: [{ edgeId: 'EDGE-001', items: edgeOneDevices.items || [] }, { edgeId: 'EDGE-002', items: edgeTwoDevices.items || [] }] }
  }
  catch (error) { vppWorkbenchError.value = (error as Error).message }
  finally { vppWorkbenchBusy.value = false }
}
const vppDeviceGroups = computed(() => vppWorkbench.value?.deviceGroups || [])
const vppDeviceEdge = ref('EDGE-001'), vppDeviceType = ref('all')
const deviceTypes = computed<string[]>(() => [...new Set<string>(vppDeviceGroups.value.flatMap((group: Row) => group.items.map((item: Row) => String(item.deviceType || ''))).filter(Boolean))])
const filteredVppDevices = computed(() => {
  const group = vppDeviceGroups.value.find((item: Row) => item.edgeId === vppDeviceEdge.value)
  return (group?.items || []).filter((item: Row) => vppDeviceType.value === 'all' || item.deviceType === vppDeviceType.value)
})
function maskSensitive(value: unknown) {
  const text = String(value || '')
  if (!text) return '未记录'
  if (text.length <= 10) return '*'.repeat(text.length)
  return text.slice(0, 6) + '****' + text.slice(-4)
}
function shortId(value: unknown) { const text = String(value || '未记录'); return text.length > 26 ? text.slice(0, 12) + '…' + text.slice(-10) : text }
const edgeOverviewFor = (id: string) => id === 'EDGE-002' ? vppWorkbench.value?.overview?.edge2 : vppWorkbench.value?.overview?.edge
async function loadGridWorkbench() {
  gridBusy.value = true; gridError.value = ''
  const results = await Promise.allSettled([
    api<Row>('/api/grid/overview'),
    api<Page>('/api/grid/batches' + query({ page: gridPage.value, pageSize: gridPageSize, ...gridFilters.value })),
    api<Page>('/api/grid/records' + query({ page: gridRecordPage.value, pageSize: gridRecordPageSize, ...gridFilters.value }))
  ])
  if (results[0].status === 'fulfilled') gridOverview.value = results[0].value
  else gridError.value = (results[0].reason as Error).message
  if (results[1].status === 'fulfilled') gridBatches.value = results[1].value
  else gridError.value = [gridError.value, (results[1].reason as Error).message].filter(Boolean).join('；')
  if (results[2]?.status === 'fulfilled') gridRecords.value = results[2].value
  else if (results[2]?.status === 'rejected') gridError.value = [gridError.value, (results[2].reason as Error).message].filter(Boolean).join('；')
  gridBusy.value = false
}
async function openGridBatch(packetId: string) {
  gridDetailDrawer.value = true
  gridBatchDetail.value = null; gridVerifications.value = []; gridBatchRecords.value = emptyPage(); gridError.value = ''
  try {
    const [result, history, records] = await Promise.all([
      api<Row>('/api/grid/batches/' + encodeURIComponent(packetId)),
      api<Row>('/api/grid/batches/' + encodeURIComponent(packetId) + '/verifications'),
      api<Page>('/api/grid/batches/' + encodeURIComponent(packetId) + '/records' + query({ page: 1, pageSize: 25 }))
    ])
    gridBatchDetail.value = result
    gridVerifications.value = history.items || history.verifications || (Array.isArray(history) ? history : [])
    gridBatchRecords.value = records
  } catch (error) { gridError.value = (error as Error).message }
}
async function verifyGridBatch(packetId: string) {
  if (gridActionBusy.value) return
  gridActionBusy.value = true; gridError.value = ''
  try { await api('/api/grid/batches/' + encodeURIComponent(packetId) + '/verify', { method: 'POST' }); await openGridBatch(packetId); await loadGridWorkbench() }
  catch (error) { gridError.value = (error as Error).message }
  finally { gridActionBusy.value = false }
}
function onAuthExpired() { applySession(null) }
function onForbidden() { forbidden.value = true }
function refreshCurrent() {
  if (authUser.value?.role === 'grid') { void loadGridWorkbench(); return }
  void refresh()
  if (page.value === 'data') void loadTable()
  if (page.value === 'control') void loadRuns()
  if (page.value === 'vpp-workbench') void loadVppWorkbench()
}
function verificationResult(item: Row): Row {
  if (item.result && typeof item.result === 'object') return item.result
  if (typeof item.result === 'string') { try { return JSON.parse(item.result) } catch { return { message: item.result } } }
  return {}
}

const perspective = ref('edge'), listKind = ref('batches'), table = ref<Page>(emptyPage()), tableBusy = ref(false), tableError = ref('')
const filterDevice = ref(''), filterPacket = ref(''), filterStatus = ref(''), dateRange = ref<Date[] | null>(null)
let tableRequest = 0
function filters() { return { page: table.value.page, pageSize: table.value.pageSize, deviceId: filterDevice.value, packetId: filterPacket.value.trim(), status: listKind.value === 'batches' ? filterStatus.value : '', from: dateRange.value?.[0]?.toISOString(), to: dateRange.value?.[1]?.toISOString() } }
async function loadTable() {
  const request = ++tableRequest; tableBusy.value = true
  const prefix = perspective.value === 'edge' && selectedEdgeId.value === 'EDGE-002' ? 'edge2' : perspective.value
  try { const result = await api<Page>('/api/' + prefix + '/' + listKind.value + query(filters())); if (request === tableRequest) { table.value = result; tableError.value = '' } }
  catch (error) { if (request === tableRequest) tableError.value = (error as Error).message }
  finally { if (request === tableRequest) tableBusy.value = false }
}
function resetTable() { table.value.page = 1; filterStatus.value = ''; void loadTable() }
function clearFilters() { filterDevice.value = ''; filterPacket.value = ''; filterStatus.value = ''; dateRange.value = null; table.value.page = 1; void loadTable() }
function searchTable() { table.value.page = 1; void loadTable() }

const runs = ref<Page>(emptyPage()), runsError = ref(''), events = ref<Page>(emptyPage()), eventsError = ref('')
async function loadRuns() {
  await Promise.all([
    (async () => { try { runs.value = await api<Page>('/api/edge/runs' + query({ page: runs.value.page })); runsError.value = '' } catch (error) { runsError.value = (error as Error).message } })(),
    (async () => { try { events.value = await api<Page>('/api/edge/events' + query({ page: events.value.page, pageSize: 10 })); eventsError.value = '' } catch (error) { eventsError.value = (error as Error).message } })()
  ])
}

const ledgerDrawer = ref(false), ledger = ref<Row | null>(null), ledgerBusy = ref(false), ledgerError = ref('')
async function openLedger(target?: string) {
  const id = target || packet.value
  if (!id) return
  ledgerDrawer.value = true; ledgerBusy.value = true; ledgerError.value = ''
  try { ledger.value = await api<Row>('/api/ledger/' + encodeURIComponent(id)) }
  catch (error) { ledgerError.value = (error as Error).message }
  finally { ledgerBusy.value = false }
}
const drawer = ref(false), detailSource = ref('edge'), packet = ref(''), detail = ref<Row | null>(null), peer = ref<Row | null>(null)
const detailError = ref(''), peerError = ref(''), detailBusy = ref(false), drawerTab = ref('summary')
const recordPage = ref<Page>(emptyPage()), checkPage = ref<Page>(emptyPage()), eventPage = ref<Page>(emptyPage())
const tabError = ref(''), tabBusy = ref(false), rawBatch = ref<Row | null>(null)
const edgeDetail = computed(() => detailSource.value === 'edge' ? detail.value : peer.value)
const vppDetail = computed(() => detailSource.value === 'vpp' ? detail.value : peer.value)
const selectedRecord = ref<Row | null>(null), recordDialog = ref(false)

async function openBatch(id: string, source = perspective.value) {
  packet.value = id; detailSource.value = source; detail.value = null; peer.value = null; rawBatch.value = null
  recordPage.value = emptyPage(); checkPage.value = emptyPage(); drawerTab.value = 'summary'; drawer.value = true
  await loadDetail()
}
async function loadDetail() {
  detailBusy.value = true; detailError.value = ''; peerError.value = ''
  const id = packet.value, source = detailSource.value
  const results = await Promise.allSettled([api('/api/' + source + '/batches/' + encodeURIComponent(id)), api('/api/' + (source === 'edge' ? 'vpp' : 'edge') + '/batches/' + encodeURIComponent(id))])
  if (packet.value !== id) return
  if (results[0].status === 'fulfilled') { detail.value = results[0].value; eventPage.value = results[0].value.events }
  else detailError.value = results[0].reason.message
  if (results[1].status === 'fulfilled') peer.value = results[1].value
  else peerError.value = results[1].reason.code === 'NOT_FOUND' ? '另一侧尚无此批次，不能据此认定已投递或核验。' : '另一侧查询不可用，保留本侧结果。'
  detailBusy.value = false
}
async function loadTab(tab: string | number = drawerTab.value) {
  drawerTab.value = String(tab); tabError.value = ''; tabBusy.value = true
  const base = '/api/' + detailSource.value + '/batches/' + encodeURIComponent(packet.value)
  try {
    if (tab === 'records') recordPage.value = await api<Page>(base + '/records' + query({ page: recordPage.value.page }))
    if (tab === 'verify' && vppDetail.value) checkPage.value = await api<Page>('/api/vpp/batches/' + encodeURIComponent(packet.value) + '/verifications' + query({ page: checkPage.value.page, pageSize: 10 }))
    if (tab === 'raw' && !rawBatch.value) rawBatch.value = await api(base + '/raw')
    if (tab === 'events') eventPage.value = await api<Page>('/api/' + detailSource.value + '/events' + query({ objectId: packet.value, page: eventPage.value.page }))
  } catch (error) { tabError.value = (error as Error).message }
  finally { tabBusy.value = false }
}
function showRecord(item: Row) { selectedRecord.value = item; recordDialog.value = true }
function downloadRaw() {
  if (!rawBatch.value) return
  const blob = new Blob([JSON.stringify(rawBatch.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = packet.value + '.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}

async function perform(kind: string, id = '') {
  if (operation.value) return
  operation.value = kind; operationError.value = ''
  let key = localStorage.getItem('prototype-single-request') || crypto.randomUUID()
  if (kind === 'sample-once') localStorage.setItem('prototype-single-request', key)
  try {
    const edgeAPI = selectedEdgeId.value === 'EDGE-002' ? 'edge2' : 'edge'
    const path = kind === 'verify' ? '/api/vpp/batches/' + encodeURIComponent(id) + '/verify'
      : kind === 'retry' ? '/api/' + edgeAPI + '/batches/' + encodeURIComponent(id) + '/retry' : '/api/' + edgeAPI + '/simulation/' + kind
    const result = await api(path, { method: 'POST', key: kind === 'sample-once' ? key : undefined })
    lastAction.value = { kind, ...result, time: new Date().toISOString() }
    if (kind === 'sample-once') { localStorage.removeItem('prototype-single-request'); ElMessage.success('已持久化 ' + result.generatedRecords + ' 条记录，等待窗口到期封批') }
    else if (kind === 'verify') ElMessage({ type: result.status === 'PASS' ? 'success' : 'warning', message: '重新核验已执行并保存：' + label(result.status) })
    else ElMessage.success(kind === 'start' ? '连续采集已开始' : kind === 'pause' ? '连续采集已暂停，存量数据继续处理' : '已安排重试，最终结果以后台回执为准')
    await refresh()
    if (drawer.value && (kind === 'verify' || kind === 'retry')) { await loadDetail(); await loadTab(kind === 'verify' ? 'verify' : drawerTab.value) }
    if (page.value === 'data') await loadTable()
    if (page.value === 'control') await loadRuns()
  } catch (error) {
    const e = error as APIError
    if (kind === 'sample-once' && !e.retryable) localStorage.removeItem('prototype-single-request')
    operationError.value = e.message; ElMessage.error(e.message)
  } finally { operation.value = '' }
}

let poll: number, tick: number
onMounted(() => {
  window.addEventListener('prototype-auth-expired', onAuthExpired)
  window.addEventListener('prototype-forbidden', onForbidden)
  void checkSession().then(() => {
    if (!authUser.value) return
    if (authUser.value.role === 'grid') void loadGridWorkbench()
    else { void refresh(); if (page.value === 'data') void loadTable(); if (page.value === 'control') void loadRuns(); if (page.value === 'vpp-workbench') void loadVppWorkbench() }
  })
  poll = window.setInterval(() => { if (!document.hidden && !operation.value && authUser.value) { if (authUser.value.role === 'grid') { if (page.value === 'grid-workbench') void loadGridWorkbench() } else { void refresh(); if (page.value === 'control') void loadRuns(); if (page.value === 'vpp-workbench') void loadVppWorkbench() } } }, 5000)
  tick = window.setInterval(() => { clock.value = Date.now() }, 1000)
})
onUnmounted(() => { clearInterval(poll); clearInterval(tick); window.removeEventListener('prototype-auth-expired', onAuthExpired); window.removeEventListener('prototype-forbidden', onForbidden) })
</script>

<template>
  <div v-if="authReady && authUser" class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#overview" @click.prevent="navigate('overview')">
        <svg class="brand-symbol" viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M20 3 35 12v16L20 37 5 28V12L20 3Z" stroke="currentColor" stroke-width="2"/><path d="m20 10 9 5v10l-9 5-9-5V15l9-5Z" fill="currentColor" opacity=".2"/><path d="m23 11-9 11h6l-3 8 10-12h-6l2-7Z" fill="currentColor"/></svg>
        <div><strong>源证<span>EDGE LAB</span></strong><small>可信数据实验台</small></div>
      </a>
      <div class="nav-label">工作空间 · FISCO BCOS 实验</div>
      <nav>
        <button v-for="item in visibleNav" :key="item.id" class="nav-item" :class="{ active: page === item.id }" @click="navigate(item.id)">
          <el-icon :size="20"><component :is="item.icon" /></el-icon><span>{{ item.title }}<small>{{ item.subtitle }}</small></span><el-icon class="nav-arrow"><ArrowRight /></el-icon>
        </button>
      </nav>
      <div class="sidebar-bottom"><div class="local-indicator"><span></span> 本机实验环境</div><p>FISCO BCOS 实验环境</p><div class="version-label">FISCO BCOS <span>REAL CHAIN</span></div></div>
    </aside>

    <div class="workspace">
      <header class="topbar"><div class="breadcrumb">边缘数据实验 <el-icon><ArrowRight /></el-icon><strong>{{ activeTitle }}</strong></div><div class="topbar-right"><span class="mode-pill">FISCO BCOS · 真实链</span><span class="local-badge">{{ authUser.username }} · {{ authUser.role === 'vpp' ? 'VPP 职责' : 'GRID 职责' }}</span><el-button text :icon="Operation" @click="logout">退出</el-button></div></header>
      <main>
        <el-alert v-if="forbidden" title="403 · 当前账号无权访问该工作台。请联系管理员确认账号角色。" type="error" show-icon :closable="false" class="page-alert" />
        <template v-else>
        <div class="page-heading"><div><div class="eyebrow">DISTRIBUTED ENERGY · EVIDENCE LAB</div><h1>{{ activeTitle }}</h1><p>{{ pageDescription }}</p></div><el-button :icon="Refresh" :loading="refreshing || gridBusy || vppWorkbenchBusy" @click="refreshCurrent">刷新数据</el-button></div>
        <el-alert v-if="refreshError" :title="refreshError + '。下方保留上次成功数据，可能已过时。'" type="warning" show-icon :closable="false" class="page-alert" />
        <el-alert v-if="operationError" :title="operationError" type="error" show-icon closable @close="operationError = ''" class="page-alert" />

        <template v-if="page === 'overview'">
          <section class="overview-hero">
            <div><div class="hero-topline"><span class="hero-chip">逻辑轻节点</span><span class="hero-label">{{ edge?.edgeId || '等待边缘服务' }}</span></div><h2>让数据流转，有据可查。</h2><p>设备独立采样 · UTC 窗口封批 · secp256k1 签名 · Merkle 根复核</p><div class="hero-actions"><el-button type="primary" :icon="Operation" @click="navigate('control')">进入实验控制</el-button><el-button class="hero-secondary" @click="navigate('data')">查看数据与批次 <el-icon><ArrowRight /></el-icon></el-button></div></div>
            <div class="window-box"><span class="window-caption">当前窗口 · 距下次封批</span><strong class="countdown">{{ countdown }}</strong><span class="window-range">{{ edge ? date(edge.windowStart).slice(-8) + ' — ' + date(edge.windowEnd).slice(-8) : '等待窗口信息' }} <small>北京时间</small></span><div class="window-foot"><span :class="['status-dot', edgeAvailable && edge?.simulation.running ? 'green' : 'amber']"></span>{{ !edgeAvailable ? '采集状态未知' : edge?.simulation.running ? '连续采集中' : '新记录生成已暂停' }}<span>{{ edge?.windowSeconds ?? '—' }} 秒 / 窗口</span></div></div>
          </section>

          <div class="health-strip"><span class="strip-label">依赖状态</span><span v-for="item in [{ name: '边缘服务', state: edgeAvailable }, { name: 'VPP 接收器', state: vppAvailable }, { name: '边缘数据库', state: edgeAvailable }, { name: 'VPP 数据库', state: vppAvailable }]" :key="item.name"><i :class="['status-dot', item.state === null ? 'neutral' : item.state ? 'green' : 'red']"></i>{{ item.name }}<b>{{ item.state === null ? '检查中' : item.state ? '可用' : '未确认可用' }}</b></span><small>最近成功刷新 {{ lastRefresh ? date(lastRefresh) : '尚无成功结果' }}</small></div>

          <section class="metrics-grid"><article v-for="item in metrics" :key="item.name" class="metric-card" :class="item.accent"><div class="metric-name">{{ item.name }}</div><div class="metric-value">{{ item.value === undefined ? '—' : Number(item.value).toLocaleString() }}<small>{{ item.unit }}</small></div><p>{{ item.note }}</p></article></section>

          <section class="panel"><div class="panel-heading"><div><h2>可信数据流</h2><p>各阶段按真实保存状态展示，流程示意不代表当前批次全部通过。</p></div><span class="soft-badge">完整记录保留 · 不做功率聚合</span></div><div class="pipeline"><div v-for="(step, index) in [{ name: '设备采集', text: '十台设备周期采集', count: edge?.totalRecords, unit: '条边缘记录' }, { name: '封批与签名', text: '原文与签名写入 outbox', count: edge ? Number(edge.pendingBatches) + Number(edge.verifiedBatches) + Number(edge.rejectedBatches) : undefined, unit: '批已封口' }, { name: 'VPP 接收验签', text: '可信公钥校验后入库', count: vpp?.recordCount, unit: '条已保存记录' }, { name: '摘要登记', text: '保存根与元数据', count: vpp?.batchCount, unit: '批链上登记' }, { name: '边缘回执复核', text: '本地重算根并比对回执', count: edge?.verifiedBatches, unit: '批复核通过' }]" :key="step.name" class="pipeline-step"><span class="step-number">0{{ index + 1 }}</span><h3>{{ step.name }}</h3><p>{{ step.text }}</p><div><strong>{{ step.count ?? '—' }}</strong> {{ step.unit }}</div><el-icon v-if="index < 4" class="step-arrow"><ArrowRight /></el-icon></div></div></section>

          <section class="panel"><div class="panel-heading"><div><h2>采集设备</h2><p>最新采集值来自边缘持久化记录，储能正值放电、负值充电。</p></div><span class="muted">{{ devices.length }} 台已配置</span></div><el-alert v-if="devicesError" :title="devicesError + '；上次设备刷新：' + date(devicesRefresh)" type="warning" :closable="false" /><div v-if="devices.length" class="device-grid"><article v-for="device in devices" :key="device.deviceId" class="device-card"><div class="device-card-top"><span class="device-glyph" :class="device.deviceType">{{ device.deviceType === 'photovoltaic' ? 'PV' : device.deviceType === 'battery_storage' ? 'ES' : 'EV' }}</span><span class="sim-tag">采集设备</span></div><h3>{{ device.deviceId }}</h3><p>{{ deviceName(device.deviceType) }}</p><div class="device-power">{{ device.latestRecord ? Number(device.latestRecord.value).toFixed(2) : '—' }}<span>kW</span></div><div class="device-meta"><span>配置范围</span><b>{{ device.minPowerKW }} ~ {{ device.maxPowerKW }} kW</b></div><div class="device-time">{{ device.latestRecord ? date(device.latestRecord.timestamp) : '尚未采集到记录' }}</div></article></div><el-empty v-else description="设备清单尚未读取成功" /></section>
        </template>

        <template v-if="page === 'data'">
          <section class="panel data-panel"><div class="data-toolbar"><el-segmented v-model="listKind" :options="[{ label: '批次与证据', value: 'batches' }, { label: '原始记录', value: 'records' }]" @change="resetTable" /><div class="perspective"><span>数据视角</span><el-radio-group v-model="perspective" size="small" @change="resetTable"><el-radio-button value="edge">边缘节点</el-radio-button><el-radio-button value="vpp">VPP 接收器</el-radio-button></el-radio-group></div></div>
            <div class="filter-bar"><el-select v-model="filterDevice" placeholder="全部设备" clearable aria-label="筛选设备"><el-option v-for="device in devices" :key="device.deviceId" :label="device.deviceId" :value="device.deviceId" /></el-select><el-input v-model="filterPacket" placeholder="输入完整 packetId" clearable aria-label="筛选批次编号" @keyup.enter="searchTable" /><el-select v-if="listKind === 'batches'" v-model="filterStatus" placeholder="全部状态" clearable aria-label="筛选状态"><el-option v-for="status in perspective === 'edge' ? ['PENDING', 'VERIFIED', 'REJECTED'] : ['SIMULATED_REGISTERED']" :key="status" :label="label(status)" :value="status" /></el-select><el-date-picker v-model="dateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /><el-button type="primary" :icon="Search" @click="searchTable">查询</el-button><el-button link @click="clearFilters">重置</el-button></div>
            <div class="table-context"><span><i class="status-dot green"></i>{{ perspective === 'edge' ? '边缘持久化视角' : 'VPP 持久化视角' }} · {{ listKind === 'batches' ? '按封批保存时间筛选' : '按采集时间筛选' }}</span><span>共 {{ table.total.toLocaleString() }} {{ listKind === 'batches' ? '批' : '条' }} · 服务端分页</span></div>
            <el-alert v-if="tableError" :title="tableError + '。当前保留上次成功列表。'" type="error" show-icon :closable="false" />
            <el-table v-if="listKind === 'batches'" v-loading="tableBusy" :data="table.items" empty-text="暂无符合条件的批次" row-key="packetId" @row-dblclick="openBatch($event.packetId)">
              <el-table-column label="批次编号 / 边缘" min-width="245"><template #default="{ row }"><button class="table-id" @click="openBatch(row.packetId)">{{ short(row.packetId) }}</button><div class="cell-secondary">{{ row.edgeId }}</div></template></el-table-column><el-table-column prop="recordCount" label="记录数" width="88" /><el-table-column label="状态" min-width="170"><template #default="{ row }"><el-tag :type="tone(row.status)" effect="light">{{ label(row.status) }}</el-tag></template></el-table-column><el-table-column label="窗口开始（北京时间）" min-width="180"><template #default="{ row }">{{ date(row.windowStart) }}</template></el-table-column><el-table-column label="Merkle 根（VPP 构建）" min-width="190"><template #default="{ row }"><el-tooltip :content="row.merkleRoot || row.vppMerkleRoot || '该包尚未在 VPP 构树'"><span class="mono">{{ short(row.merkleRoot || row.vppMerkleRoot) }}</span></el-tooltip></template></el-table-column><el-table-column v-if="perspective === 'edge'" prop="attempts" label="投递次数" width="95" /><el-table-column v-else label="最近重新核验" min-width="145"><template #default="{ row }"><el-tag :type="tone(row.verificationStatus)">{{ label(row.verificationStatus) }}</el-tag></template></el-table-column><el-table-column label="操作" fixed="right" width="150"><template #default="{ row }"><el-button link type="primary" @click="openBatch(row.packetId)">详情</el-button><el-button v-if="perspective === 'edge' && row.status === 'PENDING'" link type="warning" :disabled="!!operation" @click="perform('retry', row.packetId)">立即重试</el-button></template></el-table-column>
            </el-table>
            <el-table v-else v-loading="tableBusy" :data="table.items" empty-text="暂无符合条件的原始记录" row-key="recordId"><el-table-column label="记录编号" min-width="230"><template #default="{ row }"><button class="table-id" @click="showRecord(row)">{{ short(row.recordId) }}</button></template></el-table-column><el-table-column prop="record.deviceId" label="设备" width="105" /><el-table-column label="类型" min-width="110"><template #default="{ row }">{{ deviceName(row.record.deviceType) }}</template></el-table-column><el-table-column label="有功功率" width="115" align="right"><template #default="{ row }">{{ Number(row.record.value).toFixed(2) }} kW</template></el-table-column><el-table-column label="采集时间（北京时间）" min-width="190"><template #default="{ row }">{{ date(row.record.timestamp) }}</template></el-table-column><el-table-column label="所属包" min-width="220"><template #default="{ row }"><button v-if="row.packetId" class="table-id" @click="openBatch(row.packetId)">{{ short(row.packetId) }}</button><span v-else class="muted">等待窗口封批</span></template></el-table-column></el-table>
            <div class="pagination"><span>所有记录均来自 FISCO 实验链路</span><el-pagination v-model:current-page="table.page" v-model:page-size="table.pageSize" :total="table.total" :page-sizes="[10, 25, 50, 100]" layout="total, sizes, prev, pager, next" @current-change="loadTable" @size-change="searchTable" /></div>
          </section>
        </template>

        <template v-if="page === 'control'">
          <div class="control-grid"><section class="panel control-card"><div class="panel-heading"><div><h2>采集控制</h2><p>控制所选边缘节点的设备采集，不停止封批与投递。</p></div><el-tag :type="!selectedEdgeAvailable ? 'warning' : selectedEdge?.simulation.running ? 'success' : 'info'">{{ !selectedEdgeAvailable ? '状态未确认' : selectedEdge?.simulation.running ? '连续采集中' : '已暂停' }}</el-tag></div><div class="edge-picker"><span>操作目标</span><el-radio-group v-model="selectedEdgeId" @change="refresh"> <el-radio-button value="EDGE-001">EDGE-001 · 10 台</el-radio-button><el-radio-button value="EDGE-002">EDGE-002 · 10 台</el-radio-button></el-radio-group></div><div class="control-buttons"><el-button type="primary" :icon="VideoPlay" :disabled="!selectedEdgeAvailable || !!operation || selectedEdge?.simulation.running" :loading="operation === 'start'" @click="perform('start')">开始连续采集</el-button><el-button :icon="VideoPause" :disabled="!selectedEdgeAvailable || !!operation || !selectedEdge?.simulation.running" :loading="operation === 'pause'" @click="perform('pause')">暂停连续采集</el-button><el-button :icon="Plus" :disabled="!selectedEdgeAvailable || !!operation || selectedEdge?.simulation.running" :loading="operation === 'sample-once'" @click="perform('sample-once')">单轮生成 {{ selectedEdge?.configuredDevices ?? '—' }} 条</el-button></div><div class="control-help"><el-icon><CircleCheck /></el-icon><span>单轮生成数量按节点配置设备数确定。生成成功表示已签名并上报 VPP，链上锚定见批次详情。</span></div><div v-if="lastAction" class="last-action"><b>最近操作</b><span>{{ date(lastAction.time) }}</span><p v-if="lastAction.runId" class="mono">runId: {{ lastAction.runId }}</p><p v-if="lastAction.generatedRecords">实际生成 {{ lastAction.generatedRecords }} 条记录，状态 GENERATED。</p><p v-else>{{ lastAction.kind === 'start' ? '已开始连续采集' : lastAction.kind === 'pause' ? '已暂停连续采集' : lastAction.kind === 'retry' ? '已安排重试' : '核验结果：' + label(lastAction.status) }}</p></div><p class="muted small">状态与运行编号持久化，容器重启后恢复；单轮超时后重试沿用同一幂等键。</p></section>
            <section class="panel config-card"><h2>当前配置 <span class="soft-badge">只读</span></h2><dl><div><dt>边缘节点</dt><dd>{{ selectedEdge?.edgeId || '—' }}</dd></div><div><dt>随机采样区间</dt><dd>{{ selectedEdge ? selectedEdge.sampleMinSeconds + '–' + selectedEdge.sampleMaxSeconds + ' 秒' : '—' }}</dd></div><div><dt>批次窗口</dt><dd>{{ selectedEdge?.windowSeconds ?? '—' }} 秒 · UTC 对齐</dd></div><div><dt>窗口规则</dt><dd>左闭右开 [start, end)</dd></div><div><dt>签名算法</dt><dd>secp256k1 ECDSA + SHA-256</dd></div><div><dt>摘要存储</dt><dd>链上登记</dd></div></dl><p class="muted small">配置通过环境变量调整。浏览器不持有业务私钥或数据库凭据。</p></section></div>
          <section class="panel"><div class="panel-heading"><div><h2>运行记录</h2><p>新版本开始保存的真实运行事件；历史未记录阶段不补造。</p></div></div><el-alert v-if="runsError" :title="runsError" type="error" :closable="false" /><el-table :data="runs.items" empty-text="尚无运行记录"><el-table-column prop="runId" label="运行编号" min-width="320" class-name="mono" /><el-table-column label="方式" width="115"><template #default="{ row }">{{ label(row.kind) }}</template></el-table-column><el-table-column prop="generatedRecords" label="已生成记录数" width="140" /><el-table-column label="开始时间" min-width="180"><template #default="{ row }">{{ date(row.createdAt) }}</template></el-table-column><el-table-column label="结束时间" min-width="180"><template #default="{ row }">{{ row.endedAt ? date(row.endedAt) : '持续运行中' }}</template></el-table-column></el-table><div class="pagination"><span>生成数量不等于投递或核验通过数量</span><el-pagination v-model:current-page="runs.page" :total="runs.total" :page-size="25" layout="prev, pager, next" @current-change="loadRuns" /></div></section>
          <section class="panel"><div class="panel-heading"><div><h2>边缘操作与投递事件</h2><p>仅包含边缘实际观察或执行的事件。</p></div></div><el-alert v-if="eventsError" :title="eventsError" type="error" :closable="false" /><el-table :data="events.items" empty-text="尚无事件"><el-table-column label="发生时间" min-width="185"><template #default="{ row }">{{ date(row.occurredAt) }}</template></el-table-column><el-table-column label="事件" min-width="220"><template #default="{ row }">{{ label(row.eventType) }}</template></el-table-column><el-table-column label="关联对象" min-width="270"><template #default="{ row }"><span class="mono">{{ row.objectId }}</span></template></el-table-column><el-table-column prop="source" label="来源" width="90" /></el-table><div class="pagination"><span>事件按本侧保存顺序排列</span><el-pagination v-model:current-page="events.page" :total="events.total" :page-size="10" layout="prev, pager, next" @current-change="loadRuns" /></div></section>
        </template>
        <template v-if="false && page === 'vpp-workbench'">
          <el-alert class="role-prototype-alert" title="VPP 工作台 · 数据来自真实边缘服务与 FISCO BCOS 链；会话由服务端校验。" type="warning" show-icon :closable="false" />
          <section class="panel"><div class="panel-heading"><div><h2>VPP 收件服务</h2><p>以下状态取自当前运行中的 VPP 与边缘服务接口，不代表新拓扑已部署。</p></div><el-tag :type="vppAvailable ? 'success' : 'warning'">{{ vppAvailable === null ? '检查中' : vppAvailable ? '当前可用' : '未确认可用' }}</el-tag></div><div class="workbench-summary"><article><span>当前接入 VPP</span><strong>{{ vpp?.chainAdapter || '—' }}</strong><small>{{ vppAvailable ? '数据库 ' + (vpp?.database || '状态未知') : '状态未知' }}</small></article><article><span>当前已保存批次</span><strong>{{ vppAvailable ? Number(vpp?.batchCount).toLocaleString() : '—' }}</strong><small>来自现有 VPP 查询接口</small></article><article><span>当前已保存记录</span><strong>{{ vppAvailable ? Number(vpp?.recordCount).toLocaleString() : '—' }}</strong><small>实际持久化记录，不含规划节点数据</small></article><article><span>摘要登记方式</span><strong>FISCO BCOS 链</strong><small>不是区块链交易或共识确认</small></article></div></section>
          <section class="panel"><div class="panel-heading"><div><h2>Edge 节点与设备配置</h2><p>EDGE-001 / EDGE-002 均由当前运行的边缘服务提供。</p></div><span class="soft-badge">配置与在线状态分开呈现</span></div><div class="node-grid"><article class="node-card"><div class="node-card-heading"><div><span class="eyebrow">已接入边缘</span><h3>{{ edge?.edgeId || 'EDGE-001' }}</h3></div><el-tag :type="edgeAvailable ? 'success' : 'warning'">{{ edgeAvailable === null ? '检查中' : edgeAvailable ? '当前可用' : '未确认可用' }}</el-tag></div><dl><div><dt>设备配置</dt><dd>{{ edgeAvailable ? edge?.configuredDevices + ' 台' : '—' }}</dd></div><div><dt>采集周期</dt><dd>{{ edgeAvailable ? edge?.sampleMinSeconds + '–' + edge?.sampleMaxSeconds + ' 秒' : '—' }}</dd></div><div><dt>当前封批窗口</dt><dd>{{ edgeAvailable ? edge?.windowSeconds + ' 秒' : '—' }}</dd></div><div><dt>当前采集状态</dt><dd>{{ edgeAvailable ? (edge?.simulation.running ? '连续采集中' : '已暂停') : '未确认' }}</dd></div></dl><p class="muted small">当前实际配置：10 秒周期采集，每包 10 条；链上锚定由 VPP 完成。</p></article><article class="node-card planned"><div class="node-card-heading"><div><span class="eyebrow">目标拓扑</span><h3>EDGE-002</h3></div><el-tag type="info">规划中</el-tag></div><dl><div><dt>目标设备数</dt><dd>30 台</dd></div><div><dt>目标采集周期</dt><dd>10 分钟</dd></div><div><dt>目标封批窗口</dt><dd>30 分钟</dd></div><div><dt>实时连接状态</dt><dd>未接入</dd></div></dl><p class="muted small">设备明细、功率配置、最新采集时间和接收批次均未接入，不展示估算数据。</p></article></div></section>
        </template>
        <template v-if="false && page === 'grid-workbench'">
        </template>
        <template v-if="page === 'vpp-workbench'">
          <el-alert class="role-prototype-alert" title="VPP 职责工作台 · 接收数据以服务端记录为准；链状态如为链上登记会单独标识。" type="info" show-icon :closable="false" />
          <el-alert v-if="vppWorkbenchError" :title="vppWorkbenchError + '。保留上次成功结果。'" type="error" show-icon :closable="false" />
          <section class="panel">
            <div class="panel-heading"><div><h2>Edge 节点状态与设备配置</h2><p>状态来自 Edge 查询；最近接收时间仅统计当前已加载收件批次。</p></div><el-button :icon="Refresh" :loading="vppWorkbenchBusy" @click="loadVppWorkbench">刷新</el-button></div>
            <el-table v-loading="vppWorkbenchBusy" :data="vppEdges" empty-text="暂无已配置 Edge 节点">
              <el-table-column prop="edgeId" label="Edge 节点" min-width="140" />
              <el-table-column label="配置设备数" width="110"><template #default="{ row }">{{ row.deviceCount ?? '未记录' }}</template></el-table-column>
              <el-table-column label="服务／数据库状态" min-width="170"><template #default="{ row }"><el-tag :type="edgeOverviewFor(row.edgeId)?.available === true && edgeOverviewFor(row.edgeId)?.data?.database === 'ok' ? 'success' : 'warning'">{{ edgeOverviewFor(row.edgeId)?.available === false ? '不可用' : edgeOverviewFor(row.edgeId)?.available === true && edgeOverviewFor(row.edgeId)?.data?.database === 'ok' ? '可用 / 数据库正常' : '未确认' }}</el-tag></template></el-table-column>
              <el-table-column label="采集状态" width="130"><template #default="{ row }">{{ edgeOverviewFor(row.edgeId)?.available !== true ? '未确认' : edgeOverviewFor(row.edgeId)?.data?.simulation?.running === true ? '连续采集中' : edgeOverviewFor(row.edgeId)?.data?.simulation?.running === false ? '已暂停' : '未确认' }}</template></el-table-column>
              <el-table-column label="最近批次接收" min-width="190"><template #default="{ row }">{{ date(vppInbox.find((batch: Row) => batch.edgeId === row.edgeId)?.createdAt || vppInbox.find((batch: Row) => batch.edgeId === row.edgeId)?.receivedAt) }}</template></el-table-column>
              <el-table-column label="采集周期" width="120"><template #default="{ row }">{{ Number(row.sampleIntervalSeconds) > 0 ? (Number(row.sampleIntervalSeconds) < 60 ? Number(row.sampleIntervalSeconds) + ' 秒' : Number(row.sampleIntervalSeconds) / 60 + ' 分钟') : '未记录' }}</template></el-table-column>
              <el-table-column label="封批窗口" width="120"><template #default="{ row }">{{ Number(row.batchWindowSeconds) > 0 ? (Number(row.batchWindowSeconds) < 60 ? Number(row.batchWindowSeconds) + ' 秒' : Number(row.batchWindowSeconds) / 60 + ' 分钟') : '未记录' }}</template></el-table-column>
            </el-table>
          </section>
          <section class="panel"><div class="panel-heading"><div><h2>边缘设备</h2><p>{{ filteredVppDevices.length }} 台设备；功率与最近采样时间来自 Edge 查询接口。</p></div><div class="device-filters"><el-select v-model="vppDeviceEdge" aria-label="筛选 Edge 节点" style="width: 160px"><el-option v-for="group in vppDeviceGroups" :key="group.edgeId" :label="group.edgeId" :value="group.edgeId" /></el-select><el-select v-model="vppDeviceType" aria-label="筛选设备类型" style="width: 160px"><el-option label="全部类型" value="all" /><el-option v-for="type in deviceTypes" :key="type" :label="deviceName(type)" :value="type" /></el-select></div></div><div v-loading="vppWorkbenchBusy" class="device-waterfall"><article v-for="device in filteredVppDevices" :key="device.deviceId" class="device-tile"><div class="device-tile-heading"><strong>{{ device.deviceId }}</strong><el-tag size="small" type="info">{{ deviceName(device.deviceType) }}</el-tag></div><div class="device-power"><span>最新功率</span><b>{{ device.latestRecord ? Number(device.latestRecord.value).toFixed(2) + ' kW' : '未采集' }}</b></div><dl><div><dt>配置范围</dt><dd>{{ device.minPowerKW }} ~ {{ device.maxPowerKW }} kW</dd></div><div><dt>最近采集</dt><dd>{{ date(device.latestRecord?.timestamp) }}</dd></div></dl></article><el-empty v-if="!vppWorkbenchBusy && !filteredVppDevices.length" description="该 Edge 或类型下暂无设备" /></div></section>
          <section class="panel"><div class="panel-heading"><div><h2>收件批次与验签</h2><p>每行对应 VPP 实际收到的批次；状态与摘要字段缺失时显示“未记录”。</p></div><el-tag type="info">{{ vppWorkbench?.chainMode || '链状态未提供' }}</el-tag></div><el-table v-loading="vppWorkbenchBusy" :data="vppInbox" empty-text="暂无 VPP 收件批次"><el-table-column prop="packetId" label="Packet ID" min-width="210" /><el-table-column prop="edgeId" label="来源 Edge" width="130" /><el-table-column label="窗口" min-width="185"><template #default="{ row }">{{ date(row.windowStart) }}<div class="cell-secondary">至 {{ date(row.windowEnd) }}</div></template></el-table-column><el-table-column label="记录数" width="95"><template #default="{ row }">{{ row.recordCount ?? '未记录' }}</template></el-table-column><el-table-column label="签名验签" width="135"><template #default="{ row }"><el-tag :type="row.signatureStatus === 'PASS' ? 'success' : row.signatureStatus === 'FAIL' ? 'danger' : 'info'">{{ row.signatureStatus || '未记录' }}</el-tag></template></el-table-column><el-table-column label="本地 Merkle 根" min-width="210"><template #default="{ row }"><span class="mono">{{ maskSensitive(row.merkleRoot) }}</span></template></el-table-column><el-table-column label="链登记" width="140"><template #default="{ row }">{{ row.chainStatus || '未记录' }}</template></el-table-column><el-table-column label="登记依据" width="150"><template #default="{ row }">{{ row.chainMode || vppWorkbench?.chainMode || '未记录' }}</template></el-table-column></el-table></section>
        </template>
        <template v-if="page === 'grid-workbench'">
          
          <section class="panel flow-panel">
            <div class="panel-heading"><div><h2>数据流向与链上核验</h2><p>左侧 VPP 上报批次与根，右侧 GRID 独立读链核验；示意图用于展示链路关系。</p></div><el-tag type="success" effect="plain">FISCO BCOS · 真实链</el-tag></div>
            <div class="flow-map">
              <article class="flow-node flow-vpp">
                <div class="flow-node-top"><span class="flow-node-icon"><el-icon><Share /></el-icon></span><span class="flow-kicker">SOURCE · VPP</span></div>
                <h3>vpp-a</h3>
                <p>边缘聚合 · 业务签名 · Merkle 构树</p>
                <dl class="flow-stats"><div><dt>累计批次</dt><dd>{{ gridOverview?.batchCount ?? '—' }}</dd></div><div><dt>签名算法</dt><dd>ECDSA</dd></div></dl>
              </article>
              <div class="flow-core">
                <div class="flow-rail">
                  <span class="flow-rail-label">批次包 · 记录 · Merkle 证明</span>
                  <div class="flow-track"><i class="flow-packet p1"></i><i class="flow-packet p2"></i><i class="flow-packet p3"></i></div>
                </div>
                <div class="flow-chain-row">
                  <span class="flow-stem left"><em>registerBatch(root)</em></span>
                  <div class="flow-chain"><span class="flow-chain-dot"></span><div><b>FISCO BCOS</b><small>5 节点 · PBFT · 2f+1</small></div></div>
                  <span class="flow-stem right"><em>getBatch + verify</em></span>
                </div>
                <div class="flow-verdict"><el-icon><CircleCheck /></el-icon><span>root 比对一致</span><b>PASS</b><span>回执 status 0x0</span></div>
              </div>
              <article class="flow-node flow-grid">
                <div class="flow-node-top"><span class="flow-node-icon"><el-icon><Files /></el-icon></span><span class="flow-kicker">VERIFIER · GRID</span></div>
                <h3>GRID</h3>
                <p>独立读链 · 本地重算 · 逐条证明</p>
                <dl class="flow-stats"><div><dt>核验通过</dt><dd>{{ gridOverview?.passedBatches ?? '—' }}</dd></div><div><dt>失败</dt><dd>{{ gridOverview?.failedBatches ?? 0 }}</dd></div></dl>
              </article>
            </div>
          </section>
<el-alert class="role-prototype-alert" title="GRID 工作台 · 核验依据为 FISCO BCOS 链上根与本地重算根。" type="warning" show-icon :closable="false" />
          <el-alert v-if="gridError" :title="gridError + '。当前保留上次成功数据。'" type="error" show-icon :closable="false" />
          <section class="panel"><div class="panel-heading"><div><h2>GRID 链核验状态</h2><p>逐个核验收到的 VPP 原始批次，不执行跨批次聚合。</p></div><el-button :icon="Refresh" :loading="gridBusy" @click="loadGridWorkbench">刷新</el-button></div><div class="workbench-summary"><article><span>核验服务</span><strong>{{ gridOverview?.available === true || gridOverview?.database === 'ok' ? '可用' : gridOverview?.available === false || (gridOverview && gridOverview.database !== 'ok') ? '不可用' : '未记录' }}</strong><small>{{ gridOverview?.checkedAt ? date(gridOverview.checkedAt) : gridOverview?.serverTime ? date(gridOverview.serverTime) : '状态时间未记录' }}</small></article><article><span>已登记批次</span><strong>{{ gridOverview?.batchCount ?? '未记录' }}</strong><small>服务端实际保存的 VPP 批次数</small></article><article><span>核验通过 / 失败</span><strong>{{ gridOverview?.passedBatches ?? '未记录' }} / {{ gridOverview?.failedBatches ?? '未记录' }}</strong><small>实际核验结果</small></article><article><span>链适配器</span><strong>FISCO-BCOS</strong><small>真实链 · getBatch 核验</small></article></div></section>
          <section class="panel"><div class="panel-heading"><div><h2>VPP 来源批次</h2><p>通过来源 VPP、Edge、设备、批次编号和时间范围筛选。</p></div></div><div class="filter-bar"><el-input v-model="gridFilters.vppId" placeholder="VPP ID" clearable /><el-input v-model="gridFilters.edgeId" placeholder="Edge ID" clearable /><el-input v-model="gridFilters.deviceId" placeholder="设备 ID" clearable /><el-input v-model="gridFilters.packetId" placeholder="packetId" clearable /><el-date-picker v-model="gridDateRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /><el-button type="primary" :icon="Search" @click="applyGridFilters">查询</el-button><el-button link @click="clearGridFilters">重置</el-button></div><el-table v-loading="gridBusy" :data="gridBatches.items" empty-text="暂无 VPP 来源批次"><el-table-column label="原始批次" min-width="210"><template #default="{ row }"><button class="table-id mono" @click="openGridBatch(row.packetId)">{{ shortId(row.packetId) }}</button></template></el-table-column><el-table-column prop="vppId" label="VPP" width="115" /><el-table-column prop="edgeId" label="来源 Edge" width="125" /><el-table-column prop="recordCount" label="记录数" width="90" /><el-table-column label="核验结果" width="130"><template #default="{ row }"><el-tag :type="row.verificationStatus === 'PASS' ? 'success' : row.verificationStatus === 'FAIL' ? 'danger' : 'info'">{{ row.verificationStatus || '未核验' }}</el-tag></template></el-table-column><el-table-column label="操作" width="150"><template #default="{ row }"><el-button link @click="openGridBatch(row.packetId)">详情</el-button><el-button link type="primary" :disabled="gridActionBusy || !row.packetId" @click="verifyGridBatch(row.packetId)">重新核验</el-button></template></el-table-column></el-table><div class="pagination"><span>共 {{ gridBatches.total }} 个来源批次</span><el-pagination v-model:current-page="gridPage" :total="gridBatches.total" :page-size="gridPageSize" layout="prev, pager, next" @current-change="loadGridWorkbench" /></div></section>
          <el-drawer v-if="authUser" v-model="gridDetailDrawer" size="min(1080px, 96vw)" :title="'GRID 批次核验 · ' + (gridBatchDetail ? shortId(gridBatchDetail.packetId) : '')" destroy-on-close><div class="batch-detail"><div class="panel-heading"><div><h2>原始批次核验 · {{ shortId(gridBatchDetail.packetId) }}</h2><p>逐批比较记录本地 Merkle root 与服务端链摘要；链上数据来自 FISCO BCOS。</p></div><el-button :icon="Document" @click="openLedger(gridBatchDetail.packetId)">日志</el-button><el-button :icon="CircleCheck" :loading="gridActionBusy" @click="verifyGridBatch(gridBatchDetail.packetId)">重新核验</el-button></div><div class="digest-compare"><div><span>本地 Merkle root</span><strong class="wrap mono">{{ maskSensitive(gridBatchDetail.localMerkleRoot || gridBatchDetail.merkleRoot) }}</strong></div><div class="compare-mark">↔</div><div><span>链上 Merkle root</span><strong class="wrap mono">{{ maskSensitive(gridBatchDetail.chainMerkleRoot) }}</strong></div><div class="compare-result"><span>核验结论</span><el-tag>{{ gridBatchDetail.verificationStatus || gridBatchDetail.verification?.status || '未核验' }}</el-tag><small>交易 {{ maskSensitive(gridBatchDetail.txHash || gridBatchDetail.locator?.txHash) }} · 区块 {{ gridBatchDetail.blockNumber ?? gridBatchDetail.locator?.blockNumber ?? '未记录' }}</small></div></div><el-descriptions :column="2" border class="grid-batch-descriptions"><el-descriptions-item label="批次 ID">{{ shortId(gridBatchDetail.packetId) }}</el-descriptions-item><el-descriptions-item label="内部批次 ID">{{ gridBatchDetail.batchId || '未记录' }}</el-descriptions-item><el-descriptions-item label="来源 VPP">{{ gridBatchDetail.vppId || '未记录' }}</el-descriptions-item><el-descriptions-item label="来源 Edge">{{ gridBatchDetail.edgeId || '未记录' }}</el-descriptions-item><el-descriptions-item label="记录数" :span="2">{{ gridBatchDetail.recordCount ?? '未记录' }}</el-descriptions-item><el-descriptions-item label="链定位器" :span="2"><pre class="event-details">{{ JSON.stringify({ adapter: gridBatchDetail.locator?.adapter, blockNumber: gridBatchDetail.locator?.blockNumber, txHash: maskSensitive(gridBatchDetail.locator?.txHash) }, null, 2) }}</pre></el-descriptions-item><el-descriptions-item label="签名" :span="2"><span class="mono wrap">{{ gridBatchDetail.signature || '未记录' }}</span></el-descriptions-item></el-descriptions><h3 class="section-subheading">批次记录</h3><el-table :data="gridBatchRecords.items" class="records-table" empty-text="该批次没有可显示的记录"><el-table-column label="记录编号" min-width="250"><template #default="{ row }"><span class="record-id">{{ row.recordId }}</span></template></el-table-column><el-table-column label="设备" width="120"><template #default="{ row }"><span class="device-chip">{{ row.deviceId }}</span></template></el-table-column><el-table-column label="功率" width="130" align="right"><template #default="{ row }"><span class="record-power">{{ Number(row.value ?? row.record?.value ?? 0).toFixed(3) }}<small> kW</small></span></template></el-table-column><el-table-column label="采集时间" min-width="190"><template #default="{ row }"><span class="record-time">{{ date(row.timestamp || row.record?.timestamp) }}</span></template></el-table-column></el-table><div class="pagination"><span>共 {{ gridBatchRecords.total }} 条批次记录</span><el-pagination v-model:current-page="gridBatchRecords.page" :total="gridBatchRecords.total" :page-size="gridBatchRecords.pageSize" layout="prev, pager, next" @current-change="openGridBatch(gridBatchDetail.packetId)" /></div><h3 class="section-subheading">核验历史与分项检查</h3><article v-for="item in gridVerifications" :key="item.id" class="verification-result"><div class="verification-result-heading"><el-tag :type="item.status === 'PASS' ? 'success' : item.status === 'FAIL' ? 'danger' : 'warning'">{{ item.status || '未记录' }}</el-tag><b>{{ date(item.checkedAt) }}</b><span>{{ item.observer || '未记录' }} · {{ item.ruleVersion || '未记录' }}</span></div><p class="muted small">{{ item.message || verificationResult(item).method || '未记录检查说明' }}</p><el-table :data="verificationResult(item).checks || []" class="records-table compact" empty-text="该历史项没有分项检查"><el-table-column prop="name" label="检查项" min-width="190" /><el-table-column prop="status" label="结果" width="120" /><el-table-column prop="message" label="检查说明" min-width="260" /></el-table><p class="muted small">适配器：{{ verificationResult(item).locator?.adapter || '未记录' }} · 真实区块链：{{ verificationResult(item).realBlockchain === true ? '是' : '否 / 未声明' }}</p></article></div></el-drawer>
          <section class="panel"><div class="panel-heading"><div><h2>完整记录</h2><p>GRID 保存的来源记录，沿用与批次相同的筛选条件。</p></div></div><el-table v-loading="gridBusy" :data="gridRecords.items" class="records-table" empty-text="暂无符合筛选条件的记录"><el-table-column prop="recordId" label="记录编号" min-width="230" /><el-table-column prop="vppId" label="VPP" width="110" /><el-table-column prop="edgeId" label="Edge" width="120" /><el-table-column prop="deviceId" label="设备" width="120" /><el-table-column prop="packetId" label="来源批次" min-width="210" /><el-table-column label="采集时间" min-width="190"><template #default="{ row }">{{ date(row.timestamp || row.collectedAt) }}</template></el-table-column></el-table><div class="pagination"><span>共 {{ gridRecords.total }} 条记录</span><el-pagination v-model:current-page="gridRecordPage" :total="gridRecords.total" :page-size="gridRecordPageSize" layout="prev, pager, next" @current-change="loadGridWorkbench" /></div></section>
        </template>
        <footer class="workspace-footer"><el-icon><Connection /></el-icon><span>FISCO BCOS 真实链 · 边缘签名 / 链上锚定 / 独立 GRID 核验</span><span>时间显示：北京时间 UTC+8 · 原文保留 UTC</span></footer>
        </template>
      </main>
    </div>

    <el-drawer v-if="authUser" v-model="drawer" size="min(1050px, 96vw)" :title="'批次详情 · ' + (detailSource === 'edge' ? '边缘视角' : 'VPP 视角')" destroy-on-close>
      <div v-loading="detailBusy" class="batch-detail"><div class="detail-title"><span class="eyebrow">PACKET</span><h2 class="mono">{{ packet }}</h2><el-button size="small" :icon="Document" @click="openLedger">日志</el-button><el-button size="small" :icon="Refresh" @click="loadDetail">刷新详情</el-button></div><el-alert v-if="detailError" :title="detailError" type="error" :closable="false" /><el-alert v-if="peerError" :title="peerError" type="warning" :closable="false" />
        <template v-if="detail"><div class="detail-statuses"><div><span>边缘回执复核</span><el-tag :type="tone(edgeDetail?.status || '')">{{ edgeDetail ? label(edgeDetail.status) : '尚无结果' }}</el-tag></div><div><span>链上登记</span><el-tag :type="vppDetail ? 'success' : 'info'">{{ vppDetail ? '已链上登记' : '尚未取得结果' }}</el-tag></div><div><span>VPP 最近重新核验</span><el-tag :type="tone(vppDetail?.latestVerification?.status || '')">{{ vppDetail?.latestVerification ? label(vppDetail.latestVerification.status) : '尚未重新核验' }}</el-tag></div></div>
          <el-tabs v-model="drawerTab" @tab-change="loadTab"><el-tab-pane label="批次与摘要" name="summary" /><el-tab-pane label="记录明细" name="records" /><el-tab-pane label="核验历史" name="verify" /><el-tab-pane label="原始签名包" name="raw" /><el-tab-pane label="本侧事件" name="events" /></el-tabs><el-alert v-if="tabError" :title="tabError" type="error" :closable="false" />
          <div v-loading="tabBusy">
            <template v-if="drawerTab === 'summary'"><el-descriptions :column="2" border><el-descriptions-item label="edgeId">{{ detail.edgeId }}</el-descriptions-item><el-descriptions-item label="记录数">{{ detail.recordCount }}</el-descriptions-item><el-descriptions-item label="窗口开始">{{ date(detail.windowStart) }}</el-descriptions-item><el-descriptions-item label="窗口结束">{{ date(detail.windowEnd) }}</el-descriptions-item><el-descriptions-item label="保存时间">{{ date(detail.createdAt) }}</el-descriptions-item><el-descriptions-item label="batchId"><span class="mono wrap">{{ detail.batchId || '尚未取得' }}</span></el-descriptions-item><el-descriptions-item label="签名" :span="2"><span class="mono wrap">{{ detail.signature }}</span></el-descriptions-item><el-descriptions-item label="原文 SHA-256" :span="2"><span class="mono wrap">{{ detail.payloadHash }}</span></el-descriptions-item><el-descriptions-item label="边缘保存的回执根" :span="2"><span class="mono wrap">{{ edgeDetail?.merkleRoot || '边缘只签名原文，不构建 Merkle 根（由 VPP 构建）' }}</span></el-descriptions-item><el-descriptions-item label="VPP 保存的批次根" :span="2"><span class="mono wrap">{{ vppDetail?.merkleRoot || '尚未取得' }}</span></el-descriptions-item><el-descriptions-item label="链上 Merkle 根" :span="2"><span class="mono wrap">{{ vppDetail?.merkleRoot || '尚未取得' }}</span></el-descriptions-item><el-descriptions-item label="真实交易 / 区块" :span="2">{{ vppDetail?.txHash || '尚未上链' }} · 区块 {{ vppDetail?.blockNumber ?? '—' }}</el-descriptions-item></el-descriptions>
              <div v-if="edgeDetail" class="delivery-box"><h3>投递信息</h3><p>投递次数：{{ edgeDetail.attempts }} · 下次重试：{{ edgeDetail.status === 'PENDING' && Number(edgeDetail.nextAttemptAt) > 0 ? date(new Date(Number(edgeDetail.nextAttemptAt) * 1000).toISOString()) : '未安排 / 已完成' }}</p><el-alert v-if="edgeDetail.lastError" :title="edgeDetail.lastError" type="warning" :closable="false" /><el-button v-if="edgeDetail.status === 'PENDING'" type="warning" plain :disabled="!!operation" @click="perform('retry', packet)">立即重试此批次</el-button></div><p class="muted small">历史未保存的核验时间显示“未记录”。已有状态与本次重新核验结果分开保留。</p>
            </template>
            <template v-if="drawerTab === 'records'"><el-table :data="recordPage.items" empty-text="暂无记录"><el-table-column label="记录编号" min-width="200"><template #default="{ row }"><button class="table-id" @click="showRecord(row)">{{ short(row.recordId) }}</button></template></el-table-column><el-table-column prop="record.deviceId" label="设备" width="105" /><el-table-column label="功率" width="110" align="right"><template #default="{ row }">{{ Number(row.record.value).toFixed(2) }} kW</template></el-table-column><el-table-column label="采集时间" min-width="185"><template #default="{ row }">{{ date(row.record.timestamp) }}</template></el-table-column></el-table><div class="pagination"><span>共 {{ recordPage.total }} 条</span><el-pagination v-model:current-page="recordPage.page" :total="recordPage.total" :page-size="25" layout="prev, pager, next" @current-change="loadTab('records')" /></div></template>
          <template v-if="drawerTab === 'verify'"><div class="verification-toolbar"><div><h3>VPP 本地整批重新核验</h3></div><el-button type="primary" :disabled="!vppDetail || !!operation" :loading="operation === 'verify'" :icon="CircleCheck" @click="perform('verify', packet)">重新核验</el-button></div><el-empty v-if="!checkPage.items.length" :description="vppDetail ? '尚未执行重新核验' : 'VPP 尚未保存此批次，无法核验'" /><article v-for="item in checkPage.items" :key="item.id" class="verification-result"><div class="verification-result-heading"><el-tag :type="tone(item.status)">{{ label(item.status) }}</el-tag><b>{{ date(item.checkedAt) }}</b><span>{{ item.result.observer }} · {{ item.result.ruleVersion }}</span></div><p class="muted small">{{ item.result.method }}</p><el-table :data="item.result.checks"><el-table-column prop="name" label="检查项" width="160" /><el-table-column label="结果" width="115"><template #default="{ row }"><el-tag :type="tone(row.status)">{{ label(row.status) }}</el-tag></template></el-table-column><el-table-column label="说明" min-width="220"><template #default="{ row }">{{ row.message }}<div class="cell-secondary mono">{{ row.code }}</div></template></el-table-column></el-table><p v-if="item.result.computedRoot" class="small wrap mono">重算根：{{ item.result.computedRoot }}</p></article><div class="pagination"><span>核验历史追加保存</span><el-pagination v-model:current-page="checkPage.page" :total="checkPage.total" :page-size="10" layout="prev, pager, next" @current-change="loadTab('verify')" /></div></template>
            <template v-if="drawerTab === 'raw'"><div class="verification-toolbar"><div><h3>原始签名包</h3><p>canonicalPayload 为保存的规范化签名字节文本，签名不包含自身。</p></div><el-button :icon="Download" :disabled="!rawBatch" @click="downloadRaw">下载原包</el-button></div><pre v-if="rawBatch" class="json-block">{{ JSON.stringify(rawBatch, null, 2).slice(0, 200000) }}</pre><p v-if="rawBatch && JSON.stringify(rawBatch).length > 200000" class="muted">预览已截断，请下载完整原包。</p></template>
            <template v-if="drawerTab === 'events'"><el-empty v-if="!eventPage.items.length" description="该批次没有已保存事件，历史阶段未补造" /><el-timeline v-else class="event-timeline"><el-timeline-item v-for="event in eventPage.items" :key="event.id" :timestamp="date(event.occurredAt)" placement="top"><b>{{ label(event.eventType) }}</b><el-tag size="small" type="info">{{ event.source }} 本地观察</el-tag><pre class="event-details">{{ JSON.stringify(event.details, null, 2) }}</pre></el-timeline-item></el-timeline><div class="pagination"><span>按本侧保存顺序排列</span><el-pagination v-model:current-page="eventPage.page" :total="eventPage.total" :page-size="25" layout="prev, pager, next" @current-change="loadTab('events')" /></div></template>
          </div>
        </template>
      </div>
    </el-drawer>
    <el-drawer v-if="authUser" v-model="ledgerDrawer" size="min(1120px, 96vw)" title="区块链账本 · 本次数据传输" destroy-on-close>
      <div v-loading="ledgerBusy" class="batch-detail">
        <el-alert v-if="ledgerError" :title="ledgerError" type="error" :closable="false" />
        <template v-if="ledger">
          <div class="detail-statuses"><div><span>链上登记</span><el-tag :type="ledger.chain.record.exists ? 'success' : 'danger'">{{ ledger.chain.record.exists ? '已确认' : '未找到' }}</el-tag></div><div><span>交易回执</span><el-tag :type="ledger.chain.receipt.status === '0x0' ? 'success' : 'danger'">{{ ledger.chain.receipt.status || '未记录' }}</el-tag></div><div><span>GRID 核验</span><el-tag :type="ledger.verificationStatus === 'PASS' ? 'success' : 'danger'">{{ ledger.verificationStatus || '未核验' }}</el-tag></div></div>
          <h3 class="section-subheading">证书与签名</h3>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="边缘 keyId">{{ ledger.certificates.edge.keyId }}</el-descriptions-item>
            <el-descriptions-item label="签名算法">{{ ledger.certificates.edge.algorithm }}</el-descriptions-item>
            <el-descriptions-item label="边缘公钥（证书）" :span="2"><span class="mono wrap">{{ ledger.certificates.edge.publicKey }}</span></el-descriptions-item>
            <el-descriptions-item label="包签名 r‖s" :span="2"><span class="mono wrap">{{ ledger.certificates.edge.signature }}</span></el-descriptions-item>
            <el-descriptions-item label="VPP 交易账户">{{ ledger.certificates.vpp.account }}</el-descriptions-item>
            <el-descriptions-item label="合约地址">{{ ledger.certificates.contract.address }}</el-descriptions-item>
          </el-descriptions>
          <h3 class="section-subheading">哈希</h3>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="包摘要 packetDigest"><span class="mono wrap">{{ ledger.hashes.packetDigest }}</span></el-descriptions-item>
            <el-descriptions-item label="Merkle 根"><span class="mono wrap">{{ ledger.hashes.merkleRoot }}</span></el-descriptions-item>
            <el-descriptions-item label="GRID 重算根"><span class="mono wrap">{{ ledger.hashes.recomputedRoot }}</span></el-descriptions-item>
            <el-descriptions-item label="交易哈希"><span class="mono wrap">{{ ledger.hashes.txHash }}</span></el-descriptions-item>
            <el-descriptions-item label="区块哈希 / 父哈希"><span class="mono wrap">{{ ledger.hashes.blockHash }}<br />{{ ledger.chain.block.parentHash }}</span></el-descriptions-item>
            <el-descriptions-item label="状态根 / 回执根"><span class="mono wrap">{{ ledger.hashes.stateRoot }}<br />{{ ledger.hashes.receiptsRoot }}</span></el-descriptions-item>
          </el-descriptions>
          <h3 class="section-subheading">链上记录 getBatch</h3>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="batchId"><span class="mono wrap">{{ ledger.batchId }}</span></el-descriptions-item>
            <el-descriptions-item label="recordCount">{{ ledger.chain.record.recordCount }}</el-descriptions-item>
            <el-descriptions-item label="rootHash" :span="2"><span class="mono wrap">{{ ledger.chain.record.rootHash }}</span></el-descriptions-item>
            <el-descriptions-item label="vppId">{{ ledger.chain.record.vppId }}</el-descriptions-item>
            <el-descriptions-item label="schemaVersion">{{ ledger.chain.record.schemaVersion }}</el-descriptions-item>
            <el-descriptions-item label="查询节点" :span="2"><span class="mono wrap">{{ ledger.chain.queriedVia }}</span></el-descriptions-item>
          </el-descriptions>
          <h3 class="section-subheading">交易回执</h3>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="status">{{ ledger.chain.receipt.status }}</el-descriptions-item>
            <el-descriptions-item label="gasUsed">{{ ledger.chain.receipt.gasUsed }}</el-descriptions-item>
            <el-descriptions-item label="from" :span="2"><span class="mono wrap">{{ ledger.chain.receipt.from }}</span></el-descriptions-item>
            <el-descriptions-item label="to" :span="2"><span class="mono wrap">{{ ledger.chain.receipt.to }}</span></el-descriptions-item>
            <el-descriptions-item label="blockNumber">{{ ledger.chain.receipt.blockNumber }}</el-descriptions-item>
            <el-descriptions-item label="remainGas">{{ ledger.chain.receipt.remainGas }}</el-descriptions-item>
            <el-descriptions-item label="calldata" :span="2"><pre class="json-block">{{ ledger.chain.transaction.input }}</pre></el-descriptions-item>
          </el-descriptions>
          <h3 class="section-subheading">逐条叶子哈希</h3>
          <el-table :data="ledger.hashes.leaves" empty-text="无"><el-table-column prop="recordId" label="记录编号" min-width="240" /><el-table-column label="叶子哈希" min-width="320"><template #default="{ row }"><span class="mono wrap">{{ row.leafHash }}</span></template></el-table-column></el-table>
          <h3 class="section-subheading">GRID 核验检查</h3>
          <el-table :data="ledger.checks" empty-text="无"><el-table-column prop="name" label="检查项" min-width="220" /><el-table-column prop="status" label="结果" width="120" /><el-table-column prop="message" label="说明" min-width="240" /></el-table>
        </template>
      </div>
    </el-drawer>
    <el-dialog v-if="authUser" v-model="recordDialog" title="记录原文" width="min(720px, 94vw)"><template v-if="selectedRecord"><p class="mono wrap">{{ selectedRecord.recordId }}</p><pre class="json-block">{{ JSON.stringify(selectedRecord.record, null, 2) }}</pre><p class="muted small">原始时间为 UTC；页面列表显示北京时间。该记录本身不代表独立成员证明已验证。</p></template></el-dialog>
  </div>
  <div v-else-if="authReady" class="login-shell"><form class="login-panel" @submit.prevent="login"><div class="eyebrow">DISTRIBUTED ENERGY · EVIDENCE LAB</div><h1>登录实验工作台</h1><p>使用已配置的 VPP 或 GRID 账号继续。</p><el-alert v-if="authError" :title="authError" type="error" show-icon :closable="false" /><label>用户名<el-input v-model="loginForm.username" autocomplete="username" required /></label><label>密码<el-input v-model="loginForm.password" type="password" autocomplete="current-password" show-password required /></label><el-button type="primary" native-type="submit" :loading="authBusy">登录</el-button><small>此页面会话由服务端验证；职责菜单不替代后端授权。</small></form></div>
  <div v-else class="auth-loading"><el-skeleton animated :rows="4" /></div>
</template>
