const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const test=require('node:test');
function harness(){
  const elements=new Map();
  const el=(id)=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,open:false,parentElement:{},addEventListener(){},showModal(){this.open=true;},close(){this.open=false;}});return elements.get(id);};
  const context=vm.createContext({console,AbortSignal,URLSearchParams,window:{},document:{getElementById:el,addEventListener(){}}});
  const run=(s)=>vm.runInContext(s,context);
  run(fs.readFileSync(__dirname+'/public/app.js','utf8').replace(/\ninitAuth\(\);\s*$/,''));
  run(fs.readFileSync(__dirname+'/public/device-lifecycle.js','utf8'));
  run(`rooms=[{id:'room',name:'QA',devices:[{id:'device',name:'QA'}]}];topologySites=[{id:'site',name:'Collector'}];topologyCapabilities={device_lifecycle:true};
    lifecycleDevices=[{id:'device',name:'QA',cleanroom_id:'room',site_id:'site',host:'192.168.1.2',tcp_port:502,slave:1,enabled:true,updated_at:'revision',sync_state:'pending'}];
    loadDeviceLifecycle=async()=>{};lifecycleConnections=true;
    renderTopology=()=>{};updateRealtimeDisplay=()=>{};applyTopologyRooms=()=>{};showToast=(message)=>{lastToast=message;};
    latestByDevice={device:{},other:{}};realtimeByDevice={device:[],other:[]};`);
  return {context,run,el};
}

test('invalid connection disables save and correction re-enables it',async()=>{
  const h=harness();await h.run(`openEquipment('edit','device')`);
  h.el('equipmentHost').value='8.8.8.8';assert.equal(h.run('validateEquipment()'),false);assert.equal(h.el('saveEquipment').disabled,true);
  h.el('equipmentHost').value='192.168.1.9';assert.equal(h.run('validateEquipment()'),true);assert.equal(h.el('saveEquipment').disabled,false);
  h.el('equipmentSlave').value='1.5';assert.equal(h.run('validateEquipment()'),false);
});
test('maintenance needs reason and bounded end time before submission',async()=>{
  const h=harness();await h.run(`openEquipment('maintenance','device')`);assert.equal(h.el('saveEquipment').disabled,true);
  h.el('equipmentReason').value='Calibration';assert.equal(h.run('validateEquipment()'),true);
  h.el('equipmentUntil').value='2000-01-01T00:00';assert.equal(h.run('validateEquipment()'),false);
});
test('saving prevents duplicate submission and closing; success clears only target caches',async()=>{
  const h=harness();await h.run(`openEquipment('restore','device')`);let resolve,calls=0;
  h.context.api=()=>{calls++;return new Promise((done)=>{resolve=done;});};
  const pending=h.run('saveEquipment({preventDefault(){}})');
  assert.equal(h.el('saveEquipment').disabled,true);h.run('closeEquipment()');assert.equal(h.el('equipmentDialog').open,true);
  await h.run('saveEquipment({preventDefault(){}})');assert.equal(calls,1);resolve([]);await pending;
  assert.equal(h.el('equipmentDialog').open,false);assert.equal(h.run('latestByDevice.device'),undefined);assert.ok(h.run('latestByDevice.other'));
});
test('conflict leaves device and confirmation available for correction',async()=>{
  const h=harness();await h.run(`openEquipment('restore','device')`);
  h.context.api=async()=>{throw new Error('Device address is already in use; resolve the active device before restoring or saving');};
  await h.run('saveEquipment({preventDefault(){}})');assert.equal(h.el('equipmentDialog').open,true);assert.equal(h.el('saveEquipment').disabled,false);assert.ok(h.run('latestByDevice.device'));assert.match(h.el('equipmentError').textContent,/占用/);
});
test('a refresh failure after commit never claims the operation failed',async()=>{
  const h=harness();await h.run(`openEquipment('restore','device')`);h.context.api=async()=>[];h.run('loadDeviceLifecycle=async()=>{throw new Error("offline")};');
  await h.run('saveEquipment({preventDefault(){}})');assert.equal(h.el('equipmentDialog').open,false);assert.match(h.run('lastToast'),/已保存/);
});
test('expired login dismisses both nested dialogs',async()=>{
  const h=harness();await h.run(`openEquipment('restore','device')`);h.el('lifecycleListDialog').open=true;
  h.context.api=async()=>{const e=new Error('expired');e.status=401;throw e;};await h.run('saveEquipment({preventDefault(){}})');
  assert.equal(h.el('equipmentDialog').open,false);assert.equal(h.el('lifecycleListDialog').open,false);
});
test('applied configuration does not claim successful readings',()=>{
  const h=harness();h.run(`lifecycleDevices[0].sync_state='applied'`);assert.match(h.run(`equipmentSyncText('device')`),/读数状态/);
  h.run(`lifecycleDevices[0].enabled=false`);assert.match(h.run(`equipmentSyncText('device')`),/删除配置/);
});
test('maintenance notice expires by time without hiding an underlying reading',()=>{
  const h=harness();h.run(`renderMaintenanceNotice({maintenance:{reason:'QA',until:Date.now()/1000+60}},null)`);assert.equal(h.el('deviceMaintenanceNotice').hidden,false);
  h.run(`renderMaintenanceNotice({maintenance:{reason:'QA',until:Date.now()/1000-1}},null)`);assert.equal(h.el('deviceMaintenanceNotice').hidden,true);
});

test('archived history shortcut selects the original ID across workshops',async()=>{
  const h=harness();h.context.api=async()=>[{id:'room',devices:[{id:'device',enabled:false}]}];
  h.run("setHistoryRange=(hours)=>{chosenHours=hours;};switchView=(view)=>{chosenView=view;}");
  await h.run("openEquipmentHistory('device')");
  assert.equal(h.el('historyScopeInput').value,'all');assert.equal(h.run("historySelectedDeviceIds.has('device')"),true);
  assert.equal(h.run('historySelectedDeviceIds.size'),1);assert.equal(h.run('chosenView'),'history');assert.equal(h.run('chosenHours'),168);
});
test('wallboard keeps maintenance distinct from normal without masking alarms or disconnection',()=>{
  const source=fs.readFileSync(__dirname+'/public/wallboard.js','utf8');
  const fn=source.slice(source.indexOf('function deviceState('),source.indexOf('function statePriority('));
  const context=vm.createContext({reading:{online:true,maintenance:{until:Date.now()/1000+60}},readingFor(){return this.reading;},isFresh:()=>true});
  vm.runInContext('readingFor=()=>reading;',context);vm.runInContext(fn,context);
  assert.equal(vm.runInContext("deviceState({id:'device'})",context),'maintenance');
  context.reading.alarm_status='ALARM_ACTIVE';assert.equal(vm.runInContext("deviceState({id:'device'})",context),'alarm');
  context.reading.online=false;assert.equal(vm.runInContext("deviceState({id:'device'})",context),'offline');
});

test('IP changes require explicit physical identity confirmation and a valid address',async()=>{
 const h=harness();await h.run("openIpChange('device')");h.el('ipNewHost').value='10.12.0.140';assert.equal(h.run('validateIpChange()'),false);assert.equal(h.el('saveIpChange').disabled,true);
 h.el('ipSameDevice').checked=true;assert.equal(h.run('validateIpChange()'),true);h.el('ipNewHost').value='8.8.8.8';assert.equal(h.run('validateIpChange()'),false);
});
test('discovery never selects an original automatically',async()=>{
 const h=harness();await h.run("openIpChange(null,{site_id:'site',host:'10.12.0.140',tcp_port:502,slave:1})");assert.equal(h.el('ipKeepDevice').value,'');assert.equal(h.el('ipNewHost').value,'10.12.0.140');assert.equal(h.el('saveIpChange').disabled,true);
});
test('linked history selects both immutable registration IDs',async()=>{
 const h=harness();h.context.api=async()=>[];h.run("lifecycleDevices[0].related_device_ids=['device','duplicate'];setHistoryRange=()=>{};switchView=()=>{}");await h.run("openEquipmentHistory('device')");assert.equal(h.run('historySelectedDeviceIds.size'),2);assert.equal(h.run("historySelectedDeviceIds.has('duplicate')"),true);
});
test('IP rebind conflict keeps dialog open; retry sends captured revisions only once',async()=>{
 const h=harness();await h.run("openIpChange('device')");h.el('ipNewHost').value='10.12.0.140';h.el('ipSameDevice').checked=true;
 h.context.api=async()=>{throw new Error('Device changed in another session; refresh and retry')};await h.run('saveIpChange({preventDefault(){}})');assert.equal(h.el('ipChangeDialog').open,true);assert.match(h.el('ipChangeError').textContent,/重新打开/);
 let done,calls=0;h.context.api=(url,options)=>{calls++;assert.equal(JSON.parse(options.body).expected_updated_at,'revision');return new Promise(resolve=>{done=resolve})};h.run('loadDiscoveredDevices=async()=>{}');
 const pending=h.run('saveIpChange({preventDefault(){}})');h.run('closeIpChange()');assert.equal(h.el('ipChangeDialog').open,true);await h.run('saveIpChange({preventDefault(){}})');assert.equal(calls,1);done([]);await pending;assert.equal(h.el('ipChangeDialog').open,false);
});

test('historical registration counts are explained only for a selected linked instrument',()=>{
 const h=harness();h.run("lifecycleDevices[0].related_device_ids=['device','duplicate'];historySelectedDeviceIds=new Set(['device']);renderHistoryRegistrationNotice()");assert.equal(h.el('historyRegistrationNotice').hidden,false);assert.match(h.el('historyRegistrationNotice').textContent,/登记编号/);h.run('historySelectedDeviceIds.clear();renderHistoryRegistrationNotice()');assert.equal(h.el('historyRegistrationNotice').hidden,true);
});
