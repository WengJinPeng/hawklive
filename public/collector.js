const $ = (id) => document.getElementById(id);
const uiText = (text) => window.HawkI18n?.t?.(text) || text;
const uiLocale = () => window.HawkI18n?.locale?.() || "zh-CN";

let rooms = [];
let dirty = false;
let csrfToken = "";
let statusCache = null;
let deviceStates = {};
let deviceFilter = "all";
let deviceQuery = "";
let scanResults = [];
let scanSelection = new Set();
let scanAssignments = new Map();
let localNetworks = [];
let deviceTests = {};
let recentLogs = [];
let toastTimer = null;

const eventLabels = {
  monitor_started: "采集服务启动",
  monitor_worker_failed: "采集任务异常",
  device_offline: "设备连接失败",
  device_connection_tested: "设备测试成功",
  device_connection_test_failed: "设备测试失败",
  reading_recorded: "设备读数已保存",
  customer_settings: "客户设置已更新",
  collector_connections: "设备清单已更新",
  cloud_connected: "云端连接成功",
  cloud_config_saved: "配置已同步至云端",
  cloud_upload: "数据上传完成",
  cloud_upload_failed: "数据上传失败",
  device_discovery_completed: "局域网扫描完成",
  device_discovery_failed: "局域网扫描失败",
  local_storage_maintained: "本机存储维护完成",
  local_storage_maintenance_failed: "本机存储维护失败",
  alarm_started: "指标报警",
  alarm_cleared: "报警已恢复",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function draftKey() {
  return globalThis.crypto?.randomUUID?.() || `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function hydrateRooms(config) {
  return (config || []).map((room) => ({
    ...room,
    devices: (room.devices || []).map((device) => ({
      ...device,
      _key: String(device.id || draftKey()),
      _removed: false,
    })),
  }));
}

function timestamp(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  return number > 1e12 ? number / 1000 : number;
}

function when(value) {
  const time = timestamp(value);
  return time ? new Date(time * 1000).toLocaleString(uiLocale(), { hour12: false }) : "尚未发生";
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

function relativeTime(value) {
  const time = timestamp(value);
  if (!time) return "尚无数据";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - time));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function retryTime(value) {
  const time = timestamp(value);
  if (!time) return "";
  const seconds = Math.max(0, Math.round(time - Date.now() / 1000));
  if (seconds < 5) return "即将重试";
  if (seconds < 60) return `${seconds} 秒后重试`;
  return `${Math.ceil(seconds / 60)} 分钟后重试`;
}

function friendlyError(message) {
  const text = String(message || "请求失败");
  const mappings = [
    [/Failed to fetch/i, "无法连接本机采集服务。"],
    [/Local collector access required/i, "采集器管理页只能在安装电脑本机打开。"],
    [/Collector request token is invalid/i, "本机安全会话已过期，请刷新页面后重试。"],
    [/already configured/i, "这个 IP、端口和 Slave ID 已经被其他设备使用。"],
    [/private IPv4/i, "设备 IP 必须是同一局域网内的私有 IPv4 地址。"],
    [/timed out|timeout/i, "等待设备响应超时，请检查设备电源、Wi-Fi、IP 和端口。"],
    [/refused/i, "设备拒绝连接，请检查 IP、TCP 端口和设备网络设置。"],
    [/Invalid edge token|Token is not valid for this site|tenant does not match edge token/i, "边缘令牌无效，请重新复制平台提供的令牌。"],
    [/Edge Token is too short/i, "边缘令牌长度不足，请完整复制平台提供的令牌。"],
  ];
  return mappings.find(([pattern]) => pattern.test(text))?.[1] || text;
}

function friendlyLogMessage(message) {
  const text = String(message || "");
  const rules = [
    [/^(.+): humidity outside (.+)$/i, (match) => `${match[1]}：湿度超出 ${match[2]}`],
    [/^(.+): temperature outside (.+)$/i, (match) => `${match[1]}：温度超出 ${match[2]}`],
    [/^(.+): particle_([\d_]+)_um > (.+)$/i, (match) => `${match[1]}：${match[2].replaceAll("_", ".")} μm 粒子数超过 ${match[3].replace("particles/ft³", "个/ft³")}`],
    [/^(.+): (humidity|temperature|particle_[\d_]+_um) returned to normal$/i, (match) => `${match[1]}：${match[2] === "humidity" ? "湿度" : match[2] === "temperature" ? "温度" : `${match[2].slice(9, -3).replaceAll("_", ".")} μm 粒子数`}已恢复正常`],
    [/^(.+): timed out$/i, (match) => `${match[1]}：等待设备响应超时`],
    [/^(.+): \[Errno 54\] Connection reset by peer$/i, (match) => `${match[1]}：设备中断了当前连接`],
    [/^Verified (.+)$/i, (match) => `已验证 ${match[1]}`],
    [/^Scanned (.+); found (\d+) candidate\(s\)$/i, (match) => `已扫描 ${match[1]}，发现 ${match[2]} 台候选设备`],
    [/^Background monitor started in device mode$/i, () => "真实设备后台采集已启动"],
    [/^The collector is not connected through a private IPv4 network$/i, () => "当时未能从当前网卡识别私有 IPv4 网段"],
    [/^timed out$/i, () => "等待设备响应超时"],
    [/^database is locked$/i, () => "本地数据库当时正忙"],
    [/^\[Errno 61\] Connection refused$/i, () => "设备拒绝了连接"],
    [/^Socket closed while waiting for initial Modbus TCP data$/i, () => "等待 Modbus TCP 首帧时设备关闭了连接"],
  ];
  for (const [pattern, formatter] of rules) {
    const match = text.match(pattern);
    if (match) return formatter(match);
  }
  return text;
}

function showToast(message, kind = "ok") {
  clearTimeout(toastTimer);
  const toast = $("collectorToast");
  toast.textContent = message;
  toast.className = `toast ${kind === "error" ? "error" : ""} show`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3600);
}

async function refreshCollectorSessionToken() {
  const response = await fetch("/api/collector/session", { cache: "no-store" });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回了无法识别的内容（${response.status}）`);
  }
  const token = payload?.data?.csrf_token;
  if (!response.ok || !payload.ok || typeof token !== "string" || !token) {
    throw new Error("无法刷新采集器安全会话，请刷新页面后重试。");
  }
  csrfToken = token;
}

async function api(url, options = {}, allowTokenRefresh = true) {
  const method = String(options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD"].includes(method)) {
    if (!csrfToken) throw new Error("采集器安全会话尚未就绪。请刷新页面后重试。");
    headers.set("X-DCP-CSRF", csrfToken);
  }
  const response = await fetch(url, { cache: "no-store", ...options, headers });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`服务返回了无法识别的内容（${response.status}）`);
  }
  const rawError = payload?.error || payload?.detail || `请求失败（${response.status}）`;
  if (
    allowTokenRefresh
    && !["GET", "HEAD"].includes(method)
    && response.status === 403
    && /Collector request token is invalid/i.test(String(rawError))
  ) {
    await refreshCollectorSessionToken();
    return api(url, options, false);
  }
  if (!response.ok || !payload.ok) {
    throw new Error(friendlyError(rawError));
  }
  return payload.data;
}

function setDirty(value, message = null, isError = false) {
  dirty = value;
  $("saveBtn").disabled = !value;
  $("discardBtn").disabled = !value;
  $("saveState").textContent = message || (value ? "设备清单有尚未保存的更改" : "没有未保存的更改");
  $("saveState").className = isError ? "status-error" : "";
  $("savePulse").className = `save-pulse ${isError ? "error" : value ? "active" : ""}`;
}

function allDevices({ includeRemoved = true } = {}) {
  return rooms.flatMap((room) => (room.devices || [])
    .filter((device) => includeRemoved || !device._removed)
    .map((device) => ({ room, device })));
}

function findDevice(key) {
  for (const room of rooms) {
    const index = (room.devices || []).findIndex((device) => device._key === key);
    if (index >= 0) return { room, device: room.devices[index], index };
  }
  return null;
}

function parsePrivateIPv4(value) {
  const text = String(value || "").trim();
  const parts = text.split(".");
  if (parts.length !== 4 || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
  if (parts.some((part) => String(Number(part)) !== part || Number(part) > 255)) return null;
  const values = parts.map(Number);
  const privateAddress = values[0] === 10
    || (values[0] === 172 && values[1] >= 16 && values[1] <= 31)
    || (values[0] === 192 && values[1] === 168);
  return privateAddress ? values.join(".") : null;
}

function endpointKey(device) {
  const host = parsePrivateIPv4(device.host) || String(device.host || "").trim();
  return `${host}:${Number(device.tcpPort || 502)}:${Number(device.slave || 1)}`;
}

function suggestedScanCidr() {
  const configured = allDevices({ includeRemoved: false })
    .map(({ device }) => parsePrivateIPv4(device.host))
    .find(Boolean);
  const configuredCidr = configured
    ? `${configured.split(".").slice(0, 3).join(".")}.0/24` : "";
  const matchingActiveNetwork = localNetworks.find((item) => item.scan_cidr === configuredCidr);
  return matchingActiveNetwork
    ? matchingActiveNetwork.scan_cidr
    : localNetworks.length === 1 ? localNetworks[0].scan_cidr : "";
}

async function loadLocalNetworks() {
  localNetworks = await api("/api/collector/networks");
  const select = $("networkSelect");
  if (!localNetworks.length) {
    select.innerHTML = '<option value="">未识别到可用的私有 IPv4 网卡</option>';
    return;
  }
  select.innerHTML = '<option value="">请选择网络接口</option>' + localNetworks.map((item) => {
    const partial = item.partial ? ` · 接口范围 ${escapeHtml(item.adapter_cidr)}，仅建议当前 /24` : "";
    return `<option value="${escapeHtml(item.scan_cidr)}">${escapeHtml(item.name)} · ${escapeHtml(item.address)} · ${escapeHtml(item.scan_cidr)}${partial}</option>`;
  }).join("");
  const suggested = suggestedScanCidr();
  const match = localNetworks.find((item) => item.scan_cidr === suggested);
  if (match || localNetworks.length === 1) {
    const selected = match || localNetworks[0];
    select.value = selected.scan_cidr;
    if (!$("cidrInput").value.trim()) $("cidrInput").value = selected.scan_cidr;
  }
}

function deviceVisualState(device) {
  if (device._removed) return "removed";
  if (!device.id) return "draft";
  if (device._connectionChanged) return "waiting";
  const state = device._connectionChanged ? {} : (deviceStates[String(device.id)] || {});
  if (state.online === true && !state.error) return "online";
  if (state.error || timestamp(state.last_reading_at)) return "offline";
  return "waiting";
}

function updateInventorySummary() {
  const items = allDevices();
  const active = items.filter(({ device }) => !device._removed);
  const online = active.filter(({ device }) => deviceVisualState(device) === "online").length;
  const needsAttention = active.filter(({ device }) => ["offline", "waiting"].includes(deviceVisualState(device))).length;
  const pending = items.filter(({ device }) => !device.id || device._removed).length;
  const capacity = Number(statusCache?.max_active_devices || 100);
  $("allCount").textContent = active.length;
  $("onlineCount").textContent = online;
  $("offlineCount").textContent = needsAttention;
  $("draftCount").textContent = pending;
  $("deviceCapacity").textContent = `${active.length} / ${capacity}`;
  $("capacityBar").style.width = `${Math.min(100, active.length / capacity * 100)}%`;
  $("addDeviceBtn").disabled = active.length >= capacity;
}

function matchesCurrentFilter(room, device) {
  const state = deviceVisualState(device);
  const filterMatch = deviceFilter === "all"
    || (deviceFilter === "online" && state === "online")
    || (deviceFilter === "offline" && ["offline", "waiting"].includes(state))
    || (deviceFilter === "draft" && (!device.id || device._removed));
  const query = deviceQuery.trim().toLocaleLowerCase();
  const queryMatch = !query || [device.name, device.host, room.name]
    .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  return filterMatch && queryMatch;
}

function stateDetails(device) {
  const visual = deviceVisualState(device);
  const state = device._connectionChanged ? {} : (deviceStates[String(device.id)] || {});
  const test = deviceTests[device._key];
  if (visual === "removed") return { label: "保存后停用", dot: "waiting", source: "保留历史记录", sourceClass: "" };
  if (device._connectionChanged) return { label: "地址变更待保存", dot: "waiting", source: "保存后等待新地址的实际读数", sourceClass: "" };
  if (visual === "draft") return { label: "待加入", dot: "draft", source: test?.state === "success" ? "连接已测试" : "尚未保存", sourceClass: test?.state === "success" ? "source-real" : "" };
  if (visual === "online") return { label: "在线", dot: "online", source: state.source === "device" ? "真实设备数据" : state.source === "demo" ? "模拟数据" : "来源待确认", sourceClass: state.source === "device" ? "source-real" : state.source === "demo" ? "source-demo" : "" };
  if (visual === "offline") return { label: "离线", dot: "offline", source: state.error || "最近读数已过期", sourceClass: "source-demo" };
  if (test?.state === "success") return { label: "连接测试通过", dot: "waiting", source: "测试已取得真实数据", sourceClass: "source-real" };
  return { label: "等待首次读数", dot: "waiting", source: "等待首次自动采集", sourceClass: "" };
}

function renderDeviceRow(room, device) {
  const state = device._connectionChanged ? {} : (deviceStates[String(device.id)] || {});
  const details = stateDetails(device);
  const lastReading = timestamp(state.last_reading_at);
  const test = deviceTests[device._key];
  const retryText = timestamp(state.retry_at) ? ` · ${retryTime(state.retry_at)}` : "";
  const testText = test?.state === "success" ? ` · 测试 ${test.data.latency_ms} ms` : "";
  return `<article class="device-row ${device._removed ? "removed" : !device.id ? "draft" : ""}" data-device-key="${escapeHtml(device._key)}">
    <div class="device-main">
      <i class="status-dot ${details.dot}" aria-hidden="true"></i>
      <div><strong data-i18n-ignore>${escapeHtml(device.name)}</strong><small><span data-i18n-ignore>${escapeHtml(room.name)}</span> · <span>${details.label}</span></small></div>
    </div>
    <div class="device-cell endpoint"><span>网络端点</span><strong>${escapeHtml(device.host)}:${Number(device.tcpPort || 502)}</strong><small>Slave ID ${Number(device.slave || 1)}</small></div>
    <div class="device-cell reading-cell"><span>最近读数</span><strong>${lastReading ? relativeTime(lastReading) : "尚无"}</strong><small>${lastReading ? when(lastReading) : "等待设备响应"}${retryText}</small></div>
    <div class="device-cell source-cell"><span>数据来源</span><strong class="${details.sourceClass}">${escapeHtml(details.source)}${testText}</strong><small>${state.failure_count ? `连续失败 ${state.failure_count} 次` : "连接身份已隔离保存"}</small></div>
    <div class="device-actions">
      ${device._removed
        ? `<button class="mini-button restore" data-action="restore" type="button">撤销停用</button>`
        : `<button class="mini-button" data-action="test" type="button">${test?.state === "testing" ? "测试中…" : "测试"}</button>
           <button class="mini-button" data-action="edit" type="button">编辑</button>
           <button class="mini-button danger" data-action="remove" type="button">${device.id ? "停用" : "移除"}</button>`}
    </div>
  </article>`;
}

function renderDevices() {
  updateInventorySummary();
  const groups = rooms.map((room) => ({
    room,
    devices: (room.devices || []).filter((device) => matchesCurrentFilter(room, device)),
  })).filter((group) => group.devices.length);
  if (!groups.length) {
    const hasDevices = allDevices().length > 0;
    $("deviceList").innerHTML = `<div class="empty">${hasDevices ? "没有符合当前搜索或筛选条件的设备。" : "尚未配置设备。可以扫描同一局域网，或手动添加第一台设备。"}</div>`;
    return;
  }
  $("deviceList").innerHTML = groups.map(({ room, devices }) => `<section class="room-group">
    <header class="room-heading"><h3 data-i18n-ignore>${escapeHtml(room.name)}</h3><span>${devices.length} 台设备</span></header>
    ${devices.map((device) => renderDeviceRow(room, device)).join("")}
  </section>`).join("");
}

function populateRoomSelects() {
  const options = rooms.map((room) => `<option value="${escapeHtml(room.id)}" data-i18n-ignore>${escapeHtml(room.name)}</option>`).join("");
  $("deviceRoomInput").innerHTML = options;
  $("scanRoomInput").innerHTML = options;
}

function suggestedDeviceName(roomId) {
  const room = rooms.find((item) => String(item.id) === String(roomId)) || rooms[0];
  const used = new Set((room?.devices || []).filter((device) => !device._removed).map((device) => device.name));
  let number = (room?.devices || []).filter((device) => !device._removed).length + 1;
  const prefix = uiLocale() === "en-US" ? "Particle Counter" : "尘埃粒子计数器";
  let name = `${prefix} ${String(number).padStart(2, "0")}`;
  while (used.has(name)) {
    number += 1;
    name = `${prefix} ${String(number).padStart(2, "0")}`;
  }
  return name;
}

function openDeviceDialog(device = null, prefill = {}) {
  populateRoomSelects();
  const roomId = prefill.roomId || (device ? findDevice(device._key)?.room.id : rooms[0]?.id);
  $("deviceKeyInput").value = device?._key || "";
  $("deviceNameInput").value = prefill.name || device?.name || suggestedDeviceName(roomId);
  $("deviceRoomInput").value = String(roomId || "");
  $("deviceRoomInput").disabled = Boolean(device?.id);
  $("deviceHostInput").value = prefill.host || device?.host || "";
  $("devicePortInput").value = Number(prefill.tcpPort || device?.tcpPort || 502);
  $("deviceSlaveInput").value = Number(prefill.slave || device?.slave || 1);
  $("deviceDialogEyebrow").textContent = device ? "EDIT DEVICE" : "NEW DEVICE";
  $("deviceDialogTitle").textContent = device ? "编辑设备" : "添加设备";
  $("dialogSaveBtn").textContent = device ? "更新设备清单" : "加入设备清单";
  $("dialogTestResult").className = "test-result";
  $("dialogTestResult").textContent = "";
  updateConnectionPreview();
  $("deviceDialog").showModal();
  setTimeout(() => (device ? $("deviceNameInput") : $("deviceHostInput")).focus(), 0);
}

function closeDeviceDialog() {
  $("deviceDialog").close();
}

function formDeviceValues() {
  return {
    name: $("deviceNameInput").value.trim(),
    host: $("deviceHostInput").value.trim(),
    tcpPort: Number($("devicePortInput").value),
    slave: Number($("deviceSlaveInput").value),
    roomId: $("deviceRoomInput").value,
  };
}

function validateSingleDevice(device) {
  if (!device.name) return "请填写设备名称。";
  if (device.name.length > 100) return "设备名称不能超过 100 个字符。";
  if (!parsePrivateIPv4(device.host)) return "请输入同一局域网内的私有 IPv4 地址。";
  if (!Number.isInteger(device.tcpPort) || device.tcpPort < 1 || device.tcpPort > 65535) return "TCP 端口必须在 1–65535 之间。";
  if (!Number.isInteger(device.slave) || device.slave < 1 || device.slave > 247) return "Slave ID 必须在 1–247 之间。";
  if (!rooms.some((room) => String(room.id) === String(device.roomId))) return "请选择设备所属洁净室。";
  return null;
}

function validateInventory(payloadRooms = rooms) {
  const active = payloadRooms.flatMap((room) => (room.devices || []).filter((device) => !device._removed).map((device) => ({ room, device })));
  const capacity = Number(statusCache?.max_active_devices || 100);
  if (active.length > capacity) return `最多可配置 ${capacity} 台启用设备。`;
  const endpoints = new Set();
  const roomNames = new Map();
  for (const { room, device } of active) {
    const basicError = validateSingleDevice({ ...device, roomId: room.id });
    if (basicError) return `${device.name || "未命名设备"}：${basicError}`;
    const endpoint = endpointKey(device);
    if (endpoints.has(endpoint)) return `${device.host}:${device.tcpPort} / Slave ${device.slave} 已被其他设备使用。`;
    endpoints.add(endpoint);
    const names = roomNames.get(room.id) || new Set();
    const folded = device.name.toLocaleLowerCase();
    if (names.has(folded)) return `${room.name} 内的设备名称不能重复。`;
    names.add(folded);
    roomNames.set(room.id, names);
  }
  return null;
}

function inventoryPayload() {
  return {
    rooms: rooms.map((room) => ({
      id: room.id,
      customer_id: room.customer_id,
      name: room.name,
      thresholds: room.thresholds,
      devices: (room.devices || []).filter((device) => !device._removed).map((device) => ({
        id: device.id || "",
        name: device.name,
        host: device.host,
        tcpPort: Number(device.tcpPort),
        slave: Number(device.slave),
        enabled: true,
        site_id: device.site_id || "",
      })),
    })),
  };
}

function updateConnectionPreview() {
  const values = formDeviceValues();
  const host = values.host || "等待填写 IP";
  $("connectionPreview").textContent = values.host ? `${host}:${values.tcpPort || "—"} / Slave ${values.slave || "—"}` : host;
}

function saveDialogDevice(event) {
  event.preventDefault();
  const values = formDeviceValues();
  const error = validateSingleDevice(values);
  if (error) {
    $("dialogTestResult").className = "test-result error";
    $("dialogTestResult").textContent = error;
    return;
  }
  const currentKey = $("deviceKeyInput").value;
  const duplicate = allDevices({ includeRemoved: false }).find(({ device }) => (
    device._key !== currentKey && endpointKey(device) === endpointKey(values)
  ));
  if (duplicate) {
    $("dialogTestResult").className = "test-result error";
    $("dialogTestResult").textContent = `连接参数已被“${duplicate.device.name}”使用。`;
    return;
  }
  const room = rooms.find((item) => String(item.id) === String(values.roomId));
  const duplicateName = (room.devices || []).find((device) => (
    !device._removed
    && device._key !== currentKey
    && device.name.toLocaleLowerCase() === values.name.toLocaleLowerCase()
  ));
  if (duplicateName) {
    $("dialogTestResult").className = "test-result error";
    $("dialogTestResult").textContent = "同一洁净室内的设备名称不能重复。";
    return;
  }

  const found = currentKey ? findDevice(currentKey) : null;
  if (found) {
    const updated = { ...found.device, ...values };
    delete updated.roomId;
    if (!found.device.id && String(found.room.id) !== String(room.id)) {
      found.room.devices.splice(found.index, 1);
      room.devices.push(updated);
    } else {
      found.room.devices[found.index] = updated;
    }
  } else {
    room.devices.push({
      id: "",
      _key: draftKey(),
      _removed: false,
      enabled: true,
      name: values.name,
      host: values.host,
      tcpPort: values.tcpPort,
      slave: values.slave,
      site_id: "",
    });
  }
  setDirty(true, "设备已加入清单，请保存后开始持续采集");
  renderDevices();
  renderScanResults();
  closeDeviceDialog();
  showToast(found ? "设备信息已更新，记得保存设备清单。" : "设备已加入清单，记得保存设备清单。");
}

function removeDevice(key) {
  const found = findDevice(key);
  if (!found) return;
  if (found.device.id) {
    found.device._removed = true;
    setDirty(true, `“${found.device.name}”将在保存后停用，历史数据会保留`);
  } else {
    found.room.devices.splice(found.index, 1);
    delete deviceTests[key];
    setDirty(true, "未保存的新设备已从清单移除");
  }
  renderDevices();
  renderScanResults();
}

function restoreDevice(key) {
  const found = findDevice(key);
  if (!found) return;
  found.device._removed = false;
  setDirty(true, `已撤销停用“${found.device.name}”`);
  renderDevices();
  renderScanResults();
}

async function testConnection(device, target = "row") {
  const payload = {
    host: device.host,
    tcpPort: Number(device.tcpPort),
    slave: Number(device.slave),
  };
  const error = validateSingleDevice({ ...device, roomId: device.roomId || rooms[0]?.id });
  if (error) {
    if (target === "dialog") {
      $("dialogTestResult").className = "test-result error";
      $("dialogTestResult").textContent = error;
    } else {
      showToast(error, "error");
    }
    return null;
  }
  if (target === "dialog") {
    $("dialogTestBtn").disabled = true;
    $("dialogTestBtn").textContent = "测试中…";
    $("dialogTestResult").className = "test-result testing";
    $("dialogTestResult").textContent = "正在读取完整设备数据，请稍候…";
  } else {
    deviceTests[device._key] = { state: "testing" };
    renderDevices();
  }
  try {
    const result = await api("/api/collector/test-device", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const sample = result.sample || {};
    const detail = `真实设备响应 · ${result.latency_ms} ms · 0.5 µm ${sample.particle_0_5_um ?? "—"} ${sample.particle_unit || "单位未确认"} · ${sample.temperature ?? "—"} °C · ${sample.humidity ?? "—"} %RH`;
    if (target === "dialog") {
      $("dialogTestResult").className = "test-result success";
      $("dialogTestResult").textContent = detail;
    } else {
      deviceTests[device._key] = { state: "success", data: result };
      renderDevices();
      showToast(`“${device.name}”连接成功，数据来源已确认是设备。`);
    }
    loadLogs();
    return result;
  } catch (requestError) {
    const message = friendlyError(requestError.message);
    if (target === "dialog") {
      $("dialogTestResult").className = "test-result error";
      $("dialogTestResult").textContent = `连接失败：${message}`;
    } else {
      deviceTests[device._key] = { state: "error", message };
      renderDevices();
      showToast(`“${device.name}”测试失败：${message}`, "error");
    }
    loadLogs();
    return null;
  } finally {
    if (target === "dialog") {
      $("dialogTestBtn").disabled = false;
      $("dialogTestBtn").textContent = "测试连接";
    }
  }
}

function setHealth(id, kind, title, detail) {
  const element = $(id);
  element.textContent = title;
  element.closest(".health-card").className = `health-card ${kind || ""}`;
  $(`${id}Detail`).textContent = detail;
}

function setReadiness(kind, eyebrow, title, detail, action, target) {
  $("readinessPanel").className = `readiness-panel ${kind}`;
  $("readinessIcon").textContent = kind === "ready" ? "✓" : kind === "error" ? "!" : kind === "warning" ? "…" : "…";
  $("readinessEyebrow").textContent = eyebrow;
  $("readinessTitle").textContent = title;
  $("readinessDetail").textContent = detail;
  $("readinessAction").textContent = action;
  $("readinessAction").dataset.target = target || "";
}

function renderStatus(status) {
  const devices = status.devices || [];
  const freshWindow = Math.max(45, Number(status.poll_seconds || 20) * 2.5);
  const freshRealDevices = devices.filter((item) => (
    item.online && item.source === "device" && timestamp(item.last_reading_at)
    && Date.now() / 1000 - timestamp(item.last_reading_at) <= freshWindow
  ));
  const hasConfig = status.device_total > 0;
  const hasRealReading = status.mode === "device" && freshRealDevices.length > 0;
  const cloudReady = Boolean(status.cloud_configured && status.cloud_connected);
  const uploadReady = Boolean(
    cloudReady && timestamp(status.last_upload_at) && status.pending_uploads === 0
    && status.quarantined_uploads === 0 && status.unassigned_uploads === 0
  );

  setHealth(
    "deviceHealth",
    status.device_online === status.device_total && hasConfig ? "ok" : status.device_online > 0 ? "warning" : "error",
    `${status.device_online}/${status.device_total} 在线`,
    hasConfig ? "逐台状态和最近读数见设备清单" : "尚未配置设备",
  );
  setHealth(
    "collectionHealth",
    status.monitor_running ? "ok" : "error",
    status.monitor_running ? "采集运行中" : "采集已停止",
    status.mode === "demo"
      ? "当前是模拟数据模式"
      : `每 ${status.poll_seconds || "—"} 秒 · 最多 ${status.poll_workers || "—"} 台并发`,
  );
  const cloudLabels = { connected: "云端已连接", attention: "云端需处理", error: "云端连接失败", connecting: "正在连接", stopped: "云端已停止", not_configured: "云端未配置" };
  setHealth(
    "cloudHealth",
    status.cloud_state === "attention" ? "warning" : status.cloud_connected ? "ok" : status.cloud_configured ? "error" : "warning",
    cloudLabels[status.cloud_state] || "云端状态未知",
    status.site_id || "本机采集不受影响",
  );
  const queueTitle = status.quarantined_uploads
    ? `${status.quarantined_uploads} 条已隔离`
    : status.unassigned_uploads
      ? `${status.unassigned_uploads} 条归属异常`
      : `${status.pending_uploads} 条待上传`;
  const queueKind = status.quarantined_uploads || status.unassigned_uploads ? "error" : status.pending_uploads ? "warning" : "ok";
  const queueDetail = status.quarantined_uploads
    ? "无效记录需要人工检查"
    : status.unassigned_uploads
      ? "记录站点归属不一致"
      : status.pending_uploads
        ? `最早积压：${when(status.oldest_pending_at)}`
        : "上传队列为空";
  setHealth("queueHealth", queueKind, queueTitle, queueDetail);
  const storage = status.storage || {};
  const storageKind = storage.state === "ok" ? "ok" : storage.state === "warning" ? "warning" : "error";
  setHealth(
    "storageHealth",
    storageKind,
    storage.state === "ok" ? "本机存储正常" : storage.state === "warning" ? "磁盘空间偏低" : "本机存储需处理",
    `剩余 ${formatBytes(storage.disk_free_bytes)} · 保留 ${storage.retention_days || "—"} 天`,
  );

  $("siteName").textContent = status.site_id || "尚未配置站点";
  $("lastUpload").textContent = when(status.last_upload_at);
  $("lastReading").textContent = when(status.last_reading_at);
  $("lastCloudContact").textContent = when(status.last_cloud_contact_at);
  $("cloudHint").textContent = status.cloud_configured
    ? "连接信息和令牌只保存在本机；新记录会自动进入上传队列。"
    : "设备读取成功后再配置远程平台，不影响当前本机采集。";
  $("modeNotice").hidden = status.mode !== "demo";
  $("modeNotice").textContent = "当前为模拟模式：页面中的数值不是设备实测数据，不能用于现场判断。";

  if (storage.state === "critical") {
    setReadiness("error", "本机存储风险", "磁盘剩余空间不足", "未上传记录不会被自动删除，请立即扩容或处理已上传数据。", "查看问题", "diagnosticCard");
  } else if (status.mode === "demo") {
    setReadiness("error", "当前不可用于现场监测", "采集器正在生成模拟数据", "请以 device 模式重启采集服务，再确认真实设备数据。", "查看设备清单", "deviceSurface");
  } else if (!hasConfig) {
    setReadiness("error", "需要配置", "还没有可采集的设备", "扫描同一局域网，或手动填写第一台设备的连接参数。", "添加设备", "add-device");
  } else if (status.device_online === 0) {
    setReadiness("error", "需要处理", "当前没有设备在线", "请逐台测试 IP、502 端口和 Slave ID，并确认设备与本机连接到同一网络。", "查看设备清单", "deviceSurface");
  } else if (!hasRealReading) {
    setReadiness("warning", "等待确认", "设备已响应，但真实读数尚未刷新", `有效读数需在 ${Math.round(freshWindow)} 秒内更新且来源必须为 device。`, "查看设备状态", "deviceSurface");
  } else if (!status.cloud_configured) {
    setReadiness("warning", "本机采集正常", `${freshRealDevices.length} 台设备正在提供真实数据`, "本机采集已经就绪；需要远程查看时，再配置云端站点。", "配置云端", "cloudCard");
  } else if (!status.cloud_connected) {
    setReadiness("error", "需要处理", "真实采集正常，但云端连接失败", "本机数据不会丢失；请检查云端地址、站点编号、令牌和外网连接。", "检查云端", "cloudCard");
  } else if (status.quarantined_uploads || status.unassigned_uploads) {
    setReadiness("error", "需要处理", "部分记录无法正常上传", "隔离或归属异常的记录不会自动出现在远程客户工作台。", "查看诊断", "diagnosticCard");
  } else if (status.cloud_state === "attention") {
    setReadiness("warning", "需要处理", "云端部分通道尚未恢复", "已恢复部分通信，请等待其余上传通道恢复或查看诊断日志。", "查看诊断", "diagnosticCard");
  } else if (freshRealDevices.length < status.device_total) {
    setReadiness("warning", "需要处理", "部分设备尚未恢复有效读数", `${freshRealDevices.length}/${status.device_total} 台设备正在提供有效读数，请查看其余设备的连接状态。`, "查看设备清单", "deviceSurface");
  } else if (!uploadReady) {
    setReadiness("warning", "上传处理中", "真实采集和云端连接正常", status.pending_uploads ? `还有 ${status.pending_uploads} 条记录等待上传。` : "正在等待首条设备记录上传。", "查看云端", "cloudCard");
  } else {
    setReadiness("ready", "链路状态正常", "真实采集与数据上传均已确认", "本机已确认真实读数和成功上传；线上页面仍建议使用客户账号最终查看。", "打开客户工作台", "index.html");
  }
  updateInventorySummary();
}

async function loadStatus({ replaceInventory = true, background = false } = {}) {
  const canReplace = () => !background || (!dirty && !$("deviceDialog").open);
  const fetchInventory = replaceInventory && canReplace();
  const [status, config] = await Promise.all([
    api("/api/collector/status"),
    fetchInventory ? api("/api/collector/config") : Promise.resolve(null),
  ]);
  statusCache = status;
  // An edit may begin while the background requests are in flight.
  const applyInventory = fetchInventory && canReplace();
  if (applyInventory) {
    rooms = hydrateRooms(config);
    deviceTests = {};
    populateRoomSelects();
  }
  deviceStates = Object.fromEntries((status.devices || []).map((item) => [String(item.id), item]));
  renderDevices();
  renderStatus(status);
  renderCurrentBlocker(recentLogs);
  if (applyInventory) setDirty(false);
}

async function loadCloudSettings() {
  const cloud = await api("/api/collector/cloud");
  $("cloudUrlInput").value = cloud.cloud_url || "";
  $("siteIdInput").value = cloud.site_id || "";
  $("edgeTokenInput").value = "";
  $("edgeTokenInput").placeholder = cloud.token_configured ? "已配置；留空可保留原令牌" : "粘贴一次性令牌";
  $("tokenHelp").textContent = cloud.token_configured ? "本机已保存一个令牌，出于安全原因不会显示。" : "只保存在本机，不会再次显示。";
  const labels = { connected: "已连接", attention: "需要处理", error: "连接失败", connecting: "连接中", stopped: "已停止", not_configured: "未连接" };
  $("cloudConnectionBadge").textContent = labels[cloud.state] || "状态未知";
  $("cloudConnectionBadge").className = `connection-badge ${cloud.connected ? "connected" : cloud.state === "error" || cloud.state === "attention" ? "error" : ""}`;
  $("lastCloudContact").textContent = when(cloud.last_contact_at);
  $("cloudFormMessage").textContent = cloud.last_error ? `最近错误：${cloud.last_error}` : "";
  $("cloudFormMessage").className = `cloud-form-message ${cloud.last_error ? "error" : ""}`;
}

async function connectCloud() {
  const button = $("connectCloudBtn");
  button.disabled = true;
  button.textContent = "正在测试…";
  $("cloudFormMessage").textContent = "正在校验 HTTPS 连接、站点身份和令牌…";
  $("cloudFormMessage").className = "cloud-form-message";
  try {
    await api("/api/collector/cloud", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cloud_url: $("cloudUrlInput").value.trim(),
        site_id: $("siteIdInput").value.trim(),
        token: $("edgeTokenInput").value.trim(),
      }),
    });
    $("cloudFormMessage").textContent = "连接成功，自动上传已经启动。";
    showToast("云端连接成功。");
    await Promise.all([loadStatus(), loadCloudSettings()]);
    await loadLogs();
  } catch (error) {
    $("cloudFormMessage").textContent = friendlyError(error.message);
    $("cloudFormMessage").className = "cloud-form-message error";
    showToast(`云端连接失败：${friendlyError(error.message)}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = "测试并连接";
  }
}

function collapseLogs(logs) {
  const groups = new Map();
  for (const item of logs || []) {
    const key = `${item.level}:${item.event}:${item.message}`;
    const current = groups.get(key);
    if (!current) groups.set(key, { ...item, count: 1 });
    else {
      current.count += 1;
      if (Number(item.timestamp) > Number(current.timestamp)) current.timestamp = item.timestamp;
    }
  }
  return [...groups.values()].sort((a, b) => Number(b.timestamp) - Number(a.timestamp));
}

function renderCurrentBlocker(logs) {
  const blocker = $("currentBlocker");
  const ordered = [...(logs || [])].sort((a, b) => Number(b.timestamp) - Number(a.timestamp));
  const hasLaterEvent = (error, events) => ordered.some((item) => (
    events.includes(item.event) && Number(item.timestamp) > Number(error.timestamp)
  ));
  const resolved = (error) => {
    if (error.event === "local_storage_maintenance_failed") {
      const storage = statusCache?.storage;
      return Boolean(storage?.integrity === "ok" && !storage.last_error
        && Number(storage.last_run_at || 0) > Number(error.timestamp));
    }
    if (["cloud_sync_failed", "cloud_connection_failed"].includes(error.event)) {
      return Boolean(statusCache?.cloud_state === "connected"
        && Number(statusCache.last_cloud_contact_at || 0) > Number(error.timestamp));
    }
    if (error.event === "device_offline" || error.event === "device_connection_test_failed") {
      return Boolean(statusCache?.device_total && statusCache.device_online === statusCache.device_total);
    }
    if (error.event === "device_discovery_failed") {
      return hasLaterEvent(error, ["device_discovery_completed"]);
    }
    if (error.event === "cloud_upload_failed") {
      return Boolean(statusCache?.cloud_connected && (timestamp(statusCache.last_upload_at) || 0) > Number(error.timestamp));
    }
    if (error.event === "monitor_worker_failed") {
      return Boolean(statusCache?.monitor_running && (timestamp(statusCache.last_reading_at) || 0) > Number(error.timestamp));
    }
    if (error.event === "alarm_started") {
      const signature = String(error.message || "").match(/^(.+): ([a-z0-9_]+)/i)?.slice(1, 3).join(": ");
      const clearedLater = signature && ordered.some((item) => (
        item.event === "alarm_cleared" && Number(item.timestamp) > Number(error.timestamp)
        && String(item.message || "").toLocaleLowerCase().startsWith(signature.toLocaleLowerCase())
      ));
      const anyActiveAlarm = (statusCache?.devices || []).some((device) => device.alarm_status === "ALARM_ACTIVE");
      return Boolean(clearedLater || !anyActiveAlarm);
    }
    return false;
  };
  const latestError = ordered.find((item) => item.level === "ERROR" && !resolved(item));
  if (!latestError) {
    blocker.hidden = true;
    return;
  }
  blocker.hidden = false;
  $("blockerTitle").textContent = eventLabels[latestError.event] || latestError.event.replaceAll("_", " ");
  $("blockerDetail").textContent = friendlyLogMessage(latestError.message);
  $("blockerTime").textContent = `最后发生：${when(latestError.timestamp)}`;
}

async function loadLogs() {
  recentLogs = await api("/api/collector/logs?limit=100");
  renderCurrentBlocker(recentLogs);
  const collapsed = collapseLogs(recentLogs).slice(0, 12);
  $("logList").innerHTML = collapsed.length ? collapsed.map((item) => `<div class="log-item">
    <i class="log-dot ${item.level === "ERROR" ? "error" : ""}"></i>
    <div><strong>${escapeHtml(eventLabels[item.event] || item.event.replaceAll("_", " "))}</strong><p>${escapeHtml(friendlyLogMessage(item.message))}</p><small>${when(item.timestamp)}</small></div>
    ${item.count > 1 ? `<span class="log-count">×${item.count}</span>` : ""}
  </div>`).join("") : '<div class="empty compact">尚无诊断记录。</div>';
}

async function saveInventory() {
  const error = validateInventory();
  if (error) {
    setDirty(true, error, true);
    showToast(error, "error");
    return;
  }
  const button = $("saveBtn");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    await api("/api/collector/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(inventoryPayload()),
    });
    await loadStatus();
    setDirty(false, "设备清单已保存，采集器正在应用新配置");
    showToast("设备清单已保存；新增设备将在下一轮开始采集。");
    await loadLogs();
  } catch (saveError) {
    setDirty(true, friendlyError(saveError.message), true);
    showToast(`保存失败：${friendlyError(saveError.message)}`, "error");
  } finally {
    button.textContent = "保存设备清单";
    button.disabled = !dirty;
  }
}

function renderScanResults() {
  if (!scanResults.length) {
    $("scanFooter").hidden = true;
    return;
  }
  const configured = new Set(allDevices({ includeRemoved: false }).map(({ device }) => endpointKey(device)));
  $("scanResults").innerHTML = scanResults.map((item) => {
    const key = endpointKey({ host: item.host, tcpPort: item.tcp_port, slave: item.slave });
    const alreadyConfigured = configured.has(key);
    const canAdd = item.verified === true;
    const quality = canAdd
      ? `协议与单位已验证 · ${item.particle_unit_label || "PCS/28.3L"}`
      : item.protocol_compatible && item.unit_supported === false
        ? `单位不支持 · ${item.particle_unit_label || "未读到单位"}`
        : item.protocol_compatible ? "目标协议响应，单位未验证"
          : item.modbus_responded ? "仅基础 Modbus 响应" : "仅 TCP 可连接";
    const choice=scanAssignments.get(key)||'';
    const options=allDevices({includeRemoved:false}).map(({device})=>`<option data-i18n-ignore value="${escapeHtml(device._key)}" ${choice===device._key?'selected':''}>${escapeHtml(device.name)} · ${escapeHtml(device.host)}</option>`).join('');
    return `<section class="scan-result ${alreadyConfigured ? "configured" : ""}">
      <input type="checkbox" aria-label="${escapeHtml(uiText('选择地址'))} ${escapeHtml(item.host)}" data-scan-key="${escapeHtml(key)}" ${scanSelection.has(key) && !alreadyConfigured && canAdd ? "checked" : ""} ${alreadyConfigured || !canAdd ? "disabled" : ""} />
      <span><strong>${escapeHtml(item.host)}:${Number(item.tcp_port)}</strong><small>Slave ID ${Number(item.slave || 1)} · ${item.latency_ms || "—"} ms${alreadyConfigured ? " · 已在设备清单" : ""}</small></span>
      <b class="scan-quality ${canAdd ? "" : "unverified"}">${escapeHtml(quality)}</b>
      ${!alreadyConfigured && canAdd ? `<label class="scan-assignment"><span>地址处理方式</span><select data-scan-assignment="${escapeHtml(key)}" aria-label="${escapeHtml(uiText('地址处理方式'))} ${escapeHtml(item.host)}"><option value="">${uiText('请选择新增或更换 IP')}</option><option value="new" ${choice==='new'?'selected':''}>${uiText('新增一台设备')}</option><optgroup label="${escapeHtml(uiText('已有设备更换 IP'))}">${options}</optgroup></select><small>更换 IP 会保留所选设备的编号、名称和车间。</small></label>` : ''}
    </section>`;
  }).join("");
  const safeEndpoints = new Set(scanResults.filter((item) => item.verified === true).map((item) => endpointKey({ host: item.host, tcpPort: item.tcp_port, slave: item.slave })));
  const availableSelection = [...scanSelection].filter((key) => !configured.has(key) && safeEndpoints.has(key));
  scanSelection = new Set(availableSelection);
  $("scanSelectionSummary").textContent = `已选择 ${scanSelection.size} 台设备`;
  $("addScannedBtn").disabled = scanSelection.size === 0 || [...scanSelection].some((key)=>!scanAssignments.get(key));
  $("scanFooter").hidden = false;
}

async function scanNetwork() {
  const button = $("startScanBtn");
  scanAssignments.clear();
  button.disabled = true;
  button.textContent = "扫描中…";
  $("scanResults").innerHTML = '<div class="empty compact">正在并发检查局域网地址，通常需要几秒钟…</div>';
  $("scanFooter").hidden = true;
  try {
    const result = await api("/api/collector/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cidr: $("cidrInput").value.trim(),
        tcp_port: Number($("scanPortInput").value),
      }),
    });
    scanResults = result.results || [];
    const configured = new Set(allDevices({ includeRemoved: false }).map(({ device }) => endpointKey(device)));
    scanSelection = new Set(scanResults
      .filter((item) => item.protocol_compatible)
      .map((item) => endpointKey({ host: item.host, tcpPort: item.tcp_port, slave: item.slave }))
      .filter((key) => !configured.has(key)));
    const scannedCidrs = Array.isArray(result.cidrs) && result.cidrs.length
      ? result.cidrs : result.cidr ? [result.cidr] : [];
    const scannedLabel = scannedCidrs.join("、");
    if (!scanResults.length) {
      $("scanResults").innerHTML = `<div class="empty compact">在 ${escapeHtml(scannedLabel)} 没有发现 Modbus TCP 设备。请确认设备与本机处于同一网络且 502 端口已启用。</div>`;
    } else {
      renderScanResults();
    }
    if (scannedCidrs.length === 1) $("cidrInput").value = scannedCidrs[0];
  } catch (error) {
    scanResults = [];
    scanSelection.clear();
    $("scanResults").innerHTML = `<div class="empty compact status-error">${escapeHtml(friendlyError(error.message))}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "开始扫描";
    loadLogs();
  }
}

function scannedDevicePlan() {
  const selected = scanResults.filter((item)=>scanSelection.has(endpointKey({host:item.host,tcpPort:item.tcp_port,slave:item.slave})));
  const used=new Set();
  const plan=[];
  for(const item of selected){
    const key=endpointKey({host:item.host,tcpPort:item.tcp_port,slave:item.slave});
    const choice=scanAssignments.get(key);
    if(!item.verified || !choice)throw new Error('请为每个所选地址选择新增设备或更换原设备 IP。');
    if(allDevices({includeRemoved:false}).some(({device})=>endpointKey(device)===key))throw new Error('地址已在清单中，请刷新扫描结果。');
    const target=choice==='new'?null:findDevice(choice);
    if(choice!=='new' && (!target || target.device._removed))throw new Error('原设备已变化，请重新选择。');
    if(target && used.has(target.device._key))throw new Error('同一原设备不能同时更新为两个地址。');
    if(target)used.add(target.device._key);
    plan.push({item,target});
  }
  const count=allDevices({includeRemoved:false}).length+plan.filter((step)=>!step.target).length;
  if(count>Number(statusCache?.max_active_devices||100))throw new Error('新增设备数量超过上限，请减少选择。');
  return plan;
}
function addSelectedScannedDevices() {
  const room=rooms.find((item)=>String(item.id)===String($('scanRoomInput').value))||rooms[0];
  if(!room)return;
  let plan;
  try{plan=scannedDevicePlan();}catch(error){showToast(uiText(error.message),'error');return;}
  if(!plan.length)return;
  const changes=plan.filter((step)=>step.target);
  if(changes.length && !window.confirm(uiText('请核对现场标签：以下新旧地址是否属于同一台物理仪表？')+'\n'+changes.map(({item,target})=>`${target.device.name}: ${target.device.host} → ${item.host}`).join('\n')))return;
  for(const {item,target} of plan){
    const device=target?.device || {id:'',_key:draftKey(),_removed:false,enabled:true,name:suggestedDeviceName(room.id),site_id:''};
    Object.assign(device,{host:item.host,tcpPort:Number(item.tcp_port),slave:Number(item.slave||1)});
    if(!target)room.devices.push(device);
    if(target)device._connectionChanged=true;
    delete deviceTests[device._key];
  }
  scanSelection.clear();scanAssignments.clear();
  setDirty(true,'设备清单已更新，请保存后应用。');renderDevices();renderScanResults();showToast('设备清单已更新，请保存后应用。');
}

async function refreshAll() {
  if (dirty && !window.confirm("刷新会放弃尚未保存的设备清单更改，是否继续？")) return;
  const button = $("refreshBtn");
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    await Promise.all([loadStatus(), loadCloudSettings()]);
    await loadLogs();
  } catch (error) {
    $("modeNotice").hidden = false;
    $("modeNotice").textContent = `刷新失败：${friendlyError(error.message)}`;
    showToast(`刷新失败：${friendlyError(error.message)}`, "error");
  } finally {
    button.disabled = false;
    button.innerHTML = '<span aria-hidden="true">↻</span> 刷新状态';
  }
}

async function discardChanges() {
  await loadStatus();
  renderScanResults();
  showToast("已放弃尚未保存的设备清单更改。");
}

async function init() {
  try {
    const session = await api("/api/collector/session");
    csrfToken = session.csrf_token;
    await Promise.all([loadStatus(), loadCloudSettings(), loadLocalNetworks()]);
    await loadLogs();
  } catch (error) {
    $("modeNotice").hidden = false;
    $("modeNotice").textContent = `采集器启动失败：${friendlyError(error.message)}`;
    setReadiness("error", "服务异常", "无法读取采集器状态", friendlyError(error.message), "刷新页面", "reload");
  }
}

$("refreshBtn").addEventListener("click", refreshAll);
$("connectCloudBtn").addEventListener("click", connectCloud);
$("refreshLogsBtn").addEventListener("click", loadLogs);
$("addDeviceBtn").addEventListener("click", () => openDeviceDialog());
$("discoverBtn").addEventListener("click", () => {
  populateRoomSelects();
  if (!$("cidrInput").value.trim()) $("cidrInput").value = suggestedScanCidr();
  $("discoveryPanel").hidden = false;
  $("cidrInput").focus();
});
$("closeScanBtn").addEventListener("click", () => { $("discoveryPanel").hidden = true; });
$("startScanBtn").addEventListener("click", scanNetwork);
$("networkSelect").addEventListener("change", (event) => {
  if (event.target.value) $("cidrInput").value = event.target.value;
});
$("addScannedBtn").addEventListener("click", addSelectedScannedDevices);
$("scanResults").addEventListener("change", (event) => {
  const assignment=event.target.closest('[data-scan-assignment]');
  if(assignment){scanAssignments.set(assignment.dataset.scanAssignment,assignment.value);renderScanResults();return;}
  const input = event.target.closest("[data-scan-key]");
  if (!input) return;
  if (input.checked) scanSelection.add(input.dataset.scanKey);
  else scanSelection.delete(input.dataset.scanKey);
  renderScanResults();
});
$("deviceSearchInput").addEventListener("input", (event) => {
  deviceQuery = event.target.value;
  renderDevices();
});
document.querySelector(".filter-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  deviceFilter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
  renderDevices();
});
$("deviceList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  const row = event.target.closest("[data-device-key]");
  if (!button || !row) return;
  const found = findDevice(row.dataset.deviceKey);
  if (!found) return;
  if (button.dataset.action === "edit") openDeviceDialog(found.device);
  if (button.dataset.action === "remove") removeDevice(found.device._key);
  if (button.dataset.action === "restore") restoreDevice(found.device._key);
  if (button.dataset.action === "test") testConnection({ ...found.device, roomId: found.room.id });
});
$("deviceForm").addEventListener("submit", saveDialogDevice);
$("closeDeviceDialogBtn").addEventListener("click", closeDeviceDialog);
$("cancelDeviceDialogBtn").addEventListener("click", closeDeviceDialog);
$("dialogTestBtn").addEventListener("click", () => testConnection(formDeviceValues(), "dialog"));
["deviceHostInput", "devicePortInput", "deviceSlaveInput"].forEach((id) => $(id).addEventListener("input", updateConnectionPreview));
$("saveBtn").addEventListener("click", saveInventory);
$("discardBtn").addEventListener("click", discardChanges);
$("readinessAction").addEventListener("click", () => {
  const target = $("readinessAction").dataset.target;
  if (target === "reload") window.location.reload();
  else if (target === "index.html") window.location.href = "./index.html";
  else if (target === "add-device") openDeviceDialog();
  else document.getElementById(target)?.scrollIntoView({ behavior: "smooth", block: "start" });
});
window.addEventListener("beforeunload", (event) => { if (dirty) event.preventDefault(); });
window.addEventListener("hawkhive:localechange", () => {
  if (statusCache) {
    renderDevices();
    renderStatus(statusCache);
    renderCurrentBlocker(recentLogs);
  }
  loadCloudSettings().catch(() => {});
  loadLogs().catch(() => {});
});
setInterval(() => {
  if (!document.hidden) loadStatus({ background: true }).catch(() => {});
}, 10000);

init();
