(() => {
  const el = (id) => document.getElementById(id);
  const dialog = el("diagnosticsDialog");
  if (!dialog) return;
  let siteId = "", items = [], before = null, requestId = 0;
  let activeFilters = null;
  const text = (value) => typeof uiText === "function" ? uiText(value) : value;
  function status(value) { el("diagnosticsStatus").textContent = text(value); }
  function render() {
    el("diagnosticsResults").innerHTML = items.map((item) => `<article class="diagnostic-entry">
      <header><strong class="diagnostic-level ${item.level === "ERROR" || item.level === "CRITICAL" ? "error" : ""}">${escapeHtml(item.level)}</strong><time data-i18n-ignore>${escapeHtml(new Date(item.timestamp * 1000).toLocaleString())}</time><code data-i18n-ignore>${escapeHtml(item.event)}</code></header>
      <pre data-i18n-ignore>${escapeHtml(item.message)}</pre>
      <small><span>云端接收时间</span> <span data-i18n-ignore>${escapeHtml(new Date(item.received_at * 1000).toLocaleString())}</span></small>
    </article>`).join("");
    el("moreDiagnostics").hidden = !before;
    el("exportDiagnostics").disabled = !items.length;
  }
  async function load(more = false) {
    const id = ++requestId;
    if (!more) {
      items = []; before = null;
      activeFilters = {hours: el("diagnosticsHours").value, level: el("diagnosticsLevel").value, q: el("diagnosticsQuery").value.trim()};
      render();
    }
    const params = new URLSearchParams(activeFilters);
    if (more && before) params.set("before", before);
    status("正在读取日志…");
    el("refreshDiagnostics").disabled = true;
    el("moreDiagnostics").disabled = true;
    el("exportDiagnostics").disabled = true;
    try {
      const data = await api(`/api/admin/sites/${encodeURIComponent(siteId)}/diagnostics?${params}`);
      if (id !== requestId || !dialog.open) return;
      const seen = new Set(items.map((item) => item.id));
      items.push(...data.items.filter((item) => !seen.has(item.id)));
      before = data.next_before;
      render();
      status(items.length ? "日志已更新" : "当前条件下没有已上传日志。可扩大时间范围，或确认采集器已升级并联网。");
    } catch (error) {
      if (id !== requestId || !dialog.open) return;
      status("日志读取失败，请重新查询。");
      // Do not leave stale rows/export looking like a successful new query.
      if (!more) { items = []; before = null; render(); }
    } finally {
      if (id === requestId) {
        el("refreshDiagnostics").disabled = false;
        el("moreDiagnostics").disabled = false;
        el("exportDiagnostics").disabled = !items.length;
      }
    }
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-collector-diagnostics]");
    if (!button) return;
    siteId = button.dataset.collectorDiagnostics;
    const site = topologySites.find((item) => String(item.id) === siteId);
    el("diagnosticsNode").textContent = site ? `${collectorDisplayName(site)} · ${site.hostname || ""} · v${site.version || "—"}` : siteId;
    dialog.showModal();
    load();
  });
  el("closeDiagnostics").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { ++requestId; items = []; before = null; render(); });
  el("diagnosticsFilters").addEventListener("submit", (event) => { event.preventDefault(); load(); });
  el("moreDiagnostics").addEventListener("click", () => load(true));
  el("exportDiagnostics").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({site_id: siteId, filters: activeFilters, exported_at: new Date().toISOString(), items}, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = "collector-diagnostics.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
