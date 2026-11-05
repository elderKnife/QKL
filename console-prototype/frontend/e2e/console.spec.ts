import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: { user: { username: 'vpp-operator', role: 'vpp' } } }))
})

test('overview shows real service health and five configured devices', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '运行总览', level: 1 })).toBeVisible()
  await expect(page.getByText('边缘服务').first()).toBeVisible()
  await expect(page.getByText('设备独立采样', { exact: false })).toBeVisible()
  await expect(page.getByText('PV-01', { exact: true })).toBeVisible()
  await expect(page.getByText('PV-02', { exact: true })).toBeVisible()
  await expect(page.getByText('BESS-01', { exact: true })).toBeVisible()
  await expect(page.getByText('BESS-02', { exact: true })).toBeVisible()
  await expect(page.getByText('EVSE-01', { exact: true })).toBeVisible()
  await page.screenshot({ path: 'overview-desktop.png', fullPage: true })
})

test('overview fits a mobile viewport without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '运行总览', level: 1 })).toBeVisible()
  await expect(page.getByText('PV-01', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.screenshot({ path: 'overview-mobile.png', fullPage: true })
})

test('VPP workbench renders only API-backed edge roster and received batches', async ({ page }) => {
  const root = 'abcdef' + '1'.repeat(54) + '9876'
  let edgeTwoAvailable = true
  let edgeTwoRunning: boolean | undefined = false
  await page.route('**/api/vpp/workbench/overview', route => route.fulfill({ json: { edgeRoster: [{ edgeId: 'EDGE-001', deviceCount: 5, status: 'CONFIGURED', sampleIntervalSeconds: 600, batchWindowSeconds: 1800 }, { edgeId: 'EDGE-002', deviceCount: 30, status: 'CONFIGURED', sampleIntervalSeconds: 600, batchWindowSeconds: 1800 }], batches: [{ packetId: 'packet-real-1', edgeId: 'EDGE-001', recordCount: 5, signatureStatus: 'PASS', merkleRoot: root, createdAt: '2026-11-04T10:00:00Z', chainStatus: 'SIMULATED_REGISTERED', chainMode: 'FISCO BCOS' }, { packetId: 'packet-short-root', merkleRoot: '0123456789' }, { packetId: 'packet-eleven-root', merkleRoot: '0123456789a' }, { packetId: 'packet-no-root' }], chainMode: 'FISCO BCOS' } }))
  await page.route('**/api/overview', route => route.fulfill({ json: { edge: { available: true, data: { edgeId: 'EDGE-001', database: 'ok', simulation: { running: true } } }, edge2: { available: edgeTwoAvailable, data: { edgeId: 'EDGE-002', database: 'ok', simulation: { running: edgeTwoRunning } } }, vpp: { available: true, data: { database: 'ok' } } } }))
  await page.route('**/api/edge/devices', route => route.fulfill({ json: { items: [{ deviceId: 'PV-01', deviceType: 'photovoltaic', minPowerKW: 0, maxPowerKW: 100, latestRecord: { value: 40, timestamp: '2026-11-04T10:00:00Z' } }] } }))
  await page.route('**/api/edge2/devices', route => route.fulfill({ json: { items: [{ deviceId: 'PV-30', deviceType: 'photovoltaic', minPowerKW: 0, maxPowerKW: 100, latestRecord: { value: 50, timestamp: '2026-11-04T10:00:00Z' } }] } }))
  await page.goto('/#vpp-workbench')
  await expect(page.getByRole('heading', { name: 'VPP 工作台', level: 1 })).toBeVisible()
  const nodePanel = page.locator('section.panel').filter({ has: page.getByRole('heading', { name: 'Edge 节点状态与设备配置', exact: true }) })
  const edgeOneRow = nodePanel.getByRole('row').filter({ hasText: 'EDGE-001' })
  const edgeTwoRow = nodePanel.getByRole('row').filter({ hasText: 'EDGE-002' })
  await expect(edgeOneRow.getByRole('cell', { name: '5', exact: true })).toBeVisible()
  await expect(edgeTwoRow.getByRole('cell', { name: '30', exact: true })).toBeVisible()
  await expect(edgeOneRow.getByText('连续采集中', { exact: true })).toBeVisible()
  await expect(edgeTwoRow.getByText('已暂停', { exact: true })).toBeVisible()
  for (const row of [edgeOneRow, edgeTwoRow]) {
    await expect(row.getByText('可用 / 数据库正常', { exact: true })).toBeVisible()
    await expect(row.getByRole('cell', { name: '10 分钟', exact: true })).toBeVisible()
    await expect(row.getByRole('cell', { name: '30 分钟', exact: true })).toBeVisible()
  }
  await expect(edgeOneRow.getByRole('cell', { name: '2026/11/04 18:00:00', exact: true })).toBeVisible()
  await expect(edgeTwoRow.getByRole('cell', { name: '未记录', exact: true })).toBeVisible()
  await expect(nodePanel).not.toContainText(/FISCO|PBFT|FISCO BCOS|模拟链|运行适配器|登记状态/)
  await expect(page.locator('.device-tile')).toHaveCount(1)
  await page.locator('.device-filters .el-select').nth(1).click()
  await page.getByRole('option', { name: '光伏' }).click()
  await expect(page.locator('.device-tile')).toHaveCount(1)
  await page.locator('.device-filters .el-select').nth(0).click()
  await page.getByRole('option', { name: 'EDGE-002' }).click()
  await expect(page.getByText('PV-30')).toBeVisible()
  await expect(page.getByRole('heading', { name: '收件批次与验签' })).toBeVisible()
  await expect(page.getByText('packet-real-1')).toBeVisible()
  await expect(page.getByText('FISCO BCOS').first()).toBeVisible()
  await expect(page.getByText(root, { exact: true })).toHaveCount(0)
  await expect(page.getByText('abcdef****9876', { exact: true })).toBeVisible()
  await expect(page.getByText('**********', { exact: true })).toBeVisible()
  await expect(page.getByText('012345****789a', { exact: true })).toBeVisible()
  await expect(page.getByRole('row').filter({ hasText: 'packet-no-root' }).getByText('未记录', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: /显示|隐藏|复制/ })).toHaveCount(0)
  edgeTwoAvailable = false
  edgeTwoRunning = true
  await nodePanel.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(edgeTwoRow.getByText('不可用', { exact: true })).toBeVisible()
  await expect(edgeTwoRow.getByRole('cell', { name: '未确认', exact: true })).toBeVisible()
  edgeTwoAvailable = true
  edgeTwoRunning = undefined
  await nodePanel.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(edgeTwoRow.getByText('可用 / 数据库正常', { exact: true })).toBeVisible()
  await expect(edgeTwoRow.getByRole('cell', { name: '未确认', exact: true })).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('vpp-edge-panel-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await expect(nodePanel.locator('.el-scrollbar__wrap')).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('vpp-edge-panel-mobile.png'), fullPage: true })
})

test('GRID workbench limits navigation and loads paged batch and record data', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: { user: { username: 'grid-operator', role: 'grid' } } }))
  await page.route('**/api/grid/overview', route => route.fulfill({ json: { available: true, batchCount: 0, chainAdapter: 'simulated', realBlockchain: false } }))
  await page.route('**/api/grid/batches**', route => route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 25 } }))
  await page.route('**/api/grid/records**', route => route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 25 } }))
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/#grid-workbench')
  await expect(page.getByRole('heading', { name: 'GRID 工作台', level: 1 })).toBeVisible()
  await expect(page.getByRole('button', { name: 'VPP 工作台' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '运行总览' })).toHaveCount(0)
  await expect(page.getByText('完整记录')).toBeVisible()
  await expect(page.getByText('simulated', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
})

test('login submits credentials and routes the authenticated GRID role', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ status: 401, json: { error: { message: '未登录', code: 'UNAUTHENTICATED' } } }))
  await page.route('**/api/auth/login', async route => {
    expect(route.request().postDataJSON()).toEqual({ username: 'operator', password: 'secret' })
    await route.fulfill({ json: { user: { username: 'operator', role: 'grid' } } })
  })
  await page.route('**/api/grid/overview', route => route.fulfill({ json: { available: false } }))
  await page.route('**/api/grid/batches**', route => route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 25 } }))
  await page.route('**/api/grid/records**', route => route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 25 } }))
  await page.goto('/')
  await page.getByLabel('用户名').fill('operator')
  await page.getByLabel('密码').fill('secret')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('heading', { name: 'GRID 工作台', level: 1 })).toBeVisible()
  await expect(page.getByText('operator · GRID 职责')).toBeVisible()
})

test('GRID batch detail masks digest evidence and preserves re-verification', async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ json: { user: { username: 'grid-operator', role: 'grid' } } }))
  const packetId = 'grid-packet-0123456789-abcdefghijklmnopqrstuvwxyz-7'
  const batch = { packetId, sourceBatches: ['vpp-a-2', 'vpp-b-9'], localMerkleRoot: 'local-root-7', chainMerkleRoot: 'chain-root-7', txHash: '0xabc7', locator: { adapter: 'FISCO BCOS', txHash: '0xabc7', blockNumber: 407 }, blockNumber: 407, verificationStatus: 'PASS' }
  let verificationRequests = 0
  await page.route('**/api/grid/overview', route => route.fulfill({ json: { available: true, aggregationCount: 1, chainMode: 'simulated' } }))
  await page.route('**/api/grid/records**', route => route.fulfill({ json: { items: [], total: 0, page: 1, pageSize: 25 } }))
  await page.route('**/api/grid/batches**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname
    if (request.method() === 'POST') { expect(path).toBe('/api/grid/batches/' + packetId + '/verify'); verificationRequests++; await route.fulfill({ json: { status: 'PASS' } }); return }
    if (path.endsWith('/verifications')) { await route.fulfill({ json: { items: [{ checkedAt: '2026-11-04T10:00:00Z', status: 'PASS', observer: 'GRID', ruleVersion: 'prototype-v1', message: '摘要一致', result: { checks: [{ name: '本地 Merkle 摘要', status: 'PASS', message: '根匹配' }], realBlockchain: false } }] } }); return }
    if (path.endsWith('/' + packetId + '/records')) { await route.fulfill({ json: { items: [{ recordId: 'record-1', deviceId: 'PV-01', value: 12.4, unit: 'kW', timestamp: '2026-11-04T10:00:00Z' }], total: 1, page: 1, pageSize: 25 } }); return }
    if (path.endsWith('/' + packetId)) { await route.fulfill({ json: { ...batch, localMerkleRoot: batch.localMerkleRoot, verification: { status: 'PASS' } } }); return }
    await route.fulfill({ json: { items: [batch], total: 1, page: 1, pageSize: 25 } })
  })
  await page.goto('/#grid-workbench')
  const batchLink = page.locator('button.table-id').filter({ hasText: 'grid-packet-' })
  await expect(batchLink).toHaveText('grid-packet-…stuvwxyz-7')
  await expect(batchLink).not.toHaveAttribute('title')
  await batchLink.click()
  await expect(page.getByText('local-root-7').first()).toHaveCount(0)
  await expect(page.getByText('chain-root-7').first()).toHaveCount(0)
  await expect(page.getByText('0xabc7').first()).toHaveCount(0)
  await expect(page.getByText('local-****ot-7').first()).toBeVisible()
  await expect(page.getByText('chain-****ot-7').first()).toBeVisible()
  await expect(page.getByText('******', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: /显示|隐藏|复制完整批次编号/ })).toHaveCount(0)
  await expect(page.getByText('区块 407')).toBeVisible()
  await expect(page.getByText('摘要一致')).toBeVisible()
  await expect(page.getByText('根匹配')).toBeVisible()
  await expect(page.getByText('record-1')).toBeVisible()
  await page.getByRole('button', { name: '重新核验' }).last().click()
  await expect.poll(() => verificationRequests).toBe(1)
  await expect(page.getByText('摘要一致')).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
  await page.screenshot({ path: test.info().outputPath('grid-masked-mobile.png'), fullPage: true })
})

test('pause, generate one durable round, seal it and verify the saved batch', async ({ page, request }) => {
  await page.goto('/#control')
  const overview = await request.get('/api/overview')
  expect(overview.ok()).toBeTruthy()
  const initial = await overview.json()
  if (initial.edge.data.simulation.running) await page.getByRole('button', { name: '暂停连续采集' }).click()
  const sampleOnce = page.getByRole('button', { name: '单轮生成五条' })
  await expect(sampleOnce).toBeEnabled()
  await sampleOnce.click()
  await expect(page.getByText('已持久化 5 条记录，等待窗口到期封批')).toBeVisible()

  await expect.poll(async () => {
    const response = await request.get('/api/edge/batches?page=1&pageSize=100')
    if (!response.ok()) return 0
    const result = await response.json()
    return result.items.filter((batch: Record<string, any>) => Number(batch.recordCount) === 5).length
  }, { timeout: 35000, intervals: [1000, 1500, 2000] }).toBeGreaterThan(0)

  await page.getByRole('button', { name: '数据与批次' }).click()
  await page.getByRole('radio', { name: 'VPP 接收器' }).check({ force: true })
  await page.getByRole('button', { name: '详情' }).first().click()
  await page.getByRole('tab', { name: '核验历史' }).click()
  await page.getByRole('button', { name: '重新核验' }).click()
  await expect(page.getByText('VPP 本地整批核验 / MySQL 模拟登记依据')).toBeVisible()
  await expect(page.locator('.verification-result-heading').getByText('通过')).toBeVisible()
  await expect(page.getByText('本原型未接入真实区块链。', { exact: false })).toBeVisible()
  await page.screenshot({ path: 'batch-verification-desktop.png', fullPage: true })
})
