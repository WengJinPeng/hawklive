const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const elements = new Map();
const context = vm.createContext({Date, Set, console, window:{}, document:{getElementById(id){
  if (!elements.has(id)) elements.set(id,{hidden:false,textContent:''});
  return elements.get(id);
}}});
vm.runInContext(fs.readFileSync(__dirname+'/public/collector.js','utf8').split('$("refreshBtn").addEventListener')[0],context);
function check(status, event) {
  context.status = status;
  context.log = {timestamp:100,level:'ERROR',event,message:'test'};
  vm.runInContext('statusCache=status; renderCurrentBlocker([log])',context);
  return elements.get('currentBlocker').hidden;
}
test('successful maintenance resolves old error but retains history',()=>{
  assert.equal(check({storage:{integrity:'ok',last_error:null,last_run_at:200}},'local_storage_maintenance_failed'),true);
  assert.equal(context.log.event,'local_storage_maintenance_failed');
});
test('current or unverified backup failure stays visible',()=>{
  for (const storage of [null,{integrity:'not_checked',last_run_at:200},{integrity:'ok',last_error:'busy',last_run_at:200},{integrity:'ok',last_run_at:90}])
    assert.equal(check({storage},'local_storage_maintenance_failed'),false);
});
test('only healthy cloud recovery resolves old sync errors',()=>{
  assert.equal(check({cloud_state:'connected',last_cloud_contact_at:200},'cloud_sync_failed'),true);
  assert.equal(check({cloud_state:'connected',last_cloud_contact_at:200},'cloud_connection_failed'),true);
  assert.equal(check({cloud_state:'attention',last_cloud_contact_at:200},'cloud_sync_failed'),false);
});
