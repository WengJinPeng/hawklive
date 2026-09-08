const $ = (id) => document.getElementById(id);
const uiLocale = () => window.HawkI18n?.locale?.() || "zh-CN";
const uiText = (value) => window.HawkI18n?.t?.(value) || String(value ?? "");

const particleKeys = [
  "pm_0_3_um",
  "pm_0_5_um",
  "pm_1_0_um",
  "pm_2_5_um",
  "pm_5_0_um",
  "pm_10_0_um",
];
const particleAlarmChannels = [
  { key: "pm_0_3_um", metric: "particle_0_3_um", maxField: "particle_0_3_max", enabledField: "particle_0_3_enabled", maxInput: "particle03MaxInput", enabledInput: "particle03EnabledInput", label: "0.3 µm" },
  { key: "pm_0_5_um", metric: "particle_0_5_um", maxField: "particle_0_5_max", enabledField: "particle_0_5_enabled", maxInput: "particle05MaxInput", enabledInput: "particle05EnabledInput", label: "0.5 µm" },
  { key: "pm_1_0_um", metric: "particle_1_0_um", maxField: "particle_1_0_max", enabledField: "particle_1_0_enabled", maxInput: "particle10MaxInput", enabledInput: "particle10EnabledInput", label: "1.0 µm" },
  { key: "pm_2_5_um", metric: "particle_2_5_um", maxField: "particle_2_5_max", enabledField: "particle_2_5_enabled", maxInput: "particle25MaxInput", enabledInput: "particle25EnabledInput", label: "2.5 µm" },
  { key: "pm_5_0_um", metric: "particle_5_0_um", maxField: "particle_5_0_max", enabledField: "particle_5_0_enabled", maxInput: "particle50MaxInput", enabledInput: "particle50EnabledInput", label: "5.0 µm" },
  { key: "pm_10_0_um", metric: "particle_10_0_um", maxField: "particle_10_0_max", enabledField: "particle_10_0_enabled", maxInput: "particle100MaxInput", enabledInput: "particle100EnabledInput", label: "10.0 µm" },
];
const particleAlarmByKey = new Map(particleAlarmChannels.map((channel) => [channel.key, channel]));
const channelLabels = {
  pm_0_3_um: "0.3 µm",
  pm_0_5_um: "0.5 µm",
  pm_1_0_um: "1.0 µm",
  pm_2_5_um: "2.5 µm",
  pm_5_0_um: "5.0 µm",
  pm_10_0_um: "10.0 µm",
  temperature: "温度",
  humidity: "湿度",
};
const metricLabels = {
  particle_0_3_um: "≥ 0.3 µm 粒子数",
  particle_0_5_um: "≥ 0.5 µm 粒子数",
  particle_1_0_um: "≥ 1.0 µm 粒子数",
  particle_2_5_um: "≥ 2.5 µm 粒子数",
  particle_5_0_um: "≥ 5.0 µm 粒子数",
  particle_10_0_um: "≥ 10.0 µm 粒子数",
  particle_5_um: "≥ 5.0 µm 粒子数（兼容项）",
  temperature: "温度",
  humidity: "湿度",
};
const activeAlarmStates = new Set(["ALARM_ACTIVE", "PENDING_CLEAR"]);

let rooms = [];
let historyRooms = null;
let latestByDevice = {};
let realtimeByDevice = {};
let storedHistory = [];
let storedTrendHistory = [];
let alarmEvents = [];
let realtimeSelectedDeviceIds = new Set();
let historySelectedDeviceIds = new Set();
let historyPage = 1;
let alarmFilter = "active";
let refreshTimer = null;
let resizeTimer = null;
let toastTimer = null;
let settingsDirty = false;
let emailSaved = null;
let emailDirty = false;
let emailBusy = false;
let emailLoadVersion = 0;
let lastManagedRoomId = null;
let currentUser = null;
let topologySites = [];
let topologyCapabilities = {};
let collectorActivation = null;
let discoveredDevices = [];
let pendingCollectors = [];
let deviceToDelete = null;
let deviceDeleteBusy = false;

const topologyManagerRoles = new Set(["admin", "customer_admin", "customer"]);

function canManageTopology(user = currentUser) {
  return topologyManagerRoles.has(String(user?.role || "").toLowerCase());
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function displayParticleUnit(rows) {
  const labels = new Set((rows || []).map((row) => row?.particle_unit_label).filter(Boolean));
  if (labels.size === 0) return "单位未确认";
  if (labels.size > 1) return "单位不一致";
  const label = [...labels][0];
  return label === "PCS/28.3L" ? "PCS/28.3 L（约等于 particles/ft³）" : label;
}

function selectedRoom() {
  return rooms.find((room) => String(room.id) === $("cleanroomInput").value) || rooms[0] || null;
}

function managedRoom() {
  return rooms.find((room) => String(room.id) === $("manageRoomInput").value) || selectedRoom();
}

function selectedDevice() {
  const room = selectedRoom();
  return room?.devices?.find((device) => String(device.id) === $("deviceInput").value) || room?.devices?.[0] || null;
}

function findDevice(deviceId) {
  for (const room of rooms) {
    const device = room.devices?.find((item) => String(item.id) === String(deviceId));
    if (device) return { room, device };
  }
  return { room: null, device: null };
}

function devicesForScope(scopeValue) {
  const scopedRooms = scopeValue === "all" ? rooms : [selectedRoom()].filter(Boolean);
  return scopedRooms.flatMap((room) => (room.devices || []).map((device) => ({ room, device })));
}

function realtimeScopeDevices() {
  return devicesForScope($("realtimeScopeInput")?.value || "room");
}

function historyScopeDevices() {
  const available = historyRooms || rooms;
  const scoped = ($("historyScopeInput")?.value || "room") === "all"
    ? available : available.filter((room) => String(room.id) === String(selectedRoom()?.id));
  const items = scoped.flatMap((room) => (room.devices || []).map((device) => ({ room, device })));
  items.sort((a,b) => Number(Boolean(a.device.historical_assignment)) - Number(Boolean(b.device.historical_assignment)));
  return items.filter((item, index) => items.findIndex((other) => String(other.device.id) === String(item.device.id)) === index);
}

function deviceColor(deviceId) {
  let hash = 2166136261;
  for (const character of String(deviceId || "device")) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  hash ^= hash >>> 16;
  hash = Math.imul(hash, 0x7feb352d);
  hash ^= hash >>> 15;
  hash = Math.imul(hash, 0x846ca68b);
  hash ^= hash >>> 16;
  const hue = (hash >>> 0) % 360;
  return `hsl(${hue} 58% 39%)`;
}

function seriesName(room, device, scopeValue) {
  const name = device.enabled === false ? `${device.name} (${uiText("已删除")})` : device.name;
  return scopeValue === "all" ? `${room.name} / ${name}` : name;
}

function collectorDisplayName(site) {
  if (!site) return uiText("本机采集器");
  return site.is_current && site.name === "本机采集器" ? uiText("本机采集器") : site.name;
}

function renderSeriesSelector(containerId, countId, items, selectedIds, attribute, scopeValue) {
  const validIds = new Set(items.map(({ device }) => String(device.id)));
  for (const id of [...selectedIds]) {
    if (!validIds.has(id)) selectedIds.delete(id);
  }
  const selectedCount = items.filter(({ device }) => selectedIds.has(String(device.id))).length;
  $(countId).textContent = `${selectedCount} / ${items.length} 台设备`;
  $(containerId).innerHTML = items.length ? items.map(({ room, device }) => {
    const id = String(device.id);
    const checked = selectedIds.has(id);
    return `<label class="series-toggle ${checked ? "selected" : ""}">
      <input type="checkbox" ${attribute}="${escapeHtml(id)}" ${checked ? "checked" : ""} />
      <i style="--series-color:${deviceColor(id)}"></i>
      <span><strong data-i18n-ignore>${escapeHtml(seriesName(room, device, scopeValue))}</strong><small data-i18n-ignore>${escapeHtml(room.name)}${device.enabled === false && device.disabled_at ? ` · ${escapeHtml(uiText("删除时间"))} ${escapeHtml(formatTime(device.disabled_at))}` : ""}</small></span>
    </label>`;
  }).join("") : '<div class="empty-inline">当前范围没有设备。</div>';
}

function renderRealtimeSeriesSelector() {
  renderSeriesSelector(
    "realtimeDeviceLegend",
    "realtimeDeviceCount",
    realtimeScopeDevices(),
    realtimeSelectedDeviceIds,
    "data-realtime-device",
    $("realtimeScopeInput").value,
  );
}

function renderHistorySeriesSelector() {
  if (typeof renderHistoryRegistrationNotice === "function") renderHistoryRegistrationNotice();
  renderSeriesSelector(
    "historyDeviceLegend",
    "historySelectedDeviceCount",
    historyScopeDevices(),
    historySelectedDeviceIds,
    "data-history-device",
    $("historyScopeInput").value,
  );
}

function selectAllRealtimeDevices() {
  realtimeSelectedDeviceIds = new Set(realtimeScopeDevices().map(({ device }) => String(device.id)));
  renderRealtimeSeriesSelector();
  drawRealtimeTrend();
}

function selectAllHistoryDevices() {
  historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
  renderHistorySeriesSelector();
  renderHistory();
}

function thresholdsFor(room = selectedRoom()) {
  return room?.thresholds || {};
}

function thresholdEnabled(value) {
  return value === true || value === 1 || value === "1" || String(value).toLowerCase() === "true";
}

function thresholdNumber(value) {
  if (value === null || value === undefined || value === "") return Number.NaN;
  return Number(value);
}

function asTimestamp(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  return number > 1e12 ? number / 1000 : number;
}

function dateFromTimestamp(value) {
  const timestamp = asTimestamp(value);
  return timestamp ? new Date(timestamp * 1000) : null;
}

function formatNumber(value, maximumFractionDigits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString(uiLocale(), { maximumFractionDigits });
}

function formatBytes(value) {
  if (value === null || value === undefined || value === "") return "--";
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "--";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes;
  let index = -1;
  do {
    amount /= 1024;
    index += 1;
  } while (amount >= 1024 && index < units.length - 1);
  return `${amount.toLocaleString(uiLocale(), { maximumFractionDigits: amount >= 10 ? 0 : 1 })} ${units[index]}`;
}

function formatTime(value) {
  const date = dateFromTimestamp(value);
  return date ? date.toLocaleString(uiLocale(), { hour12: false }) : "--";
}

function formatShortTime(value) {
  const date = dateFromTimestamp(value);
  return date ? date.toLocaleString(uiLocale(), { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "--";
}

function relativeTime(value) {
  const timestamp = asTimestamp(value);
  if (!timestamp) return "尚无数据";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function formatDuration(start, end = Date.now() / 1000) {
  const startTime = asTimestamp(start);
  const endTime = asTimestamp(end) || Date.now() / 1000;
  if (!startTime) return "时长未知";
  const total = Math.max(0, Math.floor(endTime - startTime));
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分钟`;
  if (total < 86400) return `${Math.floor(total / 3600)} 小时 ${Math.floor((total % 3600) / 60)} 分钟`;
  return `${Math.floor(total / 86400)} 天 ${Math.floor((total % 86400) / 3600)} 小时`;
}

function localInputValue(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function friendlyError(message) {
  const text = String(message || "请求失败");
  const mappings = [
    [/SMTP is not configured/i, "邮件服务器尚未配置，请联系系统管理员。"],
    [/At least one recipient is required/i, "启用预警前，请至少填写一个收件邮箱。"],
    [/Save recipients before sending a test/i, "请先保存收件邮箱，再发送测试邮件。"],
    [/Wait 60 seconds/i, "请等待 60 秒后再发送测试邮件。"],
    [/Authentication required/i, "登录已失效，请重新登录。"],
    [/Invalid username or password/i, "账号或密码不正确。"],
    [/Too many/i, "尝试次数过多，请稍后再试。"],
    [/Failed to fetch/i, "无法连接到服务，请确认本机服务仍在运行。"],
    [/NetworkError/i, "网络连接失败，请稍后重试。"],
    [/Administrator permission is required/i, "只有企业管理员可以维护车间和设备。"],
    [/Cleanroom name is already in use/i, "这个车间名称已经存在。"],
    [/Device name is already in use/i, "该车间内已经有同名设备。"],
    [/Device not found/i, "设备不存在，请刷新列表。"],
    [/already configured/i, "这个 IP、端口和 Slave ID 已被同一采集器中的其他设备使用。"],
    [/private IPv4/i, "请输入现场局域网内的私有 IPv4 地址。"],
    [/No site collector is configured/i, "尚未配置现场采集器，暂时无法添加设备。"],
    [/Select a site collector/i, "请选择负责这台设备的现场采集器。"],
    [/Collector name is already in use/i, "这个采集器名称已经存在。"],
    [/Another collector instance is already active/i, "这个节点已被另一台采集器占用，请勿让两台电脑共用同一节点。"],
  ];
  return mappings.find(([pattern]) => pattern.test(text))?.[1] || text;
}

function showToast(message, kind = "ok") {
  clearTimeout(toastTimer);
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast ${kind === "error" ? "error" : ""} show`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function setStatus(kind, text, detail) {
  $("statusBadge").className = `status ${kind}`;
  $("statusBadge").textContent = text;
  $("lastMessage").textContent = detail;
}

function setHealth(elementId, kind) {
  const item = $(elementId)?.closest(".health-item");
  if (item) item.className = `health-item ${kind ? `is-${kind}` : ""}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回了无法识别的内容（${response.status}）`);
  }
  if (response.status === 401) {
    clearInterval(refreshTimer);
    document.body.className = "auth-required";
  }
  if (!response.ok || !payload.ok) {
    const error = new Error(friendlyError(payload.error || payload.detail || `请求失败（${response.status}）`));
    error.status = response.status;
    throw error;
  }
  return payload.data;
}

function parsePrivateIPv4(value) {
  const text = String(value || "").trim();
  const parts = text.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
  const values = parts.map(Number);
  if (values.some((part) => part < 0 || part > 255)) return null;
  const privateAddress = values[0] === 10
    || (values[0] === 172 && values[1] >= 16 && values[1] <= 31)
    || (values[0] === 192 && values[1] === 168);
  return privateAddress ? values.join(".") : null;
}

function renderTopologySiteOptions() {
  const current = String($("manualDeviceSiteInput").value || "");
  const options = topologySites.map((site) => (
    `<option value="${escapeHtml(site.id)}" data-i18n-ignore>${escapeHtml(collectorDisplayName(site))} · ${escapeHtml(uiText(collectorNodeState(site).label))}</option>`
  )).join("");
  $("manualDeviceSiteInput").innerHTML = options || '<option value="">尚无可用采集器</option>';
  if (topologySites.some((site) => String(site.id) === current)) $("manualDeviceSiteInput").value = current;
  $("manualDeviceSiteInput").disabled = topologySites.length <= 1;
}

async function loadTopologySites(force = false) {
  if (!canManageTopology()) return;
  if (topologySites.length && !force) return;
  const data = await api("/api/admin/sites");
  topologySites = Array.isArray(data?.sites) ? data.sites : [];
  topologyCapabilities = data || {};
  renderTopologySiteOptions();
  $("addCollectorBtn").hidden = !topologyCapabilities.can_download_collector;
  $("addCollectorBtn").disabled = false;
  $("collectorScopeNotice").textContent = topologyCapabilities.scope_notice || "每台设备必须归属一台采集器。";
  updateManualDeviceSiteHelp();
  if (typeof loadDeviceLifecycle === "function" && topologyCapabilities.device_lifecycle) await loadDeviceLifecycle();
  renderTopology();
}

function collectorNodeState(site) {
  const needsAttention = site.config_state === "failed"
    || Number(site.quarantined_uploads || 0) > 0
    || Number(site.unassigned_uploads || 0) > 0
    || ["warning", "attention", "critical"].includes(String(site.storage_state || ""));
  if (needsAttention) return { key: "attention", label: "需要处理" };
  if (site.connected) return { key: "online", label: "在线" };
  if (site.connection_state === "upgrade_required") return { key: "attention", label: "需升级心跳" };
  if (site.connection_state === "awaiting_activation") return { key: "waiting", label: "待激活" };
  return { key: "offline", label: "离线" };
}

function collectorConfigLabel(site) {
  if (site.config_state === "local") return "本机配置";
  if (site.config_state === "failed") return "应用失败";
  const desired = Number(site.config_version);
  const applied = Number(site.applied_config_version);
  const hasDesired = site.config_version !== null && site.config_version !== undefined && Number.isFinite(desired);
  const hasApplied = site.applied_config_version !== null && site.applied_config_version !== undefined && Number.isFinite(applied);
  if (site.config_state === "applied" && hasApplied) return `v${applied} 已应用`;
  if (site.config_state === "pending" && hasDesired) {
    return hasApplied ? `v${applied} → v${desired}` : `等待 v${desired}`;
  }
  return "等待首次配置";
}

function collectorStorageLabel(state) {
  return {
    ok: "正常", unknown: "待上报", warning: "磁盘空间不足",
    attention: "存储异常", critical: "存储风险",
  }[state] || "待上报";
}

function renderCollectorNodes() {
  if (!$("collectorNodeList")) return;
  const onlineCount = topologySites.filter((site) => site.connected).length;
  $("collectorOnlineCount").textContent = `${onlineCount} / ${topologySites.length}`;
  $("collectorNodeList").innerHTML = topologySites.length ? topologySites.map((site) => {
    const state = collectorNodeState(site);
    const contactAt = site.last_heartbeat_at || site.last_contact_at;
    const identity = [site.hostname, site.version ? `v${site.version}` : null].filter(Boolean).join(" · ");
    const queueCount = Number(site.pending_uploads || 0);
    const error = site.config_apply_error || site.last_error;
    return `<article class="collector-node-card ${state.key}">
      <div class="collector-node-head">
        <div class="collector-node-identity"><span class="collector-node-glyph" aria-hidden="true">采</span><div><strong data-i18n-ignore>${escapeHtml(collectorDisplayName(site))}</strong><small data-i18n-ignore>${escapeHtml(identity || site.id)}</small></div></div>
        <div class="collector-tags">${site.is_current ? '<span class="collector-tag current">本机</span>' : '<span class="collector-tag">远程</span>'}<span class="collector-tag ${state.key}">${state.label}</span></div>
      </div>
      <dl class="collector-node-metrics">
        <div><dt>设备在线</dt><dd>${Number(site.device_online || 0)} / ${Number(site.device_total || 0)}</dd></div>
        <div><dt>配置状态</dt><dd>${escapeHtml(collectorConfigLabel(site))}</dd></div>
        <div><dt>待上传</dt><dd>${formatNumber(queueCount, 0)} 条</dd></div>
        <div><dt>最后采集</dt><dd>${site.last_reading_at ? relativeTime(site.last_reading_at) : "尚无真实数据"}</dd></div>
        <div><dt>本地存储</dt><dd>${escapeHtml(collectorStorageLabel(site.storage_state))}</dd></div>
        <div><dt>磁盘可用</dt><dd>${formatBytes(site.disk_free_bytes)}</dd></div>
      </dl>
      <div class="collector-node-foot"><span>${contactAt ? `心跳 ${relativeTime(contactAt)}` : site.is_current ? "本机状态实时读取" : "尚未收到心跳"}</span><span>${escapeHtml(site.id)}</span></div>
      ${state.key === "waiting" && !site.is_current ? `<button class="secondary-action collector-package-action" type="button" data-download-collector-package="${escapeHtml(site.id)}">下载自动安装包</button>` : ""}
      ${error ? `<p class="collector-node-error"><strong>需要检查：</strong>${escapeHtml(error)}</p>` : ""}
    </article>`;
  }).join("") : '<div class="collector-empty">还没有采集器节点。请先添加并激活一台采集器。</div>';
}

function updateManualDeviceSiteHelp() {
  const selected = topologySites.find((site) => String(site.id) === String($("manualDeviceSiteInput").value));
  if (!selected) {
    $("manualDeviceSiteHelp").textContent = "尚无可用采集器。";
    return;
  }
  const state = collectorNodeState(selected);
  if (state.key === "waiting") {
    $("manualDeviceSiteHelp").textContent = "可先分配设备；节点激活并取得配置后才会开始采集。";
  } else if (state.key === "offline" || state.key === "attention") {
    $("manualDeviceSiteHelp").textContent = `当前节点${state.label}；配置会保存，恢复连接后自动下发。`;
  } else {
    $("manualDeviceSiteHelp").textContent = topologySites.length > 1
      ? "请确认该采集器与设备处于同一网络。"
      : "设备配置会下发到这台采集器。";
  }
}

function topologyDeviceState(device) {
  const reading = latestByDevice[String(device.id)] || null;
  const readingIsReal = reading?.source === "device";
  const lastSeen = (readingIsReal ? readingTimestamp(reading) : null) || asTimestamp(device.last_seen_at);
  const fresh = Boolean(lastSeen && Date.now() / 1000 - lastSeen <= 45);
  const hasCurrentResult = Boolean(reading);
  const currentReadSucceeded = readingIsReal && reading.online !== false && !reading.error;
  const online = fresh && (currentReadSucceeded || (!hasCurrentResult && device.online === true));
  if (online) {
    return { key: "online", label: "在线", detail: `真实读数更新于${relativeTime(lastSeen)}`, lastSeen };
  }
  if (!lastSeen) {
    return {
      key: "pending", label: "待采集器确认",
      detail: reading && !readingIsReal ? "仅有模拟数据，真实连接未确认" : "尚未收到首条真实读数",
      lastSeen: null,
    };
  }
  return { key: "offline", label: "离线", detail: `最后读数在${relativeTime(lastSeen)}`, lastSeen };
}

function topologySiteName(siteId) {
  const site = topologySites.find((item) => String(item.id) === String(siteId));
  return site ? collectorDisplayName(site) : (siteId ? String(siteId) : uiText("本机采集器"));
}

function renderTopology() {
  if (!canManageTopology()) return;
  renderCollectorNodes();
  const devices = rooms.flatMap((room) => (room.devices || []).map((device) => ({ room, device })));
  const states = devices.map(({ device }) => topologyDeviceState(device));
  $("topologyRoomCount").textContent = String(rooms.length);
  $("topologyDeviceCount").textContent = String(devices.length);
  $("topologyOnlineCount").textContent = String(states.filter((state) => state.key === "online").length);
  $("topologyOfflineCount").textContent = String(states.filter((state) => state.key === "offline").length);
  $("topologyPendingCount").textContent = String(states.filter((state) => state.key === "pending").length);
  $("topologyRoomCapacity").textContent = `最多 ${Number(topologyCapabilities.max_cleanrooms || 200)} 个`;
  $("topologyDeviceCapacity").textContent = `最多 ${Number(topologyCapabilities.max_active_devices || 100)} 台`;
  $("addCleanroomBtn").disabled = rooms.length >= Number(topologyCapabilities.max_cleanrooms || 200);
  $("manualAddDeviceBtn").disabled = !rooms.length || devices.length >= Number(topologyCapabilities.max_active_devices || 100);

  $("topologyRoomList").innerHTML = rooms.length ? rooms.map((room, index) => {
    const roomDevices = room.devices || [];
    const roomStates = roomDevices.map((device) => topologyDeviceState(device));
    const onlineCount = roomStates.filter((state) => state.key === "online").length;
    return `<article class="panel topology-room-card">
      <header>
        <div class="room-card-identity"><span>${String(index + 1).padStart(2, "0")}</span><div><p class="eyebrow">车间</p><h3 data-i18n-ignore>${escapeHtml(room.name)}</h3><small>${roomDevices.length ? `${onlineCount}/${roomDevices.length} 台在线` : "尚未添加设备"}</small></div></div>
        <button class="secondary-action" type="button" data-add-device-room="${escapeHtml(room.id)}">＋ 添加设备</button>
      </header>
      <div class="topology-device-list">
        ${roomDevices.length ? roomDevices.map((device) => {
          const state = topologyDeviceState(device);
          return `<section class="topology-device-row">
            <div class="topology-device-name"><i class="topology-state-dot ${state.key}" aria-hidden="true"></i><div><strong data-i18n-ignore>${escapeHtml(device.name)}</strong><small>${escapeHtml(state.detail)}</small></div></div>
            <div><span>连接地址</span><strong>${escapeHtml(device.host)}:${Number(device.tcpPort || 502)}</strong><small>Slave ID ${Number(device.slave || 1)}</small></div>
            <div><span>负责采集器</span><strong data-i18n-ignore>${escapeHtml(topologySiteName(device.site_id))}</strong><small>${typeof equipmentSyncText === "function" ? escapeHtml(equipmentSyncText(device.id)) : "配置由采集器主动拉取"}</small></div>
            <div class="topology-device-actions"><b class="topology-status ${state.key}">${state.label}</b>${typeof equipmentActions === "function" ? equipmentActions(device.id) : ""}${topologyCapabilities.can_delete_devices ? `<button type="button" class="device-delete-action" data-delete-device="${escapeHtml(device.id)}">删除设备</button>` : ""}</div>
          </section>`;
        }).join("") : `<div class="topology-empty"><strong>这个车间还没有设备</strong><p>可以让 Windows 采集器扫描后上传，也可以由管理员手动登记固定 IP。</p><button type="button" data-add-device-room="${escapeHtml(room.id)}">手动添加第一台设备</button></div>`}
      </div>
    </article>`;
  }).join("") : `<div class="panel topology-empty standalone"><strong>还没有车间</strong><p>先创建第一个车间，再为它添加设备。</p><button type="button" data-create-cleanroom>新建车间</button></div>`;
}

function suggestedDiscoveredName(item) {
  const suffix = String(item.host || "").split(".").pop() || "新设备";
  return `DPC8001-${suffix}`;
}

function suggestedCollectorName(item) {
  const hostname = String(item.hostname || "").trim();
  return hostname && hostname.length <= 100 ? hostname : "Windows 采集器";
}

function renderPendingCollectors() {
  const inbox = $("pendingCollectorInbox");
  inbox.hidden = pendingCollectors.length === 0;
  $("pendingCollectorCount").textContent = String(pendingCollectors.length);
  if (!pendingCollectors.length) {
    $("pendingCollectorList").innerHTML = "";
    return;
  }
  $("pendingCollectorList").innerHTML = pendingCollectors.map((item, index) => (
    `<section class="discovered-device-row" data-pending-collector-index="${index}">
      <div class="discovered-device-evidence"><strong data-i18n-ignore>${escapeHtml(item.hostname || "Windows PC")}</strong><small data-i18n-ignore>${escapeHtml(item.platform)} · ${escapeHtml(uiText("最近报到"))} ${escapeHtml(uiText(relativeTime(item.last_seen_at)))}</small></div>
      <label><span>采集器名称</span><input data-pending-collector-name maxlength="100" value="${escapeHtml(suggestedCollectorName(item))}" /></label>
      <button class="primary" type="button" data-approve-pending-collector>加入并启用</button>
      <button class="secondary-action" type="button" data-reject-pending-collector>拒绝加入</button>
    </section>`
  )).join("");
}

async function loadPendingCollectors() {
  if (!canManageTopology()) return;
  const drafts = new Map();
  document.querySelectorAll("[data-pending-collector-index]").forEach((row) => {
    const item = pendingCollectors[Number(row.dataset.pendingCollectorIndex)];
    if (item) drafts.set(String(item.id), row.querySelector("[data-pending-collector-name]")?.value || "");
  });
  pendingCollectors = await api("/api/admin/pending-collectors");
  if (!Array.isArray(pendingCollectors)) pendingCollectors = [];
  renderPendingCollectors();
  document.querySelectorAll("[data-pending-collector-index]").forEach((row) => {
    const item = pendingCollectors[Number(row.dataset.pendingCollectorIndex)];
    const draft = item && drafts.get(String(item.id));
    if (draft) row.querySelector("[data-pending-collector-name]").value = draft;
  });
}

async function approvePendingCollector(row) {
  const item = pendingCollectors[Number(row.dataset.pendingCollectorIndex)];
  const name = row.querySelector("[data-pending-collector-name]").value.trim();
  const button = row.querySelector("[data-approve-pending-collector]");
  if (!item) return;
  if (!name) return void showToast("请填写采集器名称。", "error");
  button.disabled = true;
  button.textContent = "正在确认…";
  try {
    await api(`/api/admin/pending-collectors/${encodeURIComponent(item.id)}/approve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await Promise.all([loadPendingCollectors(), loadTopologySites(true)]);
    showToast(uiLocale() === "en-US"
      ? `Collector “${name}” is enabled. The on-site PC will start automatically.`
      : `采集器“${name}”已启用，现场电脑将自动开始工作。`);
  } catch (error) {
    button.disabled = false;
    button.textContent = "加入并启用";
    showToast(friendlyError(error.message), "error");
  }
}

async function rejectPendingCollector(row) {
  const item = pendingCollectors[Number(row.dataset.pendingCollectorIndex)];
  if (!item) return;
  const button = row.querySelector("[data-reject-pending-collector]");
  button.disabled = true;
  try {
    await api(`/api/admin/pending-collectors/${encodeURIComponent(item.id)}`, {
      method: "DELETE",
    });
    await loadPendingCollectors();
    showToast("已拒绝这台电脑的加入申请。");
  } catch (error) {
    button.disabled = false;
    showToast(friendlyError(error.message), "error");
  }
}

function renderDiscoveredDevices() {
  const inbox = $("discoveredInbox");
  inbox.hidden = discoveredDevices.length === 0;
  $("discoveredDeviceCount").textContent = String(discoveredDevices.length);
  if (!discoveredDevices.length) {
    $("discoveredDeviceList").innerHTML = "";
    return;
  }
  const roomOptions = rooms.map((room) => (
    `<option value="${escapeHtml(room.id)}" data-i18n-ignore>${escapeHtml(room.name)}</option>`
  )).join("");
  $("discoveredDeviceList").innerHTML = discoveredDevices.map((item, index) => (
    `<section class="discovered-device-row" data-discovered-index="${index}">
      <div class="discovered-device-evidence"><strong>${escapeHtml(item.host)}:${Number(item.tcp_port || 502)}</strong><small>Slave ID ${Number(item.slave || 1)} · ${escapeHtml(item.site_name || item.site_id)} · ${escapeHtml(item.particle_unit_label || "单位已验证")}</small></div>
      <label><span>所属车间</span><select data-discovered-room ${rooms.length ? "" : "disabled"}>${roomOptions || '<option value="">请先创建车间</option>'}</select></label>
      <label><span>设备名称</span><input data-discovered-name maxlength="100" value="${escapeHtml(suggestedDiscoveredName(item))}" /></label>
      <div class="discovered-ip-choice"><p>如果只是设备换了 IP，请更新原设备，不要重复新增。</p><button type="button" data-discovered-ip-change="${index}">这是已有设备换了 IP</button></div>
      <button class="primary" type="button" data-assign-discovered ${rooms.length ? "" : "disabled"}>保存并开始采集</button>
    </section>`
  )).join("");
}

async function loadDiscoveredDevices() {
  if (!canManageTopology()) return;
  const drafts = new Map();
  document.querySelectorAll("[data-discovered-index]").forEach((row) => {
    const item = discoveredDevices[Number(row.dataset.discoveredIndex)];
    if (!item) return;
    const key = `${item.site_id}|${item.host}|${item.tcp_port}|${item.slave}`;
    drafts.set(key, {
      name: row.querySelector("[data-discovered-name]")?.value || "",
      cleanroomId: row.querySelector("[data-discovered-room]")?.value || "",
    });
  });
  discoveredDevices = await api("/api/admin/discovered-devices");
  if (!Array.isArray(discoveredDevices)) discoveredDevices = [];
  renderDiscoveredDevices();
  document.querySelectorAll("[data-discovered-index]").forEach((row) => {
    const item = discoveredDevices[Number(row.dataset.discoveredIndex)];
    if (!item) return;
    const key = `${item.site_id}|${item.host}|${item.tcp_port}|${item.slave}`;
    const draft = drafts.get(key);
    if (!draft) return;
    const name = row.querySelector("[data-discovered-name]");
    const room = row.querySelector("[data-discovered-room]");
    if (name) name.value = draft.name;
    if (room && [...room.options].some((option) => option.value === draft.cleanroomId)) {
      room.value = draft.cleanroomId;
    }
  });
}

async function assignDiscoveredDevice(row) {
  const index = Number(row.dataset.discoveredIndex);
  const item = discoveredDevices[index];
  const name = row.querySelector("[data-discovered-name]").value.trim();
  const cleanroomId = row.querySelector("[data-discovered-room]").value;
  const button = row.querySelector("[data-assign-discovered]");
  if (!item || !cleanroomId) return void showToast("请先选择设备所属车间。", "error");
  if (!name) return void showToast("请填写设备名称。", "error");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const updated = await api("/api/admin/discovered-devices/assign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site_id: item.site_id, cleanroom_id: cleanroomId, name,
        host: item.host, tcp_port: item.tcp_port, slave: item.slave,
      }),
    });
    applyTopologyRooms(updated, cleanroomId);
    await loadDiscoveredDevices();
    showToast(`“${name}”已分配，采集器将自动开始读取。`);
  } catch (error) {
    showToast(friendlyError(error.message), "error");
    button.disabled = false;
    button.textContent = "保存并开始采集";
  }
}

function populateManualDeviceRooms(preferredRoomId = null) {
  $("manualDeviceRoomInput").innerHTML = rooms.map((room) => (
    `<option value="${escapeHtml(room.id)}" data-i18n-ignore>${escapeHtml(room.name)}</option>`
  )).join("");
  const requested = String(preferredRoomId || selectedRoom()?.id || rooms[0]?.id || "");
  $("manualDeviceRoomInput").value = rooms.some((room) => String(room.id) === requested)
    ? requested : String(rooms[0]?.id || "");
}

function suggestedManualDeviceName(roomId) {
  const room = rooms.find((item) => String(item.id) === String(roomId)) || rooms[0];
  const names = new Set((room?.devices || []).map((device) => String(device.name).toLowerCase()));
  let number = (room?.devices || []).length + 1;
  const prefix = uiLocale() === "en-US" ? "Particle Counter" : "尘埃粒子计数器";
  let name = `${prefix} ${String(number).padStart(2, "0")}`;
  while (names.has(name.toLowerCase())) {
    number += 1;
    name = `${prefix} ${String(number).padStart(2, "0")}`;
  }
  return name;
}

function resetCollectorDialog() {
  collectorActivation = null;
  $("collectorForm").reset();
  $("collectorFormError").textContent = "";
  $("collectorCreateStep").hidden = false;
  $("collectorActivationStep").hidden = true;
  $("collectorCloudUrl").textContent = "--";
  $("collectorSiteId").textContent = "--";
  $("collectorToken").textContent = "--";
  $("saveCollectorBtn").hidden = false;
  $("saveCollectorBtn").disabled = false;
  $("saveCollectorBtn").textContent = "创建并生成接入信息";
  $("cancelCollectorBtn").textContent = "取消";
}

function closeCollectorDialog() {
  $("collectorDialog").close();
  resetCollectorDialog();
}

function openCollectorDialog() {
  resetCollectorDialog();
  $("collectorDialog").showModal();
  setTimeout(() => $("newCollectorNameInput").focus(), 0);
}

async function createCollector(event) {
  event.preventDefault();
  const name = $("newCollectorNameInput").value.trim();
  if (!name) return void ($("collectorFormError").textContent = "请填写采集器名称。");
  const button = $("saveCollectorBtn");
  button.disabled = true;
  button.textContent = "正在创建…";
  try {
    const result = await api("/api/admin/collectors", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
    if (!result?.collector?.id) throw new Error("服务未返回完整接入信息");
    collectorActivation = {
      cloud_url: window.location.origin,
      site_id: result.collector.id,
    };
    $("collectorCloudUrl").textContent = collectorActivation.cloud_url;
    $("collectorSiteId").textContent = collectorActivation.site_id;
    $("collectorToken").textContent = "等待生成安装包";
    $("collectorCreateStep").hidden = true;
    $("collectorActivationStep").hidden = false;
    $("saveCollectorBtn").hidden = true;
    $("cancelCollectorBtn").textContent = "完成";
    $("collectorFormError").textContent = "";
    await loadTopologySites(true);
    downloadCollectorActivation().catch((error) => {
      $("collectorFormError").textContent = friendlyError(error.message);
    });
    setTimeout(() => $("copyCollectorActivationBtn").focus(), 0);
  } catch (error) {
    $("collectorFormError").textContent = friendlyError(error.message);
    button.disabled = false;
    button.textContent = "创建并生成接入信息";
  }
}

async function downloadCollectorActivation() {
  if (!collectorActivation) return;
  const button = $("copyCollectorActivationBtn");
  return downloadCollectorPackage(collectorActivation.site_id, button);
}

async function downloadReusableCollectorInstaller() {
  const button = $("addCollectorBtn");
  button.disabled = true;
  button.textContent = "正在生成安装包…";
  try {
    const response = await fetch("/api/admin/collector-installer", {
      method: "POST", cache: "no-store",
    });
    if (!response.ok) {
      let message = `安装包生成失败（${response.status}）`;
      try {
        const payload = await response.json();
        message = payload.detail || payload.error || message;
      } catch { /* Keep the status-based message. */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "HawkHive-Collector-Windows.zip";
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast("通用安装包已下载，可用于本客户的多台 Windows 电脑。");
  } finally {
    button.disabled = false;
    button.textContent = "下载通用 Windows 采集器";
  }
}

async function downloadCollectorPackage(siteId, button) {
  if (!siteId || !button) return;
  button.disabled = true;
  button.textContent = "正在生成安装包…";
  const response = await fetch(`/api/admin/collectors/${encodeURIComponent(siteId)}/package`, {
    method: "POST", cache: "no-store",
  });
  if (!response.ok) {
    let message = `安装包生成失败（${response.status}）`;
    try {
      const payload = await response.json();
      message = payload.detail || payload.error || message;
    } catch { /* Keep the status-based message. */ }
    button.disabled = false;
    button.textContent = "重新下载自动安装包";
    throw new Error(message);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "HawkHive-Collector-Windows.zip";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  button.disabled = false;
  button.textContent = "重新下载安装包";
  if (collectorActivation && String(collectorActivation.site_id) === String(siteId)) {
    $("collectorToken").textContent = "安装包已生成";
  }
  await loadTopologySites(true);
  showToast("客户专属 Windows 安装包已下载。");
}

function openCleanroomDialog() {
  $("cleanroomForm").reset();
  $("cleanroomFormError").textContent = "";
  $("cleanroomDialog").showModal();
  setTimeout(() => $("newCleanroomNameInput").focus(), 0);
}

async function openManualDeviceDialog(roomId = null) {
  $("manualDeviceForm").reset();
  $("manualDevicePortInput").value = "502";
  $("manualDeviceSlaveInput").value = "1";
  $("manualDeviceFormError").textContent = "";
  populateManualDeviceRooms(roomId);
  try {
    await loadTopologySites();
  } catch (error) {
    $("manualDeviceFormError").textContent = friendlyError(error.message);
  }
  $("manualDeviceNameInput").value = suggestedManualDeviceName($("manualDeviceRoomInput").value);
  $("manualDeviceDialog").showModal();
  setTimeout(() => $("manualDeviceHostInput").focus(), 0);
}

function applyTopologyRooms(updatedRooms, preferredRoomId = null) {
  rooms = Array.isArray(updatedRooms) ? updatedRooms : rooms;
  historyRooms = null;
  populateRoomSelectors(preferredRoomId);
  realtimeSelectedDeviceIds = new Set(realtimeScopeDevices().map(({ device }) => String(device.id)));
  historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
  renderRealtimeSeriesSelector();
  renderHistorySeriesSelector();
  renderTopology();
  updateExportLink();
}

function openDeleteDeviceDialog(deviceId) {
  if (!canManageTopology() || !topologyCapabilities.can_delete_devices || deviceDeleteBusy) return;
  const { room, device } = findDevice(deviceId);
  if (!device || !room) return;
  deviceToDelete = { id: String(device.id), roomId: String(room.id) };
  $("deleteDeviceName").textContent = device.name;
  $("deleteDeviceContext").textContent = `${room.name} · ${device.host}:${Number(device.tcpPort || 502)} · Slave ID ${Number(device.slave || 1)} · ${topologySiteName(device.site_id)}`;
  $("deleteDeviceFormError").textContent = "";
  $("deleteDeviceDialog").showModal();
  $("cancelDeleteDeviceBtn").focus();
}

function closeDeleteDeviceDialog() {
  if (deviceDeleteBusy) return;
  $("deleteDeviceDialog").close();
  deviceToDelete = null;
}

async function deleteDevice(event) {
  event.preventDefault();
  if (!deviceToDelete || deviceDeleteBusy || !canManageTopology()) return;
  const target = deviceToDelete;
  let committed = false;
  deviceDeleteBusy = true;
  $("deleteDeviceFormError").textContent = "";
  ["confirmDeleteDeviceBtn", "cancelDeleteDeviceBtn", "closeDeleteDeviceDialogBtn"].forEach((id) => { $(id).disabled = true; });
  $("confirmDeleteDeviceBtn").textContent = "正在删除…";
  try {
    const updated = await api(`/api/admin/devices/${encodeURIComponent(target.id)}`, {
      method: "DELETE", signal: AbortSignal.timeout(15000),
    });
    committed = true;
    delete latestByDevice[target.id];
    delete realtimeByDevice[target.id];
    alarmEvents = alarmEvents.filter((item) => String(item.device_id) !== target.id);
    applyTopologyRooms(updated, target.roomId);
    updateRealtimeDisplay();
    updateNavAlarmCount();
    $("deleteDeviceDialog").close();
    deviceToDelete = null;
    showToast("设备已删除，历史记录已保留。采集器联网后将自动更新配置。");
    // These are follow-up reads: failure must not report a committed deletion as failed.
    Promise.all([loadTopologySites(true), loadDiscoveredDevices()]).catch(() => {
      showToast("设备已删除，接入状态刷新失败，请刷新页面。", "error");
    });
  } catch (error) {
    if (committed || error.status === 401) {
      $("deleteDeviceDialog").close();
      deviceToDelete = null;
      showToast(committed ? "设备已删除，页面刷新失败，请重新加载页面。" : "登录已失效，请重新登录。", "error");
    } else {
      $("deleteDeviceFormError").textContent = ["TimeoutError", "AbortError"].includes(error.name)
        ? "请求超时，删除结果尚未确认。可重试，或关闭后刷新页面核对。" : friendlyError(error.message);
    }
  } finally {
    deviceDeleteBusy = false;
    ["confirmDeleteDeviceBtn", "cancelDeleteDeviceBtn", "closeDeleteDeviceDialogBtn"].forEach((id) => { $(id).disabled = false; });
    $("confirmDeleteDeviceBtn").textContent = "确认删除";
  }
}

async function createCleanroom(event) {
  event.preventDefault();
  const name = $("newCleanroomNameInput").value.trim();
  if (!name) {
    $("cleanroomFormError").textContent = "请填写车间名称。";
    return;
  }
  const button = $("saveCleanroomBtn");
  button.disabled = true;
  button.textContent = "正在创建…";
  try {
    const previousIds = new Set(rooms.map((room) => String(room.id)));
    const updated = await api("/api/admin/cleanrooms", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    });
    const created = (updated || []).find((room) => !previousIds.has(String(room.id)))
      || (updated || []).find((room) => room.name === name);
    applyTopologyRooms(updated, created?.id);
    $("cleanroomDialog").close();
    renderDiscoveredDevices();
    showToast(`已创建“${name}”，可为自动发现的设备选择这个车间。`);
  } catch (error) {
    $("cleanroomFormError").textContent = friendlyError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "创建并继续添加设备";
  }
}

async function createManualDevice(event) {
  event.preventDefault();
  const host = parsePrivateIPv4($("manualDeviceHostInput").value);
  const name = $("manualDeviceNameInput").value.trim();
  const tcpPort = Number($("manualDevicePortInput").value);
  const slave = Number($("manualDeviceSlaveInput").value);
  if (!name) return void ($("manualDeviceFormError").textContent = "请填写设备名称。");
  if (!host) return void ($("manualDeviceFormError").textContent = "请输入现场局域网内的私有 IPv4 地址。");
  if (!Number.isInteger(tcpPort) || tcpPort < 1 || tcpPort > 65535) return void ($("manualDeviceFormError").textContent = "TCP 端口必须在 1–65535 之间。");
  if (!Number.isInteger(slave) || slave < 1 || slave > 247) return void ($("manualDeviceFormError").textContent = "Slave ID 必须在 1–247 之间。");
  if (!topologySites.length) return void ($("manualDeviceFormError").textContent = "尚无可用的现场采集器。");
  const button = $("saveManualDeviceBtn");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const roomId = $("manualDeviceRoomInput").value;
    const updated = await api("/api/admin/devices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cleanroom_id: roomId, site_id: $("manualDeviceSiteInput").value,
        name, host, tcp_port: tcpPort, slave,
      }),
    });
    applyTopologyRooms(updated, roomId);
    $("manualDeviceDialog").close();
    showToast(`已登记“${name}”，等待现场采集器返回首条真实读数。`);
    await refreshLatest();
  } catch (error) {
    $("manualDeviceFormError").textContent = friendlyError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "保存并等待确认";
  }
}

function populateRoomSelectors(preferredRoomId = null) {
  const current = String(preferredRoomId || $("cleanroomInput").value || localStorage.getItem("selected-room") || rooms[0]?.id || "");
  const options = rooms.map((room) => `<option value="${escapeHtml(room.id)}" data-i18n-ignore>${escapeHtml(room.name)}</option>`).join("");
  $("cleanroomInput").innerHTML = options;
  $("manageRoomInput").innerHTML = options;
  $("cleanroomInput").value = rooms.some((room) => String(room.id) === current) ? current : String(rooms[0]?.id || "");
  $("manageRoomInput").value = $("cleanroomInput").value;
  populateDevices();
  renderRoomManager();
}

function populateDevices(preferredDeviceId = null) {
  const room = selectedRoom();
  const devices = room?.devices || [];
  const current = String(preferredDeviceId || $("deviceInput").value || localStorage.getItem(`selected-device:${room?.id}`) || devices[0]?.id || "");
  $("deviceInput").innerHTML = devices.length
    ? devices.map((device) => `<option value="${escapeHtml(device.id)}" data-i18n-ignore>${escapeHtml(device.name)}</option>`).join("")
    : '<option value="">尚未配置设备</option>';
  $("deviceInput").disabled = devices.length === 0;
  $("deviceInput").value = devices.some((device) => String(device.id) === current) ? current : String(devices[0]?.id || "");
  updateScopeTitle();
  updateRealtimeDisplay();
}

function updateScopeTitle() {
  const room = selectedRoom();
  const device = selectedDevice();
  const scopeTitle = $("scopeTitle");
  scopeTitle.toggleAttribute("data-i18n-ignore", Boolean(room));
  scopeTitle.textContent = room && device ? `${room.name} · ${device.name}` : room?.name || "尚未配置监控范围";
}

async function loadConfiguration(preferredRoomId = null) {
  rooms = await api("/api/config");
  historyRooms = null;
  if (!Array.isArray(rooms) || rooms.length === 0) throw new Error("当前账号尚未配置洁净室。请联系管理员完成初始化。");
  populateRoomSelectors(preferredRoomId);
  realtimeSelectedDeviceIds = new Set(realtimeScopeDevices().map(({ device }) => String(device.id)));
  historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
  renderRealtimeSeriesSelector();
  renderHistorySeriesSelector();
  updateExportLink();
}

function readingTimestamp(reading) {
  return asTimestamp(reading?.last_reading_at) || asTimestamp(reading?.timestamp);
}

function appendRealtime(reading) {
  if (!reading?.device_id || reading.online === false || reading.error) return;
  const timestamp = asTimestamp(reading.timestamp);
  if (!timestamp) return;
  const id = String(reading.device_id);
  const points = realtimeByDevice[id] || [];
  const index = points.findIndex((item) => asTimestamp(item.timestamp) === timestamp);
  if (index >= 0) points[index] = reading;
  else points.push(reading);
  realtimeByDevice[id] = points.sort((a, b) => Number(a.timestamp) - Number(b.timestamp)).slice(-180);
}

function activeDetails(reading) {
  return (reading?.alarm_details || []).filter((detail) => activeAlarmStates.has(detail.state));
}

function pendingDetails(reading) {
  return (reading?.alarm_details || []).filter((detail) => detail.state === "PENDING_ALARM");
}

function detailFor(reading, metric) {
  return (reading?.alarm_details || []).find((detail) => detail.metric === metric) || null;
}

function valueForKey(reading, key) {
  const raw = particleKeys.includes(key) ? reading?.particles?.[key] : reading?.environment?.[key];
  if (raw === null || raw === undefined || raw === "") return Number.NaN;
  return Number(raw);
}

function stateMeta(detail) {
  if (!detail) return { kind: "normal", label: "正常" };
  if (detail.state === "ALARM_ACTIVE") return { kind: "alarm", label: "报警中" };
  if (detail.state === "PENDING_CLEAR") return { kind: "alarm", label: "恢复确认中" };
  if (detail.state === "PENDING_ALARM") return { kind: "pending", label: "超限观察中" };
  return { kind: "normal", label: "正常" };
}

function translateLimit(limit) {
  return String(limit || "--")
    .replace(/^outside\s+/i, "超出 ")
    .replace(/particles\/ft³/gi, "particles/ft³");
}

function renderEnvironment(reading, key, metric, cardId, valueId, rangeId, stateId, deltaId, unit) {
  const limits = thresholdsFor();
  const min = Number(limits[`${key}_min`]);
  const max = Number(limits[`${key}_max`]);
  const value = valueForKey(reading, key);
  const detail = detailFor(reading, metric);
  const meta = stateMeta(detail);
  const online = Boolean(reading && reading.online !== false && !reading.error);
  const hasValue = Number.isFinite(value);
  const displayMeta = online && hasValue ? meta : { kind: "unknown", label: online ? "等待数据" : "数据不可用" };
  $(valueId).textContent = hasValue ? `${formatNumber(value, 1)} ${unit}` : "--";
  $(rangeId).textContent = Number.isFinite(min) && Number.isFinite(max) ? `正常范围 ${formatNumber(min, 1)}–${formatNumber(max, 1)} ${unit}` : "正常范围未配置";
  $(stateId).textContent = displayMeta.label;
  $(stateId).className = `metric-state ${displayMeta.kind === "normal" ? "" : displayMeta.kind}`;
  $(cardId).classList.toggle("alarm", meta.kind === "alarm" && online);
  $(cardId).classList.toggle("pending", meta.kind === "pending" && online);

  const points = realtimeByDevice[String(reading?.device_id)] || [];
  const previous = [...points].reverse().find((item) => Number(item.timestamp) < Number(reading?.timestamp) && Number.isFinite(valueForKey(item, key)));
  if (!hasValue || !previous) {
    $(deltaId).textContent = "暂无上次读数对比";
  } else {
    const delta = value - valueForKey(previous, key);
    const prefix = delta > 0 ? "+" : "";
    $(deltaId).textContent = `较上次 ${prefix}${formatNumber(delta, 1)} ${unit}`;
  }
}

function renderParticleCards(reading) {
  const thresholds = thresholdsFor();
  for (const channel of particleAlarmChannels) {
    const value = valueForKey(reading, channel.key);
    $(channel.key).textContent = Number.isFinite(value) ? formatNumber(value, 0) : "--";
    const card = $(channel.key).closest(".metric");
    card.classList.remove("alarm", "pending");
    const stateId = channel.key.replace("_um", "_state");
    const enabled = thresholdEnabled(thresholds[channel.enabledField]);
    const threshold = thresholdNumber(thresholds[channel.maxField]);
    if (!enabled) {
      $(stateId).textContent = "报警关闭";
      continue;
    }
    const detail = detailFor(reading, channel.metric);
    const meta = stateMeta(detail);
    const usable = Boolean(reading && reading.online !== false && !reading.error && Number.isFinite(value));
    if (usable && meta.kind !== "normal") card.classList.add(meta.kind);
    $(stateId).textContent = Number.isFinite(threshold)
      ? `${usable ? meta.label : "数据不可用"} · 阈值 ${formatNumber(threshold, 0)}`
      : "报警阈值未配置";
  }
}

function readingSyncStale(reading) {
  const timestamp = readingTimestamp(reading);
  return Boolean(reading && (reading.connection_state === "sync_stale" ||
    (!reading.error && timestamp && Date.now() / 1000 - timestamp > 45)));
}

function renderAlarmBanner(reading, online, stale, active, pending) {
  const banner = $("alarmBanner");
  banner.classList.remove("pending");
  let visible = true;
  if (!reading) {
    $("alarmBannerKicker").textContent = "需要检查";
    $("alarmBannerTitle").textContent = "设备还没有返回数据";
    $("alarmBannerDetail").textContent = "请在现场采集器确认设备地址、网络和采集服务。";
    $("openAlarmBtn").textContent = "查看设备列表";
  } else if (readingSyncStale(reading)) {
    banner.classList.add("pending");
    $("alarmBannerKicker").textContent = "需要检查";
    $("alarmBannerTitle").textContent = "数据更新已超时";
    $("alarmBannerDetail").textContent = "显示的是最近一次读数；云端尚未收到新数据，不能据此认定设备离线。";
    $("openAlarmBtn").textContent = "查看设备列表";
  } else if (!online) {
    $("alarmBannerKicker").textContent = "需要处理";
    $("alarmBannerTitle").textContent = "设备连接已中断";
    $("alarmBannerDetail").textContent = reading.error || `最近一次有效读数：${formatTime(readingTimestamp(reading))}`;
    $("openAlarmBtn").textContent = "查看设备列表";
  } else if (reading.source !== "device") {
    $("alarmBannerKicker").textContent = "数据不可用于现场判断";
    $("alarmBannerTitle").textContent = "当前显示的是模拟数据";
    $("alarmBannerDetail").textContent = "请切换到设备模式并确认真实设备数据后再用于监测。";
    $("openAlarmBtn").textContent = "查看报警中心";
  } else if (active.length) {
    const first = active[0];
    $("alarmBannerKicker").textContent = "需要处理";
    $("alarmBannerTitle").textContent = `${metricLabels[first.metric] || first.metric}正在报警`;
    $("alarmBannerDetail").textContent = `当前 ${formatNumber(first.value, 1)}，判定条件：${translateLimit(first.limit)}；已持续 ${formatDuration(first.active_since || first.pending_since)}。`;
    $("openAlarmBtn").textContent = "查看报警详情";
  } else if (pending.length) {
    banner.classList.add("pending");
    const first = pending[0];
    $("alarmBannerKicker").textContent = "待观察";
    $("alarmBannerTitle").textContent = `${metricLabels[first.metric] || first.metric}刚刚超限`;
    $("alarmBannerDetail").textContent = `若持续超限满 5 分钟将进入报警；目前已持续 ${formatDuration(first.pending_since)}。`;
    $("openAlarmBtn").textContent = "查看待观察项";
  } else if (stale) {
    banner.classList.add("pending");
    $("alarmBannerKicker").textContent = "需要检查";
    $("alarmBannerTitle").textContent = "数据更新已超时";
    $("alarmBannerDetail").textContent = `最后一次读数在 ${relativeTime(readingTimestamp(reading))}，请确认 Wi-Fi 与采集服务。`;
    $("openAlarmBtn").textContent = "查看设备列表";
  } else {
    visible = false;
  }
  banner.hidden = !visible;
}

function overallState(reading) {
  if (!reading) return { kind: "danger", label: "尚无数据", message: "设备还没有返回任何读数。" };
  const online = reading.online !== false && !reading.error;
  const timestamp = readingTimestamp(reading);
  const stale = !timestamp || Date.now() / 1000 - timestamp > 45;
  if (readingSyncStale(reading)) return { kind: "warning", label: "更新超时", message: "云端数据更新超时，设备连接状态待确认。" };
  if (!online) return { kind: "danger", label: "设备离线", message: reading.error || "设备连接已中断。" };
  if (reading.source !== "device") return { kind: "simulated", label: "模拟数据", message: "当前数据不是来自真实设备。" };
  if (activeDetails(reading).length) return { kind: "danger", label: "需要处理", message: "发现活动报警。" };
  if (pendingDetails(reading).length) return { kind: "warning", label: "待观察", message: "有指标刚刚超限，尚在确认时间内。" };
  if (stale) return { kind: "warning", label: "更新超时", message: "设备在线，但数据超过 45 秒未更新。" };
  return { kind: "normal", label: "状态正常", message: "设备在线，真实数据持续更新，当前无报警。" };
}

function updateRealtimeDisplay() {
  updateScopeTitle();
  const device = selectedDevice();
  const reading = device ? latestByDevice[String(device.id)] : null;
  const timestamp = readingTimestamp(reading);
  const online = Boolean(reading && reading.online !== false && !reading.error);
  const stale = Boolean(timestamp && Date.now() / 1000 - timestamp > 45);
  const active = activeDetails(reading);
  const pending = pendingDetails(reading);
  let state = overallState(reading);
  if (device?.maintenance?.until > Date.now()/1000 && state.kind === "normal") state = {kind:"warning",label:"维护中",message:"继续记录数据和报警，仅暂停云端电邮通知。"};
  if (typeof renderMaintenanceNotice === "function") renderMaintenanceNotice(device, reading);

  $("particleUnitText").textContent = displayParticleUnit(reading ? [reading] : []);

  $("overallState").className = `overall-state ${state.kind}`;
  $("overallStateLabel").textContent = state.label;
  $("scopeFreshness").textContent = timestamp ? relativeTime(timestamp) : "尚无数据";
  $("scopeDot").className = `state-dot ${!timestamp || stale ? "warning" : online ? "ok" : "error"}`;
  $("lastUpdate").textContent = timestamp ? `有效读数：${formatTime(timestamp)}` : "尚无有效读数";

  const syncStale = readingSyncStale(reading);
  $("deviceConnectionState").textContent = syncStale ? "状态待确认" : online ? "在线" : reading ? "离线" : "无数据";
  $("deviceConnectionDetail").textContent = syncStale ? "云端数据更新超时，设备连接状态待确认。" : online ? `${device?.name || "设备"}连接正常` : reading?.error || (reading ? "已有历史读数，当前连接尚未确认。" : "等待设备首次连接");
  setHealth("deviceConnectionState", syncStale ? "warning" : online ? "ok" : "error");

  $("readingFreshness").textContent = timestamp ? relativeTime(timestamp) : "等待中";
  $("readingFreshnessDetail").textContent = timestamp ? formatTime(timestamp) : "尚无有效时间戳";
  setHealth("readingFreshness", !timestamp || stale ? "warning" : "ok");

  const sourceLabel = reading?.source === "device" ? "真实设备" : reading?.source === "demo" ? "模拟数据" : "未知";
  $("readingSource").textContent = sourceLabel;
  $("readingSourceDetail").textContent = reading?.source === "device" ? "来源标记：device" : reading?.source === "demo" ? "不可用于现场判断" : "尚未确认数据来源";
  setHealth("readingSource", reading?.source === "device" ? "ok" : "error");

  $("activeAlarmCount").textContent = `${active.length} 项`;
  $("activeAlarmDetail").textContent = active.length ? `${metricLabels[active[0].metric] || active[0].metric}需要处理` : pending.length ? `${pending.length} 项正在观察` : "当前无活动报警";
  setHealth("activeAlarmCount", active.length ? "error" : pending.length ? "warning" : "ok");

  renderAlarmBanner(reading, online, stale, active, pending);
  renderEnvironment(reading, "temperature", "temperature", "temperatureCard", "temperatureValue", "temperatureRange", "temperatureState", "temperatureDelta", "°C");
  renderEnvironment(reading, "humidity", "humidity", "humidityCard", "humidityValue", "humidityRange", "humidityState", "humidityDelta", "%RH");
  renderParticleCards(reading);
  renderRoomDevices();
  drawRealtimeTrend();
  setStatus(state.kind === "normal" ? "ok" : state.kind === "warning" ? "warning" : "error", state.label, state.message);
}

function renderRoomDevices() {
  const room = selectedRoom();
  const devices = room?.devices || [];
  const selectedId = String(selectedDevice()?.id || "");
  const onlineCount = devices.filter((device) => {
    const reading = latestByDevice[String(device.id)];
    return reading && !readingSyncStale(reading) && reading.online !== false && !reading.error;
  }).length;
  $("roomOnlineSummary").textContent = `${onlineCount}/${devices.length} 在线`;
  $("roomDeviceList").innerHTML = devices.length ? devices.map((device) => {
    const reading = latestByDevice[String(device.id)];
    const online = Boolean(reading && reading.online !== false && !reading.error);
    const hasAlarm = activeDetails(reading).length > 0;
    const pending = pendingDetails(reading).length > 0;
    const syncStale = readingSyncStale(reading);
    const stateLabel = syncStale ? "更新超时" : !online ? "离线" : hasAlarm ? "报警中" : pending ? "待观察" : "在线";
    const stateClass = syncStale ? "warning" : !online ? "offline" : hasAlarm || pending ? "warning" : "";
    return `<article class="current-device-card ${String(device.id) === selectedId ? "selected" : ""}" data-select-device="${escapeHtml(device.id)}" tabindex="0" role="button" aria-label="查看 ${escapeHtml(device.name)}">
      <header><strong data-i18n-ignore>${escapeHtml(device.name)}</strong><span class="device-state ${stateClass}">${stateLabel}</span></header>
      <p>${readingTimestamp(reading) ? `更新于${relativeTime(readingTimestamp(reading))}` : "尚无读数"}${reading?.source === "demo" ? " · 模拟数据" : ""}</p>
      <div class="device-reading-grid">
        <span><small>0.5 µm</small><strong>${formatNumber(valueForKey(reading, "pm_0_5_um"), 0)}</strong></span>
        <span><small>温度</small><strong>${Number.isFinite(valueForKey(reading, "temperature")) ? `${formatNumber(valueForKey(reading, "temperature"), 1)} °C` : "--"}</strong></span>
        <span><small>湿度</small><strong>${Number.isFinite(valueForKey(reading, "humidity")) ? `${formatNumber(valueForKey(reading, "humidity"), 1)} %` : "--"}</strong></span>
      </div>
    </article>`;
  }).join("") : '<div class="empty-state">该洁净室尚未配置设备。</div>';
}

function updateNavAlarmCount() {
  const room = selectedRoom();
  const count = (room?.devices || []).reduce((total, device) => total + activeDetails(latestByDevice[String(device.id)]).length, 0);
  $("navAlarmCount").textContent = String(count);
  $("navAlarmCount").hidden = count === 0;
}

async function refreshLatest() {
  if ($("topologyView").classList.contains("active") && canManageTopology()) {
    Promise.all([loadTopologySites(true), loadPendingCollectors(), loadDiscoveredDevices()]).catch(() => {});
  }
  if (!rooms.length) return;
  try {
    const readings = await api("/api/latest");
    for (const reading of readings || []) {
      if (!findDevice(reading.device_id).device) continue;
      latestByDevice[String(reading.device_id)] = reading;
      if (Object.prototype.hasOwnProperty.call(reading, "maintenance")) findDevice(reading.device_id).device.maintenance = reading.maintenance;
      appendRealtime(reading);
    }
    updateRealtimeDisplay();
    updateNavAlarmCount();
    if ($("topologyView").classList.contains("active")) {
      renderTopology();
    }
  } catch (error) {
    setStatus("error", "连接失败", friendlyError(error.message));
    $("scopeFreshness").textContent = "连接失败";
    $("scopeDot").className = "state-dot error";
  }
}

async function seedRealtimeHistory() {
  if (!rooms.length) return;
  const end = Math.floor(Date.now() / 1000);
  const start = end - 3600;
  try {
    const rows = await api(`/api/trends?max_points=180&start=${start}&end=${end}`);
    for (const row of rows || []) appendRealtime({ ...row, online: true });
    drawRealtimeTrend();
  } catch {
    // 实时读数仍可正常使用，历史预热失败不阻断工作台。
  }
}

function metricThresholdLines(items, key) {
  const roomIds = new Set(items.map(({ room }) => String(room.id)));
  if (roomIds.size !== 1) return [];
  const thresholds = items[0]?.room?.thresholds || {};
  const particleChannel = particleAlarmByKey.get(key);
  if (particleChannel && thresholdEnabled(thresholds[particleChannel.enabledField])) {
    const value = thresholdNumber(thresholds[particleChannel.maxField]);
    return Number.isFinite(value) ? [{ value, label: `${particleChannel.label} 报警上限` }] : [];
  }
  if (key === "temperature") {
    return [
      { value: Number(thresholds.temperature_min), label: "温度下限" },
      { value: Number(thresholds.temperature_max), label: "温度上限" },
    ].filter((item) => Number.isFinite(item.value));
  }
  if (key === "humidity") {
    return [
      { value: Number(thresholds.humidity_min), label: "湿度下限" },
      { value: Number(thresholds.humidity_max), label: "湿度上限" },
    ].filter((item) => Number.isFinite(item.value));
  }
  return [];
}

function chartSeries(items, selectedIds, rowsForDevice, scopeValue) {
  return items
    .filter(({ device }) => selectedIds.has(String(device.id)))
    .map(({ room, device }) => ({
      id: String(device.id),
      name: seriesName(room, device, scopeValue),
      color: deviceColor(device.id),
      rows: rowsForDevice(String(device.id)),
      room,
      device,
    }));
}

// Inspired by the source package's sampling-point inspector; use actual units,
// textContent, and keyboard navigation for the current multi-device workbench.
function bindChartInspector(canvas, points) {
  canvas._inspectionPoints = points.slice().sort((a, b) => a.timestamp - b.timestamp || a.name.localeCompare(b.name));
  canvas._inspectionIndex = 0;
  if (!canvas._inspector) {
    const tooltip = document.createElement("div");
    tooltip.id = `${canvas.id}-point-detail`;
    tooltip.className = "chart-point-tooltip";
    tooltip.setAttribute("role", "status");
    canvas.parentElement.appendChild(tooltip);
    canvas.setAttribute("aria-describedby", tooltip.id);
    canvas.tabIndex = 0;
    const hide = () => { tooltip.hidden = true; };
    const show = (point) => {
      if (!point) return hide();
      tooltip.textContent = `${point.name}\n${new Date(point.timestamp * 1000).toLocaleString(uiLocale(), { hour12: false })}\n${uiText(channelLabels[point.key])}: ${formatNumber(point.value, particleKeys.includes(point.key) ? 0 : 1)} ${point.unit}`;
      tooltip.hidden = false;
      const left = canvas.offsetLeft + point.x + 12;
      tooltip.style.left = `${Math.max(8, Math.min(left, canvas.parentElement.clientWidth - tooltip.offsetWidth - 8))}px`;
      tooltip.style.top = `${Math.max(8, canvas.offsetTop + point.y - tooltip.offsetHeight - 12)}px`;
    };
    const inspectPointer = (event) => {
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      let nearest = null, distance = 48;
      for (const point of canvas._inspectionPoints) {
        const next = Math.hypot(point.x - x, point.y - y);
        if (next < distance) { nearest = point; distance = next; }
      }
      show(nearest);
    };
    canvas.addEventListener("pointermove", inspectPointer);
    canvas.addEventListener("click", inspectPointer);
    canvas.addEventListener("pointerleave", hide);
    canvas.addEventListener("blur", hide);
    canvas.addEventListener("focus", () => show(canvas._inspectionPoints[0]));
    canvas.addEventListener("keydown", (event) => {
      if (event.key === "Escape") return hide();
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const last = canvas._inspectionPoints.length - 1;
      canvas._inspectionIndex = event.key === "Home" ? 0 : event.key === "End" ? last : Math.max(0, Math.min(last, canvas._inspectionIndex + (event.key === "ArrowRight" ? 1 : -1)));
      show(canvas._inspectionPoints[canvas._inspectionIndex]);
    });
    canvas._inspector = { hide, show };
  }
  canvas._inspector.hide();
  if (document.activeElement === canvas) canvas._inspector.show(canvas._inspectionPoints[0]);
}

function drawChart(canvas, series, key, thresholdLines, summaryElement) {
  if (!canvas || !summaryElement) return;
  canvas.setAttribute("aria-label", uiText(`${series?.length || 0} 台设备的${channelLabels[key]}趋势`));
  const prepared = (series || []).map((item) => ({
    ...item,
    points: (item.rows || [])
      .map((reading) => ({ timestamp: asTimestamp(reading.timestamp), value: valueForKey(reading, key) }))
      .filter((point) => point.timestamp && Number.isFinite(point.value))
      .sort((a, b) => a.timestamp - b.timestamp),
  })).filter((item) => item.points.length);
  const points = prepared.flatMap((item) => item.points);
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 20) return;
  const cssWidth = Math.max(280, Math.floor(rect.width));
  const cssHeight = Math.max(210, Math.floor(rect.height || 260));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(cssWidth * dpr);
  canvas.height = Math.floor(cssHeight * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  bindChartInspector(canvas, []);
  if (!points.length) {
    ctx.fillStyle = "#71858d";
    ctx.font = '13px Aptos, "PingFang SC", sans-serif';
    ctx.textAlign = "center";
    ctx.fillText(uiText("当前范围内没有可绘制的数据"), cssWidth / 2, cssHeight / 2);
    summaryElement.textContent = series?.length
      ? `当前范围内没有 ${channelLabels[key]} 数据。`
      : "请至少选择一台设备。";
    return;
  }

  const padding = { top: 20, right: 18, bottom: 36, left: 62 };
  const graphWidth = cssWidth - padding.left - padding.right;
  const graphHeight = cssHeight - padding.top - padding.bottom;
  const values = points.map((point) => point.value);
  const thresholdValues = (thresholdLines || []).map((item) => Number(item.value)).filter(Number.isFinite);
  const zeroBaseline = particleKeys.includes(key);
  let minValue = zeroBaseline ? 0 : Math.min(...values, ...thresholdValues);
  let maxValue = Math.max(...values, ...thresholdValues, zeroBaseline ? 1 : -Infinity);
  const rawSpan = Math.max(0.5, maxValue - minValue);
  if (!zeroBaseline) minValue -= rawSpan * 0.12;
  maxValue += rawSpan * 0.12;
  if (key === "humidity") {
    minValue = Math.max(0, minValue);
    maxValue = Math.min(100, Math.max(minValue + 1, maxValue));
  }
  const xMin = Math.min(...points.map((point) => point.timestamp));
  const xMax = Math.max(...points.map((point) => point.timestamp));
  const xSpan = Math.max(1, xMax - xMin);
  const xAt = (timestamp) => padding.left + ((timestamp - xMin) / xSpan) * graphWidth;
  const yAt = (value) => padding.top + graphHeight - ((value - minValue) / (maxValue - minValue)) * graphHeight;

  ctx.lineWidth = 1;
  ctx.font = '11px Aptos, "PingFang SC", sans-serif';
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const axisDecimals = particleKeys.includes(key) ? 0 : 1;
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const y = padding.top + graphHeight * ratio;
    const labelValue = maxValue - (maxValue - minValue) * ratio;
    ctx.strokeStyle = "#e3eaec";
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(cssWidth - padding.right, y);
    ctx.stroke();
    ctx.fillStyle = "#71858d";
    ctx.fillText(formatNumber(labelValue, axisDecimals), padding.left - 9, y);
  }

  (thresholdLines || []).forEach((threshold, index) => {
    const y = yAt(Number(threshold.value));
    ctx.save();
    ctx.setLineDash([6, 5]);
    ctx.strokeStyle = index % 2 ? "#b36b1d" : "#b52e43";
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(cssWidth - padding.right, y);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = index % 2 ? "#8b5518" : "#8e2032";
    ctx.textAlign = "left";
    ctx.fillText(`${uiText(threshold.label)} ${formatNumber(threshold.value, particleKeys.includes(key) ? 0 : 1)}`, padding.left + 7, Math.max(10, y - 10 - index * 12));
  });

  const inspectionPoints = [];
  prepared.forEach((item) => {
    const unit = key === "temperature" ? "°C" : key === "humidity" ? "%RH" : displayParticleUnit(item.rows);
    const gaps = item.points.slice(1).map((point, index) => point.timestamp - item.points[index].timestamp).filter((gap) => gap > 0).sort((a, b) => a - b);
    const typicalGap = gaps.length ? gaps[Math.floor(gaps.length / 2)] : Infinity;
    const breakAfter = Number.isFinite(typicalGap) ? Math.max(90, typicalGap * 3) : Infinity;
    ctx.strokeStyle = item.color;
    ctx.lineWidth = prepared.length > 12 ? 1.45 : 2.15;
    ctx.globalAlpha = prepared.length > 12 ? 0.72 : 0.9;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    item.points.forEach((point, index) => {
      inspectionPoints.push({ ...point, x: xAt(point.timestamp), y: yAt(point.value), key, unit, name: item.name });
      const shouldBreak = index === 0 || point.timestamp - item.points[index - 1].timestamp > breakAfter;
      if (shouldBreak) ctx.moveTo(xAt(point.timestamp), yAt(point.value));
      else ctx.lineTo(xAt(point.timestamp), yAt(point.value));
    });
    ctx.stroke();
    const latest = item.points[item.points.length - 1];
    ctx.globalAlpha = 1;
    ctx.fillStyle = item.color;
    ctx.beginPath();
    ctx.arc(xAt(latest.timestamp), yAt(latest.value), prepared.length > 12 ? 2 : 3, 0, Math.PI * 2);
    ctx.fill();
  });

  bindChartInspector(canvas, inspectionPoints);
  const xLabels = xMin === xMax ? [xMin] : [xMin, xMin + xSpan / 2, xMax];
  ctx.fillStyle = "#71858d";
  ctx.textBaseline = "top";
  xLabels.forEach((timestamp, index) => {
    ctx.textAlign = index === 0 ? "left" : index === 2 ? "right" : "center";
    const date = new Date(timestamp * 1000);
    ctx.fillText(date.toLocaleString(uiLocale(), { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", ...(xSpan < 300 ? { second: "2-digit" } : {}), hour12: false }), padding.left + graphWidth * (index / 2), cssHeight - 25);
  });

  const min = Math.min(...values);
  const max = Math.max(...values);
  const decimals = particleKeys.includes(key) ? 0 : 1;
  const roomCount = new Set(prepared.map((item) => String(item.room.id))).size;
  const thresholdNote = roomCount > 1 ? "；跨车间时各自阈值不合并显示" : "";
  summaryElement.textContent = `${prepared.length} 台设备 · ${points.length} 个采样点 · 最低 ${formatNumber(min, decimals)} · 最高 ${formatNumber(max, decimals)}${thresholdNote}`;
  canvas.setAttribute("aria-label", uiText(`${prepared.length} 台设备的${channelLabels[key]}趋势`));
}

function drawRealtimeTrend() {
  const scopeValue = $("realtimeScopeInput").value;
  const items = realtimeScopeDevices();
  const selectedItems = items.filter(({ device }) => realtimeSelectedDeviceIds.has(String(device.id)));
  const series = chartSeries(items, realtimeSelectedDeviceIds, (deviceId) => realtimeByDevice[deviceId] || [], scopeValue);
  const key = $("chartChannel").value;
  const selectedRows = series.flatMap((item) => item.rows || []);
  $("realtimeParticleUnit").textContent = displayParticleUnit(selectedRows);
  $("roomTrendTitle").textContent = scopeValue === "all" ? "全部车间设备趋势" : `${selectedRoom()?.name || "当前车间"}设备趋势`;
  $("particleTrendTitle").textContent = `${channelLabels[key]} 粒子趋势`;
  drawChart($("trendCanvas"), series, key, metricThresholdLines(selectedItems, key), $("trendSummary"));
  drawChart($("temperatureTrendCanvas"), series, "temperature", metricThresholdLines(selectedItems, "temperature"), $("temperatureTrendSummary"));
  drawChart($("humidityTrendCanvas"), series, "humidity", metricThresholdLines(selectedItems, "humidity"), $("humidityTrendSummary"));
}

function setHistoryRange(hours, refresh = true) {
  const end = new Date();
  const start = new Date(end.getTime() - Number(hours) * 3600 * 1000);
  $("historyStart").value = localInputValue(start);
  $("historyEnd").value = localInputValue(end);
  document.querySelectorAll("[data-range-hours]").forEach((button) => button.classList.toggle("active", Number(button.dataset.rangeHours) === Number(hours)));
  $("historyRangeText").textContent = Number(hours) === 1 ? "最近 1 小时" : Number(hours) === 24 ? "最近 24 小时" : "最近 7 天";
  updateExportLink();
  if (refresh) refreshHistory();
}

function historyParams() {
  const start = $("historyStart").value ? new Date($("historyStart").value).getTime() / 1000 : null;
  const endValue = $("historyEnd").value;
  // The picker displays minutes; include the complete selected end minute.
  const end = endValue ? new Date(endValue).getTime() / 1000 + (endValue.length === 16 ? 59.999 : 0) : null;
  return { start, end };
}

function updateExportLink() {
  const { start, end } = historyParams();
  const link = $("exportExcelLink");
  if (!historySelectedDeviceIds.size) {
    link.href = "#";
    link.setAttribute("aria-disabled", "true");
    link.classList.add("disabled");
    return;
  }
  const params = new URLSearchParams({ limit: "5000" });
  params.set("locale", uiLocale());
  if (start) params.set("start", String(start));
  if (end) params.set("end", String(end));
  if ($("historyScopeInput").value === "room" && selectedRoom()) params.set("cleanroom_ids", String(selectedRoom().id));
  if (historySelectedDeviceIds.size) params.set("device_ids", [...historySelectedDeviceIds].join(","));
  link.href = `/api/history/export-xlsx?${params.toString()}`;
  link.removeAttribute("aria-disabled");
  link.classList.remove("disabled");
}

function visibleHistory() {
  return storedHistory.filter((row) => historySelectedDeviceIds.has(String(row.device_id)));
}

function visibleTrendHistory() {
  return storedTrendHistory.filter((row) => historySelectedDeviceIds.has(String(row.device_id)));
}

function historyScopeParams() {
  const params = new URLSearchParams();
  if ($("historyScopeInput").value === "room" && selectedRoom()) {
    params.set("cleanroom_ids", String(selectedRoom().id));
  }
  if (historySelectedDeviceIds.size) {
    params.set("device_ids", [...historySelectedDeviceIds].join(","));
  }
  return params;
}

async function refreshHistory() {
  const room = selectedRoom();
  if (!room) return;
  try {
    const previousItems = historyScopeDevices();
    const selectedAll = historyRooms === null || (previousItems.length > 0
      && previousItems.every(({ device }) => historySelectedDeviceIds.has(String(device.id))));
    const updated = await api("/api/config?include_disabled=true");
    if (!Array.isArray(updated)) throw new Error("历史设备列表加载失败，请重试。");
    historyRooms = updated;
    if (selectedAll) historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
    renderHistorySeriesSelector();
    updateExportLink();
  } catch (error) {
    showToast(friendlyError(error.message), "error");
    return;
  }
  if (!historySelectedDeviceIds.size) {
    storedHistory = [];
    storedTrendHistory = [];
    renderHistory();
    showToast("请至少选择一台设备后再查询。", "error");
    return;
  }
  const { start, end } = historyParams();
  if (!start || !end || start >= end) {
    showToast("请选择有效的开始和结束时间。", "error");
    return;
  }
  const button = $("refreshHistoryBtn");
  button.disabled = true;
  button.textContent = "查询中…";
  $("historyView").setAttribute("aria-busy", "true");
  try {
    const scopeParams = historyScopeParams();
    scopeParams.set("start", String(start));
    scopeParams.set("end", String(end));
    const tableParams = new URLSearchParams(scopeParams);
    tableParams.set("limit", "5000");
    const trendParams = new URLSearchParams(scopeParams);
    trendParams.set("max_points", "320");
    [storedHistory, storedTrendHistory] = await Promise.all([
      api(`/api/history?${tableParams.toString()}`),
      api(`/api/trends?${trendParams.toString()}`),
    ]);
    historyPage = 1;
    const activePreset = document.querySelector("[data-range-hours].active");
    if (!activePreset) $("historyRangeText").textContent = `${formatShortTime(start)} 至 ${formatShortTime(end)}`;
    renderHistory();
  } catch (error) {
    showToast(`历史数据查询失败：${friendlyError(error.message)}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "查询";
    $("historyView").removeAttribute("aria-busy");
  }
}

function historyStatus(row) {
  if (row.source === "demo") return { label: "模拟数据", className: "alarm" };
  if (row.alarm_status === "ALARM_ACTIVE") return { label: "报警", className: "alarm" };
  if (row.alarm_status === "PENDING") return { label: "待观察", className: "pending" };
  return { label: "正常", className: "" };
}

function renderHistory() {
  const rows = visibleHistory();
  const pageSize = 50;
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  historyPage = Math.min(Math.max(1, historyPage), pageCount);
  const pageRows = rows.slice((historyPage - 1) * pageSize, historyPage * pageSize);
  $("historyRecordCount").textContent = formatNumber(rows.length, 0);
  $("historyDeviceCount").textContent = String(historySelectedDeviceIds.size);
  $("historyAlarmCount").textContent = formatNumber(rows.filter((row) => row.alarm_status === "ALARM_ACTIVE").length, 0);
  $("historyTableUnit").textContent = `颗粒单位：${displayParticleUnit(rows)}`;
  $("historyTable").innerHTML = pageRows.length ? pageRows.map((row) => {
    const status = historyStatus(row);
    const particleCells = particleKeys.map((key) => `<td>${formatNumber(row.particles?.[key], 0)}</td>`).join("");
    return `<tr>
      <td>${escapeHtml(formatTime(row.timestamp))}</td>
      <td data-i18n-ignore>${escapeHtml(row.cleanroom || findDevice(row.device_id).room?.name || "--")}</td>
      <td data-i18n-ignore>${escapeHtml(row.device || findDevice(row.device_id).device?.name || "--")}</td>
      ${particleCells}
      <td>${formatNumber(row.environment?.temperature, 1)}</td>
      <td>${formatNumber(row.environment?.humidity, 1)}</td>
      <td><span class="status-pill ${status.className}">${status.label}</span>${row.maintenance ? `<small class="maintenance-badge" title="${escapeHtml(row.maintenance.reason)}">${uiText("维护期间")}</small>` : ""}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="12"><div class="empty-state">当前所选设备和时间范围内没有监测记录。</div></td></tr>';
  $("historyPageLabel").textContent = `第 ${historyPage} / ${pageCount} 页`;
  $("historyPrevBtn").disabled = historyPage <= 1;
  $("historyNextBtn").disabled = historyPage >= pageCount;
  updateExportLink();
  drawHistoryTrend();
}

function drawHistoryTrend() {
  const scopeValue = $("historyScopeInput").value;
  const items = historyScopeDevices();
  const selectedItems = items.filter(({ device }) => historySelectedDeviceIds.has(String(device.id)));
  const rowsByDevice = new Map();
  for (const row of visibleTrendHistory()) {
    const id = String(row.device_id);
    if (!rowsByDevice.has(id)) rowsByDevice.set(id, []);
    rowsByDevice.get(id).push(row);
  }
  const series = chartSeries(items, historySelectedDeviceIds, (deviceId) => rowsByDevice.get(deviceId) || [], scopeValue);
  const key = $("historyChannel").value;
  $("historyParticleUnit").textContent = displayParticleUnit(series.flatMap((item) => item.rows || []));
  $("historyParticleTrendTitle").textContent = `${channelLabels[key]} 粒子趋势`;
  drawChart($("historyCanvas"), series, key, metricThresholdLines(selectedItems, key), $("historyTrendSummary"));
  drawChart($("historyTemperatureCanvas"), series, "temperature", metricThresholdLines(selectedItems, "temperature"), $("historyTemperatureSummary"));
  drawChart($("historyHumidityCanvas"), series, "humidity", metricThresholdLines(selectedItems, "humidity"), $("historyHumiditySummary"));
}

function alarmViewItems() {
  const deviceId = String(selectedDevice()?.id || "");
  const persisted = (alarmEvents || [])
    .filter((event) => !deviceId || String(event.device_id) === deviceId)
    .map((event) => ({ ...event, uiState: event.ended_at ? "cleared" : "active" }));
  const persistedOpenKeys = new Set(persisted.filter((event) => !event.ended_at).map((event) => `${event.device_id}:${event.metric}`));
  const current = deviceId ? [latestByDevice[deviceId]].filter(Boolean) : Object.values(latestByDevice);
  const pending = [];
  const currentActive = [];
  for (const reading of current) {
    for (const detail of reading.alarm_details || []) {
      const key = `${reading.device_id}:${detail.metric}`;
      if (detail.state === "PENDING_ALARM") {
        pending.push({
          id: `pending:${key}`,
          cleanroom_id: reading.cleanroom_id,
          device_id: reading.device_id,
          source: reading.source,
          metric: detail.metric,
          started_at: detail.pending_since,
          ended_at: null,
          trigger_value: detail.value,
          peak_value: detail.value,
          limit_description: detail.limit,
          uiState: "pending",
        });
      } else if (activeAlarmStates.has(detail.state) && !persistedOpenKeys.has(key)) {
        currentActive.push({
          id: `active:${key}`,
          cleanroom_id: reading.cleanroom_id,
          device_id: reading.device_id,
          source: reading.source,
          metric: detail.metric,
          started_at: detail.active_since || detail.pending_since,
          ended_at: null,
          trigger_value: detail.value,
          peak_value: detail.value,
          limit_description: detail.limit,
          uiState: "active",
        });
      }
    }
  }
  return [...pending, ...currentActive, ...persisted].sort((a, b) => Number(b.started_at || 0) - Number(a.started_at || 0));
}

async function refreshAlarmHistory() {
  const room = selectedRoom();
  if (!room) return;
  const button = $("refreshAlarmsBtn");
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    alarmEvents = await api(`/api/alarms?cleanroom_id=${encodeURIComponent(room.id)}&limit=500`);
    renderAlarms();
  } catch (error) {
    showToast(`报警记录刷新失败：${friendlyError(error.message)}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "刷新报警";
  }
}

function renderAlarms() {
  const all = alarmViewItems();
  const active = all.filter((item) => item.uiState === "active");
  const pending = all.filter((item) => item.uiState === "pending");
  const cleared = all.filter((item) => item.uiState === "cleared");
  $("alarmActiveCount").textContent = String(active.length);
  $("alarmPendingCount").textContent = String(pending.length);
  $("alarmClearedCount").textContent = String(cleared.length);
  const visible = alarmFilter === "all" ? all : all.filter((item) => item.uiState === alarmFilter);
  $("alarmList").innerHTML = visible.length ? visible.map((item) => {
    const { device } = findDevice(item.device_id);
    const labels = {
      active: { icon: "!", state: "活动中" },
      pending: { icon: "…", state: "待观察" },
      cleared: { icon: "✓", state: "已恢复" },
    };
    const meta = labels[item.uiState];
    const duration = formatDuration(item.started_at, item.ended_at || Date.now() / 1000);
    return `<article class="alarm-card ${item.uiState}">
      <div class="alarm-card-icon" aria-hidden="true">${meta.icon}</div>
      <div class="alarm-card-body">
        <div class="alarm-card-title"><h3>${escapeHtml(metricLabels[item.metric] || item.metric)}</h3><span>${meta.state} · ${escapeHtml(device?.name || item.device_id || "未知设备")}</span></div>
        <p>触发值 ${formatNumber(item.trigger_value, 1)}，峰值 ${formatNumber(item.peak_value, 1)}；判定条件：${escapeHtml(translateLimit(item.limit_description))}</p>
        ${item.maintenance ? `<p class="maintenance-badge">${uiText("维护期间")} · ${escapeHtml(item.maintenance.reason)}</p>` : ""}
        <div class="alarm-meta"><span>开始：${escapeHtml(formatTime(item.started_at))}</span><span>持续：${escapeHtml(duration)}</span>${item.ended_at ? `<span>恢复：${escapeHtml(formatTime(item.ended_at))}</span>` : ""}${item.source === "demo" ? "<span>来源：模拟数据</span>" : ""}</div>
      </div>
      <button type="button" data-alarm-device="${escapeHtml(item.device_id)}">查看设备</button>
    </article>`;
  }).join("") : `<div class="empty-state">${alarmFilter === "active" ? "当前设备没有活动报警。" : alarmFilter === "pending" ? "当前设备没有待观察项目。" : alarmFilter === "cleared" ? "当前设备尚无已恢复事件。" : "当前设备暂无报警记录。"}</div>`;
}

function setSettingsDirty(value, message = null, kind = null) {
  settingsDirty = value;
  $("cancelSettingsBtn").disabled = !value;
  $("saveRoomFormBtn").disabled = !value;
  $("settingsSaveDot").className = `save-state-dot ${kind || (value ? "dirty" : "")}`;
  $("settingsSyncDot").className = `sync-pulse ${kind || (value ? "dirty" : "")}`;
  $("settingsSaveState").textContent = message || (value ? "有尚未保存的更改" : "所有更改已保存");
  $("settingsSyncLabel").textContent = kind === "error" ? "保存失败" : value ? "待保存" : "已保存";
}

function syncParticleRuleControl(channel) {
  const toggle = $(channel.enabledInput);
  const input = $(channel.maxInput);
  const row = toggle.closest(".particle-rule-row");
  const enabled = toggle.checked;
  input.disabled = !enabled;
  input.required = enabled;
  row.classList.toggle("enabled", enabled);
  row.querySelector(".switch-copy small").textContent = enabled ? "当前启用" : "当前关闭";
}

function particleThresholdPayload() {
  const values = {};
  for (const channel of particleAlarmChannels) {
    const raw = $(channel.maxInput).value.trim();
    values[channel.enabledField] = $(channel.enabledInput).checked;
    values[channel.maxField] = raw === "" ? null : Number(raw);
  }
  return values;
}

function renderRoomManager() {
  const room = managedRoom();
  if (!room) return;
  lastManagedRoomId = String(room.id);
  $("roomNameInput").value = room.name || "";
  $("profileNameInput").value = room.thresholds?.profile_name || "自定义规则";
  for (const channel of particleAlarmChannels) {
    $(channel.enabledInput).checked = thresholdEnabled(room.thresholds?.[channel.enabledField]);
    $(channel.maxInput).value = room.thresholds?.[channel.maxField] ?? "";
    syncParticleRuleControl(channel);
  }
  $("temperatureMinInput").value = room.thresholds?.temperature_min ?? "";
  $("temperatureMaxInput").value = room.thresholds?.temperature_max ?? "";
  $("humidityMinInput").value = room.thresholds?.humidity_min ?? "";
  $("humidityMaxInput").value = room.thresholds?.humidity_max ?? "";
  $("deviceEditorList").innerHTML = room.devices?.length ? room.devices.map((device, index) => {
    const reading = latestByDevice[String(device.id)];
    const online = Boolean(reading && reading.online !== false && !reading.error);
    return `<div class="device-name-row" data-device-id="${escapeHtml(device.id)}">
      <div><strong data-i18n-ignore>${String(index + 1).padStart(2, "0")} · ${escapeHtml(device.name)}</strong><small class="${online ? "online" : "offline"}">${online ? `在线 · 更新于${relativeTime(readingTimestamp(reading))}` : "离线或尚无数据"}</small></div>
      <label><span>设备显示名称</span><input data-device-name value="${escapeHtml(device.name)}" maxlength="100" required /></label>
    </div>`;
  }).join("") : '<div class="empty-state">该洁净室尚未配置设备。设备连接请在本机采集器中添加。</div>';
  setSettingsDirty(false);
}

function handleManagedRoomChange() {
  if (settingsDirty && !window.confirm("放弃尚未保存的更改并切换洁净室吗？")) {
    $("manageRoomInput").value = lastManagedRoomId;
    return;
  }
  renderRoomManager();
}

function settingsPayload() {
  const room = managedRoom();
  const names = [...$("deviceEditorList").querySelectorAll("[data-device-id]")].map((row) => ({
    id: row.dataset.deviceId,
    name: row.querySelector("[data-device-name]").value.trim(),
  }));
  return {
    rooms: rooms.map((item) => {
      const isManaged = String(item.id) === String(room.id);
      const selectedNames = new Map((isManaged ? names : item.devices.map((device) => ({ id: device.id, name: device.name }))).map((device) => [String(device.id), device.name]));
      return {
        ...item,
        name: isManaged ? $("roomNameInput").value.trim() : item.name,
        devices: item.devices.map((device) => ({ ...device, name: selectedNames.get(String(device.id)) || device.name })),
        thresholds: isManaged ? {
          ...item.thresholds,
          profile_name: $("profileNameInput").value.trim() || "自定义规则",
          ...particleThresholdPayload(),
          temperature_min: Number($("temperatureMinInput").value),
          temperature_max: Number($("temperatureMaxInput").value),
          humidity_min: Number($("humidityMinInput").value),
          humidity_max: Number($("humidityMaxInput").value),
        } : item.thresholds,
      };
    }),
  };
}

function validateSettings() {
  const invalid = $("settingsView").querySelector(".settings-layout input:invalid");
  if (invalid) {
    invalid.focus();
    return "请补全或修正高亮字段。";
  }
  if (!$("roomNameInput").value.trim()) return "洁净室名称不能为空。";
  const names = [...$("deviceEditorList").querySelectorAll("[data-device-name]")].map((input) => input.value.trim().toLocaleLowerCase());
  if (names.some((name) => !name)) return "设备名称不能为空。";
  if (new Set(names).size !== names.length) return "同一洁净室内的设备名称不能重复。";
  for (const channel of particleAlarmChannels) {
    if (!$(channel.enabledInput).checked) continue;
    const value = thresholdNumber($(channel.maxInput).value);
    if (!Number.isFinite(value) || value < 0) return `${channel.label} 报警启用时必须填写不小于 0 的上限。`;
  }
  if (Number($("temperatureMinInput").value) >= Number($("temperatureMaxInput").value)) return "温度最低值必须小于最高值。";
  if (Number($("humidityMinInput").value) >= Number($("humidityMaxInput").value)) return "湿度最低值必须小于最高值。";
  return null;
}

async function saveRoomSettings() {
  const validationError = validateSettings();
  if (validationError) {
    setSettingsDirty(true, validationError, "error");
    showToast(validationError, "error");
    return;
  }
  const roomId = managedRoom()?.id;
  const button = $("saveRoomFormBtn");
  button.disabled = true;
  button.textContent = "保存中…";
  $("settingsSaveState").textContent = "正在保存更改";
  try {
    rooms = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settingsPayload()),
    });
    populateRoomSelectors($("cleanroomInput").value);
    $("manageRoomInput").value = String(roomId);
    renderRoomManager();
    showToast("设置已保存，现场采集器将在下一次配置同步时应用。", "ok");
  } catch (error) {
    setSettingsDirty(true, friendlyError(error.message), "error");
    showToast(`保存失败：${friendlyError(error.message)}`, "error");
  } finally {
    button.textContent = "保存更改";
    button.disabled = !settingsDirty;
  }
}

async function refreshStats() {
  try {
    const data = await api("/api/stats");
    $("statsReadings").textContent = formatNumber(data.readings, 0);
    $("statsAlarms").textContent = formatNumber(data.alarms, 0);
    $("statsPendingSync").textContent = formatNumber(data.pending_sync, 0);
    $("statsLatest").textContent = formatTime(data.latest_reading);
    const labels = { "On-site SQLite Cache": "本机 SQLite 缓存", "Cloud PostgreSQL": "云端 PostgreSQL" };
    $("storageMode").textContent = labels[data.storage_mode] || data.storage_mode || "--";
  } catch (error) {
    showToast(`数据概况刷新失败：${friendlyError(error.message)}`, "error");
  }
}

function emailPayload() {
  return {
    enabled: $("emailEnabledInput").checked,
    recipients: $("emailRecipientsInput").value.split(",").map((value) => value.trim()).filter(Boolean),
    notify_recovery: $("emailRecoveryInput").checked,
  };
}

function validateEmailForm() {
  const payload = emailPayload();
  const address = /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
  let error = "";
  if (payload.recipients.length > 10) error = "最多填写 10 个收件邮箱。";
  else if (!$("emailRecipientsInput").validity.valid || payload.recipients.some((value) => !address.test(value) || value.length > 254 || value.split("@")[0].length > 64)) error = "请填写有效邮箱，以英文逗号分隔。";
  else if (payload.enabled && !payload.recipients.length) error = "启用预警前，请至少填写一个收件邮箱。";
  else if (payload.enabled && !emailSaved?.smtp_configured) error = "邮件服务器尚未配置，请联系系统管理员。";
  emailDirty = Boolean(emailSaved) && JSON.stringify(payload) !== JSON.stringify({ enabled: emailSaved.enabled, recipients: emailSaved.recipients, notify_recovery: emailSaved.notify_recovery });
  $("emailValidation").textContent = error || (emailDirty ? "电邮设置尚未保存。" : "");
  $("saveEmailBtn").disabled = emailBusy || !emailSaved || !emailDirty || Boolean(error);
  $("testEmailBtn").disabled = emailBusy || !emailSaved?.smtp_configured || emailDirty || Boolean(error) || !emailSaved?.recipients.length;
  ["emailEnabledInput", "emailRecipientsInput", "emailRecoveryInput"].forEach((id) => { $(id).disabled = emailBusy; });
  return !error;
}

function renderEmailSettings(data) {
  emailSaved = data;
  $("emailEnabledInput").checked = data.enabled;
  $("emailRecipientsInput").value = data.recipients.join(", ");
  $("emailRecoveryInput").checked = data.notify_recovery;
  $("emailAlertForm").hidden = false;
  $("emailServiceState").textContent = !data.smtp_configured ? "邮件服务器尚未配置，请联系系统管理员。" : data.worker_available === false ? "邮件发送服务尚未就绪或心跳已超时，请联系系统管理员。" : "邮件服务器参数已配置；请发送测试邮件验证投递。";
  validateEmailForm();
}

async function refreshEmailHistory() {
  try {
    const rows = await api("/api/email-alerts/history");
    const kinds = { alarm: "报警通知", recovery: "恢复通知", test: "测试邮件" };
    const states = { pending: "等待发送 / 重试", sending: "正在发送", sent: "服务器已接受", failed: "发送失败（已停止重试）", cancelled: "已取消" };
    const errors = { SMTPAuthenticationError: "邮件服务器认证失败，请联系系统管理员。", SMTPRecipientsRefused: "收件地址被拒绝，请核对邮箱。", DeliveryOutcomeUnknown: "投递结果不明，请核对收件箱，避免盲目重发。" };
    $("emailHistory").replaceChildren();
    if (!rows.length) $("emailHistory").textContent = "暂无发送记录。";
    rows.forEach((row) => {
      const item = document.createElement("div");
      item.className = "email-history-item";
      [new Date(row.created_at * 1000).toLocaleString(), kinds[row.kind], row.recipient, states[row.status], `${row.attempts} / 5`, errors[row.last_error] || row.last_error || "", row.room || "", row.device || "", metricLabels[row.metric] || row.metric || "", row.event_uuid || ""].forEach((text, index) => {
        const span = document.createElement("span");
        span.textContent = text;
        if ([2, 6, 7, 9].includes(index)) span.setAttribute("translate", "no");
        item.append(span);
      });
      if (row.next_attempt_at) {
        const retry = document.createElement("span");
        const label = document.createElement("span");
        label.textContent = "预计发送 / 重试：";
        retry.append(label, document.createTextNode(new Date(row.next_attempt_at * 1000).toLocaleString()));
        item.append(retry);
      }
      $("emailHistory").append(item);
    });
  } catch {
    $("emailHistory").textContent = "发送记录加载失败，请刷新重试。";
  }
}

async function loadEmailSettings() {
  if (!currentUser?.email_alerts_available) return;
  const requestVersion = ++emailLoadVersion;
  try {
    if (!emailDirty && !emailBusy) {
      const data = await api("/api/email-alerts");
      if (requestVersion !== emailLoadVersion) return;
      // A refresh must not overwrite edits made while the request was in flight.
      if (!emailDirty && !emailBusy) renderEmailSettings(data);
    }
    await refreshEmailHistory();
  } catch {
    if (requestVersion !== emailLoadVersion) return;
    $("emailServiceState").textContent = "电邮设置加载失败，请重新进入设置页。";
    if (!emailSaved) $("emailAlertForm").hidden = true;
  }
}

async function saveEmailSettings(event) {
  event.preventDefault();
  if (emailBusy || !emailSaved || !validateEmailForm()) return;
  ++emailLoadVersion;
  emailBusy = true;
  validateEmailForm();
  try {
    const data = await api("/api/email-alerts", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(emailPayload()) });
    renderEmailSettings(data);
    showToast("电邮设置已保存。", "ok");
    await refreshEmailHistory();
  } catch (error) {
    showToast(friendlyError(error.message), "error");
  } finally {
    emailBusy = false;
    validateEmailForm();
  }
}

async function sendTestEmail() {
  if (emailBusy || !validateEmailForm() || $("testEmailBtn").disabled) return;
  emailBusy = true;
  validateEmailForm();
  try {
    await api("/api/email-alerts/test", { method: "POST" });
    showToast("测试邮件已入队，请刷新发送记录并检查收件箱。", "ok");
    await refreshEmailHistory();
  } catch (error) {
    showToast(friendlyError(error.message), "error");
  } finally {
    emailBusy = false;
    validateEmailForm();
  }
}

function switchView(view) {
  if ($("settingsView").classList.contains("active")) {
    if (view === "settings") return;
    if (emailBusy) {
      showToast("电邮操作正在进行，请稍候再离开。", "error");
      return;
    }
    if ((settingsDirty || emailDirty) && !window.confirm("尚有未保存的更改，确认离开设置页吗？")) return;
    // Discard neither form until the entire navigation is confirmed.
    if (emailDirty) renderEmailSettings(emailSaved);
    if (settingsDirty) renderRoomManager();
    ++emailLoadVersion;
  }
  document.querySelectorAll(".nav-button").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === `${view}View`));
  $("scopeBar").hidden = view === "topology";
  if (view === "history") refreshHistory();
  if (view === "alarms") refreshAlarmHistory();
  if (view === "topology") {
    renderTopology();
    Promise.all([loadTopologySites(true), loadPendingCollectors(), loadDiscoveredDevices()])
      .catch((error) => showToast(`设备接入信息加载失败：${friendlyError(error.message)}`, "error"));
  }
  if (view === "settings") {
    renderRoomManager();
    refreshStats();
    loadEmailSettings();
  }
  setTimeout(() => {
    drawRealtimeTrend();
    drawHistoryTrend();
  }, 0);
}

async function handleRoomChange() {
  const room = selectedRoom();
  localStorage.setItem("selected-room", String(room?.id || ""));
  populateDevices();
  $("manageRoomInput").value = String(room?.id || "");
  renderRoomManager();
  if ($("realtimeScopeInput").value === "room") selectAllRealtimeDevices();
  else renderRealtimeSeriesSelector();
  if ($("historyScopeInput").value === "room") {
    historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
    renderHistorySeriesSelector();
  } else {
    renderHistorySeriesSelector();
  }
  await refreshLatest();
  const activeView = document.querySelector(".view.active")?.id;
  if (activeView === "historyView") refreshHistory();
  if (activeView === "alarmsView") refreshAlarmHistory();
}

function handleDeviceChange() {
  const room = selectedRoom();
  const device = selectedDevice();
  if (room && device) localStorage.setItem(`selected-device:${room.id}`, String(device.id));
  updateRealtimeDisplay();
  updateExportLink();
  if ($("historyView").classList.contains("active")) renderHistory();
  if ($("alarmsView").classList.contains("active")) renderAlarms();
}

function selectDeviceFromCard(deviceId) {
  if (!selectedRoom()?.devices?.some((device) => String(device.id) === String(deviceId))) return;
  $("deviceInput").value = String(deviceId);
  handleDeviceChange();
}

function bindEvents() {
  $("cleanroomInput").addEventListener("change", handleRoomChange);
  $("deviceInput").addEventListener("change", handleDeviceChange);
  $("manageRoomInput").addEventListener("change", handleManagedRoomChange);
  $("chartChannel").addEventListener("change", drawRealtimeTrend);
  $("historyChannel").addEventListener("change", drawHistoryTrend);
  $("realtimeScopeInput").addEventListener("change", selectAllRealtimeDevices);
  $("historyScopeInput").addEventListener("change", () => {
    historySelectedDeviceIds = new Set(historyScopeDevices().map(({ device }) => String(device.id)));
    renderHistorySeriesSelector();
    refreshHistory();
  });
  $("realtimeSelectAll").addEventListener("click", selectAllRealtimeDevices);
  $("realtimeClearAll").addEventListener("click", () => {
    realtimeSelectedDeviceIds.clear();
    renderRealtimeSeriesSelector();
    drawRealtimeTrend();
  });
  $("historySelectAll").addEventListener("click", selectAllHistoryDevices);
  $("historyClearAll").addEventListener("click", () => {
    historySelectedDeviceIds.clear();
    renderHistorySeriesSelector();
    renderHistory();
  });
  $("realtimeDeviceLegend").addEventListener("change", (event) => {
    const input = event.target.closest("[data-realtime-device]");
    if (!input) return;
    if (input.checked) realtimeSelectedDeviceIds.add(input.dataset.realtimeDevice);
    else realtimeSelectedDeviceIds.delete(input.dataset.realtimeDevice);
    renderRealtimeSeriesSelector();
    drawRealtimeTrend();
  });
  $("historyDeviceLegend").addEventListener("change", (event) => {
    const input = event.target.closest("[data-history-device]");
    if (!input) return;
    if (input.checked) historySelectedDeviceIds.add(input.dataset.historyDevice);
    else historySelectedDeviceIds.delete(input.dataset.historyDevice);
    renderHistorySeriesSelector();
    renderHistory();
  });
  $("refreshHistoryBtn").addEventListener("click", refreshHistory);
  $("refreshAlarmsBtn").addEventListener("click", refreshAlarmHistory);
  $("refreshStatsBtn").addEventListener("click", refreshStats);
  $("addCollectorBtn").addEventListener("click", () => {
    downloadReusableCollectorInstaller().catch((error) => {
      showToast(friendlyError(error.message), "error");
    });
  });
  $("addCleanroomBtn").addEventListener("click", openCleanroomDialog);
  $("manualAddDeviceBtn").addEventListener("click", () => openManualDeviceDialog());
  $("collectorForm").addEventListener("submit", createCollector);
  $("cleanroomForm").addEventListener("submit", createCleanroom);
  $("manualDeviceForm").addEventListener("submit", createManualDevice);
  $("closeCollectorDialogBtn").addEventListener("click", closeCollectorDialog);
  $("cancelCollectorBtn").addEventListener("click", closeCollectorDialog);
  $("copyCollectorActivationBtn").addEventListener("click", downloadCollectorActivation);
  $("collectorNodeList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-download-collector-package]");
    if (!button) return;
    downloadCollectorPackage(button.dataset.downloadCollectorPackage, button).catch((error) => {
      button.disabled = false;
      button.textContent = "重新下载自动安装包";
      showToast(friendlyError(error.message), "error");
    });
  });
  $("discoveredDeviceList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-assign-discovered]");
    if (button) assignDiscoveredDevice(button.closest("[data-discovered-index]"));
  });
  $("pendingCollectorList").addEventListener("click", (event) => {
    const row = event.target.closest("[data-pending-collector-index]");
    if (!row) return;
    if (event.target.closest("[data-approve-pending-collector]")) approvePendingCollector(row);
    if (event.target.closest("[data-reject-pending-collector]")) rejectPendingCollector(row);
  });
  $("collectorDialog").addEventListener("close", resetCollectorDialog);
  $("closeCleanroomDialogBtn").addEventListener("click", () => $("cleanroomDialog").close());
  $("cancelCleanroomBtn").addEventListener("click", () => $("cleanroomDialog").close());
  $("closeManualDeviceDialogBtn").addEventListener("click", () => $("manualDeviceDialog").close());
  $("cancelManualDeviceBtn").addEventListener("click", () => $("manualDeviceDialog").close());
  $("deleteDeviceForm").addEventListener("submit", deleteDevice);
  $("closeDeleteDeviceDialogBtn").addEventListener("click", closeDeleteDeviceDialog);
  $("cancelDeleteDeviceBtn").addEventListener("click", closeDeleteDeviceDialog);
  $("deleteDeviceDialog").addEventListener("cancel", (event) => {
    if (deviceDeleteBusy) event.preventDefault();
    else deviceToDelete = null;
  });
  $("manualDeviceRoomInput").addEventListener("change", () => {
    $("manualDeviceNameInput").value = suggestedManualDeviceName($("manualDeviceRoomInput").value);
  });
  $("manualDeviceSiteInput").addEventListener("change", updateManualDeviceSiteHelp);
  $("topologyRoomList").addEventListener("click", (event) => {
    const roomButton = event.target.closest("[data-add-device-room]");
    if (roomButton) openManualDeviceDialog(roomButton.dataset.addDeviceRoom);
    const deleteButton = event.target.closest("[data-delete-device]");
    if (deleteButton) openDeleteDeviceDialog(deleteButton.dataset.deleteDevice);
    if (event.target.closest("[data-create-cleanroom]")) openCleanroomDialog();
  });
  $("logoutBtn").addEventListener("click", logout);
  $("exportExcelLink").addEventListener("click", (event) => {
    if (!historySelectedDeviceIds.size) {
      event.preventDefault();
      showToast("请至少选择一台设备后再导出。", "error");
    }
  });
  $("cancelSettingsBtn").addEventListener("click", () => {
    renderRoomManager();
    showToast("已放弃未保存的更改。", "ok");
  });
  $("saveRoomFormBtn").addEventListener("click", saveRoomSettings);
  $("emailAlertForm").addEventListener("input", validateEmailForm);
  $("emailAlertForm").addEventListener("submit", saveEmailSettings);
  $("testEmailBtn").addEventListener("click", sendTestEmail);
  $("refreshEmailBtn").addEventListener("click", loadEmailSettings);
  $("settingsView").addEventListener("input", (event) => {
    if (event.target.closest("#emailAlertCard")) return;
    if (!event.target.matches("input")) return;
    const channel = particleAlarmChannels.find((item) => item.enabledInput === event.target.id);
    if (channel) syncParticleRuleControl(channel);
    setSettingsDirty(true);
  });
  $("openAlarmBtn").addEventListener("click", () => {
    if (activeDetails(latestByDevice[String(selectedDevice()?.id)]).length || pendingDetails(latestByDevice[String(selectedDevice()?.id)]).length) switchView("alarms");
    else $("roomDeviceList").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("roomDeviceList").addEventListener("click", (event) => {
    const card = event.target.closest("[data-select-device]");
    if (card) selectDeviceFromCard(card.dataset.selectDevice);
  });
  $("roomDeviceList").addEventListener("keydown", (event) => {
    const card = event.target.closest("[data-select-device]");
    if (card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      selectDeviceFromCard(card.dataset.selectDevice);
    }
  });
  $("alarmList").addEventListener("click", (event) => {
    const button = event.target.closest("[data-alarm-device]");
    if (!button) return;
    selectDeviceFromCard(button.dataset.alarmDevice);
    switchView("realtime");
  });
  document.querySelectorAll(".nav-button[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelectorAll("[data-range-hours]").forEach((button) => button.addEventListener("click", () => setHistoryRange(Number(button.dataset.rangeHours))));
  document.querySelectorAll("[data-alarm-filter]").forEach((button) => button.addEventListener("click", () => {
    alarmFilter = button.dataset.alarmFilter;
    document.querySelectorAll("[data-alarm-filter]").forEach((item) => item.classList.toggle("active", item === button));
    renderAlarms();
  }));
  [$("historyStart"), $("historyEnd")].forEach((input) => input.addEventListener("change", () => {
    document.querySelectorAll("[data-range-hours]").forEach((button) => button.classList.remove("active"));
    $("historyRangeText").textContent = "自定义范围";
    updateExportLink();
  }));
  $("historyPrevBtn").addEventListener("click", () => { historyPage -= 1; renderHistory(); });
  $("historyNextBtn").addEventListener("click", () => { historyPage += 1; renderHistory(); });
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { drawRealtimeTrend(); drawHistoryTrend(); }, 120);
  });
  window.addEventListener("beforeunload", (event) => {
    if (settingsDirty || emailDirty) event.preventDefault();
  });
  window.addEventListener("hawkhive:localechange", () => {
    updateExportLink();
    if (!rooms.length) return;
    renderRealtimeSeriesSelector();
    renderHistorySeriesSelector();
    updateRealtimeDisplay();
    renderHistory();
    renderAlarms();
    if ($("topologyView").classList.contains("active")) {
      renderTopologySiteOptions();
      updateManualDeviceSiteHelp();
      renderTopology();
      renderPendingCollectors();
      renderDiscoveredDevices();
    }
  });
}

async function startDashboard(user) {
  currentUser = user;
  document.body.className = "auth-ready";
  $("currentUser").textContent = user.display_name || user.username;
  $("topologyNav").hidden = !canManageTopology(user);
  $("settingsNav").hidden = !canManageTopology(user);
  $("openCollectorLink").hidden = !["127.0.0.1", "localhost", "::1"].includes(window.location.hostname);
  bindEvents();
  setHistoryRange(24, false);
  try {
    await loadConfiguration();
    await refreshLatest();
    await seedRealtimeHistory();
    refreshStats();
    refreshTimer = setInterval(refreshLatest, 10000);
  } catch (error) {
    setStatus("error", "启动失败", friendlyError(error.message));
    showToast(`工作台启动失败：${friendlyError(error.message)}`, "error");
  }
}

async function login(event) {
  event.preventDefault();
  const button = $("loginBtn");
  $("loginError").textContent = "";
  button.disabled = true;
  button.textContent = "登录中…";
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("usernameInput").value.trim(), password: $("passwordInput").value }),
    });
    window.location.reload();
  } catch (error) {
    $("loginError").textContent = friendlyError(error.message);
    document.body.className = "auth-required";
  } finally {
    button.disabled = false;
    button.textContent = "登录";
  }
}

async function logout() {
  if (emailBusy) {
    showToast("电邮操作正在进行，请稍候再离开。", "error");
    return;
  }
  if (emailDirty && !window.confirm("电邮设置尚未保存，确认放弃并离开吗？")) return;
  try {
    await fetch("/api/logout", { method: "POST", cache: "no-store" });
  } finally {
    window.location.reload();
  }
}

async function initAuth() {
  $("loginForm").addEventListener("submit", login);
  try {
    const user = await api("/api/session");
    await startDashboard(user);
  } catch {
    document.body.className = "auth-required";
    $("usernameInput").focus();
  }
}

initAuth();
