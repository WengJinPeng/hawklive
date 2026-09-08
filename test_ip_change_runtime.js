const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const test=require('node:test');
function collector(){
 const elements=new Map();
 const el=id=>{if(!elements.has(id))elements.set(id,{value:'room',hidden:false,disabled:false,innerHTML:'',textContent:''});return elements.get(id);};
 const context=vm.createContext({console,window:{confirm:()=>true},document:{getElementById:el}});
 const run=s=>vm.runInContext(s,context);
 run(fs.readFileSync(__dirname+'/public/collector.js','utf8').split('$("refreshBtn").addEventListener("click", refreshAll);')[0]);
 run(`rooms=[{id:'room',name:'Workshop',devices:[{_key:'stable-id',id:'stable-id',name:'Original',host:'10.12.0.14',tcpPort:502,slave:1}]}];statusCache={max_active_devices:1};scanResults=[{host:'10.12.0.140',tcp_port:502,slave:1,verified:true}];scanSelection=new Set(['10.12.0.140:502:1']);showToast=(message)=>{lastToast=message};renderDevices=()=>{};setDirty=(value)=>{dirty=value;};`);
 return {run,context,el};
}
test('scanning cannot silently add an unknown endpoint',()=>{
 const h=collector();h.run('renderScanResults()');assert.equal(h.el('addScannedBtn').disabled,true);
 h.run('addSelectedScannedDevices()');assert.match(h.run('lastToast'),/选择/);assert.equal(h.run('rooms[0].devices.length'),1);assert.equal(h.run('dirty'),false);
});
test('changing IP at capacity preserves ID, name, workshop and one registration',()=>{
 const h=collector();h.run("scanAssignments.set('10.12.0.140:502:1','stable-id');renderScanResults()");assert.equal(h.el('addScannedBtn').disabled,false);
 h.run('addSelectedScannedDevices()');assert.equal(h.run('rooms[0].devices.length'),1);assert.equal(h.run('rooms[0].devices[0].id'),'stable-id');assert.equal(h.run('rooms[0].devices[0].name'),'Original');assert.equal(h.run('rooms[0].id'),'room');assert.equal(h.run('rooms[0].devices[0].host'),'10.12.0.140');assert.equal(h.run('dirty'),true);
});
test('cancelling physical identity confirmation does not mutate the draft',()=>{
 const h=collector();h.context.window.confirm=()=>false;h.run("scanAssignments.set('10.12.0.140:502:1','stable-id');addSelectedScannedDevices()");assert.equal(h.run('rooms[0].devices[0].host'),'10.12.0.14');assert.equal(h.run('dirty'),false);
});
test('explicit new device remains separate and capacity is enforced',()=>{
 const h=collector();h.run("scanAssignments.set('10.12.0.140:502:1','new');addSelectedScannedDevices()");assert.equal(h.run('rooms[0].devices.length'),1);assert.match(h.run('lastToast'),/上限/);
 h.run('statusCache.max_active_devices=2;addSelectedScannedDevices()');assert.equal(h.run('rooms[0].devices.length'),2);assert.equal(h.run('rooms[0].devices[0].host'),'10.12.0.14');assert.equal(h.run('rooms[0].devices[1].id'),'');
});
test('two addresses cannot update one original in the same batch',()=>{
 const h=collector();h.run("scanResults.push({...scanResults[0],host:'10.12.0.141'});scanSelection.add('10.12.0.141:502:1');scanAssignments.set('10.12.0.140:502:1','stable-id');scanAssignments.set('10.12.0.141:502:1','stable-id');addSelectedScannedDevices()");assert.match(h.run('lastToast'),/两个地址/);assert.equal(h.run('rooms[0].devices[0].host'),'10.12.0.14');assert.equal(h.run('dirty'),false);
});
test('unverified units and removed originals cannot be rebound',()=>{
 const h=collector();h.run("scanAssignments.set('10.12.0.140:502:1','stable-id');scanResults[0].verified=false;addSelectedScannedDevices()");assert.equal(h.run('dirty'),false);
 h.run('scanResults[0].verified=true;rooms[0].devices[0]._removed=true;addSelectedScannedDevices()');assert.match(h.run('lastToast'),/重新选择/);assert.equal(h.run('dirty'),false);
});

test('an IP change does not present a discovery probe or old cached reading as a new reading',()=>{
 const h=collector();h.run("deviceStates['stable-id']={online:true,source:'device',last_reading_at:Date.now()/1000};scanAssignments.set('10.12.0.140:502:1','stable-id');addSelectedScannedDevices()");assert.equal(h.run("deviceTests['stable-id']"),undefined);assert.equal(h.run('deviceVisualState(rooms[0].devices[0])'),'waiting');assert.match(h.run('stateDetails(rooms[0].devices[0]).source'),/实际读数/);
});
