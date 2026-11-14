<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, date, query, short, type Row, type Page, emptyPage } from './api'

const props = defineProps<{ selectedVpp: string }>()
const catalog = ref<Row[]>([])
const mode = ref('SINGLE')
const targetVpp = ref(props.selectedVpp)
const targetEdge = ref('')
const singleDevice = ref('')
const selectedDevices = ref<string[]>([])
const stagedTargets = ref<Row[]>([])
const releases = ref<Page>(emptyPage())
const pageNumber = ref(1)
const detail = ref<Row | null>(null)
const drawer = ref(false)
const publishing = ref(false)
const detailBusy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let refreshing = false
const edges = computed<Row[]>(() => catalog.value.find(v => v.vppId === targetVpp.value)?.edges || [])
const devices = computed<Row[]>(() => edges.value.find(e => e.edgeId === targetEdge.value)?.devices || [])
const targets = computed<Row[]>(() => mode.value === 'BULK' ? stagedTargets.value :
  singleDevice.value ? [{ vppId: targetVpp.value, edgeId: targetEdge.value, deviceId: singleDevice.value }] : [])
const targetVppCount = computed(() => new Set(targets.value.map(t => t.vppId)).size)
const ms = (value: unknown, digits = 2) => typeof value === 'number' ? value.toFixed(digits) + ' ms' : '未记录'
const comparisonTime = (value: Row | null | undefined) => typeof value?.comparisonNs === 'number' ? ms(value.comparisonNs / 1e6, 6) : '未记录'
const releaseComparisonTime = (value: Row) => value.vppCount === 1 ? comparisonTime(value) : '历史多 VPP 发布，见详情'
const fullVerificationTime = (receipt: Row | undefined) => ms(receipt?.totalVerificationMs ?? (receipt?.timingVersion ? undefined : receipt?.verificationMs))
const statusName = (value: string) => ({ VERIFIED: '核验成功', PUBLISHING: '发布中', BUILT: '已生成', ANCHORED: '已上链', PARTIAL: '部分成功', ERROR: '核验未完成' }[value] || value)
const typeName = (value: string) => ({ pv: '光伏', storage: '储能', charger: '充电桩' }[value] || value)
const millisTime = (value: unknown) => {
  if (!value) return '尚未收到'
  const time = new Date(String(value))
  return Number.isNaN(time.getTime()) ? '未记录' : date(value) + '.' + String(time.getUTCMilliseconds()).padStart(3, '0')
}
function chooseEdge() {
  singleDevice.value = devices.value[0]?.deviceId || ''
  selectedDevices.value = []
}
function chooseVpp(resetTargets = false) {
  if (resetTargets && stagedTargets.value.length) {
    stagedTargets.value = []
    ElMessage.info('已切换 VPP，原发布清单已清空；每次只向一个 VPP 发布。')
  }
  targetEdge.value = edges.value[0]?.edgeId || ''
  chooseEdge()
}
watch(targetVpp, () => chooseVpp(true), { flush: 'sync' })
watch(() => props.selectedVpp, value => { targetVpp.value = value })
watch(targetEdge, chooseEdge)
watch(pageNumber, () => refresh())
function addTargets() {
  for (const deviceId of selectedDevices.value) {
    if (!stagedTargets.value.some(t => t.deviceId === deviceId)) {
      stagedTargets.value.push({ vppId: targetVpp.value, edgeId: targetEdge.value, deviceId })
    }
  }
  selectedDevices.value = []
}
async function refresh() {
  if (refreshing) return
  refreshing = true
  try {
    releases.value = await api<Page>('/api/v2/grid/commands' + query({ page: pageNumber.value, pageSize: 10 }))
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { refreshing = false }
}
async function inspect(releaseId: string) {
  detailBusy.value = true
  try { detail.value = await api('/api/v2/grid/commands/' + releaseId); drawer.value = true }
  catch (e) { ElMessage.error((e as Error).message) }
  finally { detailBusy.value = false }
}
async function publish() {
  publishing.value = true
  try {
    const result = await api('/api/v2/grid/commands', { method: 'POST', body: { mode: mode.value, targets: targets.value } })
    detail.value = result
    drawer.value = true
    if (result.status === 'VERIFIED') ElMessage.success(`已收到 ${result.verifiedVppCount} 个 VPP 的核验成功`)
    else ElMessage.error('部分指令未取得核验成功，请查看各 VPP 的结果')
    pageNumber.value = 1
    await refresh()
  } catch (e) { ElMessage.error((e as Error).message); await refresh() }
  finally { publishing.value = false }
}
onMounted(async () => {
  try { catalog.value = (await api('/api/v2/grid/command-targets')).vpps; chooseVpp() }
  catch (e) { error.value = (e as Error).message }
  await refresh()
  timer = setInterval(() => { void refresh() }, 4000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="command-workspace">
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <section class="panel command-composer">
      <div class="panel-heading"><div><h2>发布模拟指令</h2><p>选择目标后自动生成有功功率指令，原文直传 VPP，链上只存根哈希与定位信息。</p></div><span class="badge neutral">GRID → VPP</span></div>
      <div class="command-form-body">
        <div class="command-mode" role="group" aria-label="发布方式"><label><input v-model="mode" type="radio" value="SINGLE" :disabled="publishing">单条发布</label><label><input v-model="mode" type="radio" value="BULK" :disabled="publishing">批量发布</label></div>
        <div class="command-target-selects">
          <label for="command-vpp">下发 VPP<select id="command-vpp" v-model="targetVpp" :disabled="publishing"><option v-for="v in catalog" :key="v.vppId" :value="v.vppId">{{ v.vppId.toUpperCase() }}</option></select></label>
          <label for="command-edge">边缘节点<select id="command-edge" v-model="targetEdge" :disabled="publishing"><option v-for="e in edges" :key="e.edgeId" :value="e.edgeId">{{ e.edgeId }}</option></select></label>
          <label v-if="mode === 'SINGLE'" for="command-device">设备编号<select id="command-device" v-model="singleDevice" :disabled="publishing"><option v-for="d in devices" :key="d.deviceId" :value="d.deviceId">{{ d.deviceId }} · {{ typeName(d.deviceType) }}</option></select></label>
        </div>
        <template v-if="mode === 'BULK'">
          <div class="command-device-heading"><b>选择设备编号</b><div><button class="text-button" :disabled="publishing" @click="selectedDevices = devices.map(d => d.deviceId)">全选当前边缘</button><button class="text-button" :disabled="publishing" @click="selectedDevices = []">清空选择</button></div></div>
          <div class="command-device-grid"><label v-for="d in devices" :key="d.deviceId"><input v-model="selectedDevices" type="checkbox" :value="d.deviceId" :disabled="publishing"><span>{{ d.deviceId }}<small>{{ typeName(d.deviceType) }}</small></span></label></div>
          <button class="ghost" :disabled="publishing || !selectedDevices.length" @click="addTargets">加入发布清单（{{ selectedDevices.length }} 台）</button>
          <div class="command-staging"><div class="command-device-heading"><b>发布清单 · {{ stagedTargets.length }} 台设备 · {{ targetVpp.toUpperCase() }}</b><button class="text-button" :disabled="publishing" @click="stagedTargets = []">清空清单</button></div><p class="help">每次只向一个 VPP 发布，可切换其所属边缘节点继续添加设备；切换 VPP 会清空清单。</p><div v-if="stagedTargets.length" class="command-target-chips"><span v-for="t in stagedTargets" :key="t.deviceId">{{ t.edgeId }} / {{ t.deviceId }}<button :disabled="publishing" :aria-label="`移除 ${t.deviceId}`" @click="stagedTargets = stagedTargets.filter(item => item.deviceId !== t.deviceId)">×</button></span></div></div>
        </template>
        <div class="command-preview"><b>模拟内容</b><p>设置有功功率：光伏 10 kW、储能 −5 kW、充电桩 7 kW，按设备类型自动生成。</p><p>所选 VPP 的全部指令合为一个批次、整批上链一个根；VPP 读链核验后直接返回“核验成功”。本阶段到 VPP 为止。</p></div>
        <div class="command-submit"><span>{{ targets.length }} 条指令 · {{ targetVpp.toUpperCase() }} · 整批一个根</span><button class="primary" :disabled="publishing || !targets.length || targetVppCount !== 1" @click="publish">{{ publishing ? '上链并等待 VPP 核验…' : mode === 'BULK' ? '批量发布指令' : '发布指令' }}</button></div>
      </div>
    </section>
    <section class="panel command-history">
      <div class="panel-heading"><div><h2>指令发布与核验记录</h2><p>发布时间到秒，GRID 收到核验成功的时间到毫秒；所有耗时单位为 ms。</p></div><button class="ghost" @click="refresh">刷新记录</button></div>
      <div class="table-wrap"><table><thead><tr><th>发布编号 / 方式</th><th>目标数量</th><th>发布时间</th><th>GRID 收到核验成功</th><th>验证耗时（仅根比较）</th><th>发布至收到成功</th><th>状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="r in releases.items" :key="r.releaseId"><td><code :title="r.releaseId">{{ short(r.releaseId) }}</code><small class="cell-sub">{{ r.mode === 'BULK' ? '批量发布' : '单条发布' }}</small></td><td>{{ r.commandCount }} 条 / {{ r.vppId?.toUpperCase() || `${r.vppCount} 个 VPP` }}</td><td class="timestamp-cell"><time :datetime="r.issuedAt">{{ date(r.issuedAt) }}</time></td><td class="timestamp-cell"><time :datetime="r.allVerifiedAt">{{ millisTime(r.allVerifiedAt) }}</time></td><td class="timing command-history-verification-ms">{{ releaseComparisonTime(r) }}</td><td class="timing">{{ ms(r.publishToReceiptMs) }}</td><td><span class="badge" :class="r.status === 'VERIFIED' ? 'pass' : 'pending'">{{ statusName(r.status) }}</span></td><td><button class="text-button" :disabled="detailBusy" @click="inspect(r.releaseId)">查看指令详情</button></td></tr>
        <tr v-if="!releases.items.length"><td colspan="8" class="empty">选择 VPP、边缘节点和设备后，发布第一条模拟指令。</td></tr>
      </tbody></table></div>
      <el-pagination v-if="releases.total > 10" v-model:current-page="pageNumber" :total="releases.total" :page-size="10" layout="prev, pager, next, total" />
    </section>
    <div class="notice"><b>计时范围</b>　指令验证耗时只计最终两个根是否相等，不含重算、验签、读链和传输，与数据批量校验口径一致。GRID 发布至收到成功的耗时另列。</div>
    <el-drawer v-model="drawer" title="指令核验详情" size="min(1120px, 96vw)" :show-close="false">
      <template #header="{ close, titleId, titleClass }"><h2 :id="titleId" :class="titleClass">指令核验详情</h2><button class="ghost" @click="close">关闭指令详情</button></template>
      <template v-if="detail">
        <div class="detail-summary command-release-summary"><span class="badge" :class="detail.status === 'VERIFIED' ? 'pass' : 'pending'">{{ statusName(detail.status) }}</span><h3>{{ detail.mode === 'BULK' ? '批量' : '单条' }}发布 · {{ detail.commandCount }} 条指令 / {{ detail.vppId?.toUpperCase() || `${detail.vppCount} 个 VPP` }}</h3><p class="full-hash">{{ detail.releaseId }}</p><p class="detail-meta">发布时间 {{ date(detail.issuedAt) }}<br>GRID 收到核验成功 {{ millisTime(detail.allVerifiedAt) }}<br>发布至收到成功 {{ ms(detail.publishToReceiptMs) }}</p></div>
        <section v-for="d in detail.dispatches" :key="d.dispatchId" class="command-dispatch" :data-vpp-id="d.vppId">
          <div class="command-device-heading"><h3>{{ d.vppId.toUpperCase() }} · {{ d.recordCount }} 条指令</h3><span class="badge" :class="d.status === 'VERIFIED' ? 'pass' : 'pending'">{{ statusName(d.status) }}</span></div>
          <p class="full-hash">{{ d.dispatchId }}</p>
          <div v-if="d.lastError" class="notice error">{{ d.lastError }}</div>
          <div class="command-timing-grid">
            <div><span>GRID 收到核验成功</span><strong class="command-received-at">{{ millisTime(d.receivedAt) }}</strong></div>
            <div><span>{{ d.mode === 'BULK' ? '批量指令验证耗时（仅根比较）' : '指令验证耗时（仅根比较）' }}</span><strong class="command-verification-ms command-comparison-ms">{{ comparisonTime(d.receipt) }}</strong></div>
            <div><span>根哈希上链确认</span><strong>{{ ms(d.chainAnchor?.commitLatencyMs) }}</strong></div>
            <div><span>GRID 发送至收到成功</span><strong>{{ ms(d.deliveryMs) }}</strong></div>
            <div><span>GRID 发布至收到成功</span><strong>{{ ms(d.publishToReceiptMs) }}</strong></div>
          </div>
          <details class="command-processing-details"><summary>完整处理耗时（不计入根比较）</summary><p>VPP 完整处理 {{ fullVerificationTime(d.receipt) }} · 重算根 {{ ms(d.receipt?.hashComputeMs) }} · 独立读链 {{ ms(d.receipt?.chainQueryMs) }}<br>GRID 完成发布及成功返回检查 {{ ms(detail.publicationTotalMs) }}</p></details>
          <dl class="detail-list command-evidence"><dt>GRID 提交的指令根</dt><dd class="full-hash">{{ d.rootHash }}</dd><dt>VPP 独立读链取得的根</dt><dd class="full-hash">{{ d.receipt?.chainRootHash || '尚未取得' }}</dd><dt>交易哈希 / 合约地址</dt><dd class="full-hash">{{ d.chainAnchor?.txHash || '未取得' }}<br>{{ d.chainAnchor?.registryAddress }}</dd></dl>
          <div class="table-wrap"><table class="command-items"><thead><tr><th>指令 ID</th><th>边缘节点</th><th>设备编号</th><th>模拟指令内容</th><th>发布时间</th></tr></thead><tbody><tr v-for="c in d.commands" :key="c.commandId"><td><code :title="c.commandId">{{ short(c.commandId) }}</code></td><td>{{ c.edgeId }}</td><td>{{ c.deviceId }}</td><td>设置有功功率 {{ c.params.activePowerKw }} kW</td><td class="timestamp-cell"><time :datetime="c.issuedAt">{{ date(c.issuedAt) }}</time></td></tr></tbody></table></div>
          <details class="command-json"><summary>查看原始指令与签名证据</summary><pre class="json-view">{{ JSON.stringify(d, null, 2) }}</pre></details>
        </section>
      </template>
    </el-drawer>
  </div>
</template>
