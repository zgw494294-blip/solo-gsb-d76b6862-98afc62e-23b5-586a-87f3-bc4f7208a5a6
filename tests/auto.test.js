"use strict";

/* 自动发出前端回归测试（纯 Node，无第三方依赖）。
 *
 * 通过 vm 加载真实的 app/static/app.js，用迷你 DOM 模拟页面元素与事件，
 * 用可控 fetch 代替网络（记录请求体与请求头），用可控 setInterval 验证
 * 开启自动后的每秒轮询。
 *
 * 覆盖：演出页自动卡片（未开启 / 已开启的开关与文案、剩余秒数取整、等待
 * 就绪、暂停冻结、即将发出）；开关 / 应用间隔的请求体（version、enabled、
 * interval）；非法间隔只提示不发请求；409 刷新为最新状态；开启后每秒轮询
 * 同步服务端自动发出的进度、关闭后停止轮询；只读页与请求在途时禁用；
 * 开场前不轮询、点击无效。
 *
 * 运行：node tests/auto.test.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ------------------------------------------------------------ 迷你 DOM */

class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const want = force === undefined ? !this.set.has(c) : !!force;
    if (want) this.set.add(c);
    else this.set.delete(c);
    return want;
  }
}

const VOID_TAGS = new Set(["input", "br", "img"]);

function parseAttrs(attrText, el) {
  const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let m;
  while ((m = attrRe.exec(attrText))) {
    el.setAttribute(m[1], m[2] ?? m[3] ?? m[4] ?? "");
  }
}

function parseHtml(ownerDoc, html, parent = null) {
  const roots = [];
  const stack = [];
  const tokenRe =
    /<!--[\s\S]*?-->|<([a-zA-Z][-a-zA-Z0-9]*)([^>]*?)(\/?)>|<\/([a-zA-Z][-a-zA-Z0-9]*)\s*>|([^<]+)/g;
  let m;
  while ((m = tokenRe.exec(html))) {
    if (m[1] !== undefined) {
      const tag = m[1].toLowerCase();
      const el = new El(tag);
      el.ownerDocument = ownerDoc;
      const top = stack[stack.length - 1];
      el.parentElement = top || parent;
      if (top) top.children.push(el);
      else roots.push(el);
      parseAttrs(m[2], el);
      if (!(m[3] === "/" || VOID_TAGS.has(tag))) stack.push(el);
    } else if (m[4] !== undefined) {
      const tag = m[4].toLowerCase();
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].tagName === tag.toUpperCase()) {
          stack.length = i;
          break;
        }
      }
    } else if (m[5] !== undefined) {
      const top = stack[stack.length - 1];
      if (top) top._text += m[5];
    }
  }
  return roots;
}

class El {
  constructor(tag = "div", id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.classList = new ClassList();
    this.listeners = {};
    this.children = [];
    this.dataset = {};
    this._attrs = {};
    this._value = "";
    this.disabled = false;
    this._text = "";
    this.parentElement = null;
    this.ownerDocument = null;
    if (id) this._attrs.id = id;
  }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) {
    this.classList = new ClassList();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    this._attrs.class = String(v);
  }
  get textContent() {
    if (this.children.length) {
      return this.children.map((c) => c.textContent).join("") + this._text;
    }
    return this._text;
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v);
  }
  get innerHTML() { return this._innerHTML || ""; }
  set innerHTML(html) {
    this._innerHTML = String(html);
    this.children = parseHtml(this.ownerDocument, String(html), this);
  }
  setAttribute(name, val) {
    this._attrs[name] = String(val);
    if (name === "id") this.id = String(val);
    if (name === "class") {
      this.classList = new ClassList();
      String(val).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
    }
    if (name.startsWith("data-")) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(val);
    }
    if (name === "disabled") this.disabled = true;
    if (name === "value") this._value = String(val);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
  _matchesCompound(part) {
    const tokenRe = /([.#]?[a-zA-Z][-a-zA-Z0-9_]*)|(\[[^\]]+\])/g;
    let t;
    while ((t = tokenRe.exec(part))) {
      if (t[1]) {
        const tok = t[1];
        if (tok.startsWith("#")) { if (this.id !== tok.slice(1)) return false; }
        else if (tok.startsWith(".")) { if (!this.classList.contains(tok.slice(1))) return false; }
        else if (this.tagName !== tok.toUpperCase()) return false;
      } else {
        const am = /^\[([a-zA-Z0-9-]+)(?:([~|^$*]?=)\s*"([^"]*)"|=\s*'([^']*)'|=\s*([^\]]*))?\]$/.exec(t[2]);
        if (!am) return false;
        const av = this.getAttribute(am[1]);
        if (av === null) return false;
        if (am[2]) {
          const want = am[3] ?? am[4] ?? am[5] ?? "";
          if (am[2] === "=" && av !== want) return false;
        }
      }
    }
    return true;
  }
  matches(sel) {
    const parts = sel.split(/\s+/).filter(Boolean);
    if (!parts.length || !this._matchesCompound(parts[parts.length - 1])) return false;
    let el = this.parentElement;
    for (let i = parts.length - 2; i >= 0; i--) {
      let found = null;
      while (el) {
        if (el._matchesCompound(parts[i])) { found = el; break; }
        el = el.parentElement;
      }
      if (!found) return false;
      el = found.parentElement;
    }
    return true;
  }
  _walk(fn) {
    for (const c of this.children) {
      fn(c);
      c._walk(fn);
    }
  }
  querySelectorAll(sel) {
    const parts = sel.split(/\s+/).filter(Boolean);
    const out = [];
    const seen = new Set();
    const dfs = (els, depth) => {
      for (const el of els) {
        if (el._matchesCompound(parts[depth])) {
          if (depth === parts.length - 1) {
            if (!seen.has(el)) { seen.add(el); out.push(el); }
          } else {
            dfs(el.children, depth + 1);
          }
        }
        dfs(el.children, depth);
      }
    };
    dfs(this.children, 0);
    return out;
  }
  querySelector(sel) {
    const found = this.querySelectorAll(sel);
    return found.length ? found[0] : new El();
  }
  closest(sel) {
    let el = this;
    while (el) {
      if (el.matches && el.matches(sel)) return el;
      el = el.parentElement;
    }
    return null;
  }
  focus() {}
  appendChild(child) {
    child.parentElement = this;
    child.ownerDocument = this.ownerDocument;
    this.children.push(child);
    return child;
  }
  remove() {
    if (this.parentElement) {
      const arr = this.parentElement.children;
      const i = arr.indexOf(this);
      if (i !== -1) arr.splice(i, 1);
    }
  }
}

class Doc {
  constructor() {
    this.byId = new Map();
    this.listeners = {};
  }
  el(id) {
    if (!this.byId.has(id)) {
      const el = new El("div", id);
      el.ownerDocument = this;
      if (id === "archives-modal") el.classList.add("hidden");
      this.byId.set(id, el);
    }
    return this.byId.get(id);
  }
  querySelector(sel) {
    if (sel.startsWith("#")) return this.el(sel.slice(1));
    for (const root of this.byId.values()) {
      const f = root.querySelector(sel);
      if (f.tagName) return f;
    }
    return new El();
  }
  querySelectorAll(sel) {
    if (sel.startsWith("#")) {
      const idSel = sel.slice(1).split(/\s+/);
      const root = this.el(idSel[0]);
      return idSel.length > 1 ? root.querySelectorAll(idSel.slice(1).join(" ")) : [root];
    }
    const out = [];
    for (const root of this.byId.values()) {
      for (const f of root.querySelectorAll(sel)) {
        if (!out.includes(f)) out.push(f);
      }
    }
    return out;
  }
  createElement(tag) {
    const el = new El(tag);
    el.ownerDocument = this;
    return el;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn(event);
  }
}

/* -------------------------------------------------------- 加载被测脚本 */

const appCode = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "app.js"),
  "utf8"
);

function loadApp(storageData) {
  const document = new Doc();
  const pending = [];
  const intervals = new Map(); // 可控 setInterval：id -> {fn, ms}
  let nextIntervalId = 1;
  const storage = {
    _m: new Map(Object.entries(storageData || {})),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };
  const sandbox = {
    document,
    fetch: (path, options) =>
      new Promise((resolve, reject) => pending.push({ path, options, resolve, reject })),
    sessionStorage: storage,
    setTimeout,
    clearTimeout,
    setInterval: (fn, ms) => {
      const id = nextIntervalId++;
      intervals.set(id, { fn, ms });
      return id;
    },
    clearInterval: (id) => { intervals.delete(id); },
    confirm: () => true,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(appCode, sandbox, { filename: "app.js" });
  return { document, pending, storage, sandbox, intervals };
}

const flush = () => new Promise((r) => setImmediate(r));
const lastRequest = (ctx) => ctx.pending[ctx.pending.length - 1];

async function respond(req, data, status = 200) {
  req.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
  await flush();
}

/* ------------------------------------------------------------ 测试数据 */

function autoView(over) {
  return Object.assign({
    enabled: false,
    interval: 10,
    remaining_seconds: null,
    waiting_ready: false,
  }, over || {});
}

function liveState(over) {
  return Object.assign({
    version: 3,
    status: "live",
    paused: false,
    control_enabled: false,
    control_granted: true,
    current: { id: 1, number: "A1", content: "灯光渐亮" },
    next: { id: 2, number: "A2", content: "音乐起" },
    pending: [
      { id: 1, number: "A1", content: "灯光渐亮" },
      { id: 2, number: "A2", content: "音乐起" },
    ],
    issued: [],
    issues: [],
    auto: autoView(),
    total: 2,
    remaining: 2,
    min_cues: 2,
    max_cues: 12,
  }, over || {});
}

function setupState(over) {
  return liveState(Object.assign({
    status: "setup",
    current: null,
    next: null,
    pending: [],
    total: 0,
    remaining: 0,
  }, over || {}));
}

/* 初始化页面：回应启动时的 GET /api/state。 */
async function initWith(ctx, s) {
  await respond(ctx.pending[0], s);
}

/* 触发自动轮询一拍并回应。 */
async function firePoll(ctx, s) {
  const timers = [...ctx.intervals.values()];
  if (!timers.length) throw new Error("没有已安装的轮询定时器");
  timers[timers.length - 1].fn();
  await flush();
  const req = lastRequest(ctx);
  if (!req || req.path !== "/api/state") throw new Error("轮询未请求 /api/state");
  await respond(req, s);
}

/* ------------------------------------------------------------ 断言 */

let failures = 0;
function check(cond, msg) {
  if (cond) console.log("  ✓ " + msg);
  else { failures++; console.error("  ✗ " + msg); }
}

/* ------------------------------------------------------------ 用例 */

async function t1_card_off_and_enable() {
  console.log("T1 未开启：卡片显示开关，点击开启提交正确请求体");
  const ctx = loadApp();
  await initWith(ctx, liveState());
  check(ctx.document.el("auto-label").textContent === "自动发出 · 未开启",
    "未开启时标签正确");
  check(ctx.document.el("auto-toggle").textContent === "开启自动", "开关文案为开启自动");
  check(ctx.document.el("auto-interval").value === 10, "间隔输入框取自状态（默认 10）");
  check(ctx.document.el("auto-apply").classList.contains("hidden"), "应用按钮未开启时隐藏");
  check(ctx.document.el("auto-status").textContent.includes("开启后"), "未开启提示文案");
  check(ctx.intervals.size === 0, "未开启时不轮询");
  const before = ctx.pending.length;
  ctx.document.el("auto-toggle").dispatch("click");
  check(ctx.pending.length === before + 1, "点击开启发出请求");
  const req = lastRequest(ctx);
  check(req.path === "/api/auto", "请求路径为 /api/auto");
  const body = JSON.parse(req.options.body);
  check(body.version === 3 && body.enabled === true && body.interval === 10,
    "请求体携带版本号、开关与间隔");
  await respond(req, liveState({
    version: 4,
    auto: autoView({ enabled: true, interval: 10, remaining_seconds: 7.3 }),
  }));
  check(ctx.document.el("auto-label").textContent === "自动发出 · 已开启（每 10 秒）",
    "开启后标签显示间隔");
  check(ctx.document.el("auto-toggle").textContent === "关闭自动", "开关文案变为关闭自动");
  check(!ctx.document.el("auto-apply").classList.contains("hidden"), "应用按钮开启后可见");
  check(ctx.document.el("auto-status").textContent === "约 8 秒后自动发出当前提示",
    "剩余秒数向上取整显示");
}

async function t2_status_texts() {
  console.log("T2 已开启状态文案：等待就绪 / 暂停冻结 / 即将发出");
  const ctx = loadApp();
  await initWith(ctx, liveState({
    auto: autoView({ enabled: true, interval: 5, remaining_seconds: 3.1 }),
  }));
  check(ctx.document.el("auto-status").textContent === "约 4 秒后自动发出当前提示",
    "倒计时文案");
  await firePoll(ctx, liveState({
    version: 4,
    auto: autoView({ enabled: true, interval: 5, remaining_seconds: 0.0, waiting_ready: true }),
  }));
  check(ctx.document.el("auto-status").textContent.includes("待灯光、音响"),
    "到期未就绪显示等待就绪");
  await firePoll(ctx, liveState({
    version: 5,
    paused: true,
    auto: autoView({ enabled: true, interval: 5, remaining_seconds: 2.0 }),
  }));
  check(ctx.document.el("auto-status").textContent.includes("已暂停") &&
    ctx.document.el("auto-status").textContent.includes("2"),
    "暂停冻结显示剩余秒数");
  await firePoll(ctx, liveState({
    version: 6,
    auto: autoView({ enabled: true, interval: 5, remaining_seconds: 0.0 }),
  }));
  check(ctx.document.el("auto-status").textContent.includes("即将自动发出"),
    "到期且门槛满足显示即将发出");
}

async function t3_invalid_interval() {
  console.log("T3 非法间隔：只提示不发请求");
  const ctx = loadApp();
  await initWith(ctx, liveState());
  for (const bad of ["0", "61", "2.5", "", "-3"]) {
    ctx.document.el("auto-interval").value = bad;
    const before = ctx.pending.length;
    ctx.document.el("auto-toggle").dispatch("click");
    check(ctx.pending.length === before, `间隔 ${bad || "空"} 不发请求`);
    check(ctx.document.el("toast").textContent.includes("1～60"),
      `间隔 ${bad || "空"} 提示非法`);
  }
}

async function t4_disable_and_apply() {
  console.log("T4 关闭自动与应用新间隔的请求体");
  const ctx = loadApp();
  await initWith(ctx, liveState({
    auto: autoView({ enabled: true, interval: 10, remaining_seconds: 6.5 }),
  }));
  ctx.document.el("auto-toggle").dispatch("click");
  let req = lastRequest(ctx);
  let body = JSON.parse(req.options.body);
  check(req.path === "/api/auto" && body.enabled === false && body.interval === 10,
    "关闭提交 enabled:false");
  await respond(req, liveState({ version: 4, auto: autoView({ enabled: false, interval: 10 }) }));
  check(ctx.document.el("auto-label").textContent === "自动发出 · 未开启",
    "关闭后标签恢复未开启");
  // 改间隔后点应用
  ctx.document.el("auto-interval").value = "15";
  ctx.document.el("auto-apply").dispatch("click");
  req = lastRequest(ctx);
  body = JSON.parse(req.options.body);
  check(req.path === "/api/auto" && body.enabled === true && body.interval === 15,
    "应用间隔提交 enabled:true 与新间隔");
  await respond(req, liveState({
    version: 5,
    auto: autoView({ enabled: true, interval: 15, remaining_seconds: 15 }),
  }));
  check(ctx.document.el("auto-interval").value === 15, "应用成功后输入框同步为新间隔");
}

async function t5_conflict_refresh() {
  console.log("T5 409：页面刷新为附带最新状态");
  const ctx = loadApp();
  await initWith(ctx, liveState());
  ctx.document.el("auto-toggle").dispatch("click");
  const req = lastRequest(ctx);
  await respond(req, {
    error: "conflict",
    message: "操作已过期",
    state: liveState({
      version: 9,
      auto: autoView({ enabled: true, interval: 5, remaining_seconds: 4.2 }),
    }),
  }, 409);
  check(ctx.document.el("auto-label").textContent === "自动发出 · 已开启（每 5 秒）",
    "409 后按最新状态刷新自动卡片");
  check(ctx.document.el("toast").textContent.includes("已有人先操作"), "409 提示");
}

async function t6_polling_sync() {
  console.log("T6 开启后每秒轮询同步进度，关闭后停止轮询");
  const ctx = loadApp();
  await initWith(ctx, liveState({
    auto: autoView({ enabled: true, interval: 10, remaining_seconds: 9.5 }),
  }));
  check(ctx.intervals.size === 1, "开启后安装每秒轮询");
  const timer = [...ctx.intervals.values()][0];
  check(timer.ms === 1000, "轮询周期为 1 秒");
  // 服务端自动发出了一条：轮询把新进度同步到页面
  await firePoll(ctx, liveState({
    version: 4,
    current: { id: 2, number: "A2", content: "音乐起" },
    next: null,
    pending: [{ id: 2, number: "A2", content: "音乐起" }],
    issued: [{ id: 1, number: "A1", content: "灯光渐亮", issued_at: "2026-10-05T10:00:00+00:00" }],
    auto: autoView({ enabled: true, interval: 10, remaining_seconds: 9.8 }),
  }));
  check(ctx.document.el("current-card").innerHTML.includes("A2"),
    "轮询同步服务端自动发出后的当前提示");
  check(ctx.document.el("version-badge").textContent === "v4", "轮询同步版本号");
  // 服务端关闭自动（如被接管/归档）：停止轮询
  await firePoll(ctx, liveState({ version: 5, auto: autoView() }));
  check(ctx.intervals.size === 0, "关闭后停止轮询");
}

async function t7_readonly_and_busy() {
  console.log("T7 只读页与请求在途时禁用，且点击不发请求");
  const ctx = loadApp();
  await initWith(ctx, liveState({
    control_enabled: true,
    control_granted: false,
  }));
  check(ctx.document.el("auto-toggle").disabled === true, "只读页开关禁用");
  check(ctx.document.el("auto-apply").disabled === true, "只读页应用禁用");
  check(ctx.document.el("auto-interval").disabled === true, "只读页间隔输入禁用");
  let before = ctx.pending.length;
  ctx.document.el("auto-toggle").dispatch("click");
  check(ctx.pending.length === before, "只读页点击不发请求");

  const ctx2 = loadApp();
  await initWith(ctx2, liveState());
  ctx2.document.el("auto-toggle").dispatch("click"); // 开启请求在途
  check(ctx2.document.el("auto-toggle").disabled === true, "请求在途时开关禁用");
  before = ctx2.pending.length;
  ctx2.document.el("auto-toggle").dispatch("click");
  check(ctx2.pending.length === before, "在途时重复点击不发第二个请求");
  await respond(lastRequest(ctx2), liveState({
    version: 4,
    auto: autoView({ enabled: true, interval: 10, remaining_seconds: 10 }),
  }));
  check(ctx2.document.el("auto-toggle").disabled === false, "请求结束后恢复可点");
}

async function t8_setup_no_auto() {
  console.log("T8 开场前：不轮询、点击无效（卡片随演出页隐藏）");
  const ctx = loadApp();
  await initWith(ctx, setupState());
  check(ctx.intervals.size === 0, "开场前不轮询");
  const before = ctx.pending.length;
  ctx.document.el("auto-toggle").dispatch("click");
  check(ctx.pending.length === before, "开场前点击不发请求");
}

(async function run() {
  await t1_card_off_and_enable();
  await t2_status_texts();
  await t3_invalid_interval();
  await t4_disable_and_apply();
  await t5_conflict_refresh();
  await t6_polling_sync();
  await t7_readonly_and_busy();
  await t8_setup_no_auto();
  console.log(failures === 0 ? "\n全部通过（8 个用例）" : `\n失败 ${failures} 处断言`);
  process.exit(failures === 0 ? 0 : 1);
})();
