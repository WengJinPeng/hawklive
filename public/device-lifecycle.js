/* Cloud equipment operations. Hidden on collectors without this capability. */
let lifecycleDevices = [];
let lifecycleConnections = false;
let equipmentTarget = null;
let equipmentMode = null;
let equipmentBusy = false;
let lifecycleListMode = 'archive';
let lifecyclePageIndex = 0;
let lifecycleAuditDevice = null;
let lifecycleLoadSequence = 0;

async function loadDeviceLifecycle() {
  const data = await api('/api/admin/device-lifecycle');
  lifecycleDevices = data.devices || [];
  lifecycleConnections = Boolean(data.can_edit_connections);
  $('lifecycleToolbar').hidden = false;
  for (const entry of lifecycleDevices) {
    const device = findDevice(entry.id).device;
    if (device) device.maintenance = entry.maintenance_until ? {reason:entry.maintenance_reason,until:entry.maintenance_until} : null;
  }
}

function equipmentSyncText(id) {
  const item = lifecycleDevices.find((d) => String(d.id) === String(id));
  if (!item) return uiText('配置由采集器主动拉取');
  if (item.sync_state === 'failed') return uiText('现场应用失败，请检查采集器');
  if (item.sync_state === 'applied') return uiText(item.enabled ? '现场已应用；是否出数请看读数状态' : '现场已应用删除配置');
  if (!item.collector_connected) return uiText(item.enabled ? '等待采集器联网并应用配置' : '删除待同步：等待采集器联网');
  return uiText(item.enabled ? '配置待现场应用' : '删除待现场应用');
}

function equipmentActions(id) {
  if (!topologyCapabilities.device_lifecycle) return '';
  const item = lifecycleDevices.find((d) => String(d.id) === String(id));
  const active = Boolean(item?.maintenance_until && item.maintenance_until > Date.now()/1000);
  return `${lifecycleConnections ? `<button type="button" data-change-ip="${escapeHtml(id)}">更换 IP / 处理重复登记</button><button type="button" data-equipment-history="${escapeHtml(id)}">查看历史</button>` : ""}${active ? '<b class="maintenance-badge">维护中</b>' : ''}<button type="button" data-equipment="edit" data-equipment-id="${escapeHtml(id)}">编辑设备</button><button type="button" data-equipment="${active?'end':'maintenance'}" data-equipment-id="${escapeHtml(id)}">${active?'结束维护':'维护模式'}</button><button type="button" data-equipment-audit="${escapeHtml(id)}">操作记录</button>`;
}

function renderMaintenanceNotice(device, reading) {
  const maintenance = reading && Object.prototype.hasOwnProperty.call(reading,'maintenance') ? reading.maintenance : device?.maintenance;
  const active = Boolean(maintenance?.until > Date.now()/1000);
  const notice = $('deviceMaintenanceNotice');
  if (!notice) return;
  notice.hidden = !active;
  notice.textContent = active ? `${uiText('维护中')} · ${maintenance.reason} · ${uiText('预计结束时间')} ${formatTime(maintenance.until)} · ${uiText('继续记录数据和报警，仅暂停云端电邮通知。')}` : '';
}

const equipmentErrors = {
  "Confirm that both addresses refer to the same physical instrument":"请先核对并确认是同一台物理仪表。",
  "The duplicate registration must be a different record":"请选择另一条重复登记。",
  "An IP change must stay on the same collector; verify cross-collector moves separately":"更换 IP 仅处理同一采集器下的设备，跨采集器迁移请单独核对。",
  "New address must match the selected duplicate registration":"新地址必须与所选重复登记一致。",
  "The selected registration already has a device association":"所选登记已有设备关联，请核对原设备。",
  "Select the original retained device":"请选择保留的原设备。",

  'This registration is linked to another device and cannot be restored separately':'这条重复登记已关联原设备，不能单独恢复。',
  'Device changed in another session; refresh and retry':'设备已被其他人修改，请关闭后重新打开。',
  'Device address is already in use; resolve the active device before restoring or saving':'地址已被在用设备占用，请先处理冲突。',
  'Active device capacity reached':'在用设备数量已达到上限。',
  'Restore the deleted device before editing':'设备已删除，请先恢复。',
  'Deleted devices cannot enter maintenance':'已删除设备不能进入维护模式。',
  'Device is already in maintenance; end it before starting another period':'设备已在维护中，请先结束现有维护。',
  'Connection changes require an enterprise administrator':'连接参数仅限企业管理员修改。',
  'Provide a reason and an end time between 1 minute and 7 days from now':'请填写维护原因，结束时间须在 1 分钟至 7 天内。',
};
function equipmentError(error) {
  return uiText(equipmentErrors[error.message] || (['TimeoutError','AbortError'].includes(error.name) ? '请求超时，结果尚未确认，请刷新核对后重试。' : friendlyError(error.message)));
}

async function openEquipment(mode,id) {
  if (equipmentBusy) return;
  try {
    await loadDeviceLifecycle();
    const item = lifecycleDevices.find((d) => String(d.id) === String(id));
    if (!item) throw new Error('设备不存在，请刷新列表。');
    equipmentTarget = {...item}; equipmentMode = mode;
    const titles = {edit:'编辑设备',restore:'恢复设备',maintenance:'维护模式',end:'结束维护'};
    $('equipmentTitle').textContent = titles[mode];
    $('equipmentIdentity').textContent = `${item.name} · ${item.room_name} · ${item.host}:${item.tcp_port} · ${item.id}`;
    $('equipmentHelp').textContent = mode === 'edit' ? '仅用于同一台仪表的信息修正；更换物理仪表请新建设备。历史记录保留原名称和车间。'
      : mode === 'restore' ? '恢复原设备身份及历史关联，等待现场应用后重新采集；地址或名称冲突时不会强行覆盖。'
      : mode === 'end' ? '结束后恢复新事件的云端电邮通知，不补发维护期间的旧报警。'
      : '继续记录数据和报警，仅暂停云端电邮通知。到期自动结束；已发送或正在发送的邮件无法撤回，现场声光报警不受影响。';
    $('equipmentEditFields').hidden = mode !== 'edit';
    $('equipmentMaintenanceFields').hidden = mode !== 'maintenance';
    $('equipmentName').value=item.name;
    $('equipmentRoom').innerHTML=rooms.map((r)=>`<option data-i18n-ignore value="${escapeHtml(r.id)}">${escapeHtml(r.name)}</option>`).join('');
    $('equipmentRoom').value=item.cleanroom_id;
    $('equipmentSite').innerHTML=topologySites.map((s)=>`<option data-i18n-ignore value="${escapeHtml(s.id)}">${escapeHtml(collectorDisplayName(s))}</option>`).join('');
    $('equipmentSite').value=item.site_id; $('equipmentHost').value=item.host;
    $('equipmentPort').value=item.tcp_port; $('equipmentSlave').value=item.slave;
    $('equipmentConnectionFields').hidden=!lifecycleConnections; $('equipmentConnectionFields').open=false;
    ['equipmentSite','equipmentHost','equipmentPort','equipmentSlave'].forEach((id)=>{$(id).disabled=!lifecycleConnections;});
    $('equipmentReason').value='';
    const until=new Date(Date.now()+3600000); until.setMinutes(until.getMinutes()-until.getTimezoneOffset());
    $('equipmentUntil').value=until.toISOString().slice(0,16);
    $('equipmentError').textContent=''; $('saveEquipment').textContent=mode==='edit'?'保存修改':mode==='restore'?'确认恢复':mode==='end'?'确认结束维护':'开始维护';
    $('equipmentDialog').showModal(); validateEquipment();
  } catch(error) {showToast(equipmentError(error),'error');}
}

function validateEquipment() {
  if (equipmentBusy) return false;
  let error='';
  if (equipmentMode==='edit') {
    if (!$('equipmentName').value.trim() || !$('equipmentRoom').value) error='请填写设备名称并选择车间。';
    else if (lifecycleConnections && (!parsePrivateIPv4($('equipmentHost').value) || !Number.isInteger(Number($('equipmentPort').value)) || Number($('equipmentPort').value)<1 || Number($('equipmentPort').value)>65535 || !Number.isInteger(Number($('equipmentSlave').value)) || Number($('equipmentSlave').value)<1 || Number($('equipmentSlave').value)>247)) error='请填写有效私有 IP、端口和 Slave ID。';
  }
  if (equipmentMode==='maintenance') {
    const end = new Date($('equipmentUntil').value).getTime()/1000;
    if (!$('equipmentReason').value.trim() || !Number.isFinite(end) || end<Date.now()/1000+60 || end>Date.now()/1000+7*86400) error='请填写维护原因，结束时间须在 1 分钟至 7 天内。';
  }
  $('equipmentError').textContent=uiText(error); $('saveEquipment').disabled=Boolean(error); return !error;
}

function closeEquipment() {if(!equipmentBusy){$('equipmentDialog').close(); equipmentTarget=null;}}
async function saveEquipment(event) {
  event.preventDefault(); if(equipmentBusy || !equipmentTarget || !validateEquipment()) return;
  const target=equipmentTarget, mode=equipmentMode;
  let url=`/api/admin/devices/${encodeURIComponent(target.id)}`, method='POST', payload;
  if(mode==='edit') {
    method='PATCH'; payload={name:$('equipmentName').value.trim(),cleanroom_id:$('equipmentRoom').value,expected_updated_at:target.updated_at};
    if(lifecycleConnections) Object.assign(payload,{host:parsePrivateIPv4($('equipmentHost').value),site_id:$('equipmentSite').value,tcp_port:Number($('equipmentPort').value),slave:Number($('equipmentSlave').value)});
  } else if(mode==='restore') url+='/restore';
  else {url+='/maintenance'; if(mode==='end') method='DELETE'; else payload={reason:$('equipmentReason').value.trim(),ends_at:new Date($('equipmentUntil').value).getTime()/1000};}
  equipmentBusy=true; let committed=false;
  ['saveEquipment','closeEquipment','cancelEquipment'].forEach((id)=>{$(id).disabled=true;});
  $('equipmentError').textContent='';
  try {
    const data=await api(url,{method,headers:{'Content-Type':'application/json'},...(payload?{body:JSON.stringify(payload)}:{}),signal:AbortSignal.timeout(15000)});
    committed=true;
    delete latestByDevice[target.id]; delete realtimeByDevice[target.id];
    if(mode==='edit'||mode==='restore') applyTopologyRooms(data);
    await loadDeviceLifecycle(); renderTopology(); updateRealtimeDisplay();
    $('equipmentDialog').close(); equipmentTarget=null;
    showToast(mode==='maintenance'?'维护已开始；数据和报警继续保留。':mode==='end'?'维护已结束。':'已保存，请查看现场生效状态。');
    if($('lifecycleListDialog').open) await renderLifecycleList();
  } catch(error) {
    if(committed||error.status===401){$('equipmentDialog').close(); equipmentTarget=null; if(error.status===401)$('lifecycleListDialog').close(); showToast(committed?'操作已保存，页面刷新失败，请重新加载。':'登录已失效，请重新登录。','error');}
    else $('equipmentError').textContent=equipmentError(error);
  } finally {equipmentBusy=false; ['saveEquipment','closeEquipment','cancelEquipment'].forEach((id)=>{$(id).disabled=false;});}
}

const lifecycleActionNames = {'device.address_changed':'更换设备地址','device.created':'添加设备','device.updated':'编辑设备','device.deleted':'删除设备','device.restored':'恢复设备','device.maintenance_started':'开始维护','device.maintenance_ended':'结束维护'};
const lifecycleFieldNames = {name:'设备名称',cleanroom_id:'所属车间',site_id:'现场采集器',host:'设备局域网 IP',tcp_port:'TCP 端口',slave:'Slave ID'};
function auditValue(key,value){
  if(key==='cleanroom_id') return rooms.find((r)=>r.id===value)?.name || value;
  if(key==='site_id') return topologySites.find((s)=>s.id===value)?.name || value;
  return String(value ?? '—');
}
async function openLifecycleList(mode,deviceId=null){
  lifecycleListMode=mode; lifecycleAuditDevice=deviceId; lifecyclePageIndex=0; $('lifecycleSearch').value='';
  $('lifecycleSearch').parentElement.hidden=mode!=='archive';
  $('lifecycleListTitle').textContent=mode==='archive'?'已删除设备':'操作记录';
  $('lifecycleListHelp').textContent=mode==='archive'?'历史仍可查询和导出。误删可恢复原设备；更换仪表请新建设备。':'按时间倒序显示操作人、变更内容及维护原因。';
  $('lifecycleListDialog').showModal(); await renderLifecycleList();
}
async function renderLifecycleList(){
  const seq=++lifecycleLoadSequence; $('lifecycleListError').textContent='';
  try{
    let total=0,html='';
    if(lifecycleListMode==='archive'){
      await loadDeviceLifecycle(); if(seq!==lifecycleLoadSequence)return;
      const needle=$('lifecycleSearch').value.trim().toLocaleLowerCase();
      const items=lifecycleDevices.filter((d)=>!d.enabled && `${d.name} ${d.room_name} ${d.host}`.toLocaleLowerCase().includes(needle)); total=items.length;
      lifecyclePageIndex=Math.min(lifecyclePageIndex,Math.max(0,Math.ceil(total/20)-1));
      html=items.slice(lifecyclePageIndex*20,lifecyclePageIndex*20+20).map((d)=>`<article class="lifecycle-record"><strong data-i18n-ignore>${escapeHtml(d.name)}</strong><p data-i18n-ignore>${escapeHtml(d.room_name)} · ${escapeHtml(d.host)}:${d.tcp_port}</p><p>${uiText('删除时间')} ${escapeHtml(formatTime(d.disabled_at))} · ${uiText('操作人')} <span data-i18n-ignore>${escapeHtml(d.deleted_by||uiText('未记录'))}</span></p><p>${escapeHtml(equipmentSyncText(d.id))}</p><div class="lifecycle-toolbar"><button type="button" data-equipment-history="${escapeHtml(d.id)}">查看历史</button>${d.linked_to ? `<span>${uiText("重复登记已关联原设备，不能单独恢复")}</span>` : `<button type="button" data-equipment="restore" data-equipment-id="${escapeHtml(d.id)}">恢复设备</button>`}<button type="button" data-equipment-audit="${escapeHtml(d.id)}">操作记录</button></div></article>`).join('');
    } else {
      const params=new URLSearchParams({offset:String(lifecyclePageIndex*20),limit:'20'});if(lifecycleAuditDevice)params.set('device_id',lifecycleAuditDevice);
      const data=await api(`/api/admin/device-audit?${params}`); if(seq!==lifecycleLoadSequence)return; total=data.total;
      html=data.items.map((a)=>{
        const detail=a.details||{};
        const changes=Object.entries(detail.after||{}).map(([key,value])=>`<p>${escapeHtml(uiText(lifecycleFieldNames[key]||key))}: <span data-i18n-ignore>${escapeHtml(auditValue(key,detail.before?.[key]))} → ${escapeHtml(auditValue(key,value))}</span></p>`).join('');
        return `<article class="lifecycle-record"><strong>${escapeHtml(uiText(lifecycleActionNames[a.action]||a.action))}</strong><p data-i18n-ignore>${escapeHtml(detail.name||a.device_id)}</p><p>${escapeHtml(formatTime(a.created_at))} · ${uiText('操作人')} <span data-i18n-ignore>${escapeHtml(a.actor)}</span></p>${changes}${detail.reason?`<p>${uiText('原因')}: <span data-i18n-ignore>${escapeHtml(detail.reason)}</span></p>`:''}${detail.ends_at?`<p>${uiText('预计结束时间')}: ${escapeHtml(formatTime(detail.ends_at))}</p>`:''}</article>`;
      }).join('');
    }
    $('lifecycleListContent').innerHTML=html||`<p>${uiText('暂无记录')}</p>`;
    $('lifecyclePage').textContent=`${lifecyclePageIndex+1} / ${Math.max(1,Math.ceil(total/20))}`;
    $('lifecyclePrevious').disabled=lifecyclePageIndex===0; $('lifecycleNext').disabled=(lifecyclePageIndex+1)*20>=total;
  } catch(error){if(seq===lifecycleLoadSequence){$('lifecycleListError').textContent=equipmentError(error);$('lifecycleListContent').innerHTML='';} if(error.status===401)$('lifecycleListDialog').close();}
}

async function openEquipmentHistory(id) {
  try {
    historyRooms = await api('/api/config?include_disabled=true');
    $('historyScopeInput').value='all';
    const entry=lifecycleDevices.find((d)=>String(d.id)===String(id));
    historySelectedDeviceIds=new Set(entry?.related_device_ids || [String(id)]);
    setHistoryRange(168,false);
    $('lifecycleListDialog').close(); ++lifecycleLoadSequence;
    switchView('history');
  } catch(error) { $('lifecycleListError').textContent=equipmentError(error); }
}

document.addEventListener('click',(event)=>{
  const ip=event.target.closest('[data-change-ip]'); if(ip)openIpChange(ip.dataset.changeIp);
  const discoveredIp=event.target.closest('[data-discovered-ip-change]'); if(discoveredIp)openIpChange(null,discoveredDevices[Number(discoveredIp.dataset.discoveredIpChange)]);
  const action=event.target.closest('[data-equipment]'); if(action)openEquipment(action.dataset.equipment,action.dataset.equipmentId);
  const history=event.target.closest('[data-equipment-history]'); if(history)openEquipmentHistory(history.dataset.equipmentHistory);
  const audit=event.target.closest('[data-equipment-audit]'); if(audit){if($('lifecycleListDialog').open)$('lifecycleListDialog').close();openLifecycleList('audit',audit.dataset.equipmentAudit);}
});
$('equipmentForm').addEventListener('submit',saveEquipment);
$('equipmentForm').addEventListener('input',validateEquipment);
$('equipmentForm').addEventListener('change',validateEquipment);
$('equipmentDialog').addEventListener('cancel',(event)=>{if(equipmentBusy)event.preventDefault();else equipmentTarget=null;});
$('closeEquipment').addEventListener('click',closeEquipment); $('cancelEquipment').addEventListener('click',closeEquipment);
$('openArchivedDevices').addEventListener('click',()=>openLifecycleList('archive'));
$('openDeviceAudit').addEventListener('click',()=>openLifecycleList('audit'));
$('closeLifecycleList').addEventListener('click',()=>{$('lifecycleListDialog').close();++lifecycleLoadSequence;});
$('refreshLifecycleList').addEventListener('click',renderLifecycleList);
$('lifecycleSearch').addEventListener('input',()=>{lifecyclePageIndex=0;renderLifecycleList();});
$('lifecyclePrevious').addEventListener('click',()=>{lifecyclePageIndex=Math.max(0,lifecyclePageIndex-1);renderLifecycleList();});
$('lifecycleNext').addEventListener('click',()=>{lifecyclePageIndex++;renderLifecycleList();});

let ipChangeBusy=false;
let ipChangeSnapshot=[];
let ipDiscovery=null;
async function openIpChange(id=null,discovery=null){
  if(ipChangeBusy)return;
  try{
    await loadDeviceLifecycle();
    if(!lifecycleConnections)throw new Error('Connection changes require an enterprise administrator');
    ipChangeSnapshot=lifecycleDevices.map((d)=>({...d}));ipDiscovery=discovery;
    const candidates=ipChangeSnapshot.filter((d)=>d.enabled && !d.linked_to && (!discovery || d.site_id===discovery.site_id));
    $('ipKeepDevice').innerHTML=`<option value="">${uiText('请选择原设备')}</option>`+candidates.map((d)=>`<option data-i18n-ignore value="${escapeHtml(d.id)}">${escapeHtml(d.name)} · ${escapeHtml(d.host)} · ${escapeHtml(d.room_name)}</option>`).join('');
    $('ipKeepDevice').value=id||'';$('ipKeepDevice').disabled=Boolean(id);
    $('ipSameDevice').checked=false;
    $('ipNewHost').value=discovery?.host||'';$('ipNewPort').value=discovery?.tcp_port||502;$('ipNewSlave').value=discovery?.slave||1;
    renderIpDuplicateOptions();$('ipChangeDialog').showModal();validateIpChange();
  }catch(error){showToast(equipmentError(error),'error');}
}
function renderIpDuplicateOptions(){
  const kept=ipChangeSnapshot.find((d)=>d.id===$('ipKeepDevice').value);
  $('ipDuplicateDevice').innerHTML=`<option value="">${uiText('新地址尚未登记')}</option>`+ipChangeSnapshot.filter((d)=>d.enabled && !d.linked_to && d.id!==kept?.id && d.site_id===kept?.site_id && (!ipDiscovery || (d.host===ipDiscovery.host && Number(d.tcp_port)===Number(ipDiscovery.tcp_port) && Number(d.slave)===Number(ipDiscovery.slave||1)))).map((d)=>`<option data-i18n-ignore value="${escapeHtml(d.id)}">${escapeHtml(d.name)} · ${escapeHtml(d.host)} · ${escapeHtml(d.room_name)}</option>`).join('');
  ['ipNewHost','ipNewPort','ipNewSlave'].forEach((id)=>{$(id).disabled=Boolean(ipDiscovery);});
}
function validateIpChange(){
  if(ipChangeBusy)return false;
  const kept=ipChangeSnapshot.find((d)=>d.id===$('ipKeepDevice').value);
  const duplicate=ipChangeSnapshot.find((d)=>d.id===$('ipDuplicateDevice').value);
  const host=parsePrivateIPv4($('ipNewHost').value);const port=Number($('ipNewPort').value),slave=Number($('ipNewSlave').value);
  let error='';
  if(!kept)error='请选择原设备';
  else if(!host || !Number.isInteger(port)||port<1||port>65535||!Number.isInteger(slave)||slave<1||slave>247)error='请填写有效私有 IP、端口和 Slave ID。';
  else if(!$('ipSameDevice').checked)error='请先核对并确认是同一台物理仪表。';
  $('ipChangePreview').textContent=kept ? `${kept.name} · ${kept.host}:${kept.tcp_port} → ${host||'—'}:${port||'—'}${duplicate ? ` · ${uiText('停用重复登记并关联历史')}: ${duplicate.name}`:''}` : '';
  $('ipChangeError').textContent=uiText(error);$('saveIpChange').disabled=Boolean(error);return !error;
}
function closeIpChange(){if(!ipChangeBusy)$('ipChangeDialog').close();}
async function saveIpChange(event){
  event.preventDefault();if(ipChangeBusy || !validateIpChange())return;
  const kept=ipChangeSnapshot.find((d)=>d.id===$('ipKeepDevice').value),duplicate=ipChangeSnapshot.find((d)=>d.id===$('ipDuplicateDevice').value);
  const body={host:parsePrivateIPv4($('ipNewHost').value),tcp_port:Number($('ipNewPort').value),slave:Number($('ipNewSlave').value),expected_updated_at:kept.updated_at,confirmed_same_device:true,
    duplicate_device_id:duplicate?.id||null,duplicate_updated_at:duplicate?.updated_at||null};
  ipChangeBusy=true;let committed=false;
  ['saveIpChange','closeIpChange','cancelIpChange'].forEach((id)=>{$(id).disabled=true;});
  try{
    const data=await api(`/api/admin/devices/${encodeURIComponent(kept.id)}/rebind`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});committed=true;
    [kept.id,duplicate?.id].filter(Boolean).forEach((id)=>{delete latestByDevice[id];delete realtimeByDevice[id];});
    applyTopologyRooms(data,kept.cleanroom_id);await loadDeviceLifecycle();renderTopology();updateRealtimeDisplay();
    $('ipChangeDialog').close();showToast('已更新原设备地址，等待采集器应用；历史记录已保留。');
    await loadDiscoveredDevices();
  }catch(error){
    if(committed||error.status===401){$('ipChangeDialog').close();showToast(committed?'操作已保存，页面刷新失败，请重新加载。':'登录已失效，请重新登录。','error');}
    else $('ipChangeError').textContent=equipmentError(error);
  }finally{ipChangeBusy=false;['saveIpChange','closeIpChange','cancelIpChange'].forEach((id)=>{$(id).disabled=false;});}
}
$('ipChangeForm').addEventListener('submit',saveIpChange);
$('ipChangeForm').addEventListener('input',(event)=>{if(event.target.id!=='ipSameDevice')$('ipSameDevice').checked=false;validateIpChange();});
$('ipKeepDevice').addEventListener('change',()=>{renderIpDuplicateOptions();$('ipSameDevice').checked=false;validateIpChange();});
$('ipDuplicateDevice').addEventListener('change',()=>{
  const d=ipChangeSnapshot.find((d)=>d.id===$('ipDuplicateDevice').value);
  if(d){$('ipNewHost').value=d.host;$('ipNewPort').value=d.tcp_port;$('ipNewSlave').value=d.slave;}
  ['ipNewHost','ipNewPort','ipNewSlave'].forEach((id)=>{$(id).disabled=Boolean(d||ipDiscovery);});
  $('ipSameDevice').checked=false;validateIpChange();
});
$('ipSameDevice').addEventListener('change',validateIpChange);
$('closeIpChange').addEventListener('click',closeIpChange);$('cancelIpChange').addEventListener('click',closeIpChange);
$('ipChangeDialog').addEventListener('cancel',(event)=>{if(ipChangeBusy)event.preventDefault();});

function renderHistoryRegistrationNotice(){
  const notice=$('historyRegistrationNotice');if(!notice)return;
  const linked=lifecycleDevices.some((d)=>historySelectedDeviceIds.has(d.id) && (d.linked_to || d.related_device_ids?.length>1));
  notice.hidden=!linked;
  notice.textContent=linked?uiText('已关联同一仪表的原编号和重复登记。历史保留原始名称与车间，历史设备数量按登记编号计算。'):'';
}
