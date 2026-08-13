const REFRESH_INTERVAL_MS = 20000;

const state = {
  data: null,
  config: null,
  configDirty: false,
  filter: "",
  statusFilter: "",
  phaseFilter: "",
  selectedNodeKeys: new Set(),
  lastLoadedAt: null,
  refreshTimer: null,
  loading: false,
  configLoading: false,
  automation: null,
  automationLoading: false,
};

const $ = (id) => document.getElementById(id);

const CONFIG_FIELDS = {
  basic: [
    { path: "lab_name", label: "实验名称", type: "text", help: "用于区分当前实验室或交付环境。" },
  ],
  serverCore: [
    { path: "server.hostname", label: "服务端主机名", type: "text", help: "控制台和服务端节点名称。" },
    { path: "server.role", label: "服务端角色", type: "text", help: "默认保留为 server。" },
    { path: "server.os_user", label: "系统账号", type: "text", help: "远端系统账号。" },
    { path: "server.os_password", label: "系统密码", type: "password", help: "服务端系统密码。" },
    { path: "server.external_ip", label: "外网 IP", type: "text", help: "管理平面地址。" },
    { path: "server.maas_url", label: "MAAS URL", type: "text", help: "例如 http://ip:5240/MAAS。" },
    { path: "server.offline_root", label: "离线仓库目录", type: "text", help: "统一离线资源根目录。" },
  ],
  serverAccess: [
    { path: "server.http_port", label: "离线 HTTP 端口", type: "number", help: "统一工具和离线源端口。" },
    { path: "server.admin_user", label: "MAAS 管理账号", type: "text", help: "MAAS CLI 登录用户。" },
    { path: "server.admin_password", label: "MAAS 管理密码", type: "password", help: "MAAS 管理账号密码。" },
  ],
  network: [
    { path: "server.stage1_server_ip", label: "Stage1 服务 IP", type: "text", help: "留空时按 DHCP 网关/外网 IP 推导。" },
    { path: "server.stage1_port", label: "Stage1 端口", type: "number", help: "Stage1 采集/服务端口。" },
    { path: "server.stage1_uefi_ipxe_source", label: "UEFI 引导文件", type: "text", help: "默认用 ipxe.efi；如特定机型需要，可改成 snponly.efi 或 auto。" },
    { path: "server.dhcp_interface", label: "DHCP 网卡", type: "text", help: "PXE/部署平面网卡名。" },
    { path: "server.dhcp_range", label: "DHCP 地址池", type: "text", help: "例如 10.0.0.11,10.0.0.11,12h。" },
    { path: "server.dhcp_router", label: "DHCP 网关", type: "text", help: "通常也是部署网服务端 IP。" },
    { path: "server.dhcp_dns", label: "DHCP DNS", type: "text", help: "部署平面 DNS。" },
  ],
  console: [
    { path: "console.flow_tag", label: "流程标签", type: "text", help: "正式批量流程筛选用的 tag。" },
    { path: "console.deploy_policy", label: "部署策略", type: "text", help: "为空时使用脚本默认策略。" },
    { path: "console.wipe_script_name", label: "清盘脚本名", type: "text", help: "注册到 MAAS 的 Testing Script 名称。" },
    { path: "console.default_node_id", label: "默认 node_id", type: "text", help: "留空时默认取节点 sn。" },
    { path: "console.default_client_tag", label: "默认客户端标签", type: "text", help: "导出 maas.csv 和流程筛选默认标签。" },
    { path: "console.deploy_osystem", label: "MAAS OS 类型", type: "text", help: "官方 Ubuntu 使用 ubuntu；自定义镜像通常使用 custom。" },
    { path: "console.deploy_series", label: "部署镜像标识", type: "text", help: "填写 MAAS 中显示的 series/resource name，例如 jammy 或自定义镜像标识。" },
  ],
  defaults: [
    { path: "defaults.bmc_user", label: "目标 BMC 账号", type: "text", help: "Stage1 通过本机 IPMI/KCS 创建或更新的管理账号。" },
    { path: "defaults.bmc_pass", label: "目标 BMC 密码", type: "password", help: "Stage1 写入并在新地址上验证的目标密码。" },
    { path: "defaults.bmc_prefix", label: "默认 BMC 掩码", type: "number", help: "如 24。" },
    { path: "defaults.bmc_gateway", label: "默认 BMC 网关", type: "text", help: "如 192.168.2.254。" },
    { path: "defaults.node_prefix", label: "默认节点掩码", type: "number", help: "如 24。" },
    { path: "defaults.node_gateway", label: "默认节点网关", type: "text", help: "未指定时可留空。" },
    { path: "defaults.power_driver", label: "默认电源驱动", type: "text", help: "默认先用 ipmi。" },
    { path: "defaults.power_driver_fallback", label: "兜底电源驱动", type: "text", help: "默认失败后回落到 redfish。" },
    { path: "defaults.boot_mode", label: "默认启动模式", type: "text", help: "支持 uefi / bios。" },
  ],
  raid: [
    { path: "raid.tools_base_url", label: "RAID 工具地址", type: "text", help: "留空时按服务端配置自动推导。" },
    { path: "raid.boot_vd_name", label: "启动盘卷名", type: "text", help: "创建的 boot VD/LD 名称。" },
    { path: "raid.single_disk_raid_level", label: "单盘 RAID 级别", type: "text", help: "如 r0。" },
    { path: "raid.multi_disk_raid_level", label: "多盘 RAID 级别", type: "text", help: "如 r1、r10。" },
    { path: "raid.boot_disk_count", label: "启动盘数量", type: "number", help: "多盘策略取前 N 块 SSD。" },
  ],
};

const ALL_CONFIG_FIELDS = Object.values(CONFIG_FIELDS).flat();

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function cloneValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function prettyJson(value) {
  return JSON.stringify(value, null, 2);
}

function fieldId(path) {
  return `cfg-${path.replaceAll(".", "-")}`;
}

function getConfigValue(source, path) {
  return path.split(".").reduce((acc, key) => (acc == null ? undefined : acc[key]), source);
}

function setConfigValue(target, path, value) {
  const parts = path.split(".");
  let cursor = target;
  parts.forEach((key, index) => {
    if (index === parts.length - 1) {
      cursor[key] = value;
      return;
    }
    if (!cursor[key] || typeof cursor[key] !== "object" || Array.isArray(cursor[key])) {
      cursor[key] = {};
    }
    cursor = cursor[key];
  });
}

function mutationModeLabel() {
  return state.data?.allow_mutation ? "实际执行" : "仅预演";
}

function formatTimestamp(date) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function renderLastUpdated() {
  $("lastUpdatedAt").textContent = state.lastLoadedAt
    ? `最后更新：${formatTimestamp(state.lastLoadedAt)}`
    : "最后更新：--";
}

function stateClass(value) {
  const normalized = String(value ?? "").toLowerCase();
  if (["true", "active", "ready", "ok", "done", "deployed", "stage1_ready"].includes(normalized)) return "ok";
  if (["current", "running", "testing", "deploying", "commissioning"].includes(normalized)) return "info";
  if (["false", "failed", "error", "blocked", "bad", "failed testing", "failed commissioning", "failed deployment", "broken"].includes(normalized)) return "bad";
  return "warn";
}

function phaseLabel(phase) {
  return (state.data?.pipeline || []).find((item) => item.id === phase)?.label || phase || "-";
}

function renderMetrics(stats) {
  const items = [
    ["总节点", stats.total ?? 0, "全部纳管或待纳管节点"],
    ["Stage1 就绪", stats.stage1_ready ?? 0, "已完成抓配等待导入"],
    ["异常阻塞", stats.blocked ?? stats.failed ?? 0, "需要处理的节点"],
    ["已部署", stats.deployed ?? 0, "进入交付验证阶段"],
  ];
  $("overview").innerHTML = items.map(([label, value, hint]) => (
    `<div class="metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(hint)}</small>
    </div>`
  )).join("");
}

function renderPipeline() {
  const pipeline = state.data?.pipeline || [];
  const blocked = pipeline.filter((item) => item.state === "blocked").length;
  const active = pipeline.filter((item) => item.state === "active" || item.state === "ready").length;
  $("pipelineBadge").textContent = blocked ? `${blocked} 个阶段阻塞` : active ? `${active} 个阶段推进中` : "就绪";
  $("pipelineBadge").className = `badge ${blocked ? "bad" : active ? "info" : "ok"}`;
  $("pipelineSteps").innerHTML = pipeline.map((stage, index) => {
    const count = stage.counts?.total || 0;
    const blockedCount = stage.counts?.blocked || 0;
    const activeCount = stage.counts?.active || 0;
    const reason = stage.reason || stage.description || "";
    return `<div class="pipeline-step ${stateClass(stage.state)}">
      <div class="step-index">${index + 1}</div>
      <div class="step-main">
        <div class="step-title">
          <strong>${escapeHtml(stage.label)}</strong>
          <span>${escapeHtml(stage.state)}</span>
        </div>
        <p>${escapeHtml(reason)}</p>
        <div class="step-meta">
          <span>${count} 节点</span>
          <span>${activeCount} 进行中</span>
          <span>${blockedCount} 阻塞</span>
        </div>
      </div>
    </div>`;
  }).join("");
}

function renderModeControl() {
  const control = state.data?.control || {};
  const mode = control.mode || "unknown";
  $("activeModeLabel").textContent = control.label || mode;
  $("activeModeDescription").textContent = control.description || "无法识别当前服务模式";
  const actualConflict = mode === "conflict";
  $("activeModeBadge").textContent = actualConflict ? "服务冲突" : (control.healthy === false ? "状态异常" : "分时独占");
  $("activeModeBadge").className = `badge ${control.healthy === false ? "bad" : mode === "maintenance_locked" ? "warn" : "ok"}`;
  $("modeChecks").innerHTML = (control.checks || []).map((item) => (
    `<span class="mode-check ${item.ok ? "ok" : "bad"}"><i></i>${escapeHtml(item.label)}</span>`
  )).join("");
  document.querySelectorAll("[data-mode]").forEach((button) => {
    const gate = control.mode_gates?.[button.dataset.mode] || {};
    button.classList.toggle("active", button.dataset.mode === mode);
    button.disabled = button.dataset.mode === mode || gate.allowed === false;
    button.title = button.dataset.mode === mode ? "当前模式" : (gate.reason || "");
  });
  document.querySelectorAll("[data-action]").forEach((button) => {
    const gate = control.action_gates?.[button.dataset.action];
    if (!gate) return;
    button.disabled = gate.allowed === false;
    button.title = gate.reason || "";
    button.dataset.blockedReason = gate.reason || "";
  });
  const importGate = control.action_gates?.["import-nodes"] || {};
  $("importNodesHint").textContent = importGate.allowed
    ? "Stage1 就绪节点可导入；MAAS 将使用节点规划中的 BMC 凭据。"
    : (importGate.reason || "等待 Stage1 节点就绪");
  const next = control.next_action || {};
  $("actionHint").textContent = next.label
    ? `建议下一步：${next.label}${next.reason ? ` · ${next.reason}` : ""}`
    : "当前没有可自动推进的操作，请检查阻塞项。";
}

function applyGlobalActionGates() {
  const gates = state.data?.control?.action_gates || {};
  const buttonMap = {
    "reboot-nodes": ["rebootSelectedBtn", "[data-node-reboot]"],
    "reverify-bmc-nodes": ["[data-node-verify-bmc]"],
    "recommission-nodes": ["recommissionSelectedBtn", "[data-node-recommission]"],
    "delete-nodes": ["deleteSelectedBtn", "[data-node-delete]"],
    "wipe-nodes": ["wipeSelectedBtn", "[data-node-wipe]"],
    "apply-storage-nodes": ["storageSelectedBtn", "[data-node-storage]"],
    "deploy-nodes": ["deploySelectedBtn", "[data-node-deploy]"],
  };
  Object.entries(buttonMap).forEach(([action, selectors]) => {
    const gate = gates[action];
    if (!gate || gate.allowed !== false) return;
    selectors.forEach((selector) => {
      const elements = selector.startsWith("[") ? document.querySelectorAll(selector) : [$(selector)];
      elements.forEach((button) => {
        if (!button) return;
        button.disabled = true;
        button.title = gate.reason || "";
      });
    });
  });
}

function renderTopbarMeta() {
  const lab = state.data?.lab || {};
  const server = lab.server || {};
  const nodes = state.data?.nodes || [];
  $("topbarMeta").innerHTML = [
    ["Lab", lab.lab_name || "-"],
    ["Server", server.external_ip || "-"],
    ["Nodes", nodes.length],
  ].map(([label, value]) => (
    `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

function setActivePage() {
  const hash = location.hash || "#overview";
  const page = hash === "#config" ? "config" : hash === "#automation" ? "automation" : "workbench";
  document.body.dataset.page = page;
  document.querySelectorAll("nav a").forEach((link) => {
    const samePage = link.dataset.navPage === page;
    const exact = link.getAttribute("href") === hash;
    link.classList.toggle("active", page !== "workbench" ? exact : samePage && (exact || (hash === "#overview" && link.getAttribute("href") === "#overview")));
  });
}

function renderAttention() {
  const nodes = state.data?.nodes || [];
  const items = nodes
    .filter((node) => node.phase_state === "blocked" || stateClass(node.status) === "bad")
    .slice(0, 8);
  $("attentionBadge").textContent = `${items.length} open`;
  $("attentionBadge").className = `badge ${items.length ? "bad" : "ok"}`;
  $("attentionList").innerHTML = items.length
    ? items.map((node) => {
      const reason = node.blocker || node.message || node.status_message || node.next_step || "需要检查";
      return `<button type="button" class="work-row" data-focus-node="${escapeHtml(node.node_key)}">
        <span class="status-dot ${stateClass(node.phase_state || node.status)}"></span>
        <span class="work-row-main">
          <strong>${escapeHtml(nodeTitle(node))}</strong>
          <small>${escapeHtml(node.phase_label || phaseLabel(node.phase))} | ${escapeHtml(reason)}</small>
        </span>
        <span class="work-row-trail">${escapeHtml(node.status || "-")}</span>
      </button>`;
    }).join("")
    : `<div class="empty-state compact">当前没有阻塞项</div>`;
}

function renderActivity() {
  const nodes = [...(state.data?.nodes || [])]
    .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))
    .slice(0, 8);
  $("activityList").innerHTML = nodes.length
    ? nodes.map((node) => (
      `<button type="button" class="work-row" data-focus-node="${escapeHtml(node.node_key)}">
        <span class="status-dot ${stateClass(node.status || node.phase_state)}"></span>
        <span class="work-row-main">
          <strong>${escapeHtml(nodeTitle(node))}</strong>
          <small>${escapeHtml(node.phase_label || phaseLabel(node.phase))} | ${escapeHtml(node.next_step || "等待下一步")}</small>
        </span>
        <span class="work-row-trail">${escapeHtml(node.updated_at || "-")}</span>
      </button>`
    )).join("")
    : `<div class="empty-state compact">暂无节点活动</div>`;
}

function focusNode(nodeKey) {
  const node = (state.data?.nodes || []).find((item) => item.node_key === nodeKey);
  if (!node) return;
  state.filter = nodeTitle(node);
  $("filterInput").value = state.filter;
  location.hash = "#nodes";
  renderNodes();
}

function renderStatus(id, data) {
  $(id).innerHTML = Object.entries(data || {}).map(([name, value]) => (
    `<div class="status-item">
      <span>${escapeHtml(name)}</span>
      <strong class="${stateClass(value)}">${escapeHtml(String(value))}</strong>
    </div>`
  )).join("");
}

function renderLab(lab) {
  const server = lab?.server || {};
  const clients = lab?.clients || [];
  $("labName").textContent = lab?.lab_name || "lab";
  const items = [
    ["服务端", `${server.hostname || "-"} / ${server.external_ip || "-"}`],
    ["MAAS", server.maas_url || "-"],
    ["Stage1", `${server.stage1_server_ip || server.dhcp_router || server.external_ip || "-"}:${server.stage1_port || "-"}`],
    ["PXE 网卡", server.dhcp_interface || "未配置"],
    ["DHCP 池", server.dhcp_range || "未配置"],
    ["客户端", clients.map((item) => `${item.hostname || "-"} / BMC ${item.bmc_ip || "-"}`).join(", ") || "-"],
  ];
  $("labInfo").innerHTML = items.map(([name, value]) => (
    `<div class="status-item"><span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

function renderStatusFilter() {
  const select = $("statusFilter");
  const statuses = Array.from(new Set((state.data?.nodes || []).map((node) => String(node.status || "")).filter(Boolean))).sort();
  const current = state.statusFilter;
  select.innerHTML = [`<option value="">全部状态</option>`]
    .concat(statuses.map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(status)}</option>`))
    .join("");
  select.value = statuses.includes(current) ? current : "";
  state.statusFilter = select.value;
}

function renderPhaseFilter() {
  const select = $("phaseFilter");
  const phases = Array.from(new Set((state.data?.nodes || []).map((node) => String(node.phase || "")).filter(Boolean)));
  const labels = state.data?.pipeline || [];
  const current = state.phaseFilter;
  select.innerHTML = [`<option value="">全部阶段</option>`]
    .concat(labels.filter((stage) => phases.includes(stage.id)).map((stage) => (
      `<option value="${escapeHtml(stage.id)}">${escapeHtml(stage.label)}</option>`
    )))
    .join("");
  select.value = phases.includes(current) ? current : "";
  state.phaseFilter = select.value;
}

function currentNodes() {
  const text = state.filter.toLowerCase();
  return (state.data?.nodes || []).filter((node) => {
    const textMatched = !text || JSON.stringify(node).toLowerCase().includes(text);
    const statusMatched = !state.statusFilter || String(node.status || "") === state.statusFilter;
    const phaseMatched = !state.phaseFilter || String(node.phase || "") === state.phaseFilter;
    return textMatched && statusMatched && phaseMatched;
  });
}

function selectedNodes() {
  const selected = state.selectedNodeKeys;
  return (state.data?.nodes || []).filter((node) => selected.has(node.node_key));
}

function renderSelectionSummary() {
  const count = state.selectedNodeKeys.size;
  $("selectionSummary").textContent = count ? `已选择 ${count} 个节点` : "未选择节点";
  if ($("automationSelection")) {
    const eligible = new Set(state.automation?.eligible_node_keys || []);
    const selectedEligible = Array.from(state.selectedNodeKeys).filter((key) => eligible.has(key)).length;
    $("automationSelection").textContent = selectedEligible
      ? `已选择 ${selectedEligible} 个`
      : `默认全部 ${eligible.size} 个`;
  }
}

function nodeTitle(node) {
  return node.hostname || node.sn || node.bmc_ip || node.pxe_mac || node.node_key || "未命名节点";
}

function formatNodeValue(field, value) {
  if (field === "storage_status" && value && typeof value === "object") {
    const status = value.ok === true ? "ok" : value.ok === false ? "failed" : "-";
    return `${status}${value.policy ? ` / ${value.policy}` : ""}`;
  }
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return value ?? "";
}

function renderTimeline(node) {
  const timeline = node.stage_timeline || [];
  return `<div class="mini-timeline">
    ${timeline.map((item) => `<span class="${escapeHtml(item.state)}" title="${escapeHtml(item.label)}"></span>`).join("")}
  </div>`;
}

function renderNodeCards(nodes) {
  $("nodeCards").innerHTML = nodes.map((node) => {
    const actions = node.actions || {};
    const selected = state.selectedNodeKeys.has(node.node_key);
    const blocker = node.blocker || node.message || node.status_message || "";
    const hardware = node.hardware || {};
    const capturedDisks = Array.isArray(hardware.block_devices)
      ? hardware.block_devices.filter((item) => item.type === "disk").length
      : 0;
    return `<article class="node-card ${selected ? "selected" : ""}">
      <div class="node-card-head">
        <label>
          <input class="row-check" type="checkbox" data-node-check="${escapeHtml(node.node_key)}" ${selected ? "checked" : ""}>
          <strong>${escapeHtml(nodeTitle(node))}</strong>
        </label>
        <span class="pill ${stateClass(node.phase_state)}">${escapeHtml(node.phase_label || phaseLabel(node.phase))}</span>
      </div>
      <div class="progress-line"><span style="width:${Math.max(0, Math.min(Number(node.progress || 0), 100))}%"></span></div>
      ${renderTimeline(node)}
      <div class="node-meta">
        <span>状态：<b class="${stateClass(node.status)}">${escapeHtml(node.status || "-")}</b></span>
        <span>SN：${escapeHtml(node.sn || "-")}</span>
        <span>PXE：${escapeHtml(node.pxe_mac || "-")}</span>
        <span>BMC：${escapeHtml(node.bmc_ip || "-")}</span>
        <span>本机回读：<b class="${node.bmc_readback_ok ? "ok" : "warn"}">${node.bmc_readback_ok ? "通过" : "待完成"}</b></span>
        <span>远程 IPMI：<b class="${node.bmc_auth_ok ? "ok" : "warn"}">${node.bmc_auth_ok ? escapeHtml(node.bmc_verify_method || "通过") : "待验证"}</b></span>
        <span>硬件：${escapeHtml(hardware.product || "-")}${capturedDisks ? ` / ${capturedDisks} 盘` : ""}</span>
      </div>
      <p class="next-step">${escapeHtml(blocker || node.next_step || "等待下一步")}</p>
      <div class="node-actions">
        <button type="button" data-node-reboot="${escapeHtml(node.node_key)}">重启</button>
        <button type="button" data-node-verify-bmc="${escapeHtml(node.node_key)}" ${actions.verify_bmc?.allowed ? "" : "disabled"} title="${escapeHtml(actions.verify_bmc?.reason || "")}">重验 BMC</button>
        <button type="button" data-node-recommission="${escapeHtml(node.node_key)}" ${actions.recommission?.allowed ? "" : "disabled"} title="${escapeHtml(actions.recommission?.reason || "")}">重扫</button>
        <button type="button" data-node-wipe="${escapeHtml(node.node_key)}" ${actions.wipe?.allowed ? "" : "disabled"} title="${escapeHtml(actions.wipe?.reason || "")}">清盘</button>
        <button type="button" data-node-storage="${escapeHtml(node.node_key)}" ${actions.apply_storage?.allowed ? "" : "disabled"} title="${escapeHtml(actions.apply_storage?.reason || "")}">套盘</button>
        <button type="button" data-node-deploy="${escapeHtml(node.node_key)}" ${actions.deploy?.allowed ? "" : "disabled"} title="${escapeHtml(actions.deploy?.reason || "")}">部署</button>
      </div>
    </article>`;
  }).join("") || `<div class="empty-state">没有匹配的节点</div>`;
}

function renderNodeTable(nodes) {
  $("nodeHead").innerHTML = [
    `<th><input id="toggleVisibleCheck" class="row-check" type="checkbox" title="全选当前结果"></th>`,
    "<th>节点</th>",
    "<th>当前状态</th>",
    "<th>部署阶段</th>",
    "<th>网络与硬件</th>",
    "<th>交付状态</th>",
    "<th>下一步</th>",
    "<th>操作</th>",
  ].join("");
  $("nodeBody").innerHTML = nodes.map((node) => {
    const actions = node.actions || {};
    const hardware = node.hardware || {};
    const capturedDisks = Array.isArray(hardware.block_devices)
      ? hardware.block_devices.filter((item) => item.type === "disk").length
      : Number(node.block_device_count || 0);
    const compliance = node.compliance || {};
    const complianceLabel = {
      passed: "全部通过",
      failed: "存在不符合项",
      unreachable: "SSH 不可达",
      not_configured: "未配置检查",
      not_checked: "未执行项目检查",
    }[node.compliance_status] || node.compliance_status;
    const connectivityLabel = {
      passed: "网络/SSH 通过",
      failed: "网络/SSH 失败",
      running: "正在检测",
      queued: "等待检测",
      not_checked: "等待检测",
    }[node.connectivity_status] || node.connectivity_status;
    const deliveryLabel = node.ansible_status === "succeeded" ? "节点验收 Ready" : "待执行 Ansible";
    const targetIp = String(node["25g"] || "").split(/[\/,]/)[0] || "-";
    const next = node.blocker || node.next_step || "等待下一步";
    return `<tr>
      <td><input class="row-check" type="checkbox" data-node-check="${escapeHtml(node.node_key)}" ${state.selectedNodeKeys.has(node.node_key) ? "checked" : ""}></td>
      <td class="node-identity"><strong>${escapeHtml(nodeTitle(node))}</strong><span>SN ${escapeHtml(node.sn || "-")}</span><span>ID ${escapeHtml(node.system_id || "-")}</span></td>
      <td><span class="pill ${stateClass(node.status)}">${escapeHtml(node.status || "-")}</span><small>${escapeHtml(node.power_state || node.status_source || "-")}</small></td>
      <td class="node-phase"><div><strong>${escapeHtml(node.phase_label || phaseLabel(node.phase))}</strong><span>${escapeHtml(node.progress || 0)}%</span></div><div class="progress-line"><span style="width:${Math.max(0, Math.min(Number(node.progress || 0), 100))}%"></span></div>${renderTimeline(node)}</td>
      <td class="node-network"><span>业务 ${escapeHtml(targetIp)}</span><span class="${stateClass(node.connectivity_status === "passed" ? "ok" : node.connectivity_status === "failed" ? "failed" : "pending")}">${escapeHtml(connectivityLabel)}</span><span>BMC ${escapeHtml(node.bmc_ip || "-")} · ${escapeHtml(hardware.product || "硬件待采集")}${capturedDisks ? ` / ${capturedDisks} 盘` : ""}</span></td>
      <td><span class="pill ${stateClass(node.ansible_status === "succeeded" ? "ok" : "pending")}">${escapeHtml(deliveryLabel)}</span><small>项目检查：${escapeHtml(complianceLabel)}</small></td>
      <td class="node-next" title="${escapeHtml(next)}">${escapeHtml(next)}</td>
      <td>
        <details class="node-action-menu"><summary>操作</summary><div class="cell-actions">
          <button type="button" data-node-reboot="${escapeHtml(node.node_key)}">重启</button><button type="button" data-node-verify-bmc="${escapeHtml(node.node_key)}" ${actions.verify_bmc?.allowed ? "" : "disabled"} title="${escapeHtml(actions.verify_bmc?.reason || "")}">重验 BMC</button><button type="button" data-node-recommission="${escapeHtml(node.node_key)}" ${actions.recommission?.allowed ? "" : "disabled"} title="${escapeHtml(actions.recommission?.reason || "")}">重扫</button><button type="button" data-node-wipe="${escapeHtml(node.node_key)}" ${actions.wipe?.allowed ? "" : "disabled"} title="${escapeHtml(actions.wipe?.reason || "")}">清盘</button><button type="button" data-node-storage="${escapeHtml(node.node_key)}" ${actions.apply_storage?.allowed ? "" : "disabled"} title="${escapeHtml(actions.apply_storage?.reason || "")}">套盘</button><button type="button" data-node-deploy="${escapeHtml(node.node_key)}" ${actions.deploy?.allowed ? "" : "disabled"} title="${escapeHtml(actions.deploy?.reason || "")}">部署</button><button type="button" data-node-delete="${escapeHtml(node.node_key)}" ${actions.delete?.allowed ? "" : "disabled"} title="${escapeHtml(actions.delete?.reason || "")}">删除</button>
        </div></details>
      </td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">没有匹配的节点</td></tr>`;

  const visibleKeys = nodes.map((node) => node.node_key);
  const toggle = $("toggleVisibleCheck");
  if (toggle) {
    toggle.checked = visibleKeys.length > 0 && visibleKeys.every((key) => state.selectedNodeKeys.has(key));
    toggle.addEventListener("change", (event) => {
      if (event.target.checked) {
        visibleKeys.forEach((key) => state.selectedNodeKeys.add(key));
      } else {
        visibleKeys.forEach((key) => state.selectedNodeKeys.delete(key));
      }
      renderNodes();
    });
  }
}

function bindNodeControls() {
  document.querySelectorAll("[data-focus-node]").forEach((button) => {
    button.addEventListener("click", () => focusNode(button.dataset.focusNode));
  });
  document.querySelectorAll("[data-node-check]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const key = event.target.dataset.nodeCheck;
      if (event.target.checked) state.selectedNodeKeys.add(key);
      else state.selectedNodeKeys.delete(key);
      renderNodes();
    });
  });
  document.querySelectorAll("[data-node-reboot]").forEach((button) => {
    button.addEventListener("click", () => runNodeReboot([button.dataset.nodeReboot]));
  });
  document.querySelectorAll("[data-node-verify-bmc]").forEach((button) => {
    button.addEventListener("click", () => runNodeVerifyBmc([button.dataset.nodeVerifyBmc]));
  });
  document.querySelectorAll("[data-node-recommission]").forEach((button) => {
    button.addEventListener("click", () => runNodeRecommission([button.dataset.nodeRecommission]));
  });
  document.querySelectorAll("[data-node-delete]").forEach((button) => {
    button.addEventListener("click", () => runNodeDelete([button.dataset.nodeDelete]));
  });
  document.querySelectorAll("[data-node-wipe]").forEach((button) => {
    button.addEventListener("click", () => runNodeWipe([button.dataset.nodeWipe]));
  });
  document.querySelectorAll("[data-node-storage]").forEach((button) => {
    button.addEventListener("click", () => runNodeApplyStorage([button.dataset.nodeStorage]));
  });
  document.querySelectorAll("[data-node-deploy]").forEach((button) => {
    button.addEventListener("click", () => runNodeDeploy([button.dataset.nodeDeploy]));
  });
}

function renderNodes() {
  const shown = currentNodes();
  renderSelectionSummary();
  renderNodeTable(shown);
  bindNodeControls();
  applyGlobalActionGates();
}

function renderConfigFields(targetId, fields, current, defaults, effective) {
  $(targetId).innerHTML = fields.map((field) => {
    const currentValue = getConfigValue(current, field.path);
    const defaultValue = getConfigValue(defaults, field.path);
    const effectiveValue = getConfigValue(effective, field.path);
    return `<div class="config-field">
      <label class="config-label" for="${fieldId(field.path)}">${escapeHtml(field.label)}</label>
      <input id="${fieldId(field.path)}" data-config-path="${field.path}" type="${field.type}" value="${escapeHtml(currentValue ?? "")}" ${field.type === "number" ? 'step="1"' : ""}>
      <div class="config-meta">
        <div>${escapeHtml(field.help || "")}</div>
        <div>默认：${escapeHtml(defaultValue ?? "")}</div>
        <div>生效：${escapeHtml(effectiveValue ?? "")}</div>
      </div>
    </div>`;
  }).join("");
}

function renderConfigDerived(effective) {
  const server = effective?.server || {};
  const consoleConfig = effective?.console || {};
  const raid = effective?.raid || {};
  const validationErrors = effective?.validation_errors || [];
  const items = [
    ["实验名称", effective?.lab_name || "-"],
    ["配置文件", state.config?.path || "-"],
    ["tools_base_url", raid.tools_base_url || "-"],
    ["wipe_script_name", consoleConfig.wipe_script_name || "-"],
    ["flow_tag", consoleConfig.flow_tag || "-"],
    ["deploy_policy", consoleConfig.deploy_policy || "(默认)"],
    ["部署镜像", `${consoleConfig.deploy_osystem || "ubuntu"} / ${consoleConfig.deploy_series || "jammy"}`],
    ["节点验收", "Ansible 完成后进入 Ready，项目检查结果用于验收"],
    ["stage1_server_ip", server.stage1_server_ip || server.dhcp_router || server.external_ip || "-"],
    ["maas_url", server.maas_url || "-"],
    ["校验结果", validationErrors.length ? `${validationErrors.length} 项待修正` : "通过"],
  ];
  $("configDerived").innerHTML = items.map(([name, value]) => (
    `<div class="status-item"><span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong></div>`
  )).join("");
}

function renderConfigSummary(effective) {
  const server = effective?.server || {};
  const clients = effective?.clients || [];
  const errors = effective?.validation_errors || [];
  const raid = effective?.raid || {};
  const consoleConfig = effective?.console || {};
  const items = [
    ["配置文件", state.config?.path?.split("/").slice(-1)[0] || "-", state.config?.path || "-"],
    ["客户端", clients.length, "由 CSV 与 clients JSON 合成"],
    ["PXE 网卡", server.dhcp_interface || "未配置", server.dhcp_range || "DHCP 池未配置"],
    ["Stage1", `${server.stage1_server_ip || server.dhcp_router || server.external_ip || "-"}:${server.stage1_port || "-"}`, "无盘抓配入口"],
    ["MAAS", server.maas_url || "-", server.admin_user || "admin"],
    ["部署镜像", `${consoleConfig.deploy_osystem || "ubuntu"}/${consoleConfig.deploy_series || "jammy"}`, "版本与内核由所选镜像决定"],
    ["校验", errors.length ? `${errors.length} 项问题` : "通过", errors[0] || "配置可用于同步和批量流程"],
  ];
  $("configSummary").innerHTML = items.map(([label, value, hint]) => (
    `<div class="config-summary-card ${label === "校验" ? (errors.length ? "bad" : "ok") : ""}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(hint)}</small>
    </div>`
  )).join("");
}

function renderConfigValidation(errors) {
  const items = Array.isArray(errors) ? errors : [];
  $("configValidation").textContent = items.length
    ? `配置校验未通过：\n- ${items.join("\n- ")}`
    : "配置校验通过。";
}

function applyConfigForm(current, effective = null) {
  if (!state.config) return;
  state.config.formCurrent = cloneValue(current);
  renderConfigFields("configBasicFields", CONFIG_FIELDS.basic, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configServerCoreFields", CONFIG_FIELDS.serverCore, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configServerAccessFields", CONFIG_FIELDS.serverAccess, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configNetworkFields", CONFIG_FIELDS.network, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configConsoleFields", CONFIG_FIELDS.console, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configDefaultsFields", CONFIG_FIELDS.defaults, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  renderConfigFields("configRaidFields", CONFIG_FIELDS.raid, state.config.formCurrent, state.config.defaults, effective || state.config.effective);
  $("configUploadedCsv").value = state.config.formCurrent.inventory?.uploaded_csv || "";
  $("configClients").value = prettyJson(state.config.formCurrent.clients || []);
  $("configNodeTypes").value = prettyJson(state.config.formCurrent.node_types || {});
  $("configKnownBmcs").value = prettyJson(state.config.formCurrent.known_bmcs || []);
  $("configClientsDefault").textContent = prettyJson(state.config.defaults.clients || []);
  $("configNodeTypesDefault").textContent = prettyJson(state.config.defaults.node_types || {});
  $("configBmcsDefault").textContent = prettyJson(state.config.defaults.known_bmcs || []);
  const policyFiles = state.config.policyFiles || {};
  $("configDeployPolicy").value = policyFiles.deploy_policy?.content || "";
  $("configDefaultUserData").value = policyFiles.default_user_data?.content || "";
  $("configDeployPolicyPath").textContent = policyFiles.deploy_policy?.path || "";
  $("configDefaultUserDataPath").textContent = policyFiles.default_user_data?.path || "";
  renderConfigSummary(effective || state.config.effective);
  renderConfigDerived(effective || state.config.effective);
  renderConfigValidation((effective || state.config.effective)?.validation_errors || []);
  $("configPreview").textContent = prettyJson(effective || state.config.effective);
}

function renderConfig(data) {
  state.config = {
    ...data,
    formCurrent: cloneValue(data.current),
    policyFiles: cloneValue(data.policy_files || {}),
  };
  state.configDirty = false;
  $("configPath").textContent = data.path.split("/").slice(-1)[0] || data.path;
  $("configPath").title = data.path;
  applyConfigForm(data.current, data.effective);
  $("configStatus").textContent = "loaded";
}

function markConfigDirty() {
  if (!state.config) return;
  state.configDirty = true;
  $("configStatus").textContent = "modified";
  $("configStatus").className = "badge warn";
}

function readConfigFromForm() {
  if (!state.config) throw new Error("配置尚未加载完成");
  const nextConfig = cloneValue(state.config.current);
  ALL_CONFIG_FIELDS.forEach((field) => {
    const input = $(fieldId(field.path));
    const raw = input?.value ?? "";
    setConfigValue(nextConfig, field.path, field.type === "number" ? (raw === "" ? "" : Number(raw)) : raw);
  });
  nextConfig.inventory = nextConfig.inventory || {};
  nextConfig.inventory.uploaded_csv = $("configUploadedCsv").value || "";
  try {
    nextConfig.clients = JSON.parse($("configClients").value || "[]");
    nextConfig.node_types = JSON.parse($("configNodeTypes").value || "{}");
    nextConfig.known_bmcs = JSON.parse($("configKnownBmcs").value || "[]");
  } catch (error) {
    throw new Error(`JSON 格式错误: ${error.message}`);
  }
  return nextConfig;
}

async function loadConfig() {
  if (state.configLoading) return;
  state.configLoading = true;
  try {
    const res = await fetch("/api/config");
    renderConfig(await res.json());
  } finally {
    state.configLoading = false;
  }
}

async function saveConfig() {
  $("configStatus").textContent = "saving";
  let config;
  try {
    config = readConfigFromForm();
  } catch (error) {
    $("configStatus").textContent = "invalid";
    $("flowOutput").textContent = String(error.message || error);
    $("actionBadge").textContent = "failed";
    return;
  }
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      config,
      policy_files: {
        deploy_policy: {
          content: $("configDeployPolicy").value,
        },
        default_user_data: {
          content: $("configDefaultUserData").value,
        },
      },
    }),
  });
  const data = await res.json();
  if (!data.ok) {
    $("configStatus").textContent = "failed";
    $("flowOutput").textContent = data.error || JSON.stringify(data, null, 2);
    $("actionBadge").textContent = "failed";
    return;
  }
  renderConfig(data);
  $("flowOutput").textContent = "配置已保存。涉及清盘脚本模板变更时，请再执行一次同步清盘脚本。";
  $("actionBadge").textContent = "ok";
  $("configStatus").className = "badge ok";
  await load();
}

async function load() {
  if (state.loading) return;
  state.loading = true;
  try {
    const res = await fetch("/api/summary");
    state.data = await res.json();
    const currentKeys = new Set((state.data.nodes || []).map((node) => node.node_key));
    state.selectedNodeKeys = new Set(Array.from(state.selectedNodeKeys).filter((key) => currentKeys.has(key)));
    $("sourceRoot").textContent = state.data.sources_root;
    $("mutationBadge").textContent = mutationModeLabel();
    $("mutationBadge").className = state.data.allow_mutation ? "ok" : "warn";
    $("actionBadge").textContent = mutationModeLabel();
    state.lastLoadedAt = new Date();
    renderLastUpdated();
    renderMetrics(state.data.stats || {});
    renderModeControl();
    renderPipeline();
    renderTopbarMeta();
    renderAttention();
    renderActivity();
    renderLab(state.data.lab);
    renderStatus("services", state.data.services);
    renderStatus("sources", state.data.sources);
    renderStatusFilter();
    renderPhaseFilter();
    renderNodes();
  } finally {
    state.loading = false;
  }
}

function startAutoRefresh() {
  if (state.refreshTimer) return;
  state.refreshTimer = window.setInterval(() => {
    load().catch((error) => {
      $("sourceRoot").textContent = String(error);
    });
  }, REFRESH_INTERVAL_MS);
}

async function runEndpoint(action, dry = true) {
  if (state.configDirty && ["sync-lab-stage1", "export-stage1", "import-nodes", "register-wipe-script"].includes(action)) {
    $("configStatus").textContent = "modified";
    $("flowOutput").textContent = "配置有未保存修改，请先保存配置，再执行当前动作。";
    $("actionBadge").textContent = "warn";
    return;
  }
  if (action === "reset-stage1-state") {
    const confirmed = window.confirm([
      "确认清除全部 Stage1 上报记录吗？",
      "节点规划和 MAAS 节点不会删除。",
      "所有节点会回到等待无盘抓配状态，需要重新 PXE 启动采集。",
    ].join("\n"));
    if (!confirmed) return;
  }
  $("actionBadge").textContent = "running";
  $("flowOutput").textContent = "running...";
  const query = dry ? "?dry_run=1" : "?dry_run=0";
  const res = await fetch(`/api/actions/${action}${query}`);
  const data = await res.json();
  $("flowOutput").textContent = data.output || data.error || JSON.stringify(data, null, 2);
  $("actionBadge").textContent = data.ok ? "ok" : "failed";
  await load();
}

async function runMode(mode) {
  if (!state.data?.allow_mutation) {
    const message = [
      "当前控制台处于仅预演模式，未开启真实执行。",
      "如需真实执行，请在服务环境设置 MAAS_CONSOLE_ALLOW_MUTATION=1，并重启 maas-web-console.service。",
    ].join("\n");
    $("actionOutput").textContent = message;
    $("flowOutput").textContent = message;
    $("actionBadge").textContent = "warn";
    return;
  }
  if (mode === "diskless_stage1") {
    const confirmText = [
      "执行无盘抓配前，请确认：",
      "1. 目标节点已拔盘；测试环境至少已清空 RAID。",
      "2. 节点规划中的 SN、目标 BMC 地址/账号和目标业务网络已核对。",
      "3. 目标节点已连接到隔离装机网，准备 PXE 重启。",
    ].join("\n");
    if (!window.confirm(confirmText)) return;
  }
  $("actionOutput").textContent = "running...";
  const confirmStage1 = mode === "diskless_stage1" ? "&confirm_stage1=1" : "";
  const res = await fetch(`/api/actions/pxe-mode?mode=${encodeURIComponent(mode)}&dry_run=0${confirmStage1}`);
  const data = await res.json();
  let output = data.output || data.error || JSON.stringify(data, null, 2);
  if (data.ok && mode === "diskless_stage1") {
    output = `${output}\n\n下一步：重启目标节点。Stage1 将通过本机 IPMI/KCS 配置 BMC、回读验证并采集 PXE MAC/硬件。`;
  }
  $("actionOutput").textContent = output;
  $("flowOutput").textContent = output;
  $("actionBadge").textContent = data.ok ? "ok" : "failed";
  await load();
}

async function requestNodeAction(endpoint, nodeKeys) {
  if (!nodeKeys.length) {
    $("flowOutput").textContent = "请先选择节点";
    $("actionBadge").textContent = "warn";
    return { ok: false };
  }
  $("actionBadge").textContent = "running";
  $("flowOutput").textContent = "running...";
  const res = await fetch(`/api/actions/${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_keys: nodeKeys, dry_run: !state.data?.allow_mutation }),
  });
  const data = await res.json();
  $("flowOutput").textContent = data.output || data.error || JSON.stringify(data, null, 2);
  $("actionBadge").textContent = data.ok ? "ok" : "failed";
  await load();
  return data;
}

function automationStatusClass(status) {
  if (["succeeded", "passed"].includes(status)) return "ok";
  if (["running", "queued"].includes(status)) return "info";
  if (["failed", "interrupted", "unreachable"].includes(status)) return "bad";
  return "warn";
}

function renderAutomation() {
  const data = state.automation || { runtime: {}, bundles: [], jobs: [] };
  const runtime = data.runtime || {};
  $("ansibleRuntime").textContent = runtime.ansible_available ? "Ansible ready" : "Ansible missing";
  $("ansibleRuntime").className = `badge ${runtime.ansible_available ? "ok" : "bad"}`;
  $("paramikoRuntime").textContent = runtime.paramiko_available ? "SSH checks ready" : "SSH checks missing";
  $("paramikoRuntime").className = `badge ${runtime.paramiko_available ? "ok" : "bad"}`;
  $("bundleRepository").textContent = data.repository || "-";
  const bundles = data.bundles || [];
  $("bundleList").innerHTML = bundles.map((bundle) => `<label class="bundle-item">
    <input type="radio" name="bundle-item" value="${escapeHtml(bundle.id)}" ${bundle.valid ? "" : "disabled"}>
    <span><strong>${escapeHtml(bundle.name)}</strong><small>${escapeHtml(bundle.version || "unversioned")} · ${escapeHtml(bundle.playbook)}</small></span>
    <span class="pill ${bundle.valid ? "ok" : "bad"}">${bundle.valid ? "ready" : "invalid"}</span>
  </label>`).join("") || `<div class="empty-state">暂无剧本包</div>`;
  const currentBundle = $("automationBundle").value;
  $("automationBundle").innerHTML = [`<option value="">不选择剧本包</option>`]
    .concat(bundles.filter((bundle) => bundle.valid).map((bundle) => `<option value="${escapeHtml(bundle.id)}">${escapeHtml(bundle.name)} ${escapeHtml(bundle.version || "")}</option>`)).join("");
  if (bundles.some((bundle) => bundle.id === currentBundle)) $("automationBundle").value = currentBundle;
  $("runAnsibleBtn").disabled = !runtime.ansible_available;
  $("checkAnsibleBtn").disabled = !runtime.ansible_available;
  $("runComplianceBtn").disabled = !runtime.paramiko_available;
  const eligibleKeys = new Set(data.eligible_node_keys || []);
  const deployedNodes = (state.data?.nodes || []).filter((node) => eligibleKeys.has(node.node_key));
  $("automationTargets").innerHTML = deployedNodes.map((node) => `<label>
    <input type="checkbox" data-automation-node="${escapeHtml(node.node_key)}" ${state.selectedNodeKeys.has(node.node_key) ? "checked" : ""}>
    <span><strong>${escapeHtml(nodeTitle(node))}</strong><small>${escapeHtml(String(node["25g"] || "").split(/[\/,]/)[0] || "无业务 IP")}</small></span>
  </label>`).join("") || `<div class="empty-state">暂无通过网络与 SSH 门禁的节点</div>`;
  $("automationJobs").innerHTML = (data.jobs || []).map((job) => `<tr data-job-id="${escapeHtml(job.id)}">
    <td><button type="button" class="job-link" data-open-job="${escapeHtml(job.id)}">${escapeHtml(job.id)}</button></td>
    <td>${escapeHtml(job.kind)}</td><td>${escapeHtml(job.bundle_id || "-")}</td><td>${escapeHtml((job.node_keys || []).length)}</td>
    <td><span class="pill ${automationStatusClass(job.status)}">${escapeHtml(job.status)}</span></td>
    <td>${escapeHtml(new Date((job.created_at || 0) * 1000).toLocaleString("zh-CN"))}</td>
  </tr>`).join("") || `<tr><td colspan="6" class="muted">暂无任务</td></tr>`;
  document.querySelectorAll("[data-open-job]").forEach((button) => button.addEventListener("click", () => openAutomationJob(button.dataset.openJob)));
  document.querySelectorAll('input[name="bundle-item"]').forEach((input) => input.addEventListener("change", () => { $("automationBundle").value = input.value; }));
  document.querySelectorAll("[data-automation-node]").forEach((input) => input.addEventListener("change", () => {
    if (input.checked) state.selectedNodeKeys.add(input.dataset.automationNode);
    else state.selectedNodeKeys.delete(input.dataset.automationNode);
    renderNodes();
    renderAutomation();
  }));
  renderSelectionSummary();
}

async function loadAutomation() {
  if (state.automationLoading) return;
  state.automationLoading = true;
  try {
    const response = await fetch("/api/automation");
    state.automation = await response.json();
    renderAutomation();
  } finally {
    state.automationLoading = false;
  }
}

async function openAutomationJob(jobId) {
  const response = await fetch(`/api/automation/jobs/${encodeURIComponent(jobId)}`);
  const job = await response.json();
  const resultText = Array.isArray(job.results) ? `\n\n${JSON.stringify(job.results, null, 2)}` : "";
  $("automationOutput").textContent = `${job.output || job.error || JSON.stringify(job, null, 2)}${resultText}`;
}

function fileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function uploadAutomationBundle() {
  const file = $("bundleFile").files[0];
  if (!file) {
    $("automationOutput").textContent = "请选择 .tar.gz 或 .tgz 剧本包";
    return;
  }
  $("automationOutput").textContent = "上传并校验中...";
  const response = await fetch("/api/automation/bundles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, content_base64: await fileAsBase64(file) }),
  });
  const data = await response.json();
  $("automationOutput").textContent = data.ok ? `已导入 ${data.bundle.name}` : data.error;
  await loadAutomation();
}

async function startAutomationJob(kind, checkMode = false) {
  const nodeKeys = Array.from(state.selectedNodeKeys);
  const bundleId = $("automationBundle").value;
  if (kind === "jobs" && !bundleId) {
    $("automationOutput").textContent = "执行 Ansible 前请选择剧本包";
    return;
  }
  $("automationOutput").textContent = "正在创建任务...";
  const response = await fetch(`/api/automation/${kind}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_keys: nodeKeys, bundle_id: bundleId, check_mode: checkMode }),
  });
  const data = await response.json();
  $("automationOutput").textContent = data.ok ? `任务已创建：${data.job.id}` : data.error;
  await loadAutomation();
}

async function postAutomationAction(path, confirmText = "") {
  if (confirmText && !window.confirm(confirmText)) return;
  const nodeKeys = Array.from(state.selectedNodeKeys);
  $("automationOutput").textContent = "正在处理...";
  const response = await fetch(`/api/automation/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_keys: nodeKeys }),
  });
  const data = await response.json();
  $("automationOutput").textContent = data.error || JSON.stringify(data, null, 2);
  await Promise.all([load(), loadAutomation()]);
}

async function runNodeReboot(nodeKeys) {
  await requestNodeAction("reboot-nodes", nodeKeys);
}

async function runNodeVerifyBmc(nodeKeys) {
  await requestNodeAction("reverify-bmc-nodes", nodeKeys);
}

async function runNodeRecommission(nodeKeys) {
  await requestNodeAction("recommission-nodes", nodeKeys);
}

async function runNodeWipe(nodeKeys) {
  await requestNodeAction("wipe-nodes", nodeKeys);
}

async function runNodeApplyStorage(nodeKeys) {
  await requestNodeAction("apply-storage-nodes", nodeKeys);
}

async function runNodeDeploy(nodeKeys) {
  await requestNodeAction("deploy-nodes", nodeKeys);
}

async function runNodeDelete(nodeKeys) {
  if (!nodeKeys.length) {
    $("flowOutput").textContent = "请先选择节点";
    $("actionBadge").textContent = "warn";
    return;
  }
  const label = nodeKeys.length === 1 ? "这个节点" : `这 ${nodeKeys.length} 个节点`;
  if (!window.confirm(`确认删除 ${label} 吗？这会同时清理 MAAS、节点规划、Stage1 上报和导出记录。`)) return;
  const data = await requestNodeAction("delete-nodes", nodeKeys);
  if (data.ok) {
    nodeKeys.forEach((key) => state.selectedNodeKeys.delete(key));
    renderNodes();
  }
}

function bindStaticControls() {
  setActivePage();
  window.addEventListener("hashchange", setActivePage);
  $("refreshBtn").addEventListener("click", load);
  $("focusSearchBtn").addEventListener("click", () => {
    location.hash = "#nodes";
    $("filterInput").focus();
  });
  window.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      location.hash = "#nodes";
      $("filterInput").focus();
    }
  });
  $("filterInput").addEventListener("input", (event) => {
    state.filter = event.target.value;
    renderNodes();
  });
  $("statusFilter").addEventListener("change", (event) => {
    state.statusFilter = event.target.value;
    renderNodes();
  });
  $("phaseFilter").addEventListener("change", (event) => {
    state.phaseFilter = event.target.value;
    renderNodes();
  });
  $("selectVisibleBtn").addEventListener("click", () => {
    currentNodes().forEach((node) => state.selectedNodeKeys.add(node.node_key));
    renderNodes();
  });
  $("clearSelectionBtn").addEventListener("click", () => {
    state.selectedNodeKeys.clear();
    renderNodes();
  });
  $("recommissionSelectedBtn").addEventListener("click", () => runNodeRecommission(Array.from(state.selectedNodeKeys)));
  $("deleteSelectedBtn").addEventListener("click", () => runNodeDelete(Array.from(state.selectedNodeKeys)));
  $("wipeSelectedBtn").addEventListener("click", () => runNodeWipe(Array.from(state.selectedNodeKeys)));
  $("storageSelectedBtn").addEventListener("click", () => runNodeApplyStorage(Array.from(state.selectedNodeKeys)));
  $("rebootSelectedBtn").addEventListener("click", () => runNodeReboot(Array.from(state.selectedNodeKeys)));
  $("deploySelectedBtn").addEventListener("click", () => runNodeDeploy(Array.from(state.selectedNodeKeys)));
  $("uploadBundleBtn").addEventListener("click", uploadAutomationBundle);
  $("runAnsibleBtn").addEventListener("click", () => startAutomationJob("jobs", false));
  $("checkAnsibleBtn").addEventListener("click", () => startAutomationJob("jobs", true));
  $("runComplianceBtn").addEventListener("click", () => startAutomationJob("checks"));
  $("rerunConnectivityBtn").addEventListener("click", () => postAutomationAction("connectivity"));
  $("refreshAutomationBtn").addEventListener("click", loadAutomation);
  document.querySelectorAll("[data-mode]").forEach((button) => {
    button.addEventListener("click", () => runMode(button.dataset.mode));
  });
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => runEndpoint(button.dataset.action, !state.data?.allow_mutation));
  });
  $("saveConfigBtn").addEventListener("click", saveConfig);
  $("reloadConfigBtn").addEventListener("click", () => {
    loadConfig().catch((error) => {
      $("configStatus").textContent = "failed";
      $("flowOutput").textContent = String(error);
    });
  });
  $("defaultsConfigBtn").addEventListener("click", () => {
    if (!state.config) return;
    applyConfigForm(cloneValue(state.config.defaults), cloneValue(state.config.defaults));
    $("configStatus").textContent = "defaults";
  });
  $("syncWipeTemplateBtn").addEventListener("click", () => runEndpoint("register-wipe-script", !state.data?.allow_mutation));
  $("configForm").addEventListener("input", () => markConfigDirty());
  document.querySelectorAll("[data-config-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-config-tab]").forEach((item) => item.classList.toggle("active", item === button));
      document.querySelectorAll("[data-config-pane]").forEach((pane) => {
        pane.classList.toggle("active", pane.dataset.configPane === button.dataset.configTab);
      });
    });
  });
}

bindStaticControls();
renderLastUpdated();
startAutoRefresh();
Promise.all([load(), loadConfig(), loadAutomation()]).catch((error) => {
  $("sourceRoot").textContent = String(error);
  $("configStatus").textContent = "failed";
});
