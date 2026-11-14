export type Row = Record<string, any>
export interface Page { items: Row[]; total: number; page: number; pageSize: number }
export const emptyPage = (): Page => ({ items: [], total: 0, page: 1, pageSize: 25 })

export class APIError extends Error {
  constructor(message: string, public code: string, public retryable: boolean, public status = 0) { super(message) }
}

export async function api<T = Row>(path: string, options: { method?: string; key?: string; body?: Row } = {}): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), options.method === 'POST' ? 210000 : 25000)
  try {
    const headers: Record<string, string> = {}
    if (options.method === 'POST') {
      headers['Content-Type'] = 'application/json'
      headers['X-Prototype-Request'] = '1'
      if (options.key) headers['Idempotency-Key'] = options.key
    }
    const response = await fetch(path, {
      method: options.method || 'GET', headers, signal: controller.signal,
      body: options.method === 'POST' ? JSON.stringify(options.body || {}) : undefined, cache: 'no-store'
    })
    const value = await response.json()
    if (response.status === 401 && path !== '/api/auth/login') window.dispatchEvent(new Event('prototype-auth-expired'))
    if (response.status === 403) window.dispatchEvent(new Event('prototype-forbidden'))
    if (!response.ok) {
      const error = value.error || {}
      throw new APIError(error.message || '请求失败，操作结果尚未确认', error.code || 'REQUEST_FAILED', !!error.retryable, response.status)
    }
    return value as T
  } catch (error) {
    if (error instanceof APIError) throw error
    throw new APIError(error instanceof Error && error.name === 'AbortError'
      ? '请求超时，保留上次成功数据；操作结果尚未确认' : '服务连接失败，保留上次成功数据', 'NETWORK_ERROR', true)
  } finally { clearTimeout(timer) }
}

export function query(values: Row): string {
  const result = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => { if (value !== '' && value !== null && value !== undefined) result.set(key, String(value)) })
  return '?' + result.toString()
}

export function date(value: unknown): string {
  if (!value) return '未记录'
  const time = new Date(typeof value === 'number' ? value : String(value))
  if (Number.isNaN(time.getTime())) return '未记录'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(time)
}

const names: Record<string, string> = {
  VERIFIED: '边缘复核通过', PENDING: '待投递 / 重试中', REJECTED: '已拒绝',
  SIMULATED_REGISTERED: '已模拟登记', PASS: '通过', FAIL: '失败', ERROR: '检查未完成',
  NOT_CHECKED: '尚未重新核验', CONTINUOUS: '连续采集', SINGLE: '单轮生成',
  SAMPLING_STARTED: '开始连续采集', SAMPLING_PAUSED: '暂停连续采集',
  SINGLE_ROUND_GENERATED: '单轮记录已持久化', BATCH_SEALED_SIGNED: '窗口封批并签名',
  DELIVERY_ATTEMPT: '向 VPP 发起投递', DELIVERY_DEFERRED: '投递失败，已安排重试',
  DELIVERY_REJECTED: 'VPP 拒绝接收', MANUAL_RETRY_REQUESTED: '已请求立即重试',
  RECEIPT_VERIFIED: '边缘回执复核通过', BATCH_REVERIFIED: 'VPP 执行重新核验',
  SIGNATURE_VERIFIED_SIMULATED_REGISTERED: 'VPP 验签通过并模拟登记'
}
export const label = (value: string | null | undefined) => value ? names[value] || value : '未记录'
export const tone = (value: string) => ['VERIFIED', 'PASS', 'SIMULATED_REGISTERED'].includes(value) ? 'success'
  : ['REJECTED', 'FAIL'].includes(value) ? 'danger' : ['ERROR', 'PENDING'].includes(value) ? 'warning' : 'info'
export const deviceName = (type: string) => ({ photovoltaic: '光伏逆变器', battery_storage: '储能设备', charging_pile: '充电桩' }[type] || type)
export const short = (value: unknown) => value ? String(value).slice(0, 12) + '…' + String(value).slice(-8) : '未记录'
