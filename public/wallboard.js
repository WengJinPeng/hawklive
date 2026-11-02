"use strict";
// The wallboard is read-only. Alarm lifecycle and tenant scope remain server-owned.
const copy = {
  currentRoom: ["当前洁净室", "Current cleanroom"],
  roomParticle: ["≥ 0.5 µm · 有效设备均值", "≥ 0.5 µm · Fresh-device average"],
  roomTemperature: ["温度 · 有效设备均值", "Temperature · Fresh-device average"],
  roomHumidity: ["湿度 · 有效设备均值", "Humidity · Fresh-device average"],
  temperatureTrend: ["洁净室均值 · 温度", "Cleanroom average · Temperature"],
  humidityTrend: ["洁净室均值 · 湿度", "Cleanroom average · Humidity"],
  title: ["全厂运行监控", "Plant operations monitor"],
  subtitle: ["洁净环境 · 设备与数据", "Clean environment · Devices & data"],
  overview: ["全厂状态", "Plant status"],
  plantNow: ["全厂当前状态", "PLANT STATUS"],
  fullscreen: ["全屏", "Fullscreen"],
  refresh: ["刷新", "Refresh"],
  activeAlarms: ["活动报警", "Active alarms"],
  pending: ["超限观察中", "Pending alarms"],
  pendingHelp: ["尚未达到持续报警条件", "Waiting for alarm delay"],
  dataGaps: ["设备数据异常", "Device data gaps"],
  freshDevices: ["有效实时设备", "Fresh / total devices"],
  plantScope: ["全厂范围", "PLANT OVERVIEW"],
  areasTitle: ["哪些区域需要关注", "Areas requiring attention"],
  area: ["区域", "Area"],
  status: ["状态", "Status"],
  alarmWatch: ["报警 / 观察", "Alarm / pending"],
  freshTotal: ["有效 / 总设备", "Fresh / total"],
  temperatureRange: ["温度范围", "Temp. range"],
  humidityRange: ["湿度范围", "Humidity range"],
  areaHelp: [
    "异常优先 · 温湿度为有效设备的最小–最大值",
    "Issues first · Temperature / humidity: fresh-device min–max",
  ],
  attention: ["异常跟踪", "ALARM MONITOR"],
  alertsTitle: ["全厂报警位置与持续时间", "Plant alarms & duration"],
  alarmHelp: [
    "活动报警优先、持续时间长者在前；恢复不等于已处理",
    "Active first, oldest first · Recovery does not mean action completed",
  ],
  deviceFocus: ["设备核查", "DEVICE INSPECTION"],
  device: ["设备", "Device"],
  trendWindow: ["最近 24 小时", "LAST 24 HOURS"],
  trendTitle: ["洁净室平均趋势", "Cleanroom average trend"],
  trendMetric: ["趋势通道", "Trend channel"],
  dataHealth: ["设备与数据", "Devices & data"],
  workspace: ["客户工作台 ↗", "Customer workspace ↗"],
  normal: ["监测正常", "Normal"],
  alarm: ["活动报警", "Active alarm"],
  watch: ["超限观察", "Pending alarm"],
  gap: ["数据异常", "Data gap"],
  unknown: ["状态待确认", "Unconfirmed"],
  maintenance: ["维护中", "Maintenance"],
  offline: ["离线", "Offline"],
  stale: ["数据过期", "Stale data"],
  never: ["尚无读数", "No readings"],
  noDevicesInRoom: ["未配置设备", "No devices configured"],
  sourceDemoStamp: ["模拟数据", "SIMULATED DATA"],
  sourceRealStamp: ["设备上报数据", "Device-reported data"],
  sourceUnknownStamp: ["来源未确认", "Source unconfirmed"],
};
const channels = [
  ["pm_0_3_um", "particle_0_3_um", "0_3", "≥ 0.3 µm"],
  ["pm_0_5_um", "particle_0_5_um", "0_5", "≥ 0.5 µm"],
  ["pm_1_0_um", "particle_1_0_um", "1_0", "≥ 1.0 µm"],
  ["pm_2_5_um", "particle_2_5_um", "2_5", "≥ 2.5 µm"],
  ["pm_5_0_um", "particle_5_0_um", "5_0", "≥ 5.0 µm"],
  ["pm_10_0_um", "particle_10_0_um", "10_0", "≥ 10.0 µm"],
  ["temperature", "temperature", null, "°C"],
  ["humidity", "humidity", null, "%RH"],
];
const freshWindowMs = 45000;
const alarmLimit = 2000;
const areaPageSize = 3;
const trendBucketSeconds = 300;
const rotationDwellMs = 12000;
const scrollSpeed = 32; // pixels/second; pause at both ends before advancing.
let scrollElapsedMs = 0;
let scrollLastTick = 0;
function overflowDuration(distance) {
  return distance > 2 ? 4000 + (distance / scrollSpeed) * 1000 : 0;
}
function resetRotationPosition() {
  scrollElapsedMs = 0;
  document.querySelectorAll("#alertList, #roomName, #deviceTitle, .trend-context span, .data-issue").forEach(element => {
    element.scrollTop = 0;
    element.scrollLeft = 0;
  });
}
function autoScrollTick() {
  const now = Date.now();
  const delta = scrollLastTick
    ? Math.min(250, Math.max(0, now - scrollLastTick))
    : 0;
  scrollLastTick = now;
  if (blockingType || document.hidden || !rooms.length || rotationPaused) return;
  if (!rotationPaused) scrollElapsedMs += delta;
  let dwell = rotationDwellMs;
  // Nodes are rebuilt by live refresh. Drive their offsets from the same page clock,
  // so a 10-second refresh cannot restart a long label before its end is visible.
  for (const [selector, vertical] of [
    [
      ".brand small, .area-table button, .alert-item button, #deviceTitle, #roomName, .metric-card small, .metric-card strong, .device-context span, .room-kpi strong, .room-kpi small, .trend-context span, .data-issue",
      false,
    ],
    [".area-table-wrap, #alertList", true],
  ]) {
    document.querySelectorAll(selector).forEach((element) => {
      const distance = vertical
        ? element.scrollHeight - element.clientHeight
        : element.scrollWidth - element.clientWidth;
      dwell = Math.max(dwell, overflowDuration(distance));
      const offset = Math.min(
        Math.max(0, distance),
        (Math.max(0, scrollElapsedMs - 2000) / 1000) * scrollSpeed,
      );
      if (vertical) element.scrollTop = offset;
      else element.scrollLeft = offset;
    });
  }
  if (!rotationPaused && scrollElapsedMs >= dwell) rotatePanels();
}
let locale = "zh-CN";
let rooms = [],
  latestReadings = [],
  alarmEvents = [],
  trendReadings = [];
let latestByDevice = new Map(),
  currentUser = null;
let selectedRoom = "",
  selectedDevice = "",
  selectedMetric = "pm_0_5_um";
let pages = { area: 0, alert: 0, gap: 0 },
  rotationPaused = false,
  blockingType = "loading";
let loading = false,
  generation = 0,
  trendGeneration = 0,
  trendScope = "",
  trendPendingScope = "",
  trendLoading = false;
const feed = {
  config: { ok: false, at: 0 },
  latest: { ok: false, at: 0 },
  alarms: { ok: false, at: 0 },
  trends: { ok: false, at: 0 },
};
const $ = (id) => document.getElementById(id);
const tr = (zh, en) => (locale === "en-US" ? en : zh);
const t = (key) => copy[key]?.[locale === "en-US" ? 1 : 0] || key;
function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}
function number(value) {
  return value === null || value === undefined || value === ""
    ? NaN
    : Number(value);
}
function formatNumber(value, digits = 0) {
  const n = number(value);
  return Number.isFinite(n)
    ? new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(n)
    : "—";
}
function compact(value) {
  return Math.abs(value) >= 1e6
    ? `${formatNumber(value / 1e6, 1)}M`
    : Math.abs(value) >= 1000
      ? `${formatNumber(value / 1000, 1)}k`
      : formatNumber(value, 1);
}
function timestamp(reading) {
  return number(reading?.last_reading_at ?? reading?.timestamp) * 1000;
}
function readingFor(id) {
  return latestByDevice.get(String(id)) || null;
}
function isFresh(device) {
  const r = readingFor(device.id),
    age = Date.now() - timestamp(r);
  return Boolean(
    r?.online && Number.isFinite(age) && age >= -5000 && age <= freshWindowMs,
  );
}
function dataState(device) {
  const r = readingFor(device.id);
  if (!r || !Number.isFinite(timestamp(r))) return "never";
  if (!r.online) return "offline";
  return isFresh(device) ? "normal" : "stale";
}
function inMaintenance(device) {
  const r = readingFor(device.id);
  return (
    number(
      (r && Object.hasOwn(r, "maintenance")
        ? r.maintenance
        : device.maintenance
      )?.until,
    ) >
    Date.now() / 1000
  );
}
function allDevices() {
  return rooms.flatMap((room) =>
    room.devices.map((device) => ({ room, device })),
  );
}
function metricValue(reading, key) {
  return number(
    ["temperature", "humidity"].includes(key)
      ? reading?.environment?.[key]
      : reading?.particles?.[key],
  );
}
function metricInfo(metric) {
  return (
    channels.find((c) => c[0] === metric || c[1] === metric) ||
    (metric === "particle_5_um" ? channels[4] : null)
  );
}
function metricName(metric) {
  const c = metricInfo(metric);
  return c
    ? c[0] === "temperature"
      ? tr("温度", "Temperature")
      : c[0] === "humidity"
        ? tr("湿度", "Humidity")
        : c[3]
    : String(metric || tr("设备报警", "Device alarm"));
}
function particleUnit(reading) {
  return reading?.particle_unit_label || tr("单位未确认", "Unit unconfirmed");
}
function unitFor(reading, key) {
  return key === "temperature"
    ? "°C"
    : key === "humidity"
      ? "%RH"
      : particleUnit(reading);
}
function ageText(time) {
  if (!Number.isFinite(time) || time <= 0) return t("never");
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  return seconds < 60
    ? tr(`${seconds} 秒前`, `${seconds}s ago`)
    : seconds < 3600
      ? tr(
          `${Math.floor(seconds / 60)} 分钟前`,
          `${Math.floor(seconds / 60)}m ago`,
        )
      : tr(
          `${Math.floor(seconds / 3600)} 小时前`,
          `${Math.floor(seconds / 3600)}h ago`,
        );
}
function duration(start) {
  if (!number(start)) return "—";
  const minutes = Math.max(
    0,
    Math.floor((Date.now() / 1000 - number(start)) / 60),
  );
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}h ${minutes % 60}m`
    : `${minutes}m`;
}
function setLatestReadings(rows) {
  latestReadings = Array.isArray(rows) ? rows : [];
  latestByDevice = new Map(latestReadings.map((r) => [String(r.device_id), r]));
}
function displayOrder(a, b) {
  return String(a.name || a.id).localeCompare(String(b.name || b.id), "zh-CN", {
    numeric: true,
    sensitivity: "base",
  }) || String(a.id).localeCompare(String(b.id));
}
function rotationStops() {
  return rooms.flatMap((room) => room.devices.length
    ? room.devices.map((device) => ({ room, device }))
    : [{ room, device: null }]);
}
function roomTrendScope(room) {
  return room?.devices.length
    ? JSON.stringify([room.id, room.devices.map(d => d.id).sort()])
    : "";
}
function normaliseRooms(config) {
  return (Array.isArray(config) ? config : []).map((room) => ({
    ...room,
    id: String(room.id),
    name: String(room.name || tr("未命名区域", "Unnamed area")),
    devices: (Array.isArray(room.devices) ? room.devices : [])
      .filter((d) => d.enabled !== false)
      .map((d) => ({ ...d, id: String(d.id) }))
      .sort(displayOrder),
    thresholds: room.thresholds || {},
  })).sort(displayOrder);
}
function alarmItems() {
  const items = new Map();
  for (const event of alarmEvents.filter((e) => !e.ended_at)) {
    const roomId = String(event.cleanroom_id),
      deviceId = String(event.device_id),
      key = `${deviceId}:${event.metric}`;
    const item = {
      ...event,
      roomId,
      deviceId,
      metric: event.metric,
      state: "alarm",
      threshold: event.limit_description,
      startedAt: event.started_at,
    };
    const existing = items.get(key);
    if (!existing || number(item.startedAt) < number(existing.startedAt))
      items.set(key, item);
  }
  for (const { room, device } of allDevices()) {
    const r = readingFor(device.id);
    if (!isFresh(device)) continue;
    for (const detail of r?.alarm_details || []) {
      if (
        !["ALARM_ACTIVE", "PENDING_CLEAR", "PENDING_ALARM"].includes(
          detail.state,
        )
      )
        continue;
      const key = `${device.id}:${detail.metric}`,
        old = items.get(key);
      items.set(key, {
        ...old,
        roomId: room.id,
        deviceId: device.id,
        metric: detail.metric,
        state: detail.state === "PENDING_ALARM" && !old ? "watch" : "alarm",
        clearing: detail.state === "PENDING_CLEAR",
        threshold: old?.threshold || detail.limit,
        startedAt:
          old?.startedAt || detail.active_since || detail.pending_since,
        peak_value: old?.peak_value,
      });
    }
    // Preserve an explicit backend alarm even if it has no per-channel details.
    if (
      ["ALARM_ACTIVE", "PENDING_CLEAR"].includes(r?.alarm_status) &&
      ![...items.values()].some(
        (i) => i.deviceId === device.id && i.state === "alarm",
      )
    )
      items.set(`${device.id}:unknown`, {
        roomId: room.id,
        deviceId: device.id,
        metric: "",
        state: "alarm",
      });
  }
  return [...items.values()].sort(
    (a, b) =>
      (a.state === "alarm" ? 0 : 1) - (b.state === "alarm" ? 0 : 1) ||
      number(a.startedAt || Infinity) - number(b.startedAt || Infinity) ||
      `${a.deviceId}:${a.metric}`.localeCompare(`${b.deviceId}:${b.metric}`),
  );
}
function roomInfo(room, items = alarmItems()) {
  const relevant = items.filter((i) => i.roomId === room.id),
    active = relevant.filter((i) => i.state === "alarm").length,
    pending = relevant.filter((i) => i.state === "watch").length;
  const fresh = room.devices.filter(isFresh).length,
    gaps = room.devices.length - fresh,
    maintenance = room.devices.filter(inMaintenance).length;
  const state = active
    ? "alarm"
    : gaps || !room.devices.length
      ? "gap"
      : !feed.config.ok || !feed.latest.ok || !feed.alarms.ok
        ? "unknown"
        : pending
          ? "watch"
          : maintenance
            ? "maintenance"
            : "normal";
  return { room, active, pending, fresh, gaps, maintenance, state };
}
function sortedRooms(items = alarmItems()) {
  const order = {
    alarm: 0,
    gap: 1,
    unknown: 2,
    watch: 3,
    maintenance: 4,
    normal: 5,
  };
  return rooms
    .map((r) => roomInfo(r, items))
    .sort(
      (a, b) =>
        order[a.state] - order[b.state] ||
        b.active - a.active ||
        b.gaps - a.gaps ||
        a.room.name.localeCompare(b.room.name),
    );
}
function gapsList() {
  return allDevices()
    .filter(({ device }) => dataState(device) !== "normal")
    .sort(
      (a, b) =>
        (timestamp(readingFor(a.device.id)) || 0) -
        (timestamp(readingFor(b.device.id)) || 0),
    );
}
function pageSlice(items, key, size) {
  const count = Math.max(1, Math.ceil(items.length / size));
  pages[key] = ((pages[key] % count) + count) % count;
  return {
    items: items.slice(pages[key] * size, (pages[key] + 1) * size),
    count,
    page: pages[key],
  };
}
function paginate(items, key, size) {
  const result = pageSlice(items, key, size);
  $(`${key}Page`).textContent = `${result.page + 1} / ${result.count}`;
  document
    .querySelectorAll(`[data-pager="${key}"] button`)
    .forEach((b) => (b.disabled = result.count === 1));
  return result.items;
}
function badge(state, label = t(state)) {
  return `<span class="badge ${state}">${escapeHtml(label)}</span>`;
}
function selected() {
  const room = rooms.find((r) => r.id === selectedRoom);
  return { room, device: room?.devices.find((d) => d.id === selectedDevice) };
}
function selectDevice(roomId, deviceId, manual = false) {
  const room = rooms.find((r) => r.id === roomId);
  if (!room) return;
  selectedRoom = roomId;
  selectedDevice = room.devices.some((d) => d.id === deviceId)
    ? deviceId
    : room.devices[0]?.id || "";
  if (manual) {
    rotationPaused = true;
    resetRotationPosition();
  }
  const index = sortedRooms().findIndex((item) => item.room.id === roomId);
  pages.area = Math.floor(Math.max(0, index) / areaPageSize);
  renderAll();
  if (!room.devices.length || trendScope !== roomTrendScope(room))
    refreshTrends();
}
function selectRoom(roomId, manual = true) {
  const room = rooms.find((r) => r.id === roomId);
  if (!room) return;
  selectDevice(roomId, room.devices[0]?.id || "", manual);
}
function renderStaticTranslations() {
  document.documentElement.lang = locale;
  document.title = `HawkHive · ${t("title")}`;
  document
    .querySelectorAll("[data-i18n]")
    .forEach((e) => (e.textContent = t(e.dataset.i18n)));
  document
    .querySelectorAll("[data-i18n-aria]")
    .forEach((e) => e.setAttribute("aria-label", t(e.dataset.i18nAria)));
  document.querySelectorAll("[data-locale]").forEach((b) => {
    b.classList.toggle("active", b.dataset.locale === locale);
    b.setAttribute("aria-pressed", String(b.dataset.locale === locale));
  });
  document
    .querySelectorAll("[data-step]")
    .forEach((b) =>
      b.setAttribute(
        "aria-label",
        Number(b.dataset.step) < 0
          ? tr("上一页", "Previous page")
          : tr("下一页", "Next page"),
      ),
    );
  const stops = rotationStops();
  const position = stops.findIndex(({ room, device }) => room.id === selectedRoom && (device?.id || "") === selectedDevice);
  $("rotationLabel").textContent = rotationPaused
    ? tr("已暂停轮播 · 数据继续刷新", "Rotation paused · Live refresh on")
    : tr(
        `按区域、设备编号轮播 · ${Math.max(0, position) + 1} / ${stops.length || 1} · 每项 12 秒，长内容延长`,
        `Room / device name order · ${Math.max(0, position) + 1} / ${stops.length || 1} · 12s each; longer for overflow`,
      );
  const next = stops[(position + 1) % stops.length];
  $("rotationLabel").title = next
    ? tr(`下一项：${next.room.name} / ${next.device?.name || t("noDevicesInRoom")}`, `Next: ${next.room.name} / ${next.device?.name || t("noDevicesInRoom")}`)
    : "";
  $("rotationButton").textContent = rotationPaused
    ? tr("继续轮播", "Resume")
    : tr("暂停轮播", "Pause");
  $("customerName").textContent =
    currentUser?.customer_name ||
    currentUser?.display_name ||
    tr("当前客户", "Current customer");
  $("trendMetric").innerHTML = channels
    .map(
      (c) =>
        `<option value="${c[0]}" ${selectedMetric === c[0] ? "selected" : ""}>${escapeHtml(metricName(c[0]))}</option>`,
    )
    .join("");
}
function renderSummary(items) {
  const devices = allDevices(),
    active = items.filter((i) => i.state === "alarm"),
    pending = items.filter((i) => i.state === "watch"),
    gaps = gapsList(),
    fresh = devices.filter(({ device }) => isFresh(device)).length;
  const affected = new Set(active.map((i) => i.roomId)).size,
    offline = gaps.filter(
      ({ device }) => dataState(device) === "offline",
    ).length;
  const ready =
    feed.config.ok &&
    feed.latest.ok &&
    feed.alarms.ok &&
    alarmEvents.length < alarmLimit;
  document
    .querySelector(".stat.alarm")
    .classList.toggle("has-issue", active.length > 0);
  document
    .querySelector(".stat.watch")
    .classList.toggle("has-issue", pending.length > 0);
  document
    .querySelector(".stat.gap")
    .classList.toggle("has-issue", gaps.length > 0);
  $("activeAlarmCount").textContent = active.length
    ? `${!ready ? "≥ " : ""}${active.length}`
    : ready
      ? "0"
      : "—";
  $("pendingCount").textContent = feed.latest.ok ? String(pending.length) : "—";
  $("gapCount").textContent = feed.latest.at ? String(gaps.length) : "—";
  $("freshCount").textContent = feed.latest.at
    ? `${fresh} / ${devices.length}`
    : `— / ${devices.length}`;
  $("alarmFootnote").textContent = tr(
    `${affected} 个区域存在报警`,
    `${affected} areas with alarms`,
  );
  $("gapFootnote").textContent = tr(
    `${offline} 离线 · ${gaps.length - offline} 过期或无读数`,
    `${offline} offline · ${gaps.length - offline} stale / no data`,
  );
  $("freshFootnote").textContent = tr(
    `45 秒内读数 · ${devices.filter(({ device }) => inMaintenance(device)).length} 台维护中`,
    `Readings ≤ 45s · ${devices.filter(({ device }) => inMaintenance(device)).length} in maintenance`,
  );
  const empty = rooms.filter((r) => !r.devices.length).length;
  const state = active.length
    ? "alarm"
    : !ready
      ? "unknown"
      : gaps.length || empty
        ? "gap"
        : pending.length
          ? "watch"
          : "normal";
  $("verdict").className = `verdict ${state}`;
  $("verdictTitle").textContent = {
    alarm: tr("有报警，需要关注", "Alarms need attention"),
    unknown: tr("数据不完整，待确认", "Status unconfirmed"),
    gap: tr("存在监测盲区", "Monitoring gaps"),
    watch: tr("超限观察中", "Exceedances pending"),
    normal: tr("当前监测正常", "Monitoring normal"),
  }[state];
  $("verdictDetail").textContent = active.length
    ? tr(
        `${affected} 个区域 · ${new Set(active.map((i) => i.deviceId)).size} 台报警设备`,
        `${affected} areas · ${new Set(active.map((i) => i.deviceId)).size} alarm devices`,
      )
    : !ready
      ? tr(
          "数据接口异常，不能判定全厂正常",
          "Data unavailable; normal status cannot be confirmed",
        )
      : empty
        ? tr(
            `${empty} 个区域尚未配置设备`,
            `${empty} areas have no configured devices`,
          )
        : tr(
            `当前 ${rooms.length} 个区域 · ${devices.length} 台设备`,
            `Monitoring ${rooms.length} areas · ${devices.length} devices`,
          );
}
function readingRange(room, key) {
  const values = room.devices
    .filter(isFresh)
    .map((d) => metricValue(readingFor(d.id), key))
    .filter(Number.isFinite);
  if (!values.length) return "—";
  const min = Math.min(...values),
    max = Math.max(...values);
  return `${formatNumber(min, 1)}${min === max ? "" : `–${formatNumber(max, 1)}`}`;
}
function renderRoomHero(items) {
  const { room } = selected();
  $("roomSelect").innerHTML = rooms.map(r => `<option value="${escapeHtml(r.id)}" ${r.id === selectedRoom ? "selected" : ""}>${escapeHtml(r.name)}</option>`).join("");
  $("roomName").textContent = room?.name || "—";
  if (!room) return;
  const info = roomInfo(room, items);
  $("roomState").innerHTML = badge(info.state);
  const fresh = room.devices.filter(isFresh);
  const units = new Set(fresh.map(d => readingFor(d.id)?.particle_unit_label || ""));
  for (const [key, id, unit] of [["pm_0_5_um","roomParticle",""],["temperature","roomTemperature"," °C"],["humidity","roomHumidity"," %RH"]]) {
    const values = fresh.map(d => metricValue(readingFor(d.id), key)).filter(Number.isFinite);
    const compatible = key !== "pm_0_5_um" || (units.size === 1 && !units.has(""));
    $(id).textContent = values.length && compatible ? formatNumber(values.reduce((a,b)=>a+b,0)/values.length,key === "pm_0_5_um" ? 0 : 1) + unit : "—";
  }
  $("roomUnit").textContent = units.size === 1 && !units.has("") ? [...units][0] : tr("单位缺失或不一致", "Units unavailable / mixed");
  $("roomCoverage").textContent = tr(`${fresh.length} / ${room.devices.length} 台有效设备`, `${fresh.length} / ${room.devices.length} fresh devices`);
  $("roomTemperatureRange").textContent = readingRange(room,"temperature") + " °C";
  $("roomHumidityRange").textContent = readingRange(room,"humidity") + " %RH";
}
function renderAreas(items) {
  $("areaRows").innerHTML = paginate(sortedRooms(items), "area", areaPageSize)
    .map(
      ({ room, active, pending, fresh, state }) =>
        `<tr class="${room.id === selectedRoom ? "selected" : ""}"><td><button data-room="${escapeHtml(room.id)}" title="${escapeHtml(room.name)}">${escapeHtml(room.name)}</button></td><td>${badge(state, !room.devices.length ? t("noDevicesInRoom") : t(state))}</td><td><span class="${active ? "alarm-number" : ""}">${active || (!feed.alarms.ok ? "—" : "0")}</span> / ${feed.latest.ok ? pending : "—"}</td><td>${fresh} / ${room.devices.length}</td><td>${readingRange(room, "temperature")} <small>°C</small></td><td>${readingRange(room, "humidity")} <small>%</small></td></tr>`,
    )
    .join("");
  $("roomTotal").textContent = tr(
    `共 ${rooms.length} 个区域`,
    `${rooms.length} areas total`,
  );
}
function renderAlerts(items) {
  const active = items.filter((i) => i.state === "alarm"),
    oldest = active.find((i) => number(i.startedAt) > 0);
  $("alertContext").textContent =
    alarmEvents.length >= alarmLimit
      ? tr(
          "记录达到接口上限，报警总数可能不完整",
          "Record limit reached; alarm totals may be incomplete",
        )
      : oldest
        ? tr(
            `最久持续 ${duration(oldest.startedAt)} · 共 ${active.length} 项活动报警`,
            `Oldest active: ${duration(oldest.startedAt)} · ${active.length} active alarms`,
          )
        : feed.alarms.ok
          ? tr(
              "持续超限按各区域配置的延时触发报警",
              "Alarm delay follows each area's configured policy",
            )
          : tr(
              "报警接口异常，保留最近已知记录",
              "Alarm feed unavailable; showing last known records",
            );
  const visible = paginate(items, "alert", 2);
  $("alertList").innerHTML = visible.length
    ? visible
        .map((action) => {
          const room = rooms.find((r) => r.id === action.roomId),
            device = room?.devices.find((d) => d.id === action.deviceId),
            reading = readingFor(action.deviceId),
            info = metricInfo(action.metric);
          const current =
            device && isFresh(device) && info
              ? metricValue(reading, info[0])
              : NaN;
          const label = action.clearing
            ? tr("恢复观察", "Recovery pending")
            : t(action.state);
          const maintenance =
            device && inMaintenance(device) ? ` · ${t("maintenance")}` : "";
          return `<article class="alert-item ${action.state}"><h3><button data-focus-room="${escapeHtml(action.roomId)}" data-focus-device="${escapeHtml(action.deviceId)}" title="${escapeHtml(room?.name || action.roomId)} · ${escapeHtml(device?.name || action.deviceId)}">${escapeHtml(room?.name || action.roomId)} / ${escapeHtml(device?.name || action.deviceId)}</button></h3><span class="duration">${escapeHtml(label)} · ${duration(action.startedAt)}</span><p>${escapeHtml(metricName(action.metric))} · ${tr("当前", "Current")} <span class="current">${formatNumber(current, info?.[2] ? 0 : 1)}</span> ${escapeHtml(info ? unitFor(reading, info[0]) : "")}${!Number.isFinite(current) ? ` · ${tr("当前值不可用", "Current value unavailable")}` : ""}${maintenance}<br>${tr("事件峰值", "Event peak")} ${formatNumber(action.peak_value, info?.[2] ? 0 : 1)} · ${tr("报警条件", "Condition")}: ${escapeHtml(action.threshold || "—")}</p></article>`;
        })
        .join("")
    : `<div class="empty"><strong>${feed.alarms.ok && feed.latest.ok ? tr("当前无报警或待观察项", "No active or pending alarms") : tr("报警状态待确认", "Alarm status unconfirmed")}</strong><p>${tr("设备离线与数据过期另列于下方", "Offline devices and stale data are listed below")}</p></div>`;
}
function policyFor(room, channel) {
  const p = room?.thresholds || {},
    particle = channel[2];
  if (particle) {
    const enabled = p[`particle_${particle}_enabled`] ?? particle === "0_5";
    const max = number(
      p[`particle_${particle}_max`] ??
        (particle === "0_5" ? p.particle_5_max : undefined),
    );
    return { enabled: Boolean(enabled), min: NaN, max };
  }
  return {
    enabled: true,
    min: number(p[`${channel[0]}_min`]),
    max: number(p[`${channel[0]}_max`]),
  };
}
function policyText(policy) {
  if (!policy.enabled) return tr("报警规则关闭", "Alarm rule off");
  if (Number.isFinite(policy.min) && Number.isFinite(policy.max))
    return `${formatNumber(policy.min, 1)}–${formatNumber(policy.max, 1)}`;
  return Number.isFinite(policy.max)
    ? `≤ ${formatNumber(policy.max)}`
    : Number.isFinite(policy.min)
      ? `≥ ${formatNumber(policy.min, 1)}`
      : tr("阈值未配置", "No threshold");
}
function renderDevice(items) {
  const { room, device } = selected(),
    reading = device ? readingFor(device.id) : null;
  $("deviceTitle").textContent = room
    ? `${room.name} / ${device?.name || t("noDevicesInRoom")}`
    : "—";
  $("deviceSelect").innerHTML = room?.devices.length
    ? room.devices
        .map(
          (d) =>
            `<option value="${escapeHtml(d.id)}" ${d.id === selectedDevice ? "selected" : ""}>${escapeHtml(d.name)}</option>`,
        )
        .join("")
    : `<option>${t("noDevicesInRoom")}</option>`;
  $("deviceSelect").disabled = !room?.devices.length;
  const own = items.filter((i) => i.deviceId === device?.id),
    fresh = device && isFresh(device),
    state = own.some((i) => i.state === "alarm")
      ? "alarm"
      : !fresh
        ? "gap"
        : own.length
          ? "watch"
          : !feed.latest.ok || !feed.alarms.ok
            ? "unknown"
            : inMaintenance(device)
              ? "maintenance"
              : "normal";
  $("deviceState").innerHTML =
    badge(state) +
    (!fresh && device ? ` · ${escapeHtml(t(dataState(device)))}` : "");
  $("deviceMeta").textContent =
    `${tr("最近读数", "Last reading")} ${ageText(timestamp(reading))} · ${particleUnit(reading)}${reading?.source === "demo" ? ` · ${t("sourceDemoStamp")}` : ""}`;
  $("metricGrid").innerHTML = channels
    .map((channel) => {
      const key = channel[0],
        policy = policyFor(room, channel),
        value = fresh ? metricValue(reading, key) : NaN,
        alarm = own.find((i) => metricInfo(i.metric)?.[0] === key);
      return `<article class="metric-card ${!fresh ? "stale" : alarm?.state || ""}"><span>${escapeHtml(metricName(key))}${channel[2] ? "" : ` · ${channel[3]}`}</span><strong>${formatNumber(value, channel[2] ? 0 : 1)}</strong><small title="${escapeHtml(policyText(policy))}">${!fresh ? tr("当前值不可用", "Current value unavailable") : alarm?.clearing ? tr("恢复观察", "Recovery pending") : alarm ? `${t(alarm.state)} · ${escapeHtml(policyText(policy))}` : escapeHtml(policyText(policy))}</small></article>`;
    })
    .join("");
  $("policyNote").textContent = tr(
    `当前报警策略 · 持续 ${formatNumber(room?.thresholds.alarm_delay_seconds)} 秒判定 · 点击区域或设备暂停轮播`,
    `Current alarm policy · ${formatNumber(room?.thresholds.alarm_delay_seconds)}s delay · Selecting a device pauses rotation`,
  );
  $("devicePosition").textContent = room?.devices.length
    ? `${room.devices.findIndex((d) => d.id === selectedDevice) + 1} / ${room.devices.length}`
    : "";
}
function renderTrend() {
  renderTrendChannel(selectedMetric);
  renderTrendChannel("temperature", "temp");
  renderTrendChannel("humidity", "humid");
}
function roomMeanSeries(room, metric, start, end) {
  const buckets = new Map(), units = new Set();
  if (!room || trendScope !== roomTrendScope(room)) return { points: [], units };
  const deviceIds = new Set(room.devices.map(d => d.id));
  for (const row of trendReadings) {
    if (String(row.cleanroom_id) !== room.id || !deviceIds.has(String(row.device_id)) ||
        row.aggregation !== "mean" || row.bucket_seconds !== trendBucketSeconds ||
        !Number.isFinite(number(row.bucket_start)) ||
        number(row.timestamp) < start || number(row.timestamp) > end ||
        !Number.isFinite(number(row.timestamp)) || !Number.isFinite(metricValue(row, metric))) continue;
    const key = number(row.bucket_start);
    if (!buckets.has(key)) buckets.set(key, new Map());
    buckets.get(key).set(String(row.device_id), row);
  }
  const points = [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([, devices]) => {
    const rows = [...devices.values()];
    rows.forEach(row => units.add(row.particle_unit_label || ""));
    return {
      time: number(rows[0].timestamp),
      value: rows.reduce((sum, row) => sum + metricValue(row, metric), 0) / rows.length,
      count: rows.length,
      members: [...devices.keys()].sort().join(","),
    };
  });
  return { points, units };
}
function renderTrendChannel(selectedMetric, prefix = "") {
  const node = (id) => $(prefix + id);
  const { room } = selected(),
    channel = channels.find((c) => c[0] === selectedMetric) || channels[1];
  node("trendIdentity").textContent =
    `${room?.name || "—"} · ${tr("设备均值", "Device average")} · ${metricName(selectedMetric)}`;
  const end = Date.now() / 1000,
    start = end - 86400;
  const { points, units } = roomMeanSeries(room, selectedMetric, start, end);
  const mixed = Boolean(channel[2] && (units.size > 1 || units.has("")));
  node("trendUnit").textContent = channel[2]
    ? (units.size === 1 && !units.has("") ? [...units][0] : tr("单位未确认", "Unit unconfirmed"))
    : channel[3];
  const valid =
    points.length && !mixed && !trendLoading && feed.trends.ok && trendScope === roomTrendScope(room);
  node("chartEmpty").hidden = Boolean(valid);
  node("chartEmpty").textContent = trendLoading
    ? tr("正在读取洁净室平均趋势…", "Loading cleanroom averages…")
    : mixed
      ? tr(
          "粒子单位缺失或不一致，无法合并绘图",
          "Particle units missing or inconsistent",
        )
      : !feed.trends.ok
        ? tr("趋势读取失败，下次刷新重试", "Trend unavailable; will retry")
        : tr("该洁净室此通道暂无历史数据", "No cleanroom history for this channel");
  node("chartLine").setAttribute("d", "");
  node("chartLimit").setAttribute("d", "");
  const currentUnits = new Set((room?.devices || []).filter(isFresh).map(d => readingFor(d.id)?.particle_unit_label || ""));
  const policy = policyFor(room, channel),
    sameUnit = !channel[2] || (units.size === 1 && !units.has("") && currentUnits.size === 1 && [...units][0] === [...currentUnits][0]),
    limits = policy.enabled && sameUnit
      ? [policy.min, policy.max].filter(Number.isFinite)
      : [];
  const values = points.map((p) => p.value);
  const low = Math.min(...values, ...limits), high = Math.max(...values, ...limits);
  const padding = Number.isFinite(low) ? Math.max((high - low) * .12, 1) : 1;
  const min = channel[2] || !Number.isFinite(low) ? Math.min(0, ...values, ...limits) : low - padding;
  const max = channel[2] || !Number.isFinite(high) ? Math.max(1, ...values, ...limits) * 1.08 : high + padding;
  const y = (value) => 170 - ((value - min) / (max - min)) * 160;
  node("chartGrid").innerHTML = [10, 90, 170]
    .map((y) => `<line x1="0" y1="${y}" x2="680" y2="${y}"/>`)
    .join("");
  node("chartTicks").innerHTML = [max, (max + min) / 2, min]
    .map((v) => `<span>${compact(v)}</span>`)
    .join("");
  node("chartTimes").innerHTML = [start, start + 43200, end]
    .map(
      (v) =>
        `<span>${new Date(v * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hour12: false })}</span>`,
    )
    .join("");
  if (valid) {
    // A sparse/disconnected history must not be presented as continuous coverage.
    const maxGap = trendBucketSeconds * 1.5;
    const path = points
      .map((p, index) => {
        const x = ((p.time - start) / 86400) * 680;
        const previous = points[index - 1];
        const gap = !previous || p.time - previous.time > maxGap || p.members !== previous.members;
        return `${gap ? "M" : "L"}${x.toFixed(2)},${y(p.value).toFixed(2)}${gap ? " l0.3,0" : ""}`;
      })
      .join(" ");
    node("chartLine").setAttribute("d", path);
    if (sameUnit)
      node("chartLimit").setAttribute(
        "d",
        limits.map((v) => `M0,${y(v)} H680`).join(" "),
      );
    if (channel[2]) node("trendUnit").textContent = [...units][0];
  }
  node("trendNote").textContent = tr(
    "每点 5 分钟设备均值 · 虚线为单设备当前阈值",
    "5-min device averages · Dashed: current per-device limits",
  );
  const coverage = points.map(p => p.count),
    minCoverage = Math.min(...coverage), maxCoverage = Math.max(...coverage),
    coverageText = `${minCoverage === maxCoverage ? minCoverage : `${minCoverage}–${maxCoverage}`} / ${room?.devices.length || 0}`;
  node("trendSummary").textContent = valid
    ? tr(
        `参与设备 ${coverageText} · 均值最高 ${formatNumber(Math.max(...values), channel[2] ? 0 : 1)}`,
        `Devices ${coverageText} · Max average ${formatNumber(Math.max(...values), channel[2] ? 0 : 1)}`,
      )
    : "";
  node("chart").setAttribute(
    "aria-label",
    `${node("trendIdentity").textContent} · ${node("trendUnit").textContent} · ${node("trendSummary").textContent || node("chartEmpty").textContent}`,
  );
}
function renderDataIssues() {
  const gaps = gapsList();
  const entries = [
    ...gaps.map(({ room, device }) => ({
      room,
      device,
      text: `${room.name} / ${device.name} · ${t(dataState(device))} · ${ageText(timestamp(readingFor(device.id)))}`,
    })),
    ...rooms
      .filter((r) => !r.devices.length)
      .map((room) => ({
        room,
        text: `${room.name} · ${t("noDevicesInRoom")}`,
      })),
  ];
  const visible = paginate(entries, "gap", 2);
  $("dataIssues").innerHTML = visible.length
    ? visible
        .map(
          ({ room, device, text }) =>
            `<button class="data-issue" data-focus-room="${escapeHtml(room.id)}" data-focus-device="${escapeHtml(device?.id || "")}" title="${escapeHtml(text)}">${escapeHtml(text)}</button>`,
        )
        .join("")
    : `<span>${feed.latest.ok ? tr("所有已配置设备均有有效实时读数", "All configured devices have fresh readings") : tr("实时接口异常；最近读数仍可能保留，不能确认采集正常", "Live feed unavailable; cached readings do not confirm collection health")}</span>`;
}
function renderFeed() {
  const failures = ["config", "latest", "alarms", "trends"].filter(
      (k) => !feed[k].ok,
    ),
    names = {
      config: tr("配置", "config"),
      latest: tr("实时读数", "live data"),
      alarms: tr("报警", "alarms"),
      trends: tr("趋势", "trends"),
    };
  const sources = new Set(
    allDevices()
      .map(({ device }) => readingFor(device.id)?.source)
      .filter(Boolean),
  );
  const source = sources.has("demo")
    ? t("sourceDemoStamp") +
      (sources.size > 1
        ? tr("与设备数据混合", " mixed with other sources")
        : "")
    : sources.size === 1 && sources.has("device")
      ? t("sourceRealStamp")
      : t("sourceUnknownStamp");
  const warning =
    sources.has("demo") || sources.size !== 1 || !sources.has("device");
  $("feedBanner").className =
    `feed-banner ${failures.length ? "error" : warning ? "warning" : ""}`;
  $("feedSummary").textContent =
    `${source} · ${failures.length ? tr(`数据不完整：${failures.map((k) => names[k]).join("、")}`, `Incomplete feeds: ${failures.map((k) => names[k]).join(", ")}`) : tr("数据接口正常 · 每 10 秒刷新", "Data feeds connected · Refresh every 10s")}${alarmEvents.length >= alarmLimit ? tr(" · 报警记录达到上限", " · Alarm record limit reached") : ""}`;
  $("lastRefresh").textContent = tr(
    `实时数据最近成功读取：${ageText(feed.latest.at)}`,
    `Live data last fetched: ${ageText(feed.latest.at)}`,
  );
}
function renderAll() {
  if (!rooms.length) return;
  renderStaticTranslations();
  const items = alarmItems();
  renderSummary(items);
  renderAreas(items);
  renderRoomHero(items);
  renderAlerts(items);
  renderDevice(items);
  renderTrend();
  renderDataIssues();
  renderFeed();
}
async function api(path) {
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) {
      const error = new Error("AUTH_REQUIRED");
      error.code = "AUTH_REQUIRED";
      throw error;
    }
    const payload = await response.json();
    if (!response.ok || payload?.ok === false)
      throw new Error(`HTTP ${response.status}`);
    return payload.data;
  } finally {
    clearTimeout(timeout);
  }
}
function settledData(result, label, fallback) {
  if (result.status === "fulfilled") {
    feed[label] = { ok: true, at: Date.now() };
    return result.value;
  }
  feed[label].ok = false;
  if (result.reason?.code === "AUTH_REQUIRED") throw result.reason;
  return fallback;
}
function showBlocking(type) {
  blockingType = type;
  $("blockingState").hidden = false;
  $("blockingTitle").textContent =
    type === "auth"
      ? tr("登录已失效", "Session expired")
      : type === "empty"
        ? tr("尚未配置区域", "No areas configured")
        : tr("无法加载监控数据", "Unable to load monitoring data");
  $("blockingDetail").textContent =
    type === "auth"
      ? tr(
          "请返回工作台重新登录，账号权限仍由服务端校验。",
          "Return to the workspace and sign in again.",
        )
      : type === "empty"
        ? tr(
            "请在客户工作台完成区域和设备配置。",
            "Configure areas and devices in the customer workspace.",
          )
        : tr(
            "连接未成功，请检查网络后重试。",
            "Connection failed. Check the network and retry.",
          );
  $("retryButton").hidden = type === "auth";
  $("retryButton").textContent = tr("重试", "Retry");
  $("blockingAction").hidden = type !== "auth" && type !== "empty";
  $("blockingAction").textContent = tr("返回工作台", "Return to workspace");
}
async function loadInitialData() {
  if (loading) return;
  loading = true;
  const request = ++generation;
  try {
    const [session, config] = await Promise.all([
      api("/api/session"),
      api("/api/config"),
    ]);
    if (request !== generation) return;
    currentUser = session;
    rooms = normaliseRooms(config);
    feed.config = { ok: true, at: Date.now() };
    if (!rooms.length) {
      showBlocking("empty");
      return;
    }
    const [latestResult, alarmsResult] = await Promise.allSettled([
      api("/api/latest"),
      api(`/api/alarms?limit=${alarmLimit}`),
    ]);
    setLatestReadings(settledData(latestResult, "latest", []));
    alarmEvents = settledData(alarmsResult, "alarms", []) || [];
    if (!Array.isArray(alarmEvents)) alarmEvents = [];
    blockingType = null;
    $("blockingState").hidden = true;
    const first = rooms[0];
    selectRoom(first.id, false);
  } catch (error) {
    showBlocking(error.code === "AUTH_REQUIRED" ? "auth" : "error");
  } finally {
    loading = false;
  }
}
async function refreshLiveData() {
  if (loading || blockingType || !rooms.length) return;
  loading = true;
  try {
    const [latestResult, alarmsResult] = await Promise.allSettled([
      api("/api/latest"),
      api(`/api/alarms?limit=${alarmLimit}`),
    ]);
    const newReadings = settledData(latestResult, "latest", latestReadings),
      newAlarms = settledData(alarmsResult, "alarms", alarmEvents);
    setLatestReadings(newReadings);
    alarmEvents = Array.isArray(newAlarms) ? newAlarms : [];
    renderAll();
  } catch (error) {
    if (error.code === "AUTH_REQUIRED") showBlocking("auth");
    else renderAll();
  } finally {
    loading = false;
  }
}
async function refreshTrends() {
  const { room } = selected(), scope = roomTrendScope(room);
  if (scope && trendPendingScope === scope) return;
  const request = ++trendGeneration;
  if (!scope) {
    trendReadings = [];
    trendScope = "";
    trendPendingScope = "";
    trendLoading = false;
    feed.trends = { ok: true, at: Date.now() };
    renderTrend();
    renderFeed();
    return;
  }
  trendPendingScope = scope;
  trendLoading = trendScope !== scope || !feed.trends.ok;
  renderTrend();
  try {
    const end = Date.now() / 1000;
    const rows = await api(
      `/api/trends?cleanroom_ids=${encodeURIComponent(room.id)}&device_ids=${encodeURIComponent(room.devices.map(d => d.id).join(","))}&start=${end - 86400}&end=${end}&aggregation=mean&bucket_seconds=${trendBucketSeconds}`,
    );
    if (request !== trendGeneration) return;
    // Older servers ignore unknown query parameters. Never relabel sampled rows
    // as averages if the matching aggregation API has not been deployed yet.
    if (!Array.isArray(rows) || rows.some(r => r.aggregation !== "mean" || r.bucket_seconds !== trendBucketSeconds))
      throw new Error("Mean trend aggregation unavailable");
    trendReadings = rows;
    trendScope = scope;
    feed.trends = { ok: true, at: Date.now() };
  } catch (error) {
    if (request !== trendGeneration) return;
    if (error.code === "AUTH_REQUIRED") {
      showBlocking("auth");
      return;
    }
    feed.trends.ok = false;
    trendReadings = [];
    trendScope = scope;
  } finally {
    if (request === trendGeneration) {
      trendPendingScope = "";
      trendLoading = false;
      renderTrend();
      renderFeed();
    }
  }
}
async function refreshConfig() {
  if (blockingType) return;
  try {
    const config = await api("/api/config");
    rooms = normaliseRooms(config);
    feed.config = { ok: true, at: Date.now() };
    if (!rooms.length) {
      showBlocking("empty");
      return;
    }
    if (!selected().room) selectRoom(rooms[0].id, false);
    else if (!selected().device)
      selectRoom(selectedRoom, false);
    else {
      renderAll();
      if (trendScope !== roomTrendScope(selected().room)) refreshTrends();
    }
  } catch (error) {
    feed.config.ok = false;
    if (error.code === "AUTH_REQUIRED") showBlocking("auth");
    else renderAll();
  }
}
function rotatePanels() {
  if (rotationPaused || blockingType || !rooms.length) return;
  scrollElapsedMs = 0;
  pages.alert++;
  pages.gap++;
  // Human-readable numeric name order, independent of UUIDs and live severity.
  const ordered = rotationStops();
  const index = ordered.findIndex(
      (item) =>
        item.room.id === selectedRoom &&
        (item.device?.id || "") === selectedDevice,
    ),
    next = ordered[(index + 1) % ordered.length];
  selectDevice(next.room.id, next.device?.id || "", false);
}
function updateClock() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString(locale, { hour12: false });
  $("date").textContent = now.toLocaleDateString(locale, {
    month: "short",
    day: "numeric",
    weekday: "short",
  });
}
function init() {
  try {
    locale =
      localStorage.getItem("hawkhive-locale") ||
      (navigator.language.startsWith("en") ? "en-US" : "zh-CN");
  } catch {}
  if (!["zh-CN", "en-US"].includes(locale)) locale = "zh-CN";
  renderStaticTranslations();
  updateClock();
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.locale) {
      locale = button.dataset.locale;
      try {
        localStorage.setItem("hawkhive-locale", locale);
      } catch {}
      renderStaticTranslations();
      renderAll();
      updateClock();
      if (blockingType && blockingType !== "loading")
        showBlocking(blockingType);
    }
    if (button.dataset.room) selectRoom(button.dataset.room);
    if (button.dataset.focusRoom)
      selectDevice(button.dataset.focusRoom, button.dataset.focusDevice, true);
    if (button.dataset.step) {
      const key = button.closest("[data-pager]").dataset.pager;
      pages[key] += Number(button.dataset.step);
      resetRotationPosition();
      rotationPaused = true;
      renderAll();
    }
  });
  $("roomSelect").addEventListener("change", () => selectRoom($("roomSelect").value, true));
  $("deviceSelect").addEventListener("change", () =>
    selectDevice(selectedRoom, $("deviceSelect").value, true),
  );
  $("trendMetric").addEventListener("change", () => {
    selectedMetric = $("trendMetric").value;
    rotationPaused = true;
    renderAll();
  });
  $("rotationButton").addEventListener("click", () => {
    rotationPaused = !rotationPaused;
    resetRotationPosition();
    renderStaticTranslations();
  });
  $("refreshButton").addEventListener("click", () => {
    refreshLiveData();
    refreshTrends();
    refreshConfig();
  });
  $("retryButton").addEventListener("click", loadInitialData);
  $("fullscreenButton").addEventListener("click", async () => {
    try {
      if (!document.fullscreenElement)
        await document.documentElement.requestFullscreen?.();
      else await document.exitFullscreen?.();
    } catch {
      $("fullscreenButton").textContent = tr(
        "请用浏览器全屏",
        "Use browser fullscreen",
      );
    }
  });
  window.addEventListener("storage", (event) => {
    if (
      event.key === "hawkhive-locale" &&
      ["zh-CN", "en-US"].includes(event.newValue)
    ) {
      locale = event.newValue;
      renderAll();
    }
  });
  setInterval(updateClock, 1000);
  setInterval(() => {
    if (!blockingType) renderAll();
  }, 10000);
  setInterval(refreshLiveData, 10000);
  setInterval(autoScrollTick, 100);
  setInterval(() => {
    refreshConfig();
    refreshTrends();
  }, 120000);
  loadInitialData();
}
if (typeof document !== "undefined") init();
