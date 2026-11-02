const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const recordId = '11111111222243338444555555555555';
const tick = () => new Promise(setImmediate);
function fixture(valid = false) {
  const now = Date.now() / 1000;
  return {id:'entry-1',record_id:recordId,device_id:'fixture-device',cleanroom_id:'fixture-room',
    raw_text:'{"fixture":"<script>alert(1)</script>"}',raw_sha256:'a'.repeat(64),present:true,reported_at:now,
    metadata:{attempts:5,measured_at:1000,quarantined_at:now,error:'<img src=x onerror=alert(1)>'},
    validation:valid ? {id:'validation-1',valid:true,raw_sha256:'a'.repeat(64),expires_at:now+300,issues:[]} : null};
}
function harness() {
  const elements=new Map(),handlers=new Map(),timers=new Map();let next=0;
  const el=(id)=>{
    if(!elements.has(id))elements.set(id,{id,value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,open:false,
      listeners:{},addEventListener(name,fn){this.listeners[name]=fn;},focus(){},querySelectorAll(){return [];},
      showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}});
    return elements.get(id);
  };
  const document={getElementById:el,addEventListener:(name,fn)=>handlers.set(name,fn)};
  const requests=[];
  const h={el,requests,respond:async()=>({items:[fixture()],jobs:[],supported:true,can_retry:true,report:{total:1,reported_at:Date.now()/1000}})};
  const context=vm.createContext({document,Date,Map,encodeURIComponent,crypto:{randomUUID:()=>`request-${++next}`},
    setTimeout:(fn)=>{const id=++next;timers.set(id,fn);return id;},clearTimeout:(id)=>timers.delete(id),
    topologySites:[{id:'site-1',hostname:'QA',version:'0.6.11'}],collectorDisplayName:()=> 'Synthetic node',
    uiText:(v)=>v,escapeHtml:(v)=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    api:async(url,options)=>{requests.push({url,options});return h.respond(url,options);}});
  vm.runInContext(fs.readFileSync(__dirname+'/public/quarantine-maintenance.js','utf8'),context);
  h.click=(attribute,value)=>handlers.get('click')({target:{closest:(selector)=>selector===`[data-${attribute}]` ? {dataset:{[attribute.replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]:value}} : null}});
  h.open=async(site='site-1')=>{h.click('collector-quarantine',site);await tick();};
  h.input=(value)=>{el('quarantineConfirmUuid').value=value;el('quarantineConfirmUuid').listeners.input();};
  h.submit=()=>el('quarantineConfirmation').listeners.submit({preventDefault(){}});
  return h;
}

test('old collectors show upgrade guidance and original text is escaped',async()=>{
  const h=harness();h.respond=async()=>({items:[fixture(true)],jobs:[],supported:false,can_retry:false,report:null});
  await h.open();
  assert.match(h.el('quarantineStatus').textContent,/需要升级/);
  assert.match(h.el('quarantineResults').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(h.el('quarantineResults').innerHTML,/<img|<script>/);
  h.click('quarantine-retry','entry-1');assert.equal(h.el('quarantineConfirmation').hidden,true);
});

test('validation failure cannot enable replay and exact UUID confirmation is mandatory',async()=>{
  const h=harness();await h.open();
  h.el('quarantineResults').querySelectorAll=()=>[{dataset:{quarantineRaw:'entry-1'}}];
  h.respond=async()=>({valid:false,issues:[{field:'particle_unit_code',message:'Unsupported unit'}]});
  h.click('quarantine-validate','entry-1');await tick();
  assert.match(h.el('quarantineResults').innerHTML,/details data-quarantine-raw="entry-1" open/);
  assert.match(h.el('quarantineResults').innerHTML,/Unsupported unit/);
  h.click('quarantine-retry','entry-1');assert.equal(h.el('quarantineConfirmation').hidden,true);
  h.respond=async()=>({items:[fixture(true)],jobs:[],supported:true,can_retry:true,report:null});
  await h.el('refreshQuarantine').listeners.click();await tick();h.click('quarantine-retry','entry-1');
  h.input('wrong');assert.equal(h.el('confirmQuarantineRetry').disabled,true);
  h.input('11111111-2222-4333-8444-555555555555');assert.equal(h.el('confirmQuarantineRetry').disabled,false);
});

test('lost submit responses reuse one request ID and accepted status waits for local confirmation',async()=>{
  const h=harness();h.respond=async()=>({items:[fixture(true)],jobs:[],supported:true,can_retry:true,report:null});
  await h.open();h.click('quarantine-retry','entry-1');h.input(recordId);
  let post=0;
  h.respond=async(url,options)=>{
    if(options?.method==='POST'){if(++post===1)throw new Error('lost response');return {};}
    return {items:[fixture(true)],jobs:[{record_uuid:recordId,state:'accepted',created_at:1000}],supported:true,can_retry:true,report:null};
  };
  await h.submit();assert.match(h.el('quarantineConfirmError').textContent,/未获确认/);
  await h.submit();
  const bodies=h.requests.filter(r=>r.options?.method==='POST').map(r=>JSON.parse(r.options.body));
  assert.ok(h.requests.filter(r=>r.options?.method==='POST').every(r=>r.options.headers['Content-Type']==='application/json'));
  assert.equal(bodies.length,2);assert.equal(bodies[0].request_id,bodies[1].request_id);
  assert.match(h.el('quarantineTasks').innerHTML,/等待本地确认/);
  assert.doesNotMatch(h.el('quarantineTasks').innerHTML,/补传完成/);
});

test('node switches ignore stale responses and failed refresh removes old actionable data',async()=>{
  const h=harness();let resolve;
  h.respond=()=>new Promise(r=>{resolve=r;});h.click('collector-quarantine','old-site');
  h.respond=async()=>({items:[],jobs:[],supported:false,can_retry:false,report:null});await h.open('new-site');
  resolve({items:[fixture(true)],jobs:[],supported:true,can_retry:true,report:null});await tick();
  assert.equal(h.el('quarantineResults').innerHTML,'');
  h.respond=async()=>({items:[fixture(true)],jobs:[],supported:true,can_retry:true,report:null});
  await h.open();h.click('quarantine-retry','entry-1');h.input(recordId);
  h.respond=async()=>{throw new Error('offline');};h.el('refreshQuarantine').listeners.click();await tick();
  assert.equal(h.el('quarantineResults').innerHTML,'');assert.equal(h.el('quarantineConfirmation').hidden,true);
  assert.equal(h.el('confirmQuarantineRetry').disabled,true);
});
