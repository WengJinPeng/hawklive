"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "public/i18n.js"), "utf8");

// These focused DOM doubles exercise the production runtime, not a copied
// translation algorithm. Real MutationObserver scheduling is covered by UI QA.
function runtime({ messages = [], attributes = {}, ignored = false } = {}) {
  const values = new Map(Object.entries(attributes));
  const element = {
    nodeType: 1, tagName: "DIV",
    closest: () => ignored ? element : null,
    hasAttribute: (name) => values.has(name),
    getAttribute: (name) => values.get(name),
    setAttribute: (name, value) => values.set(name, value),
    querySelectorAll: () => [],
  };
  const nodes = messages.map((message) => ({ nodeType: 3, nodeValue: message, parentElement: element }));
  const document = {
    nodeType: 9, documentElement: { lang: "" },
    querySelectorAll: (selector) => selector === "[data-locale]" ? [] : [element],
    createTreeWalker: () => {
      const remaining = [...nodes];
      return { nextNode: () => remaining.shift() };
    },
    addEventListener: () => {},
  };
  const storage = new Map([["hawkhive-locale", "en-US"]]);
  const listeners = new Map();
  let callback;
  const context = {
    document,
    navigator: { language: "en-US" },
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    Node: { TEXT_NODE: 3, DOCUMENT_NODE: 9, ELEMENT_NODE: 1 },
    NodeFilter: { SHOW_TEXT: 4 },
    MutationObserver: class {
      constructor(handler) { callback = handler; }
      observe() {}
      disconnect() {}
    },
    CustomEvent: class { constructor(type, detail) { this.type = type; this.detail = detail; } },
    confirm: (message) => message,
    alert: (message) => message,
    prompt: (message) => message,
    addEventListener: (type, handler) => listeners.set(type, handler),
    dispatchEvent: () => {},
  };
  context.window = context;
  vm.runInNewContext(source, context, { filename: "public/i18n.js" });
  return { i18n: context.HawkI18n, nodes, element, document, storage, listeners, mutate: (records) => callback(records) };
}

test("cloud scope, empty states, counts, and metric labels translate", () => {
  const { i18n } = runtime();
  const cases = {
    "可在这里管理本客户的全部采集器节点。": "Manage all collector nodes for this customer here.",
    "这个车间还没有设备": "No devices in this workshop yet",
    "还没有车间": "No workshops yet",
    "1,234 条": "1,234 records",
    "1 条": "1 record",
    "0.3 µm 报警上限": "0.3 µm alarm limit",
    "灌装 A：0.5 μm 粒子数已恢复正常": "灌装 A: 0.5 μm particle count returned to normal",
    "当前节点离线；配置会保存，恢复连接后自动下发。": "This node is offline; configuration is saved and delivered after reconnection.",
  };
  for (const [input, expected] of Object.entries(cases)) assert.equal(i18n.t(input), expected, input);
});

test("nested error messages translate the cause as well as the prefix", () => {
  const { i18n } = runtime();
  for (const prefix of ["刷新失败", "保存失败", "云端连接失败", "历史数据查询失败", "报警记录刷新失败", "数据概况刷新失败", "采集器信息加载失败", "工作台启动失败", "采集器启动失败", "连接失败", "最近错误"]) {
    const translated = i18n.t(`${prefix}：网络连接失败，请稍后重试。`);
    assert.doesNotMatch(translated, /[\u3400-\u9fff]/, translated);
    assert.match(translated, /Network connection failed\. Please try again later\./);
  }
  assert.equal(i18n.t("保存失败：Custom remote error 42"), "Save failed: Custom remote error 42");
  assert.equal(i18n.t("“设备甲”测试失败：设备拒绝了连接"), "“设备甲” test failed: The device refused the connection");
});

test("API-provided text can arrive after initial rendering and switch both ways", () => {
  const app = runtime({ messages: ["正在确认管理范围。"] });
  app.nodes[0].nodeValue = "可在这里管理本客户的全部采集器节点。";
  app.mutate([{ type: "characterData", target: app.nodes[0] }]);
  assert.equal(app.nodes[0].nodeValue, "Manage all collector nodes for this customer here.");
  app.i18n.setLocale("zh-CN");
  assert.equal(app.nodes[0].nodeValue, "可在这里管理本客户的全部采集器节点。");
  app.i18n.setLocale("en-US");
  assert.equal(app.nodes[0].nodeValue, "Manage all collector nodes for this customer here.");
});

test("repeated mutation records and moved nodes do not overwrite the source language", () => {
  const app = runtime({ messages: ["等待数据"] });
  app.mutate([{ type: "childList", addedNodes: [app.nodes[0], app.nodes[0]] }]);
  app.i18n.setLocale("zh-CN");
  assert.equal(app.nodes[0].nodeValue, "等待数据");
});

test("updating one attribute preserves the other attributes' source language", () => {
  const app = runtime({ attributes: { title: "设备名称", placeholder: "请输入上限" } });
  app.element.setAttribute("title", "现场采集器");
  app.mutate([{ type: "attributes", target: app.element, attributeName: "title" }]);
  assert.equal(app.element.getAttribute("title"), "On-site collector");
  app.i18n.setLocale("zh-CN");
  assert.equal(app.element.getAttribute("placeholder"), "请输入上限");
  assert.equal(app.element.getAttribute("title"), "现场采集器");
});

test("a language switch preserves a fresh value before the mutation callback runs", () => {
  const app = runtime({ messages: ["正在连接"] });
  app.nodes[0].nodeValue = "尚无数据";
  app.i18n.setLocale("zh-CN");
  assert.equal(app.nodes[0].nodeValue, "尚无数据");
  app.i18n.setLocale("en-US");
  assert.equal(app.nodes[0].nodeValue, "No data yet");
});

test("business names and editable values are not translated as interface labels", () => {
  const app = runtime({ messages: ["温度", "上海车间 A"], attributes: { title: "设备" }, ignored: true });
  assert.deepEqual(app.nodes.map((node) => node.nodeValue), ["温度", "上海车间 A"]);
  assert.equal(app.element.getAttribute("title"), "设备");
  app.i18n.setLocale("zh-CN");
  assert.equal(app.document.documentElement.lang, "zh-CN");
  assert.equal(app.storage.get("hawkhive-locale"), "zh-CN");
});
