const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function harness() {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {open: false, dataset: {}, closest: () => ({})});
    return elements.get(id);
  };
  const context = vm.createContext({console, window: {}, document: {getElementById: el}});
  const run = source => vm.runInContext(source, context);
  run(fs.readFileSync(__dirname + '/public/collector.js', 'utf8').split('$("refreshBtn").addEventListener')[0]);
  run(`renderDevices=()=>{};populateRoomSelects=()=>{};renderCurrentBlocker=()=>{};updateInventorySummary=()=>{};
       hydrateRooms=value=>value;setDirty=value=>{dirty=value};
       rooms=[{id:'old'}];`);
  return {context, run, el};
}

test('background refresh applies newly downloaded inventory', async () => {
  const h = harness();
  h.run(`renderStatus=()=>{};api=async path=>path.endsWith('/config')?[{id:'new'}]:{devices:[]};`);
  await h.run('loadStatus({background:true})');
  assert.equal(h.run('rooms[0].id'), 'new');
});

for (const editing of ['dirty=true', '$("deviceDialog").open=true']) {
  test('background refresh preserves editing state: ' + editing, async () => {
    const h = harness();
    h.run(`renderStatus=()=>{};requests=[];api=async path=>{requests.push(path);return {devices:[]}};${editing};`);
    await h.run('loadStatus({background:true})');
    assert.equal(h.run('rooms[0].id'), 'old');
    assert.equal(h.run('requests.length'), 1);
  });
  test('edit starting during a request survives: ' + editing, async () => {
    const h = harness();
    h.run(`renderStatus=()=>{};api=async path=>{
      if(path.endsWith('/config'))return new Promise(resolve=>{finish=resolve});
      return {devices:[]};};`);
    const pending = h.run('loadStatus({background:true})');
    h.run(`${editing};finish([{id:'new'}]);`);
    await pending;
    assert.equal(h.run('rooms[0].id'), 'old');
  });
}

test('partial outage cannot claim the whole collection path is healthy', () => {
  const h = harness();
  h.run(`status={mode:'device',device_total:2,device_online:1,monitor_running:true,
    devices:[{online:true,source:'device',last_reading_at:Date.now()/1000}],
    cloud_configured:true,cloud_connected:true,last_upload_at:Date.now()/1000,
    pending_uploads:0,quarantined_uploads:0,unassigned_uploads:0,
    last_cloud_contact_at:Date.now()/1000,storage:{state:'ok'}};renderStatus(status);`);
  assert.equal(h.el('readinessPanel').className, 'readiness-panel warning');
  assert.match(h.el('readinessTitle').textContent, /部分设备/);
  assert.equal(h.el('lastCloudContact').textContent, h.run('when(status.last_cloud_contact_at)'));
  h.run('status.device_total=1;renderStatus(status)');
  assert.equal(h.el('readinessPanel').className, 'readiness-panel ready');
  h.run("status.cloud_state='attention';renderStatus(status)");
  assert.equal(h.el('readinessPanel').className, 'readiness-panel warning');
  assert.match(h.el('readinessTitle').textContent, /云端部分通道/);
});
