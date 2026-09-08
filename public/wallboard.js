const translations = {
  "zh-CN": {
    wallboardLabel: "HawkHive 全厂洁净环境态势大屏", brandTagline: "洁净环境洞察", screenNumber: "屏幕 01",
    chartDescription: "当前账号全部区域过去24小时0.5微米粒子最高值趋势",
    pageTitle: "HawkHive 全厂洁净态势大屏", language: "语言", factoryName: "当前客户", operationsCenter: "洁净环境运营中心", screenTitle: "全厂洁净态势",
    screenControls: "大屏状态与控制", apiStatus: "接口状态", connecting: "连接中", connected: "已连接", degraded: "部分接口异常", disconnected: "连接失败", enterFullscreen: "进入全屏显示", fullscreen: "全屏", loadingTitle: "正在加载大屏", loadingDetail: "正在校验登录状态并读取监测数据。",
    sourceRealStamp: "真实设备数据", sourceDemoStamp: "模拟采集数据", sourceMixedStamp: "混合数据来源", sourceUnknownStamp: "来源未验证", sourceNoneStamp: "暂无新鲜数据",
    sourceRealTitle: "当前读数来自真实设备", sourceRealDetail: "接口返回 source=device；仍需结合设备现场与采集链路判断",
    sourceDemoTitle: "当前包含模拟数据", sourceDemoDetail: "接口返回 source=demo，仅用于演示，不可用于现场或生产判断",
    sourceMixedTitle: "数据来源混合", sourceMixedDetail: "真实设备与模拟采集数据并存，汇总值不可直接用于生产判断",
    sourceUnknownTitle: "当前数据来源无法验证", sourceUnknownDetail: "接口未返回受支持的 source=device 或 source=demo，不可用于现场或生产判断",
    sourceNoneTitle: "当前没有可用实时读数", sourceNoneDetail: "历史记录不会冒充实时值，请检查设备和采集服务",
    plantVerdict: "全厂判定", healthScore: "监控覆盖率", operatingRooms: "运行区域", activeAlarms: "活动报警", pendingActions: "待办事项",
    environmentOverview: "环境概况", averageTemperature: "平均温度", averageHumidity: "平均湿度", withinRange: "新鲜均值", noCurrentValue: "无当前值", keyChannel: "关键通道", particleChannel: "≥ 0.5 µm 粒子", plantAverageUnit: "新鲜设备均值 · particles/ft³",
    dataTrust: "数据可信度", liveCompleteness: "监控覆盖率", latestUpdate: "最近更新", collectorHealthy: "所有设备均有新鲜读数", collectorDegraded: "部分设备读数不可用", collectorSetupGap: "{count} 个车间尚未配置设备", noDevices: "当前账号尚未配置设备", freshOnly: "汇总值仅使用新鲜实时读数",
    areaSituation: "区域态势", locateIssues: "一眼定位异常区域", statusLegend: "状态图例", maintenance: "维护中", verdictMaintenanceKicker: "{count} 台设备处于计划维护", verdictMaintenanceTitle: "设备维护中", verdictMaintenanceAction: "数据和报警继续记录；维护设备的云端电邮暂停", normal: "正常", watch: "待观察", alarm: "报警", offline: "离线", stale: "数据过期", selectedMarker: "已选中", noDevicesInRoom: "未配置设备",
    diagramDisclaimer: "区域示意图 · 非真实厂区平面图", selectedRoom: "当前选中", deviceAvailability: "设备可用", temperature: "温度", humidity: "湿度", selectedRoomDevicesLabel: "当前车间设备", devicesInRoom: "车间内设备", previousRoomPage: "上一页车间", nextRoomPage: "下一页车间", moreDevices: "另有 {count} 台", moreDevicesHint: "优先展示异常设备",
    trend24h: "24 小时趋势", particleChange: "全厂 ≥ 0.5 µm 最高值", plantPeak: "全厂峰值", alarmLimit: "最严格上限", now: "现在", noTrend: "暂无趋势数据",
    needsAttention: "需要关注", noActionNeeded: "当前无需处理", allNormalFresh: "全部区域数据正常且新鲜", areaOperation: "区域运行", roomListUnit: "按状态与 ≥ 0.5 µm 最高读数排序 · particles/ft³",
    todayOperation: "当前会话", stableRuntime: "数据服务已连接", hours: "时长", shiftAlarms: "活动报警", times: "项", resolvedIssues: "最近已恢复报警", items: "项",
    plantOverview: "全厂总览", refreshRate: "实时数据刷新", tenSeconds: "10 秒", alarmRule: "报警延迟", delaySeconds: "连续 {count} 秒超限", delaySecondsRange: "连续 {min}–{max} 秒超限", currentVersion: "当前版本", productionVersion: "数据接入 V1",
    verdictNormalKicker: "所有新鲜数据均在正常范围", verdictNormalTitle: "全厂正常", verdictNormalAction: "当前无需处理，可继续按现场 SOP 运行",
    verdictWatchKicker: "{count} 台设备正在等待报警确认", verdictWatchTitle: "需要持续观察", verdictWatchAction: "尚未形成活动报警，请关注对应设备",
    verdictAlarmKicker: "{count} 项活动报警正在发生", verdictAlarmTitle: "需要处理", verdictAlarmAction: "请联系对应车间负责人并确认现场",
    verdictGapKicker: "{count} 台设备没有新鲜数据", verdictGapTitle: "数据不完整", verdictGapAction: "不可用最后历史读数判断现场，请先恢复采集",
    actionNone: "无需处理", actionTotalCount: "{count} 项需要关注", actionWatchCount: "{count} 项待观察", actionAlarmCount: "{count} 项活动报警", actionGapCount: "{count} 项数据异常", noShutdown: "持续观察", needsAction: "需要处理", verifyData: "先恢复数据",
    roomCount: "{count} 个区域", allOnline: "全部在线", roomsAffected: "{count} 个区域受影响", freshDevices: "{fresh} / {total} 设备有新鲜数据", deviceCount: "{count} 台", onlineDevices: "{online}/{total} 台在线", deviceIssueCount: "{count} 台异常",
    justNow: "刚刚", minutesAgo: "{count} 分钟前", runningNormally: "运行正常", dataUnavailable: "设备数据不可用", neverReported: "从未上报", unknownRoom: "未命名车间",
    actionWatchTag: "待观察", actionAlarmTag: "立即处理", actionOfflineTag: "采集异常", actionStaleTag: "数据过期", actionSetupTag: "待配置", duration: "持续 {duration}", owner: "建议", watchAction: "继续观察；若形成活动报警，按现场 SOP 处理。", alarmAction: "确认现场并按 SOP 记录处置过程。", offlineAction: "检查设备供电、网络和采集器状态。", staleAction: "确认采集是否恢复；恢复前不参与汇总判断。", configureDeviceAction: "该车间尚未配置设备，请先完成设备归属和采集验证。", unconfiguredDevice: "未配置设备",
    pause: "暂停", resume: "继续", rotationActive: "区域自动轮播", rotationPaused: "区域轮播已暂停", retry: "重试", login: "返回登录", authTitle: "登录已失效", authDetail: "大屏只展示当前账号有权访问的数据，请重新登录客户工作台。", loadTitle: "数据加载失败", loadDetail: "无法读取大屏基础配置，请检查服务状态后重试。", emptyTitle: "尚未配置车间", emptyDetail: "当前账号没有可展示的车间，请先在客户工作台完成配置。"
  },
  "en-US": {
    wallboardLabel: "HawkHive plant clean environment wallboard", brandTagline: "CLEAN ENVIRONMENT INTELLIGENCE", screenNumber: "SCREEN 01",
    chartDescription: "Highest 0.5 micrometer particle readings across all authorized areas in the last 24 hours",
    pageTitle: "HawkHive Plant Cleanliness Wallboard", language: "Language", factoryName: "Current customer", operationsCenter: "Clean Environment Operations", screenTitle: "Plant Cleanliness Overview",
    screenControls: "Wallboard status and controls", apiStatus: "API status", connecting: "Connecting", connected: "Connected", degraded: "Partially unavailable", disconnected: "Connection failed", enterFullscreen: "Enter fullscreen", fullscreen: "Fullscreen", loadingTitle: "Loading wallboard", loadingDetail: "Validating the session and loading monitoring data.",
    sourceRealStamp: "LIVE DEVICE DATA", sourceDemoStamp: "SIMULATED COLLECTION", sourceMixedStamp: "MIXED DATA SOURCES", sourceUnknownStamp: "SOURCE UNVERIFIED", sourceNoneStamp: "NO FRESH DATA",
    sourceRealTitle: "Current readings are from devices", sourceRealDetail: "The API reports source=device; verify against the collection path and the physical site when required",
    sourceDemoTitle: "Simulated data is present", sourceDemoDetail: "The API reports source=demo. For demonstration only — not for production decisions",
    sourceMixedTitle: "Data sources are mixed", sourceMixedDetail: "Device and simulated readings coexist; aggregate values must not be used for production decisions",
    sourceUnknownTitle: "The current data source is unverified", sourceUnknownDetail: "The API did not return a supported source=device or source=demo value. Do not use it for on-site or production decisions",
    sourceNoneTitle: "No current readings are available", sourceNoneDetail: "Historical records are never presented as live values. Check devices and collection services",
    plantVerdict: "Plant verdict", healthScore: "Monitoring coverage", operatingRooms: "Operating areas", activeAlarms: "Active alarms", pendingActions: "Action items",
    environmentOverview: "Environment", averageTemperature: "Average temperature", averageHumidity: "Average humidity", withinRange: "Fresh average", noCurrentValue: "No current value", keyChannel: "Key channel", particleChannel: "≥ 0.5 µm particles", plantAverageUnit: "Fresh-device average · particles/ft³",
    dataTrust: "Data confidence", liveCompleteness: "Monitoring coverage", latestUpdate: "Latest update", collectorHealthy: "Every device has a fresh reading", collectorDegraded: "Some device readings are unavailable", collectorSetupGap: "{count} area(s) have no configured device", noDevices: "No devices are configured for this account", freshOnly: "Aggregate values only use fresh live readings",
    areaSituation: "Area status", locateIssues: "Locate affected areas at a glance", statusLegend: "Status legend", maintenance: "Maintenance", verdictMaintenanceKicker: "{count} device(s) in planned maintenance", verdictMaintenanceTitle: "Equipment under maintenance", verdictMaintenanceAction: "Data and alarms remain recorded; cloud emails are paused for these devices", normal: "Normal", watch: "Watch", alarm: "Alarm", offline: "Offline", stale: "Stale", selectedMarker: "Selected", noDevicesInRoom: "No devices configured",
    diagramDisclaimer: "Area diagram · not an actual floor plan", selectedRoom: "Selected", deviceAvailability: "Devices available", temperature: "Temperature", humidity: "Humidity", selectedRoomDevicesLabel: "Devices in selected area", devicesInRoom: "Devices in area", previousRoomPage: "Previous area page", nextRoomPage: "Next area page", moreDevices: "{count} more", moreDevicesHint: "Issues are shown first",
    trend24h: "24-hour trend", particleChange: "Plant max ≥ 0.5 µm", plantPeak: "Plant peak", alarmLimit: "Strictest limit", now: "Now", noTrend: "No trend data",
    needsAttention: "Needs attention", noActionNeeded: "No action required", allNormalFresh: "All areas are normal and reporting fresh data", areaOperation: "Area operation", roomListUnit: "Sorted by status and highest ≥ 0.5 µm reading · particles/ft³",
    todayOperation: "Current session", stableRuntime: "Data service connected", hours: "Elapsed", shiftAlarms: "Active alarms", times: "items", resolvedIssues: "Recently cleared alarms", items: "items",
    plantOverview: "Plant overview", refreshRate: "Live refresh", tenSeconds: "10 seconds", alarmRule: "Alarm delay", delaySeconds: "{count} seconds continuously over limit", delaySecondsRange: "{min}–{max} seconds continuously over limit", currentVersion: "Version", productionVersion: "API integration V1",
    verdictNormalKicker: "All fresh readings are within limits", verdictNormalTitle: "Plant normal", verdictNormalAction: "No action required; continue operating under site SOPs",
    verdictWatchKicker: "{count} device(s) are waiting for alarm confirmation", verdictWatchTitle: "Continue watching", verdictWatchAction: "No active alarm yet; monitor the affected devices",
    verdictAlarmKicker: "{count} active alarm(s) are in progress", verdictAlarmTitle: "Action required", verdictAlarmAction: "Contact the responsible area and verify conditions on site",
    verdictGapKicker: "{count} device(s) have no fresh data", verdictGapTitle: "Data incomplete", verdictGapAction: "Do not infer site conditions from historical values; restore collection first",
    actionNone: "No action required", actionTotalCount: "{count} item(s) need attention", actionWatchCount: "{count} item(s) to watch", actionAlarmCount: "{count} active alarm(s)", actionGapCount: "{count} data issue(s)", noShutdown: "Keep watching", needsAction: "Action required", verifyData: "Restore data first",
    roomCount: "{count} areas", allOnline: "All online", roomsAffected: "{count} areas affected", freshDevices: "{fresh} / {total} devices reporting fresh data", deviceCount: "{count} devices", onlineDevices: "{online}/{total} online", deviceIssueCount: "{count} issue(s)",
    justNow: "Just now", minutesAgo: "{count} min ago", runningNormally: "Operating normally", dataUnavailable: "Device data unavailable", neverReported: "Never reported", unknownRoom: "Unnamed area",
    actionWatchTag: "WATCH", actionAlarmTag: "ACT NOW", actionOfflineTag: "COLLECTION", actionStaleTag: "STALE DATA", actionSetupTag: "SETUP", duration: "For {duration}", owner: "Guidance", watchAction: "Continue observing; follow the site SOP if an active alarm is confirmed.", alarmAction: "Verify on site and record the response according to SOP.", offlineAction: "Check device power, network and collector health.", staleAction: "Confirm collection recovery; exclude stale data from aggregate decisions.", configureDeviceAction: "No device is assigned to this area. Complete device assignment and collection verification first.", unconfiguredDevice: "No device configured",
    pause: "Pause", resume: "Resume", rotationActive: "Auto-rotating areas", rotationPaused: "Area rotation paused", retry: "Retry", login: "Return to login", authTitle: "Session expired", authDetail: "The wallboard only displays data available to the current account. Sign in again to continue.", loadTitle: "Unable to load data", loadDetail: "Wallboard APIs are unavailable. Check the service and retry.", emptyTitle: "No areas configured", emptyDetail: "There are no areas to display for this account. Complete setup in the customer workspace first."
  }
};

const $ = (id) => document.getElementById(id);
const localeStorageKey = "hawkhive-locale";
const browserLocale = String(navigator.language || "").toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
const roomSlots = [[6, 8, 27, 36], [36.5, 8, 27, 36], [67, 8, 27, 36], [6, 51, 27, 36], [36.5, 51, 27, 36], [67, 51, 27, 36]];
const roomsPerPage = roomSlots.length;
const freshWindowMs = 45_000;
const sessionStartedAt = Date.now();
let locale = translations[localStorage.getItem(localeStorageKey)]
  ? localStorage.getItem(localeStorageKey)
  : browserLocale;
let rooms = [];
let latestReadings = [];
let alarmEvents = [];
let trendReadings = [];
let currentUser = null;
let selectedRoom = "";
let rotationIndex = 0;
let roomPage = 0;
let rotationPaused = false;
let lastSyncAt = null;
let loading = false;
let apiState = "connecting";
let apiErrors = [];
let blockingType = "loading";
let blockingMessage = "";
let latestByDevice = new Map();

function t(key, values = {}) {
  let value = translations[locale][key] ?? key;
  for (const [name, replacement] of Object.entries(values)) value = value.replaceAll(`{${name}}`, String(replacement));
  return value;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function formatNumber(value, digits = 0) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat(locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(value)) : "—";
}

function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number >= 1000 ? `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(number / 1000)}k` : formatNumber(number);
}

function readingFor(deviceId) {
  return latestByDevice.get(deviceId) || null;
}

function setLatestReadings(readings) {
  latestReadings = Array.isArray(readings) ? readings : [];
  latestByDevice = new Map(latestReadings.map((reading) => [reading.device_id, reading]));
}

function readingTimestamp(reading) {
  return Number(reading?.last_reading_at ?? reading?.timestamp ?? 0) * 1000;
}

function isFresh(device) {
  const reading = readingFor(device.id);
  return Boolean(reading?.online && readingTimestamp(reading) > 0 && Math.abs(Date.now() - readingTimestamp(reading)) <= freshWindowMs);
}

function deviceState(device) {
  const reading = readingFor(device.id);
  if (!reading?.online) return "offline";
  if (!isFresh(device)) return "stale";
  if (reading.alarm_status === "ALARM_ACTIVE" || (reading.alarm_details || []).some((detail) => ["ALARM_ACTIVE", "PENDING_CLEAR"].includes(detail.state))) return "alarm";
  if (reading.alarm_status === "PENDING" || (reading.alarm_details || []).some((detail) => detail.state === "PENDING_ALARM")) return "watch";
  const maintenance = reading && Object.prototype.hasOwnProperty.call(reading, "maintenance") ? reading.maintenance : device.maintenance;
  if (maintenance?.until > Date.now()/1000) return "maintenance";
  return "normal";
}

function statePriority(state) { return { alarm: 5, offline: 4, stale: 4, watch: 3, maintenance: 2, normal: 1 }[state] || 0; }
function roomState(room) { return room.devices.length ? room.devices.map(deviceState).sort((a, b) => statePriority(b) - statePriority(a))[0] : "offline"; }
function particleValue(reading) { return Number(reading?.particles?.pm_0_5_um); }
function temperatureValue(reading) { return Number(reading?.environment?.temperature); }
function humidityValue(reading) { return Number(reading?.environment?.humidity); }
function freshReadingsFor(room = null) {
  const devices = room ? room.devices : rooms.flatMap((item) => item.devices);
  return devices.filter(isFresh).map((device) => readingFor(device.id)).filter(Boolean);
}

function average(values) {
  const valid = values.filter(Number.isFinite);
  return valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : NaN;
}

function roomSummary(room) {
  if (!room.devices.length) return t("noDevicesInRoom");
  const states = room.devices.map(deviceState);
  const issues = states.filter((state) => state !== "normal").length;
  const online = states.filter((state) => !["offline", "stale"].includes(state)).length;
  return issues ? `${t("onlineDevices", { online, total: room.devices.length })} · ${t("deviceIssueCount", { count: issues })}` : t("onlineDevices", { online, total: room.devices.length });
}

function ageLabel(timestamp) {
  if (!timestamp) return t("neverReported");
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
  return minutes < 1 ? t("justNow") : t("minutesAgo", { count: minutes });
}

function durationLabel(startSeconds) {
  if (!startSeconds) return "—";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(startSeconds)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store" });
  let payload = null;
  try { payload = await response.json(); } catch { throw new Error(`HTTP ${response.status}`); }
  if (response.status === 401) { const error = new Error("AUTH_REQUIRED"); error.code = "AUTH_REQUIRED"; throw error; }
  if (!response.ok || payload?.ok === false) throw new Error(payload?.error || `HTTP ${response.status}`);
  return payload.data;
}

function renderStaticTranslations() {
  document.documentElement.lang = locale;
  document.title = t("pageTitle");
  document.querySelectorAll("[data-i18n]").forEach((element) => { element.textContent = t(element.dataset.i18n); });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => { element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel)); });
  document.querySelectorAll("[data-locale]").forEach((button) => {
    const active = button.dataset.locale === locale;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $("blockingAction").textContent = t("login");
  $("retryButton").textContent = t("retry");
  if (blockingType && blockingType !== "loading") renderBlockingCopy();
}

function normaliseRooms(config) {
  return (Array.isArray(config) ? config : []).map((room) => ({ ...room, name: String(room?.name || t("unknownRoom")), devices: Array.isArray(room?.devices) ? room.devices : [], thresholds: room?.thresholds || {} }));
}

function settledData(result, label, fallback) {
  if (result.status === "fulfilled") return result.value;
  if (result.reason?.code === "AUTH_REQUIRED") throw result.reason;
  apiErrors.push(label);
  return fallback;
}

function deriveActions() {
  const actions = [];
  const persistedKeys = new Set(alarmEvents.filter((event) => !event.ended_at).map((event) => `${event.device_id}:${event.metric}`));
  for (const event of alarmEvents.filter((item) => !item.ended_at)) {
    actions.push({ state: "alarm", roomId: event.cleanroom_id, deviceId: event.device_id, metric: event.metric, value: Number(event.peak_value ?? event.trigger_value), threshold: event.limit_description, startedAt: event.started_at });
  }
  for (const room of rooms) {
    if (!room.devices.length) actions.push({ state: "offline", roomId: room.id, deviceId: "", metric: "room_without_devices", startedAt: null, setupRequired: true });
    for (const device of room.devices) {
    const reading = readingFor(device.id);
    const state = deviceState(device);
    if (["offline", "stale"].includes(state)) actions.push({ state, roomId: room.id, deviceId: device.id, startedAt: readingTimestamp(reading) / 1000 });
    for (const detail of reading?.alarm_details || []) {
      const key = `${device.id}:${detail.metric}`;
      if (detail.state === "PENDING_ALARM") actions.push({ state: "watch", roomId: room.id, deviceId: device.id, metric: detail.metric, value: Number(detail.value), threshold: detail.limit, startedAt: detail.pending_since });
      else if (["ALARM_ACTIVE", "PENDING_CLEAR"].includes(detail.state) && !persistedKeys.has(key)) actions.push({ state: "alarm", roomId: room.id, deviceId: device.id, metric: detail.metric, value: Number(detail.value), threshold: detail.limit, startedAt: detail.active_since || detail.pending_since });
    }
    }
  }
  return actions.sort((a, b) => statePriority(b.state) - statePriority(a.state) || Number(a.startedAt || 0) - Number(b.startedAt || 0));
}

function renderSourceProof() {
  const fresh = freshReadingsFor();
  const sources = new Set(fresh.map((reading) => reading.source).filter(Boolean));
  let mode = "none";
  if (sources.size > 1) mode = "mixed";
  else if (sources.has("demo")) mode = "demo";
  else if (sources.has("device")) mode = "real";
  else if (sources.size) mode = "unknown";
  const details = {
    real: ["DEV", "sourceRealStamp", "sourceRealTitle", "sourceRealDetail"], demo: ["SIM", "sourceDemoStamp", "sourceDemoTitle", "sourceDemoDetail"],
    mixed: ["MIX", "sourceMixedStamp", "sourceMixedTitle", "sourceMixedDetail"], unknown: ["?", "sourceUnknownStamp", "sourceUnknownTitle", "sourceUnknownDetail"], none: ["—", "sourceNoneStamp", "sourceNoneTitle", "sourceNoneDetail"]
  }[mode];
  $("sourceProofIcon").textContent = details[0];
  $("sourceStampText").textContent = t(details[1]);
  $("sourceProofTitle").textContent = t(details[2]);
  $("sourceProofDetail").textContent = t(details[3]);
  $("sourceStamp").className = `demo-stamp source-${mode}`;
  $("sourceProofIcon").style.background = mode === "real" ? "var(--lime)" : mode === "none" ? "var(--blue)" : "var(--amber)";
}

function renderRooms() {
  const pageCount = Math.max(1, Math.ceil(rooms.length / roomsPerPage));
  roomPage = Math.min(roomPage, pageCount - 1);
  const visible = rooms.slice(roomPage * roomsPerPage, (roomPage + 1) * roomsPerPage);
  $("roomPageIndicator").textContent = `${roomPage + 1} / ${pageCount}`;
  $("previousRoomPage").disabled = pageCount < 2;
  $("nextRoomPage").disabled = pageCount < 2;
  $("roomZoneList").innerHTML = visible.map((room, index) => {
    const state = roomState(room);
    const [x, y, width, height] = roomSlots[index];
    const dots = room.devices.slice(0, 16).map((device) => `<i class="${deviceState(device)}" title="${escapeHtml(device.name)}"></i>`).join("");
    const code = String(room.sort_order || rooms.indexOf(room) + 1).padStart(2, "0");
    return `<button class="room-zone ${state} ${selectedRoom === room.id ? "selected" : ""}" type="button" data-room="${escapeHtml(room.id)}" data-selected-label="${t("selectedMarker")}" style="--x:${x}%;--y:${y}%;--w:${width}%;--h:${height}%"><span class="room-code">AREA-${code}</span><span class="device-dots" aria-hidden="true">${dots}</span><strong>${escapeHtml(room.name || t("unknownRoom"))}</strong><small>${roomSummary(room)}</small><i class="room-state">${t(state)}</i></button>`;
  }).join("");
  $("roomZoneList").querySelectorAll("[data-room]").forEach((button) => button.addEventListener("click", () => selectRoom(button.dataset.room)));
}

function renderSelectedRoom() {
  const room = rooms.find((candidate) => candidate.id === selectedRoom) || rooms[0];
  if (!room) return;
  const readings = freshReadingsFor(room);
  const states = room.devices.map(deviceState);
  const available = states.filter((state) => !["offline", "stale"].includes(state)).length;
  $("selectedRoomName").textContent = room.name;
  $("selectedRoomDevices").textContent = `${available} / ${room.devices.length}`;
  $("selectedRoomParticle").textContent = formatNumber(average(readings.map(particleValue)));
  $("selectedRoomTemp").textContent = Number.isFinite(average(readings.map(temperatureValue))) ? `${formatNumber(average(readings.map(temperatureValue)), 1)}°C` : "—";
  $("selectedRoomHumidity").textContent = Number.isFinite(average(readings.map(humidityValue))) ? `${formatNumber(average(readings.map(humidityValue)), 1)}%RH` : "—";
  $("selectedDeviceCount").textContent = t("deviceCount", { count: room.devices.length });
  const sorted = [...room.devices].sort((a, b) => statePriority(deviceState(b)) - statePriority(deviceState(a)));
  const visible = sorted.length > 4 ? sorted.slice(0, 3) : sorted;
  const cards = visible.map((device) => {
    const reading = readingFor(device.id);
    const state = deviceState(device);
    const current = isFresh(device);
    return `<article class="device-mini ${state}"><span class="status-dot ${state}"></span><p><strong>${escapeHtml(device.name)}</strong><small>${escapeHtml(device.id)} · ${ageLabel(readingTimestamp(reading))}</small></p><div><strong>${current ? formatNumber(particleValue(reading)) : "—"}</strong><small>≥ 0.5 µm</small></div></article>`;
  });
  if (sorted.length > 4) cards.push(`<article class="device-mini more-devices"><p><strong>${t("moreDevices", { count: sorted.length - 3 })}</strong><small>${t("moreDevicesHint")}</small></p></article>`);
  $("selectedDeviceList").innerHTML = cards.join("");
  const newest = Math.max(0, ...room.devices.map((device) => readingTimestamp(readingFor(device.id))));
  $("selectedRoomUpdated").textContent = ageLabel(newest);
}

function renderRoomList() {
  const sorted = [...rooms].sort((a, b) => statePriority(roomState(b)) - statePriority(roomState(a)) || Math.max(0, ...freshReadingsFor(b).map(particleValue)) - Math.max(0, ...freshReadingsFor(a).map(particleValue)));
  $("roomList").innerHTML = sorted.slice(0, 6).map((room) => {
    const state = roomState(room);
    const online = room.devices.filter(isFresh).length;
    const highest = Math.max(0, ...freshReadingsFor(room).map(particleValue).filter(Number.isFinite));
    return `<li data-room-list="${escapeHtml(room.id)}"><span class="status-dot ${state}"></span><p><strong>${escapeHtml(room.name)}</strong><small>${t("onlineDevices", { online, total: room.devices.length })}</small></p><b>${highest ? compactNumber(highest) : "—"}</b></li>`;
  }).join("");
  $("roomList").querySelectorAll("[data-room-list]").forEach((item) => item.addEventListener("click", () => selectRoom(item.dataset.roomList)));
}

function renderActions() {
  const actions = deriveActions();
  $("actionList").hidden = actions.length === 0;
  $("emptyAction").hidden = actions.length !== 0;
  $("actionList").classList.toggle("multi", actions.length > 1);
  $("actionList").innerHTML = actions.slice(0, 2).map((action) => {
    const room = rooms.find((item) => item.id === action.roomId);
    const device = room?.devices.find((item) => item.id === action.deviceId);
    const tag = action.setupRequired ? "actionSetupTag" : { alarm: "actionAlarmTag", watch: "actionWatchTag", offline: "actionOfflineTag", stale: "actionStaleTag" }[action.state];
    const instruction = action.setupRequired ? "configureDeviceAction" : { alarm: "alarmAction", watch: "watchAction", offline: "offlineAction", stale: "staleAction" }[action.state];
    const reading = Number.isFinite(action.value) ? `<div class="action-value"><strong>${formatNumber(action.value)}</strong><span>${escapeHtml(action.threshold || action.metric || "")}</span></div>` : `<div class="action-data-gap"><strong>—</strong><span>${t("dataUnavailable")}</span></div>`;
    const identity = action.setupRequired ? t("unconfiguredDevice") : escapeHtml(action.metric || action.deviceId);
    const title = action.setupRequired ? escapeHtml(room?.name || action.roomId) : `${escapeHtml(room?.name || action.roomId)} · ${escapeHtml(device?.name || action.deviceId)}`;
    const time = action.startedAt ? `<time>${t("duration", { duration: durationLabel(action.startedAt) })}</time>` : "";
    return `<article class="action-card ${action.state}"><div class="action-card__top"><span>${t(tag)}</span>${time}</div><h3>${title}</h3><small class="device-identity">${identity}</small>${reading}<p><i>${t(tag)}</i> ${t(instruction)}</p></article>`;
  }).join("");
}

function renderTrend() {
  const end = Date.now() / 1000;
  const windowStart = end - 86400;
  const points = trendReadings.filter((reading) => reading?.particles?.pm_0_5_um != null && reading.particles.pm_0_5_um !== "").map((reading) => ({ timestamp: Number(reading.timestamp), value: particleValue(reading) })).filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value) && point.timestamp >= windowStart && point.timestamp <= end);
  const thresholds = rooms.flatMap((room) => {
    const policy = room.thresholds || {};
    const enabled = policy.particle_0_5_enabled === undefined
      ? true
      : Boolean(policy.particle_0_5_enabled);
    const value = Number(policy.particle_0_5_max ?? policy.particle_5_max);
    return enabled && Number.isFinite(value) ? [value] : [];
  });
  const threshold = thresholds.length ? Math.min(...thresholds) : NaN;
  const peak = points.length ? Math.max(...points.map((point) => point.value)) : NaN;
  const maxY = Math.max(1000, Number.isFinite(peak) ? peak * 1.12 : 0, Number.isFinite(threshold) ? threshold * 1.12 : 0);
  document.querySelectorAll(".y-axis span").forEach((label, index) => { label.textContent = compactNumber(maxY * (1 - index / 4)); });
  $("peakValue").textContent = formatNumber(peak);
  // Trim only the leading empty window; keep elapsed time and gaps truthful.
  const start = points.length ? Math.min(...points.map((point) => point.timestamp)) : windowStart;
  const span = Math.max(1, end - start);
  const bucketSeconds = Math.max(1, span / 239);
  const bucketed = new Map();
  for (const point of points) {
    const bucket = Math.min(239, Math.floor((point.timestamp - start) / bucketSeconds));
    bucketed.set(bucket, Math.max(bucketed.get(bucket) ?? -Infinity, point.value));
  }
  const chartPoints = [...bucketed.entries()].sort((a, b) => a[0] - b[0]).map(([bucket, value]) => ({ bucket, x: bucket * bucketSeconds / span * 1000, y: 238 - value / maxY * 232, value }));
  $("trendEmpty").hidden = chartPoints.length > 0;
  const segments = [];
  for (const point of chartPoints) {
    if (!segments.length || (point.bucket - segments.at(-1).at(-1).bucket) * bucketSeconds > Math.max(90, bucketSeconds * 3)) segments.push([]);
    segments.at(-1).push(point);
  }
  const segmentPath = (segment) => segment.length === 1
    ? `M${segment[0].x.toFixed(1)} ${segment[0].y.toFixed(1)} L${Math.min(1000, segment[0].x + 4).toFixed(1)} ${segment[0].y.toFixed(1)}`
    : segment.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  $("trendMainPath").setAttribute("d", segments.map(segmentPath).join(" "));
  $("trendAreaPath").setAttribute("d", segments.filter((segment) => segment.length > 1).map((segment) => `${segmentPath(segment)} L${segment.at(-1).x.toFixed(1)} 245 L${segment[0].x.toFixed(1)} 245Z`).join(" "));
  const peakPoint = chartPoints.find((point) => point.value === peak);
  $("peakDot").hidden = !peakPoint;
  if (peakPoint) $("peakDot").querySelectorAll("circle").forEach((circle) => { circle.setAttribute("cx", peakPoint.x.toFixed(1)); circle.setAttribute("cy", peakPoint.y.toFixed(1)); });
  if (Number.isFinite(threshold)) {
    const y = 238 - threshold / maxY * 232;
    $("thresholdPath").setAttribute("d", `M0 ${y.toFixed(1)}H1000`);
    $("thresholdLabel").innerHTML = `<span>${t("alarmLimit")}</span> ${formatNumber(threshold)}`;
    $("thresholdLabel").style.top = `${Math.max(4, Math.min(88, y / 245 * 100))}%`;
  } else {
    $("thresholdPath").setAttribute("d", "");
    $("thresholdLabel").innerHTML = `<span>${t("alarmLimit")}</span> —`;
  }
  const axisLabels = [...document.querySelectorAll(".x-axis span")];
  axisLabels.forEach((label, index) => {
    const timestamp = start + (end - start) * index / (axisLabels.length - 1);
    label.textContent = index === axisLabels.length - 1 ? t("now") : new Date(timestamp * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", ...(span < 300 ? { second: "2-digit" } : {}), hour12: false });
  });
}

function renderVerdict() {
  const allDevices = rooms.flatMap((room) => room.devices);
  const fresh = allDevices.filter(isFresh);
  const states = allDevices.map(deviceState);
  const actions = deriveActions();
  const activeAlarmCount = actions.filter((action) => action.state === "alarm").length;
  const watchCount = states.filter((state) => state === "watch").length;
  const maintenanceCount = states.filter((state) => state === "maintenance").length;
  const emptyRoomCount = rooms.filter((room) => !room.devices.length).length;
  const gapCount = states.filter((state) => ["offline", "stale"].includes(state)).length + emptyRoomCount;
  let mode = "normal";
  if (activeAlarmCount) mode = "alarm";
  else if (gapCount) mode = "dataGap";
  else if (watchCount) mode = "watch";
  else if (maintenanceCount) mode = "maintenance";
  document.body.dataset.scenario = mode;
  const completenessDenominator = allDevices.length + rooms.filter((room) => !room.devices.length).length;
  const completeness = completenessDenominator ? Math.round(fresh.length / completenessDenominator * 100) : 0;
  const score = completeness;
  const count = mode === "alarm" ? activeAlarmCount : mode === "watch" ? watchCount : mode === "maintenance" ? maintenanceCount : gapCount;
  $("healthScore").textContent = `${score}%`;
  document.querySelector(".verdict-orbit").style.setProperty("--score", score);
  const verdictKey = mode === "dataGap" ? "Gap" : `${mode[0].toUpperCase()}${mode.slice(1)}`;
  $("verdictKicker").textContent = t(`verdict${verdictKey}Kicker`, { count });
  $("verdictTitle").textContent = t(`verdict${verdictKey}Title`);
  $("verdictAction").textContent = t(`verdict${verdictKey}Action`);
  $("onlineRoomCount").textContent = rooms.filter((room) => room.devices.some(isFresh)).length;
  $("activeAlarmCount").textContent = activeAlarmCount;
  $("pendingActionCount").textContent = actions.length;
  $("shiftAlarmCount").textContent = activeAlarmCount;
  $("resolvedAlarmCount").textContent = alarmEvents.filter((event) => event.ended_at).length;
  $("actionTitle").textContent = actions.length ? t("actionTotalCount", { count: actions.length }) : t("actionNone");
  $("actionSeverity").hidden = actions.length === 0;
  $("actionSeverity").textContent = t(activeAlarmCount ? "needsAction" : gapCount ? "verifyData" : "noShutdown");
  $("actionSeverity").className = `severity ${activeAlarmCount ? "alarm" : gapCount ? "offline" : "watch"}`;
  $("roomCountTitle").textContent = t("roomCount", { count: rooms.length });
  const affectedRooms = rooms.filter((room) => roomState(room) !== "normal").length;
  $("roomAvailabilityChip").textContent = affectedRooms ? t("roomsAffected", { count: affectedRooms }) : t("allOnline");
  $("roomAvailabilityChip").classList.toggle("offline", affectedRooms > 0);
  $("dataCompleteness").textContent = `${completeness}%`;
  $("dataCompleteness").nextElementSibling.textContent = t("liveCompleteness");
  $("freshDeviceSummary").textContent = allDevices.length ? t("freshDevices", { fresh: fresh.length, total: allDevices.length }) : t("noDevices");
  $("collectorSummary").textContent = emptyRoomCount ? t("collectorSetupGap", { count: emptyRoomCount }) : gapCount ? t("collectorDegraded") : t("collectorHealthy");
  $("freshnessItem").classList.toggle("offline", completeness < 100);
  $("collectorItem").classList.toggle("offline", gapCount > 0);
  const readings = freshReadingsFor();
  const temp = average(readings.map(temperatureValue));
  const humidity = average(readings.map(humidityValue));
  const particles = average(readings.map(particleValue));
  $("averageTemperature").innerHTML = Number.isFinite(temp) ? `${formatNumber(temp, 1)}<small>°C</small>` : "—";
  $("averageHumidity").innerHTML = Number.isFinite(humidity) ? `${formatNumber(humidity, 1)}<small>%RH</small>` : "—";
  document.querySelector(".environment-reading.temperature em").textContent = t(Number.isFinite(temp) ? "withinRange" : "noCurrentValue");
  document.querySelector(".environment-reading.humidity em").textContent = t(Number.isFinite(humidity) ? "withinRange" : "noCurrentValue");
  const temperatureBounds = rooms.flatMap((room) => [Number(room.thresholds?.temperature_min), Number(room.thresholds?.temperature_max)]).filter(Number.isFinite);
  const humidityBounds = rooms.flatMap((room) => [Number(room.thresholds?.humidity_min), Number(room.thresholds?.humidity_max)]).filter(Number.isFinite);
  const railPosition = (value, bounds) => {
    if (!Number.isFinite(value) || bounds.length < 2) return 0;
    const min = Math.min(...bounds); const max = Math.max(...bounds);
    return max > min ? Math.max(0, Math.min(100, (value - min) / (max - min) * 100)) : 50;
  };
  document.querySelector(".environment-reading.temperature .range-rail span").style.setProperty("--value", `${railPosition(temp, temperatureBounds)}%`);
  document.querySelector(".environment-reading.humidity .range-rail span").style.setProperty("--value", `${railPosition(humidity, humidityBounds)}%`);
  $("particleAverage").textContent = formatNumber(particles);
  const newest = Math.max(0, ...latestReadings.map(readingTimestamp));
  $("latestReadingTime").textContent = ageLabel(newest);
}

function renderAll() {
  renderStaticTranslations();
  const factoryLabel = document.querySelector('[data-i18n="factoryName"]');
  if (factoryLabel && currentUser?.display_name) factoryLabel.textContent = currentUser.display_name;
  document.querySelector(".chart-shell").setAttribute("aria-label", t("chartDescription"));
  renderSourceProof();
  renderVerdict();
  renderRooms();
  renderSelectedRoom();
  renderRoomList();
  renderActions();
  renderTrend();
  const delays = rooms.map((room) => Number(room.thresholds?.alarm_delay_seconds)).filter(Number.isFinite);
  const minDelay = delays.length ? Math.min(...delays) : NaN; const maxDelay = delays.length ? Math.max(...delays) : NaN;
  $("alarmDelayRule").textContent = !delays.length ? "—" : minDelay === maxDelay ? t("delaySeconds", { count: maxDelay }) : t("delaySecondsRange", { min: minDelay, max: maxDelay });
  renderApiStatus();
}

function renderApiStatus() {
  $("apiStatusText").textContent = t(apiState);
  const synced = lastSyncAt ? lastSyncAt.toLocaleTimeString(locale, { hour12: false }) : "—";
  $("apiLastSync").textContent = apiErrors.length ? `${synced} · ${apiErrors.join(", ")}` : synced;
}

function renderBlockingCopy() {
  const type = blockingType;
  $("blockingTitle").textContent = t(type === "auth" ? "authTitle" : type === "empty" ? "emptyTitle" : type === "loading" ? "loadingTitle" : "loadTitle");
  $("blockingDetail").textContent = blockingMessage || t(type === "auth" ? "authDetail" : type === "empty" ? "emptyDetail" : type === "loading" ? "loadingDetail" : "loadDetail");
  $("blockingAction").hidden = !["auth", "empty"].includes(type);
  $("retryButton").hidden = type !== "error";
  $("blockingIcon").textContent = type === "empty" ? "0" : type === "loading" ? "···" : "!";
}

function showBlocking(type, detail = "") {
  blockingType = type;
  blockingMessage = detail;
  $("blockingState").hidden = false;
  renderBlockingCopy();
  apiState = type === "loading" ? "connecting" : "disconnected";
  renderApiStatus();
}

async function loadInitialData() {
  if (loading) return;
  loading = true;
  apiState = "connecting"; apiErrors = []; renderApiStatus();
  try {
    const end = Date.now() / 1000;
    const [session, config] = await Promise.all([api("/api/session"), api("/api/config")]);
    const [latestResult, alarmsResult, trendsResult] = await Promise.allSettled([
      api("/api/latest"), api("/api/alarms?limit=500"), api(`/api/trends?start=${end - 86400}&end=${end}&max_points=240`)
    ]);
    currentUser = session;
    rooms = normaliseRooms(config);
    setLatestReadings(settledData(latestResult, "latest", []));
    alarmEvents = settledData(alarmsResult, "alarms", []);
    trendReadings = settledData(trendsResult, "trends", []);
    if (!Array.isArray(alarmEvents)) alarmEvents = [];
    if (!Array.isArray(trendReadings)) trendReadings = [];
    if (!rooms.length) { showBlocking("empty"); return; }
    selectedRoom = rooms.some((room) => room.id === selectedRoom) ? selectedRoom : rooms[0].id;
    rotationIndex = Math.max(0, rooms.findIndex((room) => room.id === selectedRoom));
    lastSyncAt = new Date();
    apiState = apiErrors.length ? "degraded" : "connected";
    blockingType = null; blockingMessage = "";
    $("blockingState").hidden = true;
    renderAll();
  } catch (error) {
    showBlocking(error.code === "AUTH_REQUIRED" ? "auth" : "error", error.code === "AUTH_REQUIRED" ? "" : `${t("loadDetail")} (${error.message})`);
  } finally { loading = false; }
}

async function refreshLiveData() {
  if (loading || !rooms.length) return;
  loading = true;
  try {
    apiErrors = apiErrors.filter((name) => !["latest", "alarms"].includes(name));
    const [latestResult, alarmsResult] = await Promise.allSettled([api("/api/latest"), api("/api/alarms?limit=500")]);
    setLatestReadings(settledData(latestResult, "latest", latestReadings));
    alarmEvents = settledData(alarmsResult, "alarms", alarmEvents);
    if (!Array.isArray(alarmEvents)) alarmEvents = [];
    lastSyncAt = new Date();
    apiState = apiErrors.length ? "degraded" : "connected";
    $("blockingState").hidden = true;
    renderAll();
  } catch (error) {
    if (error.code === "AUTH_REQUIRED") showBlocking("auth");
    else {
      apiState = "degraded"; apiErrors = [error.message];
      renderSourceProof(); renderVerdict(); renderRooms(); renderSelectedRoom(); renderRoomList(); renderActions();
      renderApiStatus();
    }
  } finally { loading = false; }
}

async function refreshTrends() {
  if (!rooms.length) return;
  try {
    const end = Date.now() / 1000;
    const [configResult, trendResult] = await Promise.allSettled([api("/api/config"), api(`/api/trends?start=${end - 86400}&end=${end}&max_points=240`)]);
    const refreshErrors = apiErrors.filter((name) => !["config", "trends"].includes(name));
    if (configResult.status === "fulfilled") rooms = normaliseRooms(configResult.value); else if (configResult.reason?.code === "AUTH_REQUIRED") throw configResult.reason; else refreshErrors.push("config");
    if (trendResult.status === "fulfilled") trendReadings = Array.isArray(trendResult.value) ? trendResult.value : []; else if (trendResult.reason?.code === "AUTH_REQUIRED") throw trendResult.reason; else refreshErrors.push("trends");
    apiErrors = refreshErrors;
    apiState = apiErrors.length ? "degraded" : "connected";
    if (!rooms.length) { showBlocking("empty"); return; }
    if (!rooms.some((room) => room.id === selectedRoom)) selectedRoom = rooms[0]?.id || "";
    renderAll();
  } catch (error) {
    if (error.code === "AUTH_REQUIRED") showBlocking("auth");
    else { apiState = "degraded"; apiErrors = ["trends"]; renderApiStatus(); }
  }
}

function updateClock() {
  const now = new Date();
  $("headerClock").textContent = now.toLocaleTimeString(locale, { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("headerClock").dateTime = now.toISOString();
  $("headerDate").textContent = new Intl.DateTimeFormat(locale, { year: "numeric", month: locale === "zh-CN" ? "long" : "short", day: "numeric", weekday: "long" }).format(now);
  const elapsed = Math.floor((Date.now() - sessionStartedAt) / 1000);
  $("connectedRuntime").textContent = `${String(Math.floor(elapsed / 3600)).padStart(2, "0")}:${String(Math.floor(elapsed / 60) % 60).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
}

function selectRoom(roomId, fromRotation = false) {
  selectedRoom = roomId;
  const index = rooms.findIndex((room) => room.id === roomId);
  if (index < 0) return;
  roomPage = Math.floor(index / roomsPerPage);
  if (!fromRotation) rotationIndex = index;
  renderRooms(); renderSelectedRoom();
}

function setLocale(nextLocale) {
  if (!translations[nextLocale] || nextLocale === locale) return;
  locale = nextLocale;
  localStorage.setItem(localeStorageKey, locale);
  renderAll(); updateClock();
  $("rotationLabel").textContent = t(rotationPaused ? "rotationPaused" : "rotationActive");
  $("rotationButton").textContent = t(rotationPaused ? "resume" : "pause");
}

document.querySelectorAll("[data-locale]").forEach((button) => button.addEventListener("click", () => setLocale(button.dataset.locale)));
window.addEventListener("storage", (event) => {
  if (event.key === localeStorageKey && translations[event.newValue] && event.newValue !== locale) {
    locale = event.newValue;
    renderAll(); updateClock();
    $("rotationLabel").textContent = t(rotationPaused ? "rotationPaused" : "rotationActive");
    $("rotationButton").textContent = t(rotationPaused ? "resume" : "pause");
  }
});
$("previousRoomPage").addEventListener("click", () => { const pages = Math.max(1, Math.ceil(rooms.length / roomsPerPage)); roomPage = (roomPage - 1 + pages) % pages; selectRoom(rooms[roomPage * roomsPerPage]?.id); });
$("nextRoomPage").addEventListener("click", () => { const pages = Math.max(1, Math.ceil(rooms.length / roomsPerPage)); roomPage = (roomPage + 1) % pages; selectRoom(rooms[roomPage * roomsPerPage]?.id); });
$("fullscreenButton").addEventListener("click", async () => { if (!document.fullscreenElement) await document.documentElement.requestFullscreen?.(); else await document.exitFullscreen?.(); });
$("rotationButton").addEventListener("click", () => { rotationPaused = !rotationPaused; $("rotationButton").textContent = t(rotationPaused ? "resume" : "pause"); $("rotationLabel").textContent = t(rotationPaused ? "rotationPaused" : "rotationActive"); document.querySelector(".rotation").classList.toggle("paused", rotationPaused); });
$("retryButton").addEventListener("click", loadInitialData);
setInterval(() => { if (rotationPaused || rooms.length < 2) return; rotationIndex = (rotationIndex + 1) % rooms.length; selectRoom(rooms[rotationIndex].id, true); }, 6500);
setInterval(refreshLiveData, 10_000);
setInterval(refreshTrends, 120_000);
setInterval(updateClock, 1000);
renderStaticTranslations(); updateClock(); loadInitialData();
