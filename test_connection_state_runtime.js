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
