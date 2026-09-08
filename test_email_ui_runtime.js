const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

function fixture(smtp = true) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', textContent: '', checked: false,
      disabled: false, hidden: false, validity: { valid: true } });
    return elements.get(id);
  };
  const context = vm.createContext({ Date, console, window: {}, document: {getElementById: element} });
  vm.runInContext(fs.readFileSync(__dirname + '/public/app.js', 'utf8').replace(/\ninitAuth\(\);\s*$/, ''), context);
  vm.runInContext(`renderEmailSettings({enabled:false,recipients:[],notify_recovery:true,smtp_configured:${smtp}})`, context);
  return {element, context, run: code => vm.runInContext(code, context)};
}

test('invalid address blocks save and test; correcting it recovers save', () => {
  const {element, run} = fixture();
  element('emailEnabledInput').checked = true;
  element('emailRecipientsInput').value = 'broken';
  assert.equal(run('validateEmailForm()'), false);
  assert.equal(element('saveEmailBtn').disabled, true);
  assert.equal(element('testEmailBtn').disabled, true);
  assert.match(element('emailValidation').textContent, /有效邮箱/);
  element('emailRecipientsInput').value = 'qa@example.com';
  assert.equal(run('validateEmailForm()'), true);
  assert.equal(element('saveEmailBtn').disabled, false);
  assert.equal(element('testEmailBtn').disabled, true); // Must save first.
});

test('empty recipients and too many recipients are rejected', () => {
  const {element, run} = fixture();
  element('emailEnabledInput').checked = true;
  assert.equal(run('validateEmailForm()'), false);
  element('emailRecipientsInput').value = Array.from({length:11}, (_, i) => `qa${i}@example.com`).join(',');
  assert.equal(run('validateEmailForm()'), false);
  assert.equal(element('saveEmailBtn').disabled, true);
});

test('missing SMTP permits saving recipients while disabled, but no enable/test', () => {
  const {element, run} = fixture(false);
  element('emailRecipientsInput').value = 'qa@example.com';
  assert.equal(run('validateEmailForm()'), true);
  assert.equal(element('saveEmailBtn').disabled, false);
  assert.equal(element('testEmailBtn').disabled, true);
  element('emailEnabledInput').checked = true;
  assert.equal(run('validateEmailForm()'), false);
  assert.equal(element('saveEmailBtn').disabled, true);
});

test('persisted settings unlock test, edits lock it, busy state locks fields', () => {
  const {element, run} = fixture();
  run('renderEmailSettings({enabled:true,recipients:["qa@example.com"],notify_recovery:true,smtp_configured:true})');
  assert.equal(element('testEmailBtn').disabled, false);
  assert.equal(element('saveEmailBtn').disabled, true);
  element('emailRecoveryInput').checked = false;
  run('validateEmailForm()');
  assert.equal(element('testEmailBtn').disabled, true);
  run('emailBusy=true; validateEmailForm()');
  assert.equal(element('emailRecipientsInput').disabled, true);
  assert.equal(element('saveEmailBtn').disabled, true);
  run('emailBusy=false; validateEmailForm()');
  assert.equal(element('emailRecipientsInput').disabled, false);
  assert.equal(element('saveEmailBtn').disabled, false);
});

test('slow refresh cannot overwrite an email edit made while awaiting response', async () => {
  const {element, context, run} = fixture();
  let resolve;
  context.response = new Promise(done => { resolve = done; });
  run('currentUser={email_alerts_available:true}; api=()=>response; refreshEmailHistory=async()=>{}');
  const loading = run('loadEmailSettings()');
  element('emailRecipientsInput').value = 'new@example.com';
  run('validateEmailForm()');
  resolve({enabled:false,recipients:['old@example.com'],notify_recovery:true,smtp_configured:true});
  await loading;
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
  assert.equal(run('emailDirty'), true);
});

test('load failure preserves the last saved state and unsaved recipient edits', async () => {
  const {element, context, run} = fixture();
  let reject;
  context.response = new Promise((_, done) => { reject = done; });
  run('currentUser={email_alerts_available:true}; api=()=>response; refreshEmailHistory=async()=>{}');
  const loading = run('loadEmailSettings()');
  element('emailRecipientsInput').value = 'new@example.com';
  run('validateEmailForm()');
  reject(new Error('network unavailable'));
  await loading;
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
  assert.equal(element('emailAlertForm').hidden, false);
  assert.equal(run('emailSaved !== null && emailDirty'), true);
});

test('cancel navigation preserves both forms and only prompts once', () => {
  const {element, context, run} = fixture();
  let prompts = 0;
  element('settingsView').classList = { contains: () => true };
  context.window.confirm = () => { prompts++; return false; };
  element('emailRecipientsInput').value = 'new@example.com';
  run('settingsDirty=true; validateEmailForm(); switchView("history")');
  assert.equal(prompts, 1);
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
  assert.equal(run('emailDirty && settingsDirty'), true);
});

test('busy requests and reselecting Settings never reset current inputs', () => {
  const {element, run} = fixture();
  element('settingsView').classList = { contains: () => true };
  element('emailRecipientsInput').value = 'new@example.com';
  run('validateEmailForm(); switchView("settings")');
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
  run('emailBusy=true; showToast=()=>{}; switchView("history")');
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
});

test('out-of-order refresh responses keep the newest saved configuration', async () => {
  const {element, context, run} = fixture();
  let firstResolve, secondResolve;
  context.responses = [new Promise(resolve => { firstResolve = resolve; }), new Promise(resolve => { secondResolve = resolve; })];
  run('currentUser={email_alerts_available:true}; api=()=>responses.shift(); refreshEmailHistory=async()=>{}');
  const first = run('loadEmailSettings()');
  const second = run('loadEmailSettings()');
  secondResolve({enabled:false,recipients:['new@example.com'],notify_recovery:true,smtp_configured:true});
  await second;
  firstResolve({enabled:false,recipients:['old@example.com'],notify_recovery:true,smtp_configured:true});
  await first;
  assert.equal(element('emailRecipientsInput').value, 'new@example.com');
});

test('configured SMTP with no worker displays a distinct operational warning', () => {
  const {element, run} = fixture();
  run('renderEmailSettings({enabled:true,recipients:["qa@example.com"],notify_recovery:true,smtp_configured:true,worker_available:false})');
  assert.match(element('emailServiceState').textContent, /发送服务尚未就绪/);
  run('renderEmailSettings({enabled:true,recipients:["qa@example.com"],notify_recovery:true,smtp_configured:true,worker_available:true})');
  assert.match(element('emailServiceState').textContent, /验证投递/);
});
