const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
function environment(file) {
  const elements = new Map(), axes = Array.from({length:7},()=>({textContent:''}));
  const calls = [];
  const ctx = new Proxy({}, {get:(_,key)=>(...args)=>calls.push([key,...args]),set:()=>true});
  function element(id='') {
    return {id,hidden:false,style:{setProperty(){}},dataset:{},attributes:{},events:{},children:[],clientWidth:600,offsetWidth:220,offsetHeight:85,offsetLeft:0,offsetTop:0,
      classList:{toggle(){},add(){},remove(){}},nextElementSibling:{},
      setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];},
      querySelector(){return element();},querySelectorAll(){return [];},appendChild(e){this.children.push(e);},
      addEventListener(k,fn){this.events[k]=fn;}, getBoundingClientRect(){return {width:600,height:260,left:0,top:0};},getContext(){return ctx;}};
  }
  const document = {activeElement:null,body:element(),documentElement:element(),createElement:()=>element(),getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},querySelector:()=>element(),querySelectorAll:s=>s==='.x-axis span'?axes:[]};
  const context=vm.createContext({console,document,window:{devicePixelRatio:1},navigator:{language:'en'},localStorage:{getItem:()=>null},Date,Intl,URLSearchParams});
  let source=fs.readFileSync(__dirname+'/public/'+file,'utf8');
  source=file==='app.js'?source.replace(/\ninitAuth\(\);\s*$/,''):source.slice(0,source.indexOf('\ndocument.querySelectorAll("[data-locale]").forEach((button) => button.addEventListener'));
  vm.runInContext(source,context);
  return {context,document,elements,axes,calls,run:s=>vm.runInContext(s,context)};
}
function wall(rows) {const e=environment('wallboard.js'); e.context.rows=rows;e.run('trendReadings=rows; renderTrend()');return e;}
const now=Date.now()/1000;
const row=(timestamp,value=32000)=>({timestamp,particles:{pm_0_5_um:value}});
test('cold start and unordered readings start at x=0, ordered by time',()=>{
 const e=wall([row(now-10,40000),row(now-180,30000),row(now-100,35000)]);
 const path=e.elements.get('trendMainPath').attributes.d;
 assert.match(path,/^M0\.0 /); const xs=[...path.matchAll(/[ML]([\d.]+)/g)].map(m=>+m[1]);assert.deepEqual(xs,[...xs].sort((a,b)=>a-b));
});
test('single point starts at the left and has no misleading filled triangle',()=>{
 const e=wall([row(now-5)]);assert.match(e.elements.get('trendMainPath').attributes.d,/^M0\.0 /);assert.equal(e.elements.get('trendAreaPath').attributes.d,'');
});
test('out of window and future values do not become current chart peaks',()=>{
 const e=wall([row(now-90000,999999),row(now+1000,888888),row(now-10,12345)]);assert.equal(e.elements.get('peakValue').textContent,'12,345');
});
test('empty trend clears paths and shows empty state',()=>{
 const e=wall([]);assert.equal(e.elements.get('trendMainPath').attributes.d,'');assert.equal(e.elements.get('trendEmpty').hidden,false);
});
test('missing time intervals are not filled or connected',()=>{
 const e=wall([row(now-4000),row(now-3990),row(now-20),row(now-10)]);assert.equal((e.elements.get('trendMainPath').attributes.d.match(/M/g)||[]).length,2);
});
test('data gap verdict resolves in both languages',()=>{
 const e=environment('wallboard.js');e.run('rooms=[{id:"room",devices:[{id:"offline"}]}]; renderVerdict()');assert.equal(e.elements.get('verdictTitle').textContent,'Data incomplete');e.run('locale="zh-CN"; renderVerdict()');assert.equal(e.elements.get('verdictTitle').textContent,'数据不完整');
});
test('all six canvas charts share a chronological axis and actual point units',()=>{
 const e=environment('app.js');
 for (const [id,key] of [['trendCanvas','pm_0_5_um'],['temperatureTrendCanvas','temperature'],['humidityTrendCanvas','humidity'],['historyCanvas','pm_0_5_um'],['historyTemperatureCanvas','temperature'],['historyHumidityCanvas','humidity']]) {
  const canvas=e.document.getElementById(id);canvas.parentElement=e.document.createElement();e.context.canvas=canvas;e.context.key=key;
  e.run('drawChart(canvas,[{name:"Device <A>",color:"#123456",room:{id:"a"},rows:[{timestamp:200,particles:{pm_0_5_um:20},environment:{temperature:22,humidity:60},particle_unit_label:"PCS/L"},{timestamp:100,particles:{pm_0_5_um:10},environment:{temperature:21,humidity:50},particle_unit_label:"PCS/L"}]}],key,[],document.getElementById("summary"))');
  assert.equal(canvas._inspectionPoints[0].x,62);assert.equal(canvas._inspectionPoints[1].x,582);
  canvas.events.focus();const tooltip=canvas.parentElement.children[0];assert.equal(tooltip.hidden,false);assert.match(tooltip.textContent,/Device <A>/);assert.match(tooltip.textContent,key==='pm_0_5_um'?/PCS\/L/:key==='temperature'?/°C/:/%RH/);
  canvas.events.keydown({key:'End',preventDefault(){}});assert.match(tooltip.textContent,/1970/);
  e.run('bindChartInspector(canvas,[])');assert.equal(tooltip.hidden,true);
 }
});
test('English canvas tooltips translate the derived PCS/28.3 L unit',()=>{
 const e=environment('app.js');
 e.context.window.HawkI18n={locale:()=> 'en-US',t:(value)=>value==='PCS/28.3 L（约等于 particles/ft³）'?'PCS/28.3 L (approximately particles/ft³)':String(value)};
 const canvas=e.document.getElementById('trendCanvas');canvas.parentElement=e.document.createElement();e.context.canvas=canvas;
 e.run('drawChart(canvas,[{name:"Device",color:"red",room:{id:"a"},rows:[{timestamp:100,particles:{pm_0_5_um:1},particle_unit_label:"PCS/28.3L"}]}],"pm_0_5_um",[],document.getElementById("summary"))');
 canvas.events.focus();
 const tooltip=canvas.parentElement.children[0];
 assert.match(tooltip.textContent,/approximately particles\/ft³/);
 assert.doesNotMatch(tooltip.textContent,/[\u3400-\u9fff]/);
});
test('single canvas point stays left with one real timestamp label',()=>{
 const e=environment('app.js');const canvas=e.document.getElementById('trendCanvas');canvas.parentElement=e.document.createElement();e.context.canvas=canvas;
 e.run('drawChart(canvas,[{name:"A",color:"red",room:{id:"a"},rows:[{timestamp:100,particles:{pm_0_5_um:1}}]}],"pm_0_5_um",[],document.getElementById("summary"))');assert.equal(canvas._inspectionPoints[0].x,62);
 const labels=e.calls.filter(c=>c[0]==='fillText'&&String(c[1]).includes('01/01'));assert.equal(labels.length,1);
});

test('missing particle values never create zero readings',()=>{
 const e=wall([row(now-15,null),row(now-10,'')]);assert.equal(e.elements.get('trendEmpty').hidden,false);assert.equal(e.elements.get('peakValue').textContent,'—');
});
test('device palette distinguishes adjacent devices and survives selection/scope changes',()=>{
 const e=environment('app.js');e.run('rooms=[{id:"r",devices:[{id:"a"},{id:"b"}]}]');
 const colors=e.run('[deviceColor("a"),deviceColor("b")]');assert.equal(colors[0],'#0072b2');assert.equal(colors[1],'#d55e00');
 e.run('realtimeSelectedDeviceIds=new Set(["b"]); rooms.push({id:"new",devices:[{id:"0"}]})');
 assert.equal(e.run('deviceColor("a")'),colors[0]);assert.equal(e.run('deviceColor("b")'),colors[1]);assert.notEqual(e.run('deviceColor("0")'),colors[0]);
});
test('long interruption is explicit and does not inflate normal sample cadence',()=>{
 const e=environment('app.js');const result=e.run('chartContinuity([{timestamp:100},{timestamp:110},{timestamp:3710}])');
 assert.equal(result.breakAfter,90);assert.equal(result.count,1);assert.equal(result.longest,3600);
 const continuous=e.run('chartContinuity([{timestamp:100},{timestamp:220},{timestamp:340}])');assert.equal(continuous.count,0);
 const canvas=e.document.getElementById('trendCanvas');canvas.parentElement=e.document.createElement();const summary=e.document.getElementById('summary');summary.parentElement=e.document.createElement();e.context.canvas=canvas;
 e.run('drawChart(canvas,[{name:"Device <A>",color:"#0072b2",room:{id:"a"},rows:[{timestamp:100,particles:{pm_0_5_um:1}},{timestamp:110,particles:{pm_0_5_um:2}},{timestamp:3710,particles:{pm_0_5_um:3}}]}],"pm_0_5_um",[],document.getElementById("summary"))');
 assert.match(summary._continuityElement.innerHTML,/1 处数据缺口/);assert.match(summary._continuityElement.innerHTML,/60 分钟/);assert.match(summary._continuityElement.innerHTML,/Device &lt;A&gt;/);
 assert.ok(e.calls.some(call=>call[0]==='arc'),'isolated sample remains visible');
 e.run('drawChart(canvas,[],"pm_0_5_um",[],document.getElementById("summary"))');assert.equal(summary._continuityElement.innerHTML,'');
});
