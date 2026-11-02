(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const dialog = el('quarantineDialog');
  if (!dialog) return;
  const text = (value) => typeof uiText === 'function' ? uiText(value) : value;
  let siteId = '', epoch = 0, items = [], jobs = [], supported = false, canRetry = false;
  let selected = null, busy = false, loading = false, timer = null;
  let nextAfter = null;
  const scope = () => `/api/admin/sites/${encodeURIComponent(siteId)}/quarantine`;
  const active = (id) => id === epoch && dialog.open;
  const status = (message) => { el('quarantineStatus').textContent = text(message); };
  const stamp = (value) => value ? new Date(value * 1000).toLocaleString() : '—';
  const uuid = (value) => String(value || '').replace(/-/g, '').toLowerCase();
  const valid = (item) => item.present && item.raw_text && item.validation?.valid
    && item.validation.raw_sha256 === item.raw_sha256 && item.validation.expires_at * 1000 > Date.now()
    && (Date.now() / 1000 - item.reported_at) < 300;

  function clearConfirmation() {
    selected = null;
    el('quarantineConfirmation').hidden = true;
    el('quarantineConfirmUuid').value = '';
    el('quarantineConfirmError').textContent = '';
    el('confirmQuarantineRetry').disabled = true;
  }

  function updateConfirmation() {
    el('confirmQuarantineRetry').disabled = busy || loading || !selected || !canRetry || !supported
      || !valid(selected.item) || !/^[a-f0-9]{32}$/.test(uuid(el('quarantineConfirmUuid').value))
      || uuid(el('quarantineConfirmUuid').value) !== uuid(selected.item.record_id);
  }

  function render() {
    const opened = new Set([...el('quarantineResults').querySelectorAll('details[open]')].map((node) => node.dataset.quarantineRaw));
    el('quarantineResults').innerHTML = items.map((item) => {
      const result = item.validation;
      const label = result ? (result.valid ? '校验通过，可申请补传' : '校验未通过，保留隔离') : '尚未校验';
      const issues = (result?.issues || []).map((issue) => `<li data-i18n-ignore>${escapeHtml(issue.field)}: ${escapeHtml(issue.message)}</li>`).join('');
      return `<article class="diagnostic-entry quarantine-entry">
        <header><strong data-i18n-ignore>${escapeHtml(item.record_id || '—')}</strong><time data-i18n-ignore>${escapeHtml(stamp(item.metadata.measured_at))}</time></header>
        <p data-i18n-ignore>${escapeHtml(item.device_id)} · ${escapeHtml(item.cleanroom_id)}</p>
        <p><span>上传失败次数</span> <b>${Number(item.metadata.attempts || 0)}</b> · <span>隔离时间</span> <span data-i18n-ignore>${escapeHtml(stamp(item.metadata.quarantined_at))}</span></p>
        <pre data-i18n-ignore>${escapeHtml(item.metadata.error || item.metadata.unavailable_reason || '')}</pre>
        <details data-quarantine-raw="${escapeHtml(item.id)}" ${opened.has(item.id) ? 'open' : ''}><summary>原始记录（只读）</summary><pre data-i18n-ignore>${escapeHtml(item.raw_text || item.metadata.unavailable_reason || '')}</pre><small data-i18n-ignore>SHA-256: ${escapeHtml(item.raw_sha256 || '—')}</small></details>
        <p class="quarantine-validation ${result?.valid ? 'valid' : ''}">${escapeHtml(text(label))}</p>${issues ? `<ul>${issues}</ul>` : ''}
        <div class="quarantine-row-actions"><button type="button" data-quarantine-validate="${escapeHtml(item.id)}" ${busy || loading || !supported || !item.raw_text ? 'disabled' : ''}>校验原始记录</button>
        <button type="button" data-quarantine-retry="${escapeHtml(item.id)}" ${busy || loading || !supported || !canRetry || !valid(item) ? 'disabled' : ''}>申请指定 UUID 补传</button></div>
      </article>`;
    }).join('');
    const labels = {pending:'等待采集器处理',accepted:'云端已接收，等待本地确认',completed:'补传完成',expired:'任务已过期',failed:'处理失败，原始记录保留'};
    el('quarantineTasks').innerHTML = jobs.length ? jobs.map((job) => `<p><code data-i18n-ignore>${escapeHtml(job.record_uuid)}</code> <strong>${escapeHtml(text(labels[job.state] || job.state))}</strong> <time data-i18n-ignore>${escapeHtml(stamp(job.created_at))}</time></p>${job.error ? `<pre data-i18n-ignore>${escapeHtml(job.error)}</pre>` : ''}`).join('') : `<p>${escapeHtml(text('暂无补传任务'))}</p>`;
    updateConfirmation();
    el('moreQuarantine').hidden = nextAfter === null;
    el('moreQuarantine').disabled = busy || loading;
  }

  async function load(more = false) {
    if (!dialog.open || loading) return;
    if (busy) { timer = setTimeout(load, 1000); return; }
    const id = epoch;
    clearTimeout(timer);
    loading = true; render();
    el('refreshQuarantine').disabled = true;
    try {
      const data = await api(scope() + (more && nextAfter !== null ? `?after=${encodeURIComponent(nextAfter)}` : ''));
      if (!active(id)) return;
      if (more) {
        const byId = new Map(items.map((item) => [item.id,item]));
        data.items.forEach((item) => byId.set(item.id,item));items = [...byId.values()];
      } else items = data.items;
      nextAfter = data.next_after ?? null;
      jobs = data.jobs; supported = data.supported; canRetry = data.can_retry;
      if (selected) {
        const current = items.find((item) => item.id === selected.item.id);
        if (!current || current.raw_sha256 !== selected.item.raw_sha256 || !valid(current)) {
          clearConfirmation();
        } else selected.item = current;
      }
      el('quarantineSnapshotInfo').textContent = data.report
        ? `${text('现场隔离数')} ${data.report.total} · ${text('已上报')} ${items.length} · ${text('更新时间')} ${stamp(data.report.reported_at)}`
        : text('等待现场上报隔离详情');
      render();
      status(!supported ? '此采集器需要升级后才能启用隔离维护，现有采集继续运行。'
        : data.retry_unavailable_reason === 'admin_required' ? '仅管理员可提交指定 UUID 补传。'
        : data.retry_unavailable_reason === 'collector_unavailable' ? '采集器离线，现有详情可查看；恢复连接后可申请补传。'
        : data.retry_unavailable_reason === 'signing_unavailable' ? '云端尚未启用补传，请联系管理员。'
        : !data.report ? '等待现场上报隔离详情' : items.length ? '隔离详情已更新' : '当前没有已上报的隔离读数');
    } catch (error) {
      if (!active(id)) return;
      items = []; jobs = []; clearConfirmation(); render();
      status('读取失败，请刷新重试。');
    } finally {
      if (active(id)) {
        loading = false; render();
        el('refreshQuarantine').disabled = false;
        if (items.length <= 50) timer = setTimeout(load, 10000);
      }
    }
  }

  async function validate(entryId) {
    if (busy || loading || !supported) return;
    const id = epoch, item = items.find((value) => value.id === entryId);
    if (!item) return;
    busy = true; clearConfirmation(); render(); status('正在校验原始记录…');
    try {
      const result = await api(`${scope()}/${encodeURIComponent(entryId)}/validate`, {method:'POST',body:'{}'});
      if (!active(id)) return;
      item.validation = result;
      status(result.valid ? '校验通过，可申请补传' : '校验未通过，保留隔离');
    } catch (error) {
      if (!active(id)) return;
      item.validation = null; status('校验失败，请刷新详情后重试。');
    } finally {
      if (active(id)) { busy = false; render(); }
    }
  }

  function select(entryId) {
    if (busy || loading || !supported || !canRetry) return;
    const item = items.find((value) => value.id === entryId);
    if (!item || !valid(item)) return;
    selected = {item, requestId:crypto.randomUUID()};
    el('quarantineSelectedUuid').textContent = item.record_id;
    el('quarantineConfirmUuid').value = '';
    el('quarantineConfirmError').textContent = '';
    el('quarantineConfirmation').hidden = false;
    updateConfirmation(); el('quarantineConfirmUuid').focus();
  }

  async function submit(event) {
    event.preventDefault(); updateConfirmation();
    if (el('confirmQuarantineRetry').disabled || !selected) return;
    const id = epoch, approval = selected;
    busy = true; render();
    try {
      await api(`${scope()}/${encodeURIComponent(approval.item.id)}/retry`, {method:'POST', body:JSON.stringify({
        request_id:approval.requestId,validation_id:approval.item.validation.id,
        raw_sha256:approval.item.raw_sha256,record_uuid:approval.item.record_id,
      })});
      if (!active(id)) return;
      clearConfirmation(); busy = false; await load();
      status('补传任务已提交，等待采集器和云端确认。');
    } catch (error) {
      if (!active(id)) return;
      // Keep the same request ID on retry: a lost response must not create a new job.
      el('quarantineConfirmError').textContent = text('提交未获确认，可刷新查看任务状态或重试同一请求。');
    } finally {
      if (active(id)) { busy = false; render(); }
    }
  }

  document.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-collector-quarantine]');
    if (opener) {
      ++epoch; clearTimeout(timer); clearConfirmation(); busy = false; loading = false;
      siteId = opener.dataset.collectorQuarantine; items = []; jobs = []; nextAfter = null; supported = false; canRetry = false;
      const site = topologySites.find((value) => String(value.id) === siteId);
      el('quarantineNode').textContent = site ? `${collectorDisplayName(site)} · ${site.hostname || ''} · v${site.version || '—'}` : siteId;
      el('quarantineSnapshotInfo').textContent = ''; render(); status('正在读取隔离详情…');
      dialog.showModal(); load(); return;
    }
    const check = event.target.closest('[data-quarantine-validate]');
    if (check && dialog.open) { validate(check.dataset.quarantineValidate); return; }
    const retry = event.target.closest('[data-quarantine-retry]');
    if (retry && dialog.open) select(retry.dataset.quarantineRetry);
  });
  el('closeQuarantine').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { ++epoch; clearTimeout(timer); items=[]; jobs=[]; busy=false; loading=false; clearConfirmation(); });
  el('refreshQuarantine').addEventListener('click', () => { if (!busy) load(); });
  el('moreQuarantine').addEventListener('click', () => { if (!busy && nextAfter !== null) load(true); });
  el('quarantineConfirmUuid').addEventListener('input', updateConfirmation);
  el('cancelQuarantineRetry').addEventListener('click', clearConfirmation);
  el('quarantineConfirmation').addEventListener('submit', submit);
})();
