/* webui.tools.test_session_ui -- 「对话（会话）管理」与「左栏折叠态」的静态 + 行为自检。
 *
 * 前端没有构建步骤，这两块又都很容易「看起来在、其实没接上」：
 *   - 左栏以前折叠只是把宽度从 216px 改成 56px，nav 按钮的文字是裸文本节点，
 *     CSS 里 `.nav-item span{display:none}` 根本选不中，于是字被 overflow 裁掉，
 *     按钮等于被遮挡 —— 不是折叠，是遮羞布。
 *   - 提示词卡片的页签点击以前只切 hidden、不切 .active，优化流自动切到「中文回译」后
 *     再点「英文提示词」就会出现高亮与内容错位。
 *   - #session-list 一直空着，新建对话只清空输入框，上一份上下文无处可回。
 *
 * 跑法： node webui/tools/test_session_ui.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const js = fs.readFileSync(path.join(root, 'webui/web/assets/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webui/web/index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'webui/web/assets/app.css'), 'utf8');

let bad = 0;
const ok = (m) => console.log('ok  ' + m);
const no = (m) => { bad++; console.log('x ' + m); };

/* ---- 1. 对话管理的接线 -------------------------------------------------- */
if (!/id="session-list"/.test(html)) no('index.html 缺少 #session-list');
else if (!/\$\('session-list'\)/.test(js)) no('app.js 没有渲染 #session-list');
else ok('#session-list 存在且被渲染');

if (!/id="new-session"/.test(html)) no('index.html 缺少 #new-session');
else if (!/\$\('new-session'\)\.onclick\s*=\s*\(\)\s*=>\s*newSession\(\)/.test(js)) {
  no('「新建对话」没有接到 newSession()（还在只清空输入框）');
} else ok('「新建对话」接到 newSession()');

const sessFns = ['newSessionRecord', 'loadSessions', 'applySession', 'captureSession',
  'renderSessionList', 'openSession', 'renameSession', 'deleteSession', 'initSessions', 'persistSessions'];
const missingFn = sessFns.filter(f => !new RegExp('function ' + f + '\\b').test(js));
if (missingFn.length) no('缺少会话管理函数：' + missingFn.join(', '));
else ok('会话管理函数齐备（' + sessFns.length + ' 个）');

if (!/initSessions\(\);/.test(js)) no('boot() 没有调用 initSessions()，刷新后对话不会恢复');
else ok('boot() 里调用了 initSessions()（刷新能恢复上次对话）');

if (!/addEventListener\('input', scheduleSessionSave\)/.test(js)) {
  no('输入框改动没有触发会话自动保存');
} else ok('输入框改动会触发会话自动保存');

if (!/addEventListener\('beforeunload', \(\) => captureSession\(\)\)/.test(js)) {
  no('关页面时没有落盘当前对话');
} else ok('关页面会落盘当前对话');

if (!/localStorage\.getItem\(SESS_LEGACY_KEY/.test(js)) no('没有兼容旧版单份草稿 h3ui.session');
else ok('旧版单份草稿会被收编进对话');

/* ---- 2. 页签切换：高亮与内容必须同时走 -------------------------------- */
const tabClick = js.match(/querySelectorAll\('#prompt-tabs \.tab'\)\.forEach\(t => t\.onclick = \(\) => (\w+)\(t\.dataset\.pane\)\)/);
if (!tabClick) no('页签点击没有统一走 switchPane（可能又只切 hidden 不改 active）');
else if (tabClick[1] !== 'switchPane') no('页签点击走的是 ' + tabClick[1] + '，应为 switchPane');
else ok('页签点击统一走 switchPane(t.dataset.pane)');

if (!/x\.classList\.toggle\('active', on\)/.test(js)) no('switchPane 没有切换 .active 高亮');
else if (!/PROMPT_PANES\.forEach\(p => \{ \$\('pane-' \+ p\)\.hidden/.test(js)) {
  no('switchPane 没有按同一份清单切换 pane 显隐');
} else ok('switchPane 同时切高亮、内容与 aria-selected');

/* 行为：把 switchPane 抽出来，用假 DOM 跑一遍「优化流切到中文后再手点英文」 */
try {
  const i0 = js.indexOf('/* 提示词卡片的页签');
  const i1 = js.indexOf('function setPromptPanes');
  if (i0 < 0 || i1 < 0) throw new Error('找不到 switchPane');

  const mkClassList = () => {
    const set = new Set();
    return {
      toggle: (n, on) => { if (on) set.add(n); else set.delete(n); },
      contains: (n) => set.has(n),
    };
  };
  const tabs = ['en', 'zh', 'notes', 'raw'].map((p) => {
    const t = { dataset: { pane: p }, attrs: {}, classList: mkClassList() };
    t.setAttribute = (k, v) => { t.attrs[k] = v; };
    return t;
  });
  const panes = {};
  ['en', 'zh', 'notes', 'raw'].forEach(p => { panes['pane-' + p] = { hidden: false }; });
  const S = { ui: { promptPane: 'en' } };
  const document = { querySelectorAll: (sel) => (sel === '#prompt-tabs .tab' ? tabs : []) };
  const $ = (id) => panes[id];
  const switchPane = new Function('S', 'document', '$', js.slice(i0, i1) + '; return switchPane;')(S, document, $);

  switchPane('en');
  switchPane('zh');                       // 优化流里的自动切换
  switchPane('en');                       // 用户又点回英文
  const enTab = tabs[0], zhTab = tabs[1];
  const fine = S.ui.promptPane === 'en' && enTab.classList.contains('active') && !zhTab.classList.contains('active') &&
    panes['pane-en'].hidden === false && panes['pane-zh'].hidden === true &&
    enTab.attrs['aria-selected'] === 'true' && zhTab.attrs['aria-selected'] === 'false';
  if (fine) ok('行为：切到中文再点回英文，高亮与内容一致');
  else no('行为：页签高亮/内容错位 S.ui=' + S.ui.promptPane);
} catch (e) { no('switchPane 行为测试失败：' + e.message); }

/* ---- 3. 折叠态：文字收起、图标不被裁 --------------------------------- */
if (!/\.rail\.collapsed \.nav-text/.test(css)) no('CSS 折叠态没有隐藏 .nav-text（文字仍会挤在 56px 里）');
else ok('折叠态会隐藏 .nav-text');

if (!/\.rail\.collapsed \.nav-item\{justify-content:center/.test(css)) no('折叠态导航项没有居中');
else ok('折叠态导航项居中');

if (!/id="rail-toggle"/.test(html) || !/aria-expanded/.test(html)) no('折叠按钮缺少 aria-expanded');
else ok('折叠按钮带 aria-expanded');

if (!/class="nav-item active" data-view="create" title="创作"><i>✦<\/i><span class="nav-text">/.test(html)) {
  no('导航项文字没有包进 <span class="nav-text">，折叠时无法干净隐藏');
} else ok('导航项文字包进了 .nav-text');

if (!/\$\('rail-toggle'\)\.onclick\s*=\s*\(\)\s*=>\s*toggleRail\(\)/.test(js)) no('折叠按钮没接到 toggleRail()');
else if (!/function applyRailState/.test(js)) no('缺少 applyRailState()');
else ok('折叠按钮走 toggleRail() → applyRailState()');

if (!/railCollapsed: !!S\.ui\.railCollapsed/.test(js) || !/getItem\(UI_KEY\)/.test(js)) {
  no('折叠状态没有持久化（刷新又展开）');
} else ok('折叠状态会持久化（刷新保持）');

/* 折叠态会话项仍可点：头像必须留、文字与操作按钮收起 */
if (!/\.rail\.collapsed \.session-item \.s-main/.test(css)) no('折叠态没有收起会话标题文字');
else if (!/\.rail\.collapsed \.session-item \.s-avatar/.test(css)) no('折叠态会话项缺少头像');
else ok('折叠态会话项保留头像、收起文字');

/* ---- 4. 纯函数：自动标题 / 归一化（不用浏览器） ----------------------- */
try {
  const i0 = js.indexOf('function sessUid');
  const i1 = js.indexOf('function loadSessions');   // 覆盖到 normalizeSession
  if (i0 < 0 || i1 < 0) throw new Error('找不到会话纯函数');
  const S = { values: { width: 640, steps: 20 } };
  const fns = new Function('S', js.slice(i0, i1) +
    '; return { autoTitle, sessionTitle, newSessionRecord, normalizeSession, cloneOpt };')(S);

  const t1 = fns.autoTitle('雨夜的便利店门口，一个穿蓝色外套的女孩把伞收起来走进门') === '雨夜的便利店门口，一个穿蓝色外套的女孩把伞收起来走进门'.slice(0, 18) + '…';
  const t2 = fns.autoTitle('第一行\n第二行') === '第一行';
  const t3 = fns.autoTitle('') === '新对话';
  if (t1 && t2 && t3) ok('autoTitle：首行截断 / 取首行 / 空标题回退');
  else no('autoTitle 行为不符：' + JSON.stringify([fns.autoTitle('雨夜的便利店门口，一个穿蓝色外套的女孩把伞收起来走进门'), fns.autoTitle('第一行\n第二行'), fns.autoTitle('')]));

  const rec = fns.newSessionRecord();
  if (rec.prompt === '' && rec.refs.length === 0 && rec.values.width === 640 && rec.optimize.en === '') {
    ok('newSessionRecord：干净的新对话（参数沿用当前值）');
  } else no('newSessionRecord 不符合预期：' + JSON.stringify(rec));

  const named = fns.sessionTitle({ prompt: '写点什么', renamed: true, title: '我的命名' });
  if (named === '我的命名') ok('sessionTitle：重命名优先于自动标题');
  else no('sessionTitle 没尊重重命名：' + named);

  const src = { id: 'x', ts: 1, prompt: 'abc', refs: null, optimize: { en: 'EN', attachments: [{ a: 1 }] } };
  const norm = fns.normalizeSession(src);
  norm.optimize.attachments[0].a = 99;
  const deep = src.optimize.attachments[0].a === 1;
  if (norm.id === 'x' && Array.isArray(norm.refs) && norm.optimize.en === 'EN' && deep && norm.optimize.zh === '') {
    ok('normalizeSession：补默认值、深拷贝 optimize（改副本不动原数据）');
  } else no('normalizeSession 不符合预期');
} catch (e) { no('会话纯函数测试失败：' + e.message); }


/* ---- 5. 行为：存储 / 新建 / 切换 / 重命名 / 删除 / 折叠持久化 ------------ */
try {
  const i0 = js.indexOf('const SESS_KEY');
  const i1 = js.indexOf('// 定时兜底');
  if (i0 < 0 || i1 < 0) throw new Error('找不到会话存储段');

  function FakeEl(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = []; this.className = ''; this.textContent = ''; this.value = '';
    this.hidden = false; this.disabled = false; this.attrs = {}; this.dataset = {}; this.style = {};
    this.classList = { toggle() {}, add() {}, remove() {}, contains() { return false; } };
  }
  FakeEl.prototype.appendChild = function (c) { this.children.push(c); return c; };
  FakeEl.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  FakeEl.prototype.focus = function () {};
  FakeEl.prototype.querySelector = function (sel) {
    const cls = sel.replace(/^[.#]/, '');
    const walk = (n) => {
      for (const c of n.children) {
        if (String(c.className || '').split(/\s+/).indexOf(cls) >= 0) return c;
        const r = walk(c); if (r) return r;
      }
      return null;
    };
    return walk(this);
  };
  FakeEl.prototype.querySelectorAll = function (sel) {
    const cls = sel.replace(/^[.#]/, ''); const out = [];
    const walk = (n) => { for (const c of n.children) { if (String(c.className || '').split(/\s+/).indexOf(cls) >= 0) out.push(c); walk(c); } };
    walk(this); return out;
  };

  const ids = {};
  const $ = (id) => (ids[id] = ids[id] || new FakeEl('div'));
  const store = {};
  const localStorage = {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const doc = {
    querySelector: (sel) => (sel === '.mode-tab.active' ? { dataset: { mode: 'generate' } } : null),
    querySelectorAll: (sel) => (sel === '.mode-tab'
      ? [{ dataset: { mode: 'generate' }, classList: { toggle() {} } }] : []),
  };
  const win = { scrollTo() {}, confirm: () => true };
  const noop = () => {};
  const toEl = (tag, cls, text) => {
    const n = new FakeEl(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const S = { sessions: [], activeSession: null, refs: [], mode: 'auto',
    optimize: { en: '', zh: '', notes: '', thinking: '', mode: null, running: false,
                system: '', user: '', runId: '', attachments: [], source: '', useZh: false },
    values: { width: 640, height: 384, num_frames: 73, steps: 20, seed: 42, preset: 'draft' },
    ui: { railCollapsed: false, monitor: true, promptPane: 'en' } };

  const api = new Function(
    'S', '$', 'el', 'document', 'localStorage', 'toast', 'renderRefs', 'syncInputsFromValues',
    'refreshShapeControls', 'updateSummaries', 'setPromptPanes', 'switchPane', 'updatePromptUse',
    'scheduleEstimate', 'clientLog', 'window', 'prompt', 'clearTimeout', 'setTimeout',
    js.slice(i0, i1) + '; return { initSessions, newSession, openSession, renameSession, deleteSession,' +
    ' captureSession, toggleRail, sessionTitle, loadUiPrefs };'
  )(S, $, toEl, doc, localStorage, noop, noop, noop, noop, noop, noop, noop, noop, noop, noop,
    win, () => '命名为A', noop, noop);

  api.initSessions();
  if (S.sessions.length === 1 && $('session-list').children.length === 1) {
    ok('行为：空存储启动 -> 建一个默认对话并渲染列表');
  } else no('行为：初始对话/列表不对 len=' + S.sessions.length + ' items=' + $('session-list').children.length);

  const s1 = S.activeSession;
  $('prompt-input').value = '甲方案：雨夜便利店';
  S.optimize = { en: 'EN-A', zh: 'ZH-A', notes: '', thinking: '', mode: 't2va', running: false,
                 system: '', user: '', runId: '', attachments: [], source: '', useZh: false };
  api.captureSession();
  const persisted = JSON.parse(store['h3ui.sessions']);
  if (persisted.items.length === 1 && persisted.items[0].prompt === '甲方案：雨夜便利店' &&
      persisted.items[0].optimize.en === 'EN-A') {
    ok('行为：captureSession 把提示词与优化结果落盘');
  } else no('行为：captureSession 落盘内容不对');

  api.newSession();
  const s2 = S.activeSession;
  if (S.sessions.length === 2 && s2 !== s1 && $('prompt-input').value === '') {
    ok('行为：newSession 新建空对话并切过去');
  } else no('行为：newSession 不对 len=' + S.sessions.length + ' prompt=' + JSON.stringify($('prompt-input').value));

  $('prompt-input').value = '乙方案：晴天海滩';
  api.captureSession();

  api.openSession(s1);
  if ($('prompt-input').value === '甲方案：雨夜便利店' && S.optimize.en === 'EN-A') {
    ok('行为：openSession 切回后提示词与优化结果都恢复');
  } else no('行为：openSession 没恢复上下文 prompt=' + JSON.stringify($('prompt-input').value) + ' en=' + S.optimize.en);

  api.openSession(s2);
  if ($('prompt-input').value === '乙方案：晴天海滩') ok('行为：再切到新对话，内容正确');
  else no('行为：切换串联错乱');

  api.renameSession(s1);
  if (api.sessionTitle(S.sessions.find(x => x.id === s1)) === '命名为A') ok('行为：renameSession 生效');
  else no('行为：renameSession 没生效');

  api.deleteSession(s2);
  if (S.sessions.length === 1 && S.activeSession === s1) ok('行为：删除当前对话后自动落到剩下的对话');
  else no('行为：删除后状态不对 len=' + S.sessions.length + ' active==s1? ' + (S.activeSession === s1));

  api.toggleRail();
  const ui = JSON.parse(store['h3ui.ui'] || '{}');
  if (S.ui.railCollapsed === true && ui.railCollapsed === true) ok('行为：折叠状态写入 localStorage');
  else no('行为：折叠状态没持久化 ' + JSON.stringify(ui));
} catch (e) { no('会话行为测试异常：' + e.message); }

console.log(bad ? '\n有 ' + bad + ' 项不合格' : '\n全部通过');
process.exit(bad ? 1 : 0);
