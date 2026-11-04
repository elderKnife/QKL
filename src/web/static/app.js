/* 数据链路可视化面板 —— 纯前端，无外部依赖 */
(() => {
  "use strict";

  const state = {
    overview: null,
    batches: [],
    selected: null,
    detail: null,
    detailKey: null,
    tamper: null,
    openRecord: null,
    meta: null,
    collection: null,
    commands: [],
    busy: false,
  };

  const $ = (selector) => document.querySelector(selector);
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[char]));
  const shortHash = (hash, head = 10, tail = 6) =>
    hash ? `${String(hash).slice(0, head)}…${String(hash).slice(-tail)}` : "—";
  const fmtTime = (iso) => {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  };
  const fmtMs = (ms) => {
    if (!ms && ms !== 0) return "—";
    return new Date(Number(ms)).toLocaleString("zh-CN", { hour12: false });
  };

  const CHECK_LABELS = {
    schema: "结构 / 版本校验",
    recordCountMatches: "记录数与声明一致",
    declaredRootMatchesRecords: "声明根 = 离线重算根",
    chainRecordExists: "链上存在该批次",
    chainRootMatchesDeclared: "链上根 = 声明根",
    chainVppMatches: "链上提交方一致",
    chainRecordCountMatches: "链上记录数一致",
    proofsVerified: "逐条 Merkle 证明通过",
    chainReceipt: "交易回执有效",
  };

  const RECORD_FIELD_BRIEF = {
    recordId: "记录唯一编号 · Merkle 叶子定位键",
    deviceId: "设备编号 · 边缘校验归属",
    deviceType: "pv 光伏 / storage 储能 / charger 充电桩",
    metric: "指标名 · 固定 active_power_kw",
    value: "有功功率 kW · ≤3 位小数",
    unit: "单位 · 固定 kW",
    timestamp: "采样时间 · UTC 带毫秒",
  };

  const RECORD_FIELD_NOTES = {
    recordId: "记录唯一编号；同一批次内不可重复，是 Merkle 叶子的定位键",
    deviceId: "设备编号；边缘会校验该设备确实属于自己",
    deviceType: "pv=光伏（功率非负）/ storage=储能（正=放电，负=充电）/ charger=充电桩（非负）",
    metric: "指标名；本轮冻结为 active_power_kw（有功功率）",
    value: "有功功率（kW）；JSON number，最多 3 位小数，参与哈希",
    unit: "单位；本轮固定为 kW",
    timestamp: "设备采样时间；UTC ISO-8601，必须带毫秒与 Z",
  };

  function statusClass(status) {
    if (!status) return "warn";
    if (["VERIFIED", "COMPLETED", "FORWARDED", "CONFIRMED", "DELIVERED", "PASS", "APPLIED"].includes(status)) return "ok";
    if (["REJECTED", "FAILED", "FORWARD_FAILED", "DELIVERY_FAILED", "SIGNATURE_INVALID", "UNREACHABLE"].includes(status)) return "bad";
    return "warn";
  }

  function toast(message, kind = "ok") {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show ${kind}`;
    clearTimeout(node._timer);
    node._timer = setTimeout(() => { node.className = "toast"; }, 3600);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = text; }
    if (!response.ok) {
      const message = body && body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${response.status}`;
      throw new Error(message);
    }
    return body;
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${label} 已复制`);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast(`${label} 已复制`);
    }
  }

  window.copyField = (encoded, label) => copyText(decodeURIComponent(encoded), label);

  /* ---------------- overview ---------------- */

  function renderChainPill(chain) {
    const pill = $("#chain-pill");
    const text = $("#chain-pill-text");
    if (!chain || !chain.online) {
      pill.className = "chain-pill offline";
      text.textContent = "链不可用";
      return;
    }
    pill.className = "chain-pill online";
    text.textContent = `FISCO ${chain.sealerCount}/${chain.peerCount} 节点 · 区块 ${chain.blockNumber}`;
  }

  function renderStats() {
    const overview = state.overview;
    if (!overview) return;
    const chain = overview.chain || {};
    const counts = overview.counts || {};
    const grid = counts.gridStatuses || {};
    const services = overview.services || {};
    const alive = Object.values(services).filter((item) => item && item.ok).length;
    const total = Object.keys(services).length;

    const running = counts.collectionRunning ?? 0;
    const edgeCount = counts.edges ?? 0;
    const cards = [
      { label: "批次", value: counts.batches ?? 0, sub: `记录 ${counts.records ?? 0} 条` },
      { label: "国网核验通过", value: grid.VERIFIED ?? 0, sub: `拒绝 ${grid.REJECTED ?? 0} 批` },
      { label: "链上区块", value: chain.blockNumber ?? "—", sub: `sealers ${chain.sealerCount ?? 0} · peers ${chain.peerCount ?? 0}` },
      { label: "边缘 / 设备", value: `${edgeCount} / ${counts.devices ?? 0}`, sub: `每边缘 10 台` },
      { label: "自动采集", value: `${running}/${edgeCount}`, sub: "每 10 秒一轮" },
      { label: "业务服务", value: `${alive}/${total}`, sub: alive === total ? "全部在线" : "有服务离线" },
      { label: "最近运行", value: (overview.runId || "—").replace("RUN-AUTO-", "AUTO-").replace("RUN-MINI-001-", ""), sub: overview.runId || "" },
    ];

    $("#stats").innerHTML = cards.map((card) => `
      <div class="stat">
        <div class="stat-label">${esc(card.label)}</div>
        <div class="stat-value">${esc(card.value)}</div>
        <div class="stat-sub" title="${esc(card.sub)}">${esc(card.sub)}</div>
      </div>
    `).join("");
    renderChainPill(chain);
  }

  /* ---------------- batch list ---------------- */

  function renderBatchList() {
    const list = $("#batch-list");
    $("#batch-count").textContent = `${state.batches.length} 个`;
    if (!state.batches.length) {
      list.innerHTML = `<div class="side-empty">还没有批次<br>点击右上角「发一轮数据」</div>`;
      return;
    }
    list.innerHTML = state.batches.map((row) => {
      const selected = row.batchId === state.selected;
      const grid = row.gridStatus || "处理中";
      return `
        <div class="batch-item ${selected ? "selected" : ""}" data-batch="${esc(row.batchId)}">
          <div class="batch-top">
            <span class="batch-id" title="${esc(row.packetId)}">${esc(row.packetId)}</span>
            <span class="badge ${statusClass(row.gridStatus)}">${esc(grid)}</span>
          </div>
          <div class="batch-meta">
            <span>${esc(row.edgeId)} → ${esc(row.vppId)}</span>
            <span class="sep">·</span>
            <span>${row.recordCount ?? 0} 条</span>
            <span class="sep">·</span>
            <span>区块 ${row.blockNumber ?? "—"}</span>
          </div>
        </div>`;
    }).join("");

    list.querySelectorAll(".batch-item").forEach((node) => {
      node.addEventListener("click", () => selectBatch(node.dataset.batch));
    });
  }

  async function loadBatches({ keepDetail = true } = {}) {
    try {
      const payload = await api("/api/batches");
      state.batches = payload.batches || [];
      if (state.selected && !state.batches.some((row) => row.batchId === state.selected)) {
        state.selected = null;
        state.detail = null;
      }
      if (!state.selected && state.batches.length) {
        state.selected = state.batches[0].batchId;
      }
      renderBatchList();
      if (state.selected && keepDetail) {
        const row = state.batches.find((item) => item.batchId === state.selected);
        const key = row ? `${row.status}|${row.gridStatus}|${row.blockNumber}` : null;
        if (key !== state.detailKey) await loadDetail(state.selected, { silent: true });
      }
      if (!state.selected) renderEmpty();
    } catch (error) {
      toast(`批次列表加载失败：${error.message}`, "bad");
    }
  }

  async function loadOverview() {
    try {
      state.overview = await api("/api/overview");
      renderStats();
    } catch (error) {
      const pill = $("#chain-pill");
      pill.className = "chain-pill offline";
      $("#chain-pill-text").textContent = "面板后端不可用";
    }
  }

  /* ---------------- detail ---------------- */

  function renderEmpty() {
    if (state.selected) return;
    $("#detail").innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">◈</div>
        <h3>选择一个批次查看完整链路</h3>
        <p>左侧列出 VPP 收到的每个边缘包；点开后可以看到记录内容、签名、Merkle 根、
           链上交易与国网核验结果。</p>
        <button class="btn primary" id="btn-feed-empty">发一轮数据试试</button>
      </div>`;
    const empty = $("#btn-feed-empty");
    if (empty) empty.addEventListener("click", feedRound);
  }

  async function selectBatch(batchId) {
    state.selected = batchId;
    renderBatchList();
    await loadDetail(batchId);
  }

  async function loadDetail(batchId, { silent = false } = {}) {
    if (!silent) {
      $("#detail").innerHTML = `<div class="empty-state"><div class="empty-icon">◐</div><h3>加载链路数据…</h3></div>`;
    }
    try {
      state.detail = await api(`/api/batches/${encodeURIComponent(batchId)}`);
      const row = state.batches.find((item) => item.batchId === batchId);
      state.detailKey = row ? `${row.status}|${row.gridStatus}|${row.blockNumber}` : null;
      renderDetail();
    } catch (error) {
      $("#detail").innerHTML = `<div class="empty-state"><h3>加载失败</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  function valueCell(value) {
    const number = Number(value);
    const magnitude = Math.min(Math.abs(number) / 50, 1) * 50;
    const width = Math.max(magnitude, 0.8);
    const cls = number < 0 ? "neg" : "pos";
    const style = number < 0 ? `right:50%;width:${width}%` : `left:50%;width:${width}%`;
    return `<div class="value-cell">
        <span class="value-num">${Number.isFinite(number) ? number.toFixed(3) : esc(value)}</span>
        <span class="value-bar"><i class="${cls}" style="${style}"></i></span>
      </div>`;
  }

  function renderRecords(records) {
    if (!records || !records.length) return `<div class="muted small">无记录</div>`;
    return `
      <table>
        <thead><tr>
          <th>记录编号</th><th>设备</th><th>类型</th><th>功率 (kW)</th><th>采样时间</th>
        </tr></thead>
        <tbody>
          ${records.map((record) => `
            <tr class="record-row" data-record="${esc(record.recordId)}" tabindex="0" role="button"
                title="点击查看该条记录的 JSON">
              <td class="hash">${esc(record.recordId)}</td>
              <td class="hash">${esc(record.deviceId)}</td>
              <td><span class="tag ${esc(record.deviceType)}">${esc(record.deviceType)}</span></td>
              <td>${valueCell(record.value)}</td>
              <td class="muted small">${fmtTime(record.timestamp)}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  }

  function renderSignaturePanel(edge) {
    const signature = edge.signature || {};
    const check = edge.signatureCheck || {};
    const checkBadge = check.ok
      ? `<span class="badge ok">✓ 面板独立验签通过</span>`
      : `<span class="badge bad">✗ 验签失败：${esc(check.reason || "unknown")}</span>`;
    return `
      <div class="panel">
        <h4>包签名 · ${esc(signature.ruleVersion || "BUSINESS-SIG-V1")}</h4>
        <dl class="kv">
          <dt>算法</dt><dd>${esc(signature.algorithm || "—")}</dd>
          <dt>keyId</dt><dd><span class="hash">${esc(signature.keyId || "—")}</span>
            <span class="muted small">（须等于预登记 ${esc(check.registeredKeyId || "—")}）</span></dd>
          <dt>包摘要</dt><dd><span class="hash">${esc(check.digest || "—")}</span></dd>
          <dt>验签</dt><dd>${checkBadge}</dd>
        </dl>
        <h4 style="margin-top:12px">签名值 r‖s（128 hex）</h4>
        <div class="code-block">${esc(signature.value || "—")}</div>
        <button class="btn ghost" style="margin-top:8px"
          onclick="copyField('${encodeURIComponent(signature.value || "")}','签名值')">复制签名</button>
      </div>`;
  }

  function renderDeliveryPanel(edge) {
    const delivery = edge.delivery || {};
    const http = delivery.httpStatus;
    const okHttp = http && http < 300;
    return `
      <div class="panel">
        <h4>投递结果 · 边缘 → VPP</h4>
        <dl class="kv">
          <dt>HTTP</dt><dd>
            <span class="badge ${okHttp ? "ok" : "bad"}">${http ?? "—"}</span>
            <span class="muted small">POST /api/v1/edge-packets</span>
          </dd>
          <dt>边缘状态</dt><dd><span class="badge ${statusClass(edge.status)}">${esc(edge.status || "—")}</span></dd>
          <dt>投递时间</dt><dd>${fmtTime(delivery.at)}</dd>
          <dt>VPP 返回</dt><dd class="hash small">
            ${esc((delivery.response && (delivery.response.batchId || delivery.response.packetId)) || "—")}
          </dd>
        </dl>
        <div class="stage-sub" style="margin:10px 0 0">
          这一段是普通 HTTP 数据通信：边缘进程没有链客户端，也不持有链账户。
        </div>
      </div>`;
  }

  function stageEdge(detail) {
    const edge = detail.edge || {};
    const packet = edge.packet || {};
    const records = packet.records || detail.vpp.records || [];
    return `
      <article class="stage stage-edge">
        <div class="stage-head">
          <div>
            <div class="stage-title">
              <span class="step">1</span>
              边缘节点 <b>${esc(detail.edgeId)}</b> → 虚拟电网 <b>${esc(detail.vppId)}</b>
            </div>
            <div class="stage-sub">HTTP + ECDSA 签名 · 此段不使用区块链</div>
          </div>
          <span class="badge info">${records.length} 条记录</span>
        </div>
        <div class="chips">
          <span class="chip">packetId <b>${esc(packet.packetId)}</b></span>
          <span class="chip">collectedAt <b>${fmtTime(packet.collectedAt)}</b></span>
          <span class="chip">schema <b>${esc(packet.schemaVersion || "—")}</b></span>
        </div>
        <div class="panel" style="margin-bottom:12px">
          <h4>设备记录内容（原始数据）</h4>
          ${renderRecords(records)}
        </div>
        <div class="split">
          ${renderSignaturePanel(edge)}
          ${renderDeliveryPanel(edge)}
        </div>
      </article>`;
  }

  function stageVpp(detail) {
    const vpp = detail.vpp;
    const merkle = detail.merkle || {};
    const levels = merkle.levels || [];
    const proofs = vpp.proofs || [];
    const treeRows = levels.map((level, index) => {
      const isRoot = index === levels.length - 1;
      return `
        <div class="merkle-row">
          <div class="merkle-label">${isRoot ? "根" : `L${index} 叶子`}</div>
          <div class="merkle-nodes">
            ${level.slice(0, 16).map((hash) =>
              `<span class="merkle-node ${isRoot ? "root" : ""}">${esc(hash.slice(0, 8))}…${esc(hash.slice(-4))}</span>`
            ).join("")}
            ${level.length > 16 ? `<span class="merkle-node">+${level.length - 16}</span>` : ""}
          </div>
        </div>`;
    }).join("");

    const proofRows = proofs.map((proof) => {
      const path = (proof.siblings || []).map((item) => item.position === "left" ? "L" : "R").join("");
      return `<tr>
          <td class="hash">${esc(proof.recordId)}</td>
          <td>${proof.index} / ${proof.treeSize}</td>
          <td class="hash">${esc(path || "∅")}</td>
        </tr>`;
    }).join("");

    return `
      <article class="stage stage-vpp">
        <div class="stage-head">
          <div>
            <div class="stage-title">
              <span class="step">2</span>
              虚拟电网 <b>${esc(detail.vppId)}</b>：验签 → 构树 → 根哈希上链
            </div>
            <div class="stage-sub">记录明细留在链下；只有 32 字节根进入合约</div>
          </div>
          <span class="badge ${statusClass(vpp.status)}">${esc(vpp.status || "—")}</span>
        </div>
        <div class="chips">
          <span class="chip">batchId <b>${esc(detail.batchId)}</b></span>
          <span class="chip">记录数 <b>${vpp.recordCount ?? 0}</b></span>
          <span class="chip">Merkle 根 <b>${esc(shortHash(vpp.rootHash, 16, 8))}</b></span>
          <span class="chip">树层数 <b>${levels.length}</b></span>
          <span class="chip">证明 <b>${proofs.length}</b></span>
        </div>

        <div class="panel" style="margin-bottom:12px">
          <h4>Merkle 树（MERKLE-V1）</h4>
          <div class="merkle">${treeRows || '<div class="muted small">暂无</div>'}</div>
        </div>

        <div class="split">
          <div class="panel">
            <h4>上链交易</h4>
            <dl class="kv">
              <dt>txHash</dt><dd><span class="hash">${esc(vpp.txHash || "—")}</span>
                ${vpp.txHash ? `<button class="copy" onclick="copyField('${encodeURIComponent(vpp.txHash)}','交易哈希')">复制</button>` : ""}</dd>
              <dt>区块高度</dt><dd>${vpp.blockNumber ?? "—"}</dd>
              <dt>VPP 状态</dt><dd><span class="badge ${statusClass(vpp.status)}">${esc(vpp.status || "—")}</span></dd>
              <dt>失败原因</dt><dd class="muted small">${esc(vpp.reason || "无")}</dd>
            </dl>
          </div>
          <div class="panel">
            <h4>逐条证明</h4>
            <table>
              <thead><tr><th>记录</th><th>索引/树大小</th><th>路径</th></tr></thead>
              <tbody>${proofRows || '<tr><td colspan="3" class="muted">无</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </article>`;
  }

  function stageChain(detail) {
    const chain = detail.chain || {};
    const record = chain.record || {};
    const receipt = chain.receipt || {};
    const tx = chain.tx || {};
    const input = tx.input || "";
    const blockNumber = receipt.blockNumber ? parseInt(receipt.blockNumber, 16) : null;
    const status = receipt.status !== undefined ? parseInt(receipt.status, 16) : null;
    const statusOk = status === 0;
    return `
      <article class="stage stage-chain">
        <div class="stage-head">
          <div>
            <div class="stage-title">
              <span class="step">3</span>
              FISCO BCOS 链上记录（只保存根哈希）
            </div>
            <div class="stage-sub">合约 ${esc(chain.contractAddress || "—")} · 查询节点 ${esc(chain.rpcUrl || "—")}</div>
          </div>
          <span class="badge ${record.exists ? "ok" : "bad"}">${record.exists ? "已锚定" : "未找到"}</span>
        </div>
        <div class="split">
          <div class="panel">
            <h4>合约状态 getBatch(batchId)</h4>
            <dl class="kv">
              <dt>batchId</dt><dd class="hash">${esc(detail.batchId)}</dd>
              <dt>rootHash</dt><dd class="hash">${esc(record.rootHash || "—")}</dd>
              <dt>recordCount</dt><dd>${record.recordCount ?? "—"}</dd>
              <dt>vppId</dt><dd>${esc(record.vppId || "—")}</dd>
              <dt>schemaVersion</dt><dd class="hash small">${esc(record.schemaVersion || "—")}</dd>
              <dt>submittedAt</dt><dd>${fmtMs(record.submittedAt)}</dd>
            </dl>
            <div class="stage-sub" style="margin:10px 0 0">
              链上没有设备编号、功率数值和时间序列，只有根与定位元数据。
            </div>
          </div>
          <div class="panel">
            <h4>交易回执</h4>
            <dl class="kv">
              <dt>status</dt><dd><span class="badge ${statusOk ? "ok" : "bad"}">${receipt.status ?? "—"} ${statusOk ? "成功" : ""}</span></dd>
              <dt>from</dt><dd class="hash">${esc(receipt.from || "—")}</dd>
              <dt>to</dt><dd class="hash">${esc(receipt.to || "—")}</dd>
              <dt>block</dt><dd>${blockNumber ?? "—"}</dd>
              <dt>gasUsed</dt><dd>${receipt.gasUsed ?? "—"}</dd>
            </dl>
          </div>
        </div>
        <details>
          <summary>交易 calldata（ABI 编码，根哈希在这里可见）</summary>
          <div class="code-block" style="margin-top:8px">${esc(input.slice(0, 320))}${input.length > 320 ? "…" : ""}</div>
        </details>
      </article>`;
  }

  function stageGrid(detail) {
    const grid = detail.grid || {};
    const row = grid.detail || {};
    const checks = row.checks || {};
    const failures = row.failures || [];
    const verdict = row.status || "PENDING";
    const ok = verdict === "VERIFIED";
    const rejected = verdict === "REJECTED";
    const verdictClass = ok ? "ok" : rejected ? "bad" : "warn";
    const verdictText = ok
      ? "独立核验通过：链上根与离线原文一致，逐条证明全部有效"
      : rejected
        ? "拒绝：链下数据与链上根不一致（详见失败清单）"
        : "等待国网平台核验…";
    const chainRoot = row.chainRoot;
    const checkItems = Object.entries(checks).map(([key, value]) => {
      const pass = value === true || value === "PASS";
      const fail = value === false || value === "FAIL";
      return `<div class="check ${pass ? "pass" : fail ? "fail" : ""}">
          <span class="ico">${pass ? "✓" : fail ? "✗" : "•"}</span>
          <span>${esc(CHECK_LABELS[key] || key)}</span>
        </div>`;
    }).join("");

    const rootRows = [
      ["VPP 声明根", row.declaredRoot],
      ["GRID 重算根", row.recomputedRoot],
      ["链上读取根", chainRoot],
    ].map(([label, root]) => {
      const match = root && chainRoot && root === chainRoot;
      return `<div class="root-row ${match ? "match" : "mismatch"}">
          <span class="root-label">${label}</span>
          <span class="hash">${esc(root || "—")}</span>
          <span class="root-mark ${match ? "ok" : "bad"}">${match ? "✓" : "✗"}</span>
        </div>`;
    }).join("");

    return `
      <article class="stage stage-grid">
        <div class="stage-head">
          <div>
            <div class="stage-title">
              <span class="step">4</span>
              国网平台 <b>grid</b>：独立读链 + 重算 + 逐条校验
            </div>
            <div class="stage-sub">用 node3 自己的链客户端读取，不信任 VPP 的结论</div>
          </div>
          <span class="badge ${ok ? "ok" : "bad"}">${esc(verdict)}</span>
        </div>
        <div class="split">
          <div class="panel">
            <h4>三方根哈希比对</h4>
            <div class="roots">${rootRows}</div>
          </div>
          <div class="panel">
            <h4>核验项</h4>
            <div class="checks">${checkItems || '<span class="muted small">暂无</span>'}</div>
          </div>
        </div>
        ${failures.length ? `
          <div class="failures">
            ${failures.map((item) => `
              <div class="failure">
                <span class="rid">${esc(item.recordId || "批次级")}</span>
                <span>${esc(item.reason)}</span>
              </div>`).join("")}
          </div>` : ""}
        <div class="verdict ${verdictClass}">
          <span class="big">${ok ? "✓" : rejected ? "✗" : "…"}</span>
          <span>${verdictText}</span>
        </div>
      </article>`;
  }

  function renderTamperPanel(tamper) {
    if (!tamper) return "";
    const original = tamper.original || {};
    const tampered = tamper.tampered || {};
    const failures = tampered.failures || [];
    return `
      <section class="tamper-panel">
        <div class="tamper-head">
          <div class="tamper-title">⚠ 篡改实验 · ${esc(tamper.batchId)}</div>
          <button class="btn ghost" id="btn-close-tamper">关闭</button>
        </div>
        <div class="tamper-flow">
          <span class="muted small">把记录 <span class="hash">${esc(tamper.recordId)}</span> 的 value 改掉：</span>
          <span class="tamper-val before">${esc(tamper.before)}</span>
          <span class="tamper-arrow">→</span>
          <span class="tamper-val after">${esc(tamper.after)}</span>
        </div>
        <div class="tamper-grid">
          <div class="panel">
            <h4>原始批次</h4>
            <div class="chips">
              <span class="badge ok">${esc(original.status)}</span>
            </div>
            <dl class="kv">
              <dt>重算根</dt><dd class="hash">${esc(original.recomputedRootHash || "—")}</dd>
              <dt>链上根</dt><dd class="hash">${esc(original.chainRootHash || "—")}</dd>
            </dl>
          </div>
          <div class="panel">
            <h4>篡改后</h4>
            <div class="chips">
              <span class="badge bad">${esc(tampered.status)}</span>
            </div>
            <dl class="kv">
              <dt>重算根</dt><dd class="hash">${esc(tampered.recomputedRootHash || "—")}</dd>
              <dt>链上根</dt><dd class="hash">${esc(tampered.chainRootHash || "—")}</dd>
            </dl>
            ${failures.map((item) => `
              <div class="failure" style="margin-top:8px">
                <span class="rid">${esc(item.recordId || "批次级")}</span>
                <span>${esc(item.reason)}</span>
              </div>`).join("")}
          </div>
        </div>
      </section>`;
  }

  function renderDetail() {
    const detail = state.detail;
    if (!detail) return;
    const gridStatus = (detail.grid.detail || {}).status || "PENDING";
    const block = detail.vpp.blockNumber ?? "—";
    const tamper = state.tamper && state.tamper.batchId === detail.batchId ? renderTamperPanel(state.tamper) : "";
    const html = `
      <div class="detail-head">
        <div>
          <h2>${esc(detail.batchId)}</h2>
          <div class="detail-sub">
            <span>${esc(detail.edgeId)} → ${esc(detail.vppId)}</span>
            <span>· 区块 ${esc(block)}</span>
            <span>· ${fmtTime(detail.vpp.envelope && detail.vpp.envelope.builtAt)}</span>
            <span>· 查询节点 ${esc(detail.chain.rpcUrl)}</span>
          </div>
        </div>
        <div class="detail-actions">
          <span class="badge ${statusClass(gridStatus)}">${esc(gridStatus)}</span>
        </div>
      </div>
      ${tamper}
      <div class="pipeline">
        ${stageEdge(detail)}
        <div class="connector"><span class="arrow">▼</span><span>HTTP + ECDSA 签名（无区块链）</span></div>
        ${stageVpp(detail)}
        <div class="connector"><span class="arrow">▼</span><span>registerBatch(batchId, rootHash, …) 上链</span></div>
        ${stageChain(detail)}
        <div class="connector"><span class="arrow">▼</span><span>getBatch(batchId) 独立查询</span></div>
        ${stageGrid(detail)}
      </div>`;

    $("#detail").innerHTML = html;
    bindRecordRows();
    if (state.openRecord) renderRecordModal();
    const close = $("#btn-close-tamper");
    if (close) close.addEventListener("click", () => { state.tamper = null; renderDetail(); });
    const empty = $("#btn-feed-empty");
    if (empty) empty.addEventListener("click", feedRound);
  }


  /* ---------------- record json modal ---------------- */

  function bindRecordRows() {
    document.querySelectorAll("#detail tr.record-row").forEach((row) => {
      row.addEventListener("click", () => openRecordModal(row.dataset.record));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openRecordModal(row.dataset.record);
        }
      });
    });
  }

  function currentRecord() {
    const detail = state.detail;
    if (!detail || !state.openRecord) return null;
    const records = (detail.edge.packet && detail.edge.packet.records) || detail.vpp.records || [];
    const record = records.find((item) => item.recordId === state.openRecord);
    if (!record) return null;
    const meta = (detail.recordMeta || {})[state.openRecord] || {};
    const proof = (detail.vpp.proofs || []).find((item) => item.recordId === state.openRecord);
    const path = proof
      ? (proof.siblings || []).map((item) => (item.position === "left" ? "L" : "R")).join("") || "∅"
      : "—";
    return { record, meta, proof, path };
  }

  function recordJsonText(record, withComments) {
    const keys = Object.keys(record);
    const lines = ["{"];
    keys.forEach((key, index) => {
      const isLast = index === keys.length - 1;
      const note = withComments && RECORD_FIELD_BRIEF[key] ? `  // ${RECORD_FIELD_BRIEF[key]}` : "";
      lines.push(`  ${JSON.stringify(key)}: ${JSON.stringify(record[key])}${isLast ? "" : ","}${note}`);
    });
    lines.push("}");
    return lines.join("\n") + "\n";
  }

  function recordCodeHtml(record, withComments) {
    const keys = Object.keys(record);
    const lines = ['<span class="p">{</span>'];
    keys.forEach((key, index) => {
      const value = record[key];
      const isLast = index === keys.length - 1;
      let valueHtml;
      if (typeof value === "string") valueHtml = `<span class="s">"${esc(value)}"</span>`;
      else if (typeof value === "number") valueHtml = `<span class="n">${value}</span>`;
      else valueHtml = `<span class="s">${esc(JSON.stringify(value))}</span>`;
      const note = withComments && RECORD_FIELD_BRIEF[key]
        ? `  <span class="c">// ${esc(RECORD_FIELD_BRIEF[key])}</span>`
        : "";
      lines.push(`  <span class="k">"${esc(key)}"</span>: ${valueHtml}${isLast ? "" : ","}${note}`);
    });
    lines.push('<span class="p">}</span>');
    return lines.join("\n");
  }

  function renderRecordCode() {
    const current = currentRecord();
    if (!current) return;
    const withComments = $("#record-comments").checked;
    $("#record-code").innerHTML = recordCodeHtml(current.record, withComments);
  }

  function openRecordModal(recordId) {
    state.openRecord = recordId;
    renderRecordModal();
    $("#record-modal").hidden = false;
    document.body.classList.add("modal-open");
  }

  function closeRecordModal() {
    state.openRecord = null;
    $("#record-modal").hidden = true;
    document.body.classList.remove("modal-open");
  }

  function renderRecordModal() {
    const current = currentRecord();
    if (!current) { closeRecordModal(); return; }
    const { record, meta, proof, path } = current;
    const detail = state.detail;

    $("#record-modal-title").textContent = record.recordId;
    $("#record-modal-sub").textContent =
      `${detail.edgeId} · ${detail.packetId} · ${detail.batchId}`;

    const fields = Object.keys(record).map((key) => `
      <div class="record-field">
        <span class="name">${esc(key)}</span>
        <span class="value">${esc(typeof record[key] === "string" ? record[key] : JSON.stringify(record[key]))}</span>
        <div class="note">${esc(RECORD_FIELD_NOTES[key] || "（无注释）")}</div>
      </div>`).join("");
    $("#record-fields").innerHTML = fields;

    const treeSize = proof ? proof.treeSize : (detail.merkle && detail.merkle.size) || "—";
    const indexText = meta.index !== undefined ? `${meta.index + 1} / ${treeSize}` : "—";
    $("#record-meta").innerHTML = `
      <div class="row"><span class="label">规范化字节</span>
        <span class="canonical">${esc(meta.canonical || "—")}</span></div>
      <div class="row"><span class="label">叶子哈希</span>
        <span class="hash">${esc(meta.leafHash || "—")}</span></div>
      <div class="row"><span class="label">树位置</span><span>${esc(indexText)}</span></div>
      <div class="row"><span class="label">证明路径</span><span class="hash">${esc(path)}</span></div>`;

    renderRecordCode();
  }

  function downloadRecord(withComments) {
    const current = currentRecord();
    if (!current) return;
    const text = recordJsonText(current.record, withComments);
    const suffix = withComments ? "jsonc" : "json";
    const blob = new Blob([text], { type: withComments ? "text/plain;charset=utf-8" : "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${current.record.recordId}.${suffix}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(`已下载 ${link.download}`);
  }


  /* ---------------- automation + commands ---------------- */

  const ACTION_LABELS = { set_voltage: "设置电压", power_on: "开机", power_off: "关机" };

  function renderCollection() {
    const data = state.collection;
    if (!data) return;
    const chips = (data.edges || []).map((edge) => `
      <span class="collection-chip ${edge.running ? "on" : ""}" title="${esc(edge.edgeId)} → ${esc(edge.vppId)}">
        <i class="dot"></i>
        <span>${esc(edge.edgeId)} · ${esc(edge.vppId)}</span>
        <span class="muted">${edge.running ? `运行中 · ${edge.rounds} 轮` : "已暂停"}</span>
      </span>`).join("");
    $("#collection-chips").innerHTML = chips || '<span class="muted small">无边缘</span>';
    const allRunning = data.edges && data.edges.length && data.edges.every((edge) => edge.running);
    $("#btn-collect-start").disabled = !!allRunning;
  }

  async function loadCollection() {
    try {
      state.collection = await api("/api/collection");
      renderCollection();
    } catch (error) {
      $("#collection-chips").innerHTML = `<span class="muted small">采集状态不可用：${esc(error.message)}</span>`;
    }
  }

  async function collectionAction(action) {
    const button = action === "start" ? $("#btn-collect-start") : $("#btn-collect-pause");
    await withBusy(button, async () => {
      const result = await api(`/api/collection/${action}`, { method: "POST", body: "{}" });
      const ok = result.edges.filter((edge) => edge.httpStatus === 200).length;
      toast(`${action === "start" ? "已开始" : "已暂停"} ${ok}/${result.edges.length} 个边缘的采集`);
      await loadCollection();
      await loadOverview();
    });
  }

  async function loadMeta() {
    try {
      state.meta = await api("/api/meta");
      const edgeSelect = $("#cmd-edge");
      edgeSelect.innerHTML = state.meta.edges.map(
        (edge) => `<option value="${esc(edge.edgeId)}">${esc(edge.edgeId)} → ${esc(edge.vppId)}</option>`
      ).join("");
      renderDeviceOptions();
    } catch (error) {
      toast(`指令元数据加载失败：${error.message}`, "bad");
    }
  }

  function renderDeviceOptions() {
    if (!state.meta) return;
    const edgeId = $("#cmd-edge").value;
    const edge = state.meta.edges.find((item) => item.edgeId === edgeId);
    $("#cmd-device").innerHTML = (edge ? edge.devices : []).map(
      (device) => `<option value="${esc(device.deviceId)}">${esc(device.deviceId)} · ${esc(device.deviceType)}</option>`
    ).join("");
  }

  function syncCommandForm() {
    const action = $("#cmd-action").value;
    $("#cmd-voltage-wrap").style.display = action === "set_voltage" ? "" : "none";
  }

  async function loadCommands() {
    try {
      const payload = await api("/api/commands");
      state.commands = payload.commands || [];
      $("#command-count").textContent = `${state.commands.length} 条`;
      const rows = state.commands.map((command) => {
        const param = command.action === "set_voltage" && command.params && command.params.voltageV !== undefined
          ? `${command.params.voltageV} V` : "";
        return `
          <tr data-command="${esc(command.commandId)}" title="${esc(command.commandId)}">
            <td class="muted small">${fmtTime(command.updatedAt)}</td>
            <td class="hash">${esc(shortHash(command.commandId, 10, 4))}</td>
            <td>${esc(command.targetEdgeId)}<br><span class="muted small">${esc(command.targetDeviceId)}</span></td>
            <td>${esc(ACTION_LABELS[command.action] || command.action)}
              ${param ? `<div class="muted small">${esc(param)}</div>` : ""}</td>
            <td><span class="badge ${statusClass(command.status)}">${esc(command.status)}</span></td>
            <td>${command.blockNumber ?? "—"}<br><span class="hash short">${command.txHash ? esc(shortHash(command.txHash, 8, 4)) : ""}</span></td>
            <td>${command.ackStatus ? `<span class="badge ${statusClass(command.ackStatus)}">${esc(command.ackStatus)}</span>` : "—"}
              <div class="muted small">${command.ackBlockNumber ?? ""}</div></td>
          </tr>`;
      }).join("");
      $("#command-list").innerHTML = rows || '<tr><td colspan="7" class="muted">暂无指令</td></tr>';
    } catch (error) {
      $("#command-list").innerHTML = `<tr><td colspan="7" class="muted">指令列表不可用：${esc(error.message)}</td></tr>`;
    }
  }

  async function sendCommand() {
    const action = $("#cmd-action").value;
    const body = {
      targetEdgeId: $("#cmd-edge").value,
      targetDeviceId: $("#cmd-device").value,
      action,
      params: {},
    };
    if (action === "set_voltage") {
      const voltage = Number($("#cmd-voltage").value);
      if (!Number.isFinite(voltage) || voltage < 180 || voltage > 260) {
        toast("电压必须在 180–260 V 之间", "bad");
        return;
      }
      body.params = { voltageV: Number(voltage.toFixed(1)) };
    }
    await withBusy($("#btn-send-command"), async () => {
      const command = await api("/api/commands", { method: "POST", body: JSON.stringify(body) });
      const ack = command.ack || {};
      toast(`指令 ${command.status} · 回执 ${command.ackStatus || "待处理"} · 设备状态 ${JSON.stringify(ack.appliedState || {})}`);
      await loadCommands();
      await loadOverview();
    });
  }

  /* ---------------- actions ---------------- */


  async function withBusy(button, task) {
    if (state.busy) return;
    state.busy = true;
    const original = button ? button.textContent : "";
    if (button) { button.disabled = true; button.textContent = "处理中…"; }
    try {
      await task();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      state.busy = false;
      if (button) { button.disabled = false; button.textContent = original; }
    }
  }

  async function feedRound() {
    await withBusy($("#btn-feed"), async () => {
      const result = await api("/api/feed", { method: "POST", body: JSON.stringify({ rounds: 1 }) });
      const packets = result.edges.reduce((sum, item) => sum + item.packets.length, 0);
      toast(`已注入 ${result.edges.length} 个边缘、生成 ${packets} 个包（${result.runId}）`);
      await new Promise((resolve) => setTimeout(resolve, 1200));
      await loadBatches({ keepDetail: false });
      if (state.batches.length) {
        state.selected = state.batches[0].batchId;
        renderBatchList();
        await loadDetail(state.selected);
      }
      await loadOverview();
    });
  }

  async function runTamper() {
    await withBusy($("#btn-tamper"), async () => {
      toast("正在选一个已核验批次并篡改…");
      const result = await api("/api/tamper", { method: "POST", body: "{}" });
      state.tamper = result;
      if (state.selected !== result.batchId) {
        state.selected = result.batchId;
        renderBatchList();
        await loadDetail(result.batchId);
      } else {
        await loadDetail(result.batchId, { silent: true });
      }
      toast(`篡改被拦截：${result.recordId} → ${result.tampered.status}`, "bad");
    });
  }

  /* ---------------- boot ---------------- */

  async function boot() {
    $("#btn-feed").addEventListener("click", feedRound);
    $("#record-modal-close").addEventListener("click", closeRecordModal);
    $("#record-modal").addEventListener("click", (event) => {
      if (event.target.id === "record-modal") closeRecordModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("#record-modal").hidden) closeRecordModal();
    });
    $("#record-comments").addEventListener("change", renderRecordCode);
    $("#record-copy").addEventListener("click", () => {
      const current = currentRecord();
      if (current) copyText(recordJsonText(current.record, false), "记录 JSON");
    });
    $("#record-download").addEventListener("click", () => downloadRecord(false));
    $("#record-download-annotated").addEventListener("click", () => downloadRecord(true));
    const staticEmpty = $("#btn-feed-empty");
    if (staticEmpty) staticEmpty.addEventListener("click", feedRound);
    $("#btn-collect-start").addEventListener("click", () => collectionAction("start"));
    $("#btn-collect-pause").addEventListener("click", () => collectionAction("pause"));
    $("#btn-send-command").addEventListener("click", sendCommand);
    $("#cmd-edge").addEventListener("change", renderDeviceOptions);
    $("#cmd-action").addEventListener("change", syncCommandForm);
    $("#btn-tamper").addEventListener("click", runTamper);
    $("#btn-refresh").addEventListener("click", async () => {
      await loadOverview();
      await loadBatches({ keepDetail: false });
      if (state.selected) await loadDetail(state.selected);
      toast("已刷新");
    });

    await loadOverview();
    await loadMeta();
    await loadCollection();
    await loadCommands();
    syncCommandForm();
    await loadBatches();
    if (state.selected) await loadDetail(state.selected);

    setInterval(loadOverview, 5000);
    setInterval(loadCollection, 3000);
    setInterval(loadCommands, 4000);
    setInterval(() => loadBatches({ keepDetail: true }), 5000);

    // Demo shortcuts: /?tamper=1 runs the tamper drill; /?record=first opens a record JSON.
    const params = new URLSearchParams(window.location.search);
    if (params.get("tamper") === "1" && state.batches.length) {
      await runTamper();
    }
    const recordParam = params.get("record");
    if (recordParam && state.detail) {
      const records = (state.detail.edge.packet && state.detail.edge.packet.records)
        || state.detail.vpp.records || [];
      const target = recordParam === "first"
        ? records[0]
        : records.find((item) => item.recordId === recordParam);
      if (target) openRecordModal(target.recordId);
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
