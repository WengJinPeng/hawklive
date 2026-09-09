const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function harness() {
  const elements = new Map();
  function element(id) {
    return {
      id, value:'', textContent:'', innerHTML:'', disabled:false, hidden:false, open:false,
      showModal(){this.open=true;}, close(){this.open=false;}, focus(){},
      querySelector(){return null;}, addEventListener(){},
    };
  }
  const document = {
    getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},
    querySelector(){return element('query');}, querySelectorAll(){return [];}, createElement(){return element('created');},
  };
  const context=vm.createContext({console,AbortSignal,document,window:{},Date,Intl,URLSearchParams,localStorage:{getItem:()=>null,setItem(){}}});
  vm.runInContext(fs.readFileSync(__dirname+'/public/app.js','utf8').replace(/\ninitAuth\(\);\s*$/,''),context);
  vm.runInContext(`
    topologyCapabilities={can_cleanup_data:true,can_delete_workshops:true};
    dataCleanupPreview={retired_devices:{devices:2,readings:3,latest:0,alarms:1,notifications:1,total:7},all_monitoring:{devices:0,readings:9,latest:2,alarms:4,notifications:1,total:16}};
    rooms=[{id:'room-1',name:'Workshop A',devices:[]}];
    historyRooms=rooms;storedHistory=[1];storedTrendHistory=[1];alarmEvents=[1];latestByDevice={device:1};realtimeByDevice={device:[1]};
    refreshStats=async()=>{};loadDeviceLifecycle=async()=>{};loadTopologySites=async()=>{};loadDataCleanupPreview=async()=>{};
    updateRealtimeDisplay=()=>{};updateNavAlarmCount=()=>{};applyTopologyRooms=(value)=>{rooms=value};
    showToast=(message)=>{lastToast=message};
  `,context);
  return {context,elements,el:(id)=>document.getElementById(id),run:(source)=>vm.runInContext(source,context)};
}

test('cleanup remains disabled until the exact irreversible confirmation is entered',()=>{
  const h=harness();
  h.run("openDataCleanupDialog('retired_devices')");
  assert.equal(h.el('dataCleanupDialog').open,true);
  assert.equal(h.el('dataCleanupConfirmCode').textContent,'PURGE RETIRED');
  h.el('dataCleanupConfirmInput').value='purge retired';
  assert.equal(h.run('validateDataCleanup()'),false);
  h.el('dataCleanupConfirmInput').value='PURGE RETIRED';
  assert.equal(h.run('validateDataCleanup()'),true);
  assert.equal(h.el('confirmDataCleanupBtn').disabled,false);
});

test('global cleanup sends the tenant scope and clears stale browser monitoring caches',async()=>{
  const h=harness();let body;
  h.context.api=async(url,options)=>{assert.equal(url,'/api/admin/data-cleanup');body=JSON.parse(options.body);return {remaining:h.context.dataCleanupPreview};};
  h.run("openDataCleanupDialog('all_monitoring')");
  h.el('dataCleanupConfirmInput').value='PURGE ALL';
  await h.run('runDataCleanup({preventDefault(){}})');
  assert.deepEqual(body,{scope:'all_monitoring',confirmation:'PURGE ALL'});
  assert.equal(h.el('dataCleanupDialog').open,false);
  assert.equal(h.run('Object.keys(latestByDevice).length'),0);
  assert.equal(h.run('storedHistory.length'),0);
});

test('workshop deletion requires its exact name and keeps the dialog open on a server block',async()=>{
  const h=harness();
  h.run("openDeleteWorkshopDialog('room-1')");
  h.el('deleteWorkshopConfirmInput').value='Workshop';
  assert.equal(h.run('validateWorkshopDeletion()'),false);
  h.el('deleteWorkshopConfirmInput').value='Workshop A';
  assert.equal(h.run('validateWorkshopDeletion()'),true);
  h.context.api=async()=>{throw new Error('Delete or move active devices before deleting this workshop');};
  await h.run('deleteWorkshop({preventDefault(){}})');
  assert.equal(h.el('deleteWorkshopDialog').open,true);
  assert.match(h.el('deleteWorkshopFormError').textContent,/移动或删除/);
});

test('successful workshop deletion applies the returned topology',async()=>{
  const h=harness();
  h.run("openDeleteWorkshopDialog('room-1')");
  h.el('deleteWorkshopConfirmInput').value='Workshop A';
  h.context.api=async(url,options)=>{assert.match(url,/expected_name=Workshop%20A/);assert.equal(options.method,'DELETE');return [];};
  await h.run('deleteWorkshop({preventDefault(){}})');
  assert.equal(h.el('deleteWorkshopDialog').open,false);
  assert.equal(h.run('rooms.length'),0);
});
