const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function harness() {
  const elements = new Map();
  const context = vm.createContext({
    console, AbortSignal, window: {}, document: {getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        textContent: '', disabled: false, open: false,
        showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
      });
      return elements.get(id);
    }},
  });
  vm.runInContext(fs.readFileSync(__dirname + '/public/app.js', 'utf8').replace(/\ninitAuth\(\);\s*$/, ''), context);
  vm.runInContext(`
    currentUser = {role:'customer_admin'};
    topologyCapabilities = {can_delete_devices:true};
    rooms = [{id:'room',name:'Workshop',devices:[{id:'device',name:'Test',host:'192.168.1.2'}]}];
    latestByDevice = {device:{device_id:'device'},other:{device_id:'other'}};
    realtimeByDevice = {device:[],other:[]};
    alarmEvents = [{device_id:'device'},{device_id:'other'}];
    applyTopologyRooms = (updated) => { rooms = updated; };
    updateRealtimeDisplay = () => {};
    updateNavAlarmCount = () => {};
    loadTopologySites = async () => {};
    loadDiscoveredDevices = async () => {};
    showToast = (message) => { lastToast = message; };
    openDeleteDeviceDialog('device');
  `, context);
  return {context, elements, run: (source) => vm.runInContext(source, context)};
}

test('opening and cancelling never sends a deletion', () => {
  const h = harness();
  h.context.api = () => assert.fail('Unexpected API request');
  h.run('closeDeleteDeviceDialog()');
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
  assert.equal(h.run('rooms[0].devices.length'), 1);
});

test('reopening topology removes a device deleted by another administrator', async () => {
  const h = harness();
  h.run('loadPendingCollectors=async()=>{}');
  h.context.api = async (url) => {
    assert.equal(url, '/api/config');
    return [{id:'room',name:'Workshop',devices:[]}];
  };
  await h.run('refreshTopology()');
  assert.equal(h.run('rooms[0].devices.length'), 0);
});

test('in-flight deletion blocks repeat submission and dismissal, then removes only target caches', async () => {
  const h = harness();
  let calls = 0;
  let finish;
  h.context.api = (url, options) => {
    calls++;
    assert.equal(url, '/api/admin/devices/device');
    assert.equal(options.method, 'DELETE');
    return new Promise(resolve => { finish = resolve; });
  };
  const pending = h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('confirmDeleteDeviceBtn').disabled, true);
  assert.equal(h.elements.get('cancelDeleteDeviceBtn').disabled, true);
  h.run('closeDeleteDeviceDialog()');
  assert.equal(h.elements.get('deleteDeviceDialog').open, true);
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(calls, 1);
  finish([{id:'room',devices:[]}]);
  await pending;
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
  assert.equal(h.run('latestByDevice.device'), undefined);
  assert.equal(h.run('realtimeByDevice.device'), undefined);
  assert.equal(h.run('alarmEvents.length'), 1);
  assert.ok(h.run('latestByDevice.other'));
});

test('failed deletion keeps device and dialog, shows error and allows retry', async () => {
  const h = harness();
  h.context.api = async () => { throw Error('Administrator permission is required'); };
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('deleteDeviceDialog').open, true);
  assert.equal(h.elements.get('confirmDeleteDeviceBtn').disabled, false);
  assert.match(h.elements.get('deleteDeviceFormError').textContent, /管理员/);
  assert.equal(h.run('rooms[0].devices.length'), 1);
  assert.ok(h.run('latestByDevice.device'));
  h.context.api = async () => [{id:'room',devices:[]}];
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
});

test('a failed follow-up refresh does not misreport the deletion as failed', async () => {
  const h = harness();
  h.context.api = async () => [{id:'room',devices:[]}];
  h.context.loadTopologySites = async () => { throw Error('offline'); };
  await h.run('deleteDevice({preventDefault(){}})');
  await Promise.resolve();
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
  assert.equal(h.elements.get('deleteDeviceFormError').textContent, '');
  assert.match(h.run('lastToast'), /设备已删除/);
});

test('render failure after commit closes the dialog and reports committed deletion', async () => {
  const h = harness();
  h.context.api = async () => [{id:'room',devices:[]}];
  h.context.updateRealtimeDisplay = () => { throw Error('render failed'); };
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
  assert.match(h.run('lastToast'), /设备已删除.*重新加载/);
});

test('expired session closes the modal so login is accessible', async () => {
  const h = harness();
  h.context.api = async () => { throw Object.assign(Error('Authentication required'), {status:401}); };
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('deleteDeviceDialog').open, false);
  assert.equal(h.run('deviceToDelete'), null);
  assert.match(h.run('lastToast'), /重新登录/);
});

test('timeout unlocks the dialog and reports unknown outcome rather than failed deletion', async () => {
  const h = harness();
  h.context.api = async (_url, options) => {
    assert.ok(options.signal);
    throw Object.assign(Error('timed out'), {name:'TimeoutError'});
  };
  await h.run('deleteDevice({preventDefault(){}})');
  assert.equal(h.elements.get('deleteDeviceDialog').open, true);
  assert.equal(h.elements.get('cancelDeleteDeviceBtn').disabled, false);
  assert.match(h.elements.get('deleteDeviceFormError').textContent, /结果尚未确认/);
});

test('retired devices remain selectable only in history and carry a distinct label', () => {
  const h = harness();
  h.run(`
    rooms[0].devices = [];
    historyRooms = [{id:'room',name:'Workshop',devices:[{id:'retired',name:'Test',enabled:false}]}];
  `);
  assert.equal(h.run('realtimeScopeDevices().length'), 0);
  assert.equal(h.run('historyScopeDevices()[0].device.id'), 'retired');
  assert.match(h.run("seriesName(historyRooms[0],historyRooms[0].devices[0],'room')"), /已删除/);
});

test('a stale latest response cannot restore the deleted device cache', async () => {
  const h = harness();
  h.context.document.getElementById('topologyView').classList = {contains: () => false};
  let finish;
  h.context.api = () => new Promise(resolve => { finish = resolve; });
  const pending = h.run('refreshLatest()');
  h.run('rooms[0].devices = []; delete latestByDevice.device; delete realtimeByDevice.device;');
  finish([{device_id:'device',timestamp:Date.now()/1000,source:'device'}]);
  await pending;
  assert.equal(h.run('latestByDevice.device'), undefined);
  assert.equal(h.run('realtimeByDevice.device'), undefined);
});

test('historical query includes readings throughout the selected end minute', () => {
  const h = harness();
  h.context.document.getElementById('historyStart').value = '2026-09-07T15:00';
  h.context.document.getElementById('historyEnd').value = '2026-09-07T16:00';
  const end = h.run('historyParams().end');
  assert.ok(end > new Date('2026-09-07T16:00:59').getTime()/1000);
  assert.ok(end < new Date('2026-09-07T16:01:00').getTime()/1000);
});
