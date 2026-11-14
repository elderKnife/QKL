<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, date, short, type Row, type Page, emptyPage } from './api'
import CommandWorkspace from './CommandWorkspace.vue'

const user = ref<Row | null>(null)
const username = ref('grid')
const password = ref('grid-local-password')
const loginBusy = ref(false)
const topology = ref<Row>({ vpps: [], edgeCount: 0, deviceCount: 0 })
const overview = ref<Row>({ vpps: [] })
const selected = ref('vpp-a')
const section = ref('overview')
const workbench = ref<Row>({ edges: [] })
const batches = ref<Page>(emptyPage())
const groups = ref<Page>(emptyPage())
const loading = ref(false)
const busy = ref('')
const stale = ref('')
const updatedAt = ref('')
const batchPage = ref(1)
const groupPage = ref(1)
const detail = ref<Row | null>(null)
const drawer = ref(false)
const detailTab = ref('data')
const recordSearch = ref('')
const expandedEdges = ref<string[]>([])
const detailBusy = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let refreshing = false
const vppState = computed(() => workbench.value.vpp?.data || {})
const gridState = computed(() => workbench.value.grid?.data || {})
const isRunning = computed(() => workbench.value.edges.some((e: Row) => e.data?.collection?.running))
const inFlight = computed(() => workbench.value.edges.some((e: Row) => e.data?.collection?.busy))
const ms = (value: unknown, digits = 2) => typeof value === 'number' && Number.isFinite(value)
  ? value.toFixed(digits) + ' ms' : '未记录'
const comparisonTime = (report: Row | null | undefined) => typeof report?.comparisonNs === 'number' ? ms(report.comparisonNs / 1e6, 6) : '尚未按新口径计时'
const chainTime = (anchor: Row | null | undefined) => typeof anchor?.commitLatencyMs === 'number' ? ms(anchor.commitLatencyMs) : '历史记录未测'
const filteredRecords = computed<Row[]>(() => {
  const rows: Row[] = detail.value?.records || []
  const text = recordSearch.value.trim().toLowerCase()
  return text ? rows.filter(r => [r.recordId, r.deviceId, detail.value?.deviceOrigins?.[r.deviceId]].some(v => String(v || '').toLowerCase().includes(text))) : rows
})
interface EdgeGroup { edgeId: string; records: Row[]; packet?: Row; collected: string[] }
function collectionRange(records: Row[]): string[] {
  const times = records.map(r => r.timestamp).filter((t): t is string => typeof t === 'string' && !Number.isNaN(Date.parse(t)))
    .sort((a, b) => Date.parse(a) - Date.parse(b))
  if (!times.length) return []
  return date(times[0]!) === date(times[times.length - 1]!) ? [times[0]!] : [times[0]!, times[times.length - 1]!]
}
const batchCollectionRange = computed(() => collectionRange(detail.value?.records || []))
const edgeGroups = computed<EdgeGroup[]>(() => {
  const grouped = new Map<string, EdgeGroup>()
  const packets: Row[] = detail.value?.edgePackets || []
  for (const record of detail.value?.records || []) {
    const edgeId = detail.value?.deviceOrigins?.[record.deviceId] || '未记录归属'
    if (!grouped.has(edgeId)) grouped.set(edgeId, { edgeId, records: [], packet: packets.find(p => p.edgeId === edgeId), collected: [] })
    grouped.get(edgeId)!.records.push(record)
  }
  return [...grouped.values()].sort((a, b) => a.edgeId.localeCompare(b.edgeId, 'zh-CN', { numeric: true }))
    .map(group => ({ ...group, collected: collectionRange(group.records) }))
})
const visibleEdgeGroups = computed(() => {
  const matching = new Set(filteredRecords.value)
  return edgeGroups.value.map(group => ({ ...group, total: group.records.length, records: group.records.filter(r => matching.has(r)) }))
    .filter(group => group.records.length)
})
function toggleEdge(edgeId: string) {
  expandedEdges.value = expandedEdges.value.includes(edgeId) ? expandedEdges.value.filter(id => id !== edgeId) : [...expandedEdges.value, edgeId]
}
const deviceType = (value: string) => ({ pv: '光伏', storage: '储能', charger: '充电桩' }[value] || value)
const number = (value: unknown) => Number(value || 0).toLocaleString('zh-CN')
const statusName = (status: string) => ({ VERIFIED: '验真通过', DELIVERED: '已送达', CONFIRMED: '上链成功', READY: '待批量校验', PASS: '校验通过', FAIL: '校验失败', ERROR: '校验异常', PENDING: '待重试', GRID_REJECTED: 'GRID 拒绝', BUILT: '已组批', ANCHORED: '已上链', DELIVERY_FAILED: '投递失败' }[status] || status || '暂无数据')
const statusClass = (status: string) => ['VERIFIED', 'DELIVERED', 'CONFIRMED', 'PASS'].includes(status) ? 'pass' : ['FAIL', 'GRID_REJECTED', 'DELIVERY_FAILED'].includes(status) ? 'fail' : 'pending'
const navigation = computed(() => [
  { id: 'overview', icon: '◈', title: '实验总览' },
  { id: 'collection', icon: '⌁', title: '边缘采集与 VPP' },
  { id: 'grid', icon: '▦', title: '国网校验工作台' },
  ...(user.value?.role === 'grid' ? [{ id: 'commands', icon: '↗', title: '指令下发' }] : [])
])
const currentTitle = computed(() => navigation.value.find(item => item.id === section.value)?.title || '实验总览')

async function login() {
  loginBusy.value = true
  try {
    user.value = await api('/api/auth/login', { method: 'POST', body: { username: username.value, password: password.value } })
    await initialise()
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { loginBusy.value = false }
}
async function logout() {
  await api('/api/auth/logout', { method: 'POST' })
  user.value = null
  section.value = 'overview'
}
async function initialise() {
  if (user.value?.role !== 'grid' && section.value === 'commands') section.value = 'overview'
  topology.value = await api('/api/v2/topology')
  selected.value = topology.value.vpps.some((v: Row) => v.vppId === selected.value) ? selected.value : topology.value.vpps[0].vppId
  await refresh(true)
}
async function refresh(showLoading = false) {
  if (!user.value || refreshing) return
  refreshing = true
  if (showLoading) loading.value = true
  const vpp = selected.value
  try {
    const [overviewData, workbenchData, batchData, groupData] = await Promise.all([
      api('/api/v2/overview'),
      api('/api/v2/vpps/' + vpp + '/workbench'),
      api<Page>(`/api/v2/grid/batches?vppId=${vpp}&page=${batchPage.value}&pageSize=10`),
      api<Page>(`/api/v2/grid/groups?vppId=${vpp}&page=${groupPage.value}&pageSize=10`)
    ])
    overview.value = overviewData
    if (vpp === selected.value) {
      workbench.value = workbenchData
      batches.value = batchData
      groups.value = groupData
    }
    stale.value = ''
    updatedAt.value = new Date().toISOString()
  } catch (e) { stale.value = (e as Error).message }
  finally {
    refreshing = false
    loading.value = false
    if (vpp !== selected.value && user.value) void refresh(true)
  }
}
async function control(operation: string, all = false) {
  busy.value = operation
  try {
    const ids = all ? topology.value.vpps.map((v: Row) => v.vppId) : [selected.value]
    const results = await Promise.allSettled(ids.map((vpp: string) => api(`/api/v2/vpps/${vpp}/collection/${operation}`, { method: 'POST' })))
    const errors = results.filter(r => r.status === 'rejected')
    if (errors.length) ElMessage.error(`${errors.length} 个 VPP 操作未完成，请查看服务状态`)
    else ElMessage.success(operation === 'start' ? '已启动，每 30 秒生成一轮；首轮约 2 秒后开始' : operation === 'pause' ? '已暂停调度，正在发送的批次将完成' : '已完成单轮采样及上链校验')
    await refresh()
  } finally { busy.value = '' }
}
async function inspect(kind: string, id: string) {
  detailBusy.value = true
  try {
    detail.value = await api(`/api/v2/grid/${kind}/${id}`)
    detailTab.value = 'data'
    recordSearch.value = ''
    expandedEdges.value = []
    drawer.value = true
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { detailBusy.value = false }
}
async function verify(group: Row) {
  busy.value = group.groupId
  try {
    const result = await api(`/api/v2/grid/groups/${group.groupId}/verify`, { method: 'POST' })
    const full = await api(`/api/v2/grid/groups/${group.groupId}`)
    detail.value = { ...full, verification: result, status: result.status }
    expandedEdges.value = []
    detailTab.value = 'evidence'
    drawer.value = true
    if (result.status === 'PASS') ElMessage.success(`12000 条原始数据校验通过 · 根比较 ${comparisonTime(result)}`)
    else ElMessage.error('校验未通过，请查看结果详情')
    await refresh()
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = '' }
}
function expired() { user.value = null }
watch(recordSearch, () => { expandedEdges.value = [] })
watch(drawer, open => { if (!open) expandedEdges.value = [] })
watch(selected, async () => {
  batchPage.value = groupPage.value = 1
  workbench.value = { edges: [] }
  batches.value = emptyPage()
  groups.value = emptyPage()
  // The completing request queues the selected VPP if a poll is already in flight.
  if (refreshing) return
  await refresh(true)
})
watch([batchPage, groupPage], () => refresh())
onMounted(async () => {
  window.addEventListener('prototype-auth-expired', expired)
  try { const me = await api('/api/auth/me'); user.value = me.user; await initialise() } catch { /* login screen */ }
  timer = setInterval(() => { void refresh() }, 4000)
})
onUnmounted(() => { if (timer) clearInterval(timer); window.removeEventListener('prototype-auth-expired', expired) })
</script>

<template>
  <div v-if="!user" class="login-page">
    <div class="login-art"><span class="eyebrow">QKL / ENERGY EVIDENCE LAB</span><h1>让每一批能源数据<br>都有可校验的依据。</h1><p>边缘采集 → 虚拟电厂 → 国网平台<br>原始数据直传，根哈希留证。</p><div class="login-diagram"><span>30</span><i>→</i><span>300</span><i>→</i><span>12,000</span></div><small>单边缘设备记录 / VPP 业务批次 / 聚合校验记录</small></div>
    <form class="login-card" @submit.prevent="login"><span class="eyebrow">本地实验控制台 · V2</span><h2>登录工作台</h2><label for="username">账户</label><input id="username" v-model="username" autocomplete="username"><label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password"><button class="primary" :disabled="loginBusy">{{ loginBusy ? '正在连接…' : '进入实验控制台' }}</button><p class="login-hint">GRID：grid / grid-local-password<br>VPP：vpp / vpp-local-password</p></form>
  </div>
  <div v-else class="app-shell">
    <aside class="sidebar"><div class="brand"><b>Q</b><span>QKL<span class="brand-sub">能源数据验真实验</span></span></div><div class="nav-label">WORKSPACE / V2</div><nav><button v-for="item in navigation" :key="item.id" :class="{ active: section === item.id }" @click="section = item.id"><i>{{ item.icon }}</i>{{ item.title }}</button></nav><div class="sidebar-note"><span class="live-dot"></span>FISCO BCOS<br><small>5 个 PBFT 共识节点<br>仅根哈希与定位元数据上链</small></div><div class="account"><span class="avatar">{{ user.role === 'grid' ? 'G' : 'V' }}</span><div>{{ user.username }}<small>{{ user.role.toUpperCase() }} 工作账户</small></div><button title="退出登录" @click="logout">↪</button></div></aside>
    <main class="workspace" v-loading="loading">
      <header class="topbar"><span>本地实验 <i>/</i> {{ currentTitle }}</span><div><span class="refresh-label">{{ updatedAt ? date(updatedAt).slice(-8) + ' 更新' : '等待连接' }}</span><button class="ghost" @click="refresh(true)">↻ 刷新</button></div></header>
      <div class="page-content"><div class="page-heading"><div><span class="eyebrow">ENERGY DATA / ROOT COMMITMENT</span><h1>{{ currentTitle }}</h1><p>{{ section === 'commands' ? 'GRID 直传模拟指令，VPP 独立读链核验并返回成功。' : '3 个 VPP 独立组批、独立上链，GRID 分别接收与核验。' }}</p></div><div class="context-picker"><label for="vpp-select">当前 VPP</label><select id="vpp-select" v-model="selected"><option v-for="v in topology.vpps" :key="v.vppId" :value="v.vppId">{{ v.vppId.toUpperCase() }}</option></select></div></div>
        <div v-if="stale" class="notice error" role="alert">{{ stale }}。当前保留上次成功数据。</div>
        <div v-if="!workbench.vpp?.available && workbench.vpp" class="notice error">{{ selected }} 服务不可用：{{ workbench.vpp.error }}</div>
        <CommandWorkspace v-if="section === 'commands' && user.role === 'grid'" :selected-vpp="selected" />
        <template v-if="section === 'overview'">
          <div class="metric-grid"><article class="metric"><label>虚拟电厂</label><strong>{{ topology.vpps.length }}<small>VPP</small></strong><p>每个 VPP 对应 10 个边缘节点</p></article><article class="metric"><label>边缘节点</label><strong>{{ topology.edgeCount }}<small>EDGE</small></strong><p>每个节点对应 30 台设备</p></article><article class="metric"><label>模拟设备</label><strong>{{ number(topology.deviceCount) }}<small>DEVICE</small></strong><p>光伏 · 储能 · 充电桩</p></article><article class="metric accent"><label>GRID 已接收并自动验真</label><strong>{{ number(overview.grid?.data?.recordCount) }}<small>条</small></strong><p>{{ number(overview.grid?.data?.batchCount) }} 个 300 条业务批次</p></article></div>
          <section class="panel flow-panel"><div class="panel-heading"><div><h2>数据与证据流</h2><p>数据走 HTTP，接收方独立读链比对根哈希</p></div><span class="badge neutral">SHA-256 / Merkle</span></div><div class="flow"><div><span class="step">01</span><h3>边缘节点</h3><strong>30 条 / 30 秒</strong><p>计算设备数据根哈希<br>上链后将原始数据发给 VPP</p></div><i>→</i><div><span class="step">02</span><h3>虚拟电厂 VPP</h3><strong>300 条 / 轮</strong><p>验证所属 10 个边缘数据包<br>组批、上链并将原始数据发给 GRID</p></div><i>→</i><div><span class="step">03</span><h3>国网平台 GRID</h3><strong>12,000 条 / 40 批</strong><p>每个 300 条批次自动校验<br>40 批另提供手动批量校验</p></div></div></section>
          <div class="section-line"><h2>三个 VPP 的运行状态</h2><div><button class="ghost" :disabled="!!busy" @click="control('pause', true)">暂停全部</button><button class="primary" :disabled="!!busy" @click="control('start', true)">{{ busy === 'start' ? '启动中…' : '启动全部采集' }}</button></div></div>
          <div class="vpp-grid"><article v-for="v in overview.vpps" :key="v.vppId" class="panel vpp-card"><div><h3>{{ v.vppId.toUpperCase() }}</h3><span class="badge" :class="v.available ? 'pass' : 'fail'">{{ v.available ? (v.data.session?.running ? '采集中' : '已暂停') : '离线' }}</span></div><p>10 个边缘节点 · 300 台设备</p><dl><div><dt>已生成业务批次</dt><dd>{{ number(v.data?.batchCount) }}</dd></div><div><dt>已形成 40 批聚合</dt><dd>{{ number(v.data?.groupCount) }}</dd></div><div><dt>当前聚合进度</dt><dd>{{ v.data?.groupProgress || 0 }} / 40</dd></div></dl><button class="text-button" @click="selected = v.vppId; section = 'collection'">进入 {{ v.vppId.toUpperCase() }} →</button></article></div>
          <div class="chain-strip"><span class="live-dot"></span><b>{{ topology.chainName }}</b><span>合约</span><code :title="topology.registryAddress">{{ topology.registryAddress }}</code></div>
        </template>
        <template v-if="section === 'collection'">
          <section class="panel control-panel"><div><span class="badge" :class="isRunning ? 'pass' : 'neutral'">{{ isRunning ? '每 30 秒采集中' : '采集已暂停' }}</span><h2>{{ selected.toUpperCase() }} 采集控制</h2><p>每轮 10 个边缘各上报 30 条，收齐后形成一个 300 条批次。</p></div><div class="control-buttons"><button class="ghost" :disabled="!!busy || isRunning || inFlight" @click="control('tick')">{{ busy === 'tick' ? '单轮处理中…' : '单轮采样' }}</button><button class="ghost" :disabled="!!busy || !isRunning" @click="control('pause')">暂停采集</button><button class="primary" :disabled="!!busy || isRunning || inFlight" @click="control('start')">启动 30 秒采集</button></div></section>
          <p class="help">暂停后不再安排新一轮，已经开始的上链和投递会完成。恢复采集沿用当前会话，批次序号继续累加。单轮采样便于快速实验。</p>
          <div class="metric-grid three"><article class="metric"><label>VPP 已验真边缘包</label><strong>{{ number(vppState.receivedEdgePackets) }}<small>× 30 条</small></strong></article><article class="metric"><label>VPP 业务批次</label><strong>{{ number(vppState.batchCount) }}<small>× 300 条</small></strong></article><article class="metric accent"><label>当前 40 批聚合进度</label><strong>{{ vppState.groupProgress || 0 }}<small>/ 40 批</small></strong><p>满 40 批后自动提交聚合根哈希</p></article></div>
          <section class="panel"><div class="panel-heading"><div><h2>所属边缘节点</h2><p>上链确认时延：从提交交易到独立查询合约读到匹配的根和发布者，不计后续回执查询。</p></div><span class="badge neutral">{{ workbench.edges.length }} 个节点</span></div><div class="table-wrap"><table><thead><tr><th>边缘节点</th><th>设备</th><th>采集状态</th><th>最近轮次</th><th>最近数据包</th><th>根哈希上链确认时延</th><th>投递状态</th></tr></thead><tbody><tr v-for="e in workbench.edges" :key="e.edgeId"><td><b>{{ e.edgeId }}</b><small class="cell-sub">:{{ e.port }}</small></td><td>{{ e.deviceCount }} 台</td><td><span class="live-dot" :class="{ muted: !e.data?.collection?.running }"></span>{{ !e.available ? '离线' : e.data?.collection?.running ? '采集中' : e.data?.collection?.busy ? '正在完成本轮' : '已暂停' }}</td><td>{{ e.data?.latest?.sequence || '—' }}</td><td><code :title="e.data?.latest?.packetId">{{ short(e.data?.latest?.packetId) }}</code></td><td class="timing">{{ chainTime(e.data?.latest?.chainAnchor) }}</td><td><span class="badge" :class="statusClass(e.data?.latest?.status)">{{ statusName(e.data?.latest?.status) }}</span><small v-if="e.data?.lastError" class="error-text">{{ e.data.lastError }}</small></td></tr></tbody></table></div></section>
          <section class="panel collection-batches">
            <div class="panel-heading"><div><h2>数据批次与边缘分组</h2><p>每行汇总同一轮的 10 个边缘数据包；点击查看详情，再展开边缘节点的 30 条记录。采集、组批和 GRID 接收时间为北京时间，均显示到秒。</p></div><span class="badge neutral">GRID 已接收 {{ batches.total }} 批</span></div>
            <div class="table-wrap"><table><thead><tr><th>轮次 / 批次</th><th>边缘分组 / 数据量</th><th>VPP 组批时间</th><th>GRID 接收入库时间</th><th>验真状态</th><th>操作</th></tr></thead><tbody>
              <tr v-for="b in batches.items" :key="b.batchId">
                <td><b>#{{ b.sequence }}</b><small class="cell-sub"><code :title="b.batchId">{{ short(b.batchId) }}</code></small></td>
                <td>10 组 × 30 条<small class="cell-sub">合计 {{ b.recordCount }} 条</small></td>
                <td class="timestamp-cell"><time :datetime="b.builtAt" :title="b.builtAt">{{ date(b.builtAt) }}</time></td>
                <td class="timestamp-cell"><time :datetime="b.createdAt" :title="b.createdAt">{{ date(b.createdAt) }}</time></td>
                <td><span class="badge" :class="statusClass(b.status)">{{ statusName(b.status) }}</span></td>
                <td><button class="text-button" :disabled="detailBusy" @click="inspect('batches', b.batchId)">查看详情</button></td>
              </tr>
              <tr v-if="!batches.items.length"><td colspan="6" class="empty">启动采集或点击“单轮采样”，收齐 10 个边缘后在此查看完整批次。</td></tr>
            </tbody></table></div>
            <el-pagination v-if="batches.total > 10" v-model:current-page="batchPage" :total="batches.total" :page-size="10" layout="prev, pager, next, total" />
          </section>
        </template>
        <template v-if="section === 'grid'">
          <div class="metric-grid three"><article class="metric"><label>{{ selected.toUpperCase() }} · GRID 已验真</label><strong>{{ number(gridState.batchCount) }}<small>业务批次</small></strong><p>{{ number(gridState.recordCount) }} 条原始设备数据</p></article><article class="metric"><label>可执行 12000 条批量校验</label><strong>{{ number(gridState.groupCount) }}<small>聚合批次</small></strong><p>每组来自同一 VPP、同一会话的连续 40 批</p></article><article class="metric accent"><label>已通过手动批量校验</label><strong>{{ number(gridState.verifiedGroupCount) }}<small>组</small></strong><p>重算 40 个数据根，再计算聚合根</p></article></div>
          <section class="panel"><div class="panel-heading"><div><h2>40 批聚合校验 <span class="heading-tag">12,000 条 / 组</span></h2><p>点击后从 GRID 本地原始数据重算，与独立读链取得的聚合根比较。未满 40 批时不会生成可校验项。</p></div></div><div v-if="user.role !== 'grid'" class="notice">使用 GRID 账户登录后可执行批量校验。</div><div class="table-wrap"><table><thead><tr><th>聚合 ID / 会话</th><th>批次范围</th><th>VPP 聚合 / GRID 接收时间</th><th>聚合根</th><th>上链确认时延</th><th>根比较耗时（仅比较）</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="g in groups.items" :key="g.groupId"><td><code :title="g.groupId">{{ short(g.groupId) }}</code><small class="cell-sub" :title="g.sessionId">{{ short(g.sessionId) }}</small></td><td>#{{ g.startSequence }} – {{ g.endSequence }}<small class="cell-sub">40 批 · 12,000 条</small></td><td class="timestamp-cell"><time :datetime="g.builtAt" :title="g.builtAt">{{ date(g.builtAt) }}</time><small class="cell-sub"><time :datetime="g.createdAt" :title="g.createdAt">{{ date(g.createdAt) }}</time></small></td><td><code :title="g.rootHash">{{ short(g.rootHash) }}</code></td><td class="timing">{{ chainTime(g.chainAnchor) }}</td><td class="timing">{{ comparisonTime(g.verification) }}<small class="cell-sub">不含重算与读链</small></td><td><span class="badge" :class="statusClass(g.status)">{{ statusName(g.status) }}</span></td><td><div class="row-actions"><button class="primary small-button" :disabled="!!busy || user.role !== 'grid'" @click="verify(g)">{{ busy === g.groupId ? '校验中…' : '批量校验' }}</button><button class="text-button" @click="inspect('groups', g.groupId)">查看详情</button></div></td></tr><tr v-if="!groups.items.length"><td colspan="8" class="empty"><b>正在等待本 VPP 累计 40 个业务批次</b><p>当前聚合进度 {{ vppState.groupProgress || 0 }} / 40。三个 VPP 分别累计，不合并。</p></td></tr></tbody></table></div><el-pagination v-if="groups.total > 10" v-model:current-page="groupPage" :total="groups.total" :page-size="10" layout="prev, pager, next, total" /></section>
          <section class="panel"><div class="panel-heading"><div><h2>业务批次自动验真 <span class="heading-tag">300 条 / 批</span></h2><p>VPP 原始数据直传 GRID；GRID 验签、重算根、读链比对，验真结果仅保存在 GRID，不回传给 VPP。</p></div><span class="badge neutral">共 {{ batches.total }} 批</span></div><div class="table-wrap"><table><thead><tr><th>轮次 / 批次</th><th>VPP 组批 / GRID 接收时间</th><th>边缘分组 / 数据量</th><th>根哈希</th><th>上链确认时延</th><th>GRID 自动验真时延</th><th>验真状态</th><th>操作</th></tr></thead><tbody><tr v-for="b in batches.items" :key="b.batchId"><td><b>#{{ b.sequence }}</b><small class="cell-sub"><code :title="b.batchId">{{ short(b.batchId) }}</code></small></td><td class="timestamp-cell"><time :datetime="b.builtAt" :title="b.builtAt">{{ date(b.builtAt) }}</time><small class="cell-sub"><time :datetime="b.createdAt" :title="b.createdAt">{{ date(b.createdAt) }}</time></small></td><td>10 组 × 30 条<small class="cell-sub">合计 {{ b.recordCount }} 条</small></td><td><code :title="b.rootHash">{{ short(b.rootHash) }}</code></td><td class="timing">{{ chainTime(b.chainAnchor) }}</td><td class="timing">{{ ms(b.verification?.verificationMs) }}</td><td><span class="badge" :class="statusClass(b.status)">{{ statusName(b.status) }}</span></td><td><button class="text-button" :disabled="detailBusy" @click="inspect('batches', b.batchId)">查看详情</button></td></tr><tr v-if="!batches.items.length"><td colspan="8" class="empty">GRID 暂未收到当前 VPP 的 300 条业务批次。</td></tr></tbody></table></div><el-pagination v-if="batches.total > 10" v-model:current-page="batchPage" :total="batches.total" :page-size="10" layout="prev, pager, next, total" /></section>
          <div class="notice"><b>时延口径</b>　上链确认时延从提交交易计至合约中读到匹配根及发布者，不等待后续回执查询。批量校验只计两根的相等比较，以毫秒（ms）显示；12000 条重算和读链仍执行，但不计入该比较耗时。旧批次没有新口径数据时显示未测，可在详情查看原回执时延。</div>
        </template>
      </div><footer>QKL · 本地模拟实验 <span>原始设备信息保存在各业务节点；链上保存根哈希、批次 ID、数量、发布者及协议版本。</span></footer>
    </main>
    <el-drawer v-model="drawer" title="批次与链上证据详情" size="min(1080px, 96vw)" :show-close="false">
      <template #header="{ close, titleId, titleClass }">
        <h2 :id="titleId" :class="titleClass">批次与链上证据详情</h2>
        <button class="ghost" @click="close">关闭详情</button>
      </template>
      <template v-if="detail">
        <div class="detail-summary">
          <span class="badge" :class="statusClass(detail.status)">{{ statusName(detail.status) }}</span>
          <h3>{{ detail.vppId?.toUpperCase() }} · {{ detail.recordCount?.toLocaleString() }} 条</h3>
          <p class="full-hash">{{ detail.groupId || detail.batchId }}</p>
          <p class="detail-meta">会话 {{ detail.sessionId }} · {{ detail.groupId ? `第 ${detail.startSequence}～${detail.endSequence} 批` : `第 ${detail.sequence} 批` }}</p>
          <div class="batch-timestamps">
            <div v-if="!detail.groupId"><span>设备采集时间 / 范围</span><div class="timestamp-value"><time v-for="(value, index) in batchCollectionRange" :key="value" :datetime="value" :title="value">{{ index ? ' ～ ' : '' }}{{ date(value) }}</time><span v-if="!batchCollectionRange.length">未记录</span></div></div>
            <div><span>VPP {{ detail.groupId ? '聚合' : '组批' }}时间</span><time class="timestamp-value" :datetime="detail.builtAt" :title="detail.builtAt">{{ date(detail.builtAt) }}</time></div>
            <div><span>GRID 接收入库时间</span><time class="timestamp-value" :datetime="detail.createdAt" :title="detail.createdAt">{{ date(detail.createdAt) }}</time></div>
          </div>
          <p class="timestamp-note">北京时间（UTC+8）· 所有时间戳显示到秒，耗时单位为毫秒（ms） · 悬停可查看原始时间戳</p>
        </div>
        <el-tabs v-model="detailTab">
          <el-tab-pane :label="detail.groupId ? '40 批清单' : `${detail.recordCount} 条原始记录 · 边缘分组`" name="data">
            <template v-if="detail.groupId">
              <p class="help">每批可继续查看 GRID 保存的 300 条原始设备数据。最近一次重算结果与清单并列显示。</p>
              <div class="table-wrap"><table class="manifest-table"><thead><tr><th>批次</th><th>批次 ID</th><th>VPP 提交的根 R</th><th>最近重算的根 R′</th><th>操作</th></tr></thead><tbody>
                <tr v-for="(item, index) in detail.batches" :key="item.batchId">
                  <td>#{{ item.sequence }}<small class="cell-sub">{{ item.recordCount }} 条</small></td>
                  <td><code :title="item.batchId">{{ short(item.batchId) }}</code></td>
                  <td><code :title="item.rootHash">{{ short(item.rootHash) }}</code></td>
                  <td><code :title="detail.verification?.batchChecks?.[index]?.computedRootHash">{{ short(detail.verification?.batchChecks?.[index]?.computedRootHash) }}</code></td>
                  <td><button class="text-button" :disabled="detailBusy" @click="inspect('batches', item.batchId)">查看详情</button></td>
                </tr>
              </tbody></table></div>
            </template>
            <template v-else>
              <p class="help">本批共 {{ edgeGroups.length }} 个边缘组、{{ detail.records?.length || 0 }} 条设备记录。点击边缘组查看记录；各组默认收起。</p>
              <div class="record-toolbar"><label for="record-search">筛选设备 / 边缘 / 记录 ID</label><input id="record-search" v-model="recordSearch" placeholder="例如 edge-01 或 PV-01"><span>共 {{ filteredRecords.length }} / {{ detail.records?.length || 0 }} 条</span></div>
              <div class="edge-groups">
                <section v-for="group in visibleEdgeGroups" :key="group.edgeId" class="edge-record-group" :data-edge-id="group.edgeId">
                  <button class="edge-group-toggle" :aria-expanded="expandedEdges.includes(group.edgeId)" :aria-controls="`edge-records-${group.edgeId}`" @click="toggleEdge(group.edgeId)">
                    <span class="edge-group-title"><strong>{{ group.edgeId }}</strong><small>{{ group.records.length === group.total ? `${group.total} 条设备记录` : `匹配 ${group.records.length} / ${group.total} 条` }}</small></span>
                    <span class="edge-group-collected"><small>设备采集时间{{ group.collected.length > 1 ? '范围' : '' }}</small><span><time v-for="(value, index) in group.collected" :key="value" :datetime="value" :title="value">{{ index ? ' ～ ' : '' }}{{ date(value) }}</time><span v-if="!group.collected.length">未记录</span></span></span>
                    <span class="edge-group-action">{{ expandedEdges.includes(group.edgeId) ? '收起记录' : '展开记录' }}<i aria-hidden="true">{{ expandedEdges.includes(group.edgeId) ? '−' : '+' }}</i></span>
                  </button>
                  <div v-if="expandedEdges.includes(group.edgeId)" :id="`edge-records-${group.edgeId}`" class="edge-group-body">
                    <div v-if="group.packet" class="edge-packet-evidence">
                      <div><span>边缘数据包</span><code :title="group.packet.packetId">{{ group.packet.packetId }}</code></div>
                      <div><span>边缘根哈希</span><code class="full-hash">{{ group.packet.rootHash }}</code></div>
                      <div class="edge-packet-timing"><span>上链确认 {{ chainTime(group.packet.chainAnchor) }}</span><span>VPP 验真 {{ ms(group.packet.verification?.durationMs) }}</span><span class="badge" :class="statusClass(group.packet.verification?.status)">{{ statusName(group.packet.verification?.status) }}</span></div>
                    </div>
                    <div class="table-wrap"><table class="records-table"><thead><tr><th>设备 ID</th><th>类型</th><th>功率（kW）</th><th>采集时间（北京时间，秒）</th><th>记录 ID</th></tr></thead><tbody>
                      <tr v-for="record in group.records" :key="record.recordId"><td>{{ record.deviceId }}</td><td>{{ deviceType(record.deviceType) }}</td><td class="timing">{{ record.value }}</td><td class="timestamp-cell"><time :datetime="record.timestamp" :title="record.timestamp">{{ date(record.timestamp) }}</time></td><td><code :title="record.recordId">{{ short(record.recordId) }}</code></td></tr>
                    </tbody></table></div>
                  </div>
                </section>
                <p v-if="!visibleEdgeGroups.length" class="empty">没有匹配的原始记录。</p>
              </div>
            </template>
          </el-tab-pane>
          <el-tab-pane label="根哈希与时延" name="evidence">
            <dl class="detail-list">
              <dt>本地计算根哈希</dt><dd class="full-hash">{{ detail.verification?.computedRootHash || detail.rootHash }}</dd>
              <dt>独立读链取得的根哈希</dt><dd class="full-hash">{{ detail.verification?.chainRootHash || detail.chainRootHash || '待校验' }}</dd>
              <dt v-if="detail.groupId">根比较耗时（仅比较，ms）</dt><dd v-if="detail.groupId" class="comparison-result">{{ comparisonTime(detail.verification) }}</dd>
              <dt v-if="detail.groupId">两根是否相等</dt><dd v-if="detail.groupId">{{ detail.verification?.rootsEqual === true ? '相等' : detail.verification?.rootsEqual === false ? '不相等' : '尚未按新口径比较' }}</dd>
              <dt v-if="detail.groupId">以下时间不计入根比较耗时</dt><dd v-if="detail.groupId">重算 {{ ms(detail.verification?.hashComputeMs) }} · 读链 {{ ms(detail.verification?.chainQueryMs) }} · 完整处理 {{ ms(detail.verification?.totalVerificationMs ?? (detail.verification?.timingVersion ? null : detail.verification?.verificationMs)) }}</dd>
              <dt v-else>GRID 自动验真总时间</dt><dd v-if="!detail.groupId">{{ ms(detail.verification?.verificationMs) }}</dd>
              <dt>上链确认时延（提交 → 读到已提交链状态）</dt><dd>{{ chainTime(detail.chainAnchor) }}</dd>
              <dt>RPC 接收提交耗时（仅供参考，不能单独证明上链成功）</dt><dd>{{ ms(detail.chainAnchor?.broadcastLatencyMs) }}</dd>
              <dt>交易回执取得时延（独立记录）</dt><dd>{{ ms(detail.chainAnchor?.receiptLatencyMs) }}</dd>
              <dt>交易哈希</dt><dd class="full-hash">{{ detail.chainAnchor?.txHash || '恢复的链上记录，原交易哈希不可用' }}</dd>
              <dt>区块号 / 确认方式</dt><dd>{{ detail.chainAnchor?.blockNumber ?? '未取得' }} / {{ detail.chainAnchor?.confirmationMethod || '历史回执确认' }}</dd>
              <dt>合约地址</dt><dd class="full-hash">{{ detail.chainAnchor?.registryAddress }}</dd>
            </dl>
          </el-tab-pane>
          <el-tab-pane label="完整 JSON 与签名" name="json"><pre class="json-view">{{ JSON.stringify(detail, null, 2) }}</pre></el-tab-pane>
        </el-tabs>
      </template>
    </el-drawer>
  </div>
</template>
