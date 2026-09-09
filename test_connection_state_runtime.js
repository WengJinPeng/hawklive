const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const elements = new Map();
const context = vm.createContext({ Date, console, window: {}, document: {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {textContent:'', classList:{remove(){},add(){}}, hidden:false});
  return elements.get(id);
}}});
vm.runInContext(fs.readFileSync(__dirname+'/public/app.js','utf8').replace(/\ninitAuth\(\);\s*$/, ''), context);
function state(value) {
  context.reading = value;
  return vm.runInContext('overallState(reading)', context);
}
test('cloud timeout preserves history but does not assert a physical disconnect', () => {
  const old = {timestamp:Date.now()/1000-120,source:'device',online:false,connection_state:'sync_stale'};
  assert.equal(state(old).label,'更新超时');
  vm.runInContext('renderAlarmBanner(reading,false,true,[],[])',context);
  assert.equal(elements.get('alarmBannerTitle').textContent,'数据更新已超时');
  assert.match(elements.get('alarmBannerDetail').textContent,/不能据此认定设备离线/);
});
test('explicit device error is still reported and not hidden', () => {
  assert.equal(state({timestamp:Date.now()/1000,source:'device',online:false,error:'timed out'}).label,'设备离线');
});
test('fresh readings recover and no-data is a separate state', () => {
  assert.equal(state(null).label,'尚无数据');
  assert.equal(state({timestamp:Date.now()/1000,source:'device',online:true,alarm_details:[]}).label,'状态正常');
});

function topology(site, device, reading) {
  context.siteInput = site;
  context.deviceInput = device;
  context.readingInput = reading;
  return vm.runInContext('topologySites = [siteInput]; latestByDevice = readingInput ? {[deviceInput.id]: readingInput} : {}; topologyDeviceState(deviceInput)', context);
}
test('collector heartbeat stays online despite storage or device failures', () => {
  context.siteInput = {connected:true,storage_state:'warning',device_online:0,device_total:2};
  assert.equal(vm.runInContext('collectorNodeState(siteInput).label',context),'采集器在线');
  assert.equal(vm.runInContext('collectorNeedsAttention(siteInput)',context),true);
});
test('collector disconnect never labels devices offline or leaves cached online evidence', () => {
  const result = topology({id:'s',connected:false,device_states:[{device_id:'a',state:'online'}]}, {id:'a',site_id:'s'}, {source:'device',online:true,timestamp:Date.now()/1000});
  assert.equal(result.label,'状态待确认');
  assert.equal(vm.runInContext('collectorDeviceSummary(siteInput)',context),'状态待确认');
});
test('individual communication checks distinguish failures from success and missing checks', () => {
  for (const [state, expected] of [['online','在线'],['offline','离线'],['unknown','状态待确认']]) {
    assert.equal(topology({id:'s',connected:true,monitor_running:true,device_states:[{device_id:'a',state}]},{id:'a',site_id:'s'},null).label,expected);
  }
});
test('legacy stale readings mean unknown and recover on fresh real data', () => {
  const site = {id:'s',connected:true};
  assert.equal(topology(site,{id:'a',site_id:'s'},{source:'device',timestamp:Date.now()/1000-120,online:false,connection_state:'sync_stale'}).label,'状态待确认');
  assert.equal(topology(site,{id:'a',site_id:'s'},{source:'device',timestamp:Date.now()/1000,online:true}).label,'在线');
});

test('cloud distinguishes legacy installers and update rollback from collector connectivity', () => {
  context.siteInput={connected:true,version:'0.3.0'};
  assert.equal(vm.runInContext('collectorUpdateLabel(siteInput)',context),'需首次升级以启用自动更新');
  context.siteInput={connected:true,update_status:{enabled:true,state:'rolled_back'}};
  assert.equal(vm.runInContext('collectorUpdateLabel(siteInput)',context),'更新失败，已回退旧版');
  assert.equal(vm.runInContext('collectorNodeState(siteInput).label',context),'采集器在线');
});
