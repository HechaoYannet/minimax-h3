/* MiniMax-H3 创作台 —— 前端逻辑（原生 JS，无框架、无构建步骤）
 *
 * 与后端的分工：
 *   前端只负责「收集意图 / 展示状态 / 发请求」，所有校验与执行都在 WSL 后端。
 *   通信协议：REST + SSE，全部走 /api/*，见 webui/README.md。
 *
 * 这个文件刻意「一块写完」：没有模块打包、没有构建产物，浏览器打开就是最终形态，
 * 改完刷新即可，出问题也能直接读源码排查。
 */
'use strict';

/* ------------------------------------------------------------------ 全局状态 */
const S = {
  cfg: null, spec: null, presets: {},
  values: {},                 // 表单值 id -> value，取值口径来自 config/params.spec.json
  refs: [],                   // 参考素材
  mode: 'auto',
  optimize: { en: '', zh: '', notes: '', thinking: '', mode: null, running: false, system: '', user: '', runId: '' },
  estimate: null, risks: [], jobs: [], activeJob: null, telemetry: null,
  llm: { runs: [], stats: null },
  ui: { monitor: true, railCollapsed: false, view: 'create', promptPane: 'en',
        logLevel: 'info', logSource: 'all', logQ: '', logFollow: true },
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

function toast(msg, kind, ms) {
  kind = kind || 'info'; ms = ms || 5200;
  const t = el('div', 'toast ' + kind, msg);
  $('toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 320); }, ms);
}
function fmtDur(s) {
  if (s === null || s === undefined || isNaN(s)) return '--';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
  return Math.floor(s / 3600) + 'h' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
}
function fmtClock(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}
function fmtBytes(b) {
  if (!b && b !== 0) return '--';
  const u = ['B', 'KiB', 'MiB', 'GiB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v < 10 ? 2 : 1) + ' ' + u[i];
}
function esc(s) {
  return String(s === undefined || s === null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function num(v, d) { const n = Number(v); return isNaN(n) ? d : n; }
function inlineMd(s) {
  return esc(s).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    const msg = (data && (data.error || data.message)) || ('HTTP ' + r.status);
    const err = new Error(msg);
    err.data = data; err.status = r.status;
    throw err;
  }
  return data;
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  try {
    const cfg = await api('/api/config');
    S.cfg = cfg;
    S.spec = cfg.spec || {};
    S.presets = S.spec.presets || {};
    buildSidebarInfo(cfg);
    buildParamFields();
    initValues();
    buildPresets();
    buildRefButtons();
    renderPanelDocs();
    bindEvents();
    applyLoggingConfig(cfg);
    startTelemetry();
    startLogStream();
    await refreshJobs();
    loadGuideDoc();
    loadAboutDoc();
    $('app').hidden = false;
    $('boot').hidden = true;
    if (!((cfg.deepseek || {}).key_present)) {
      toast('未配置 DeepSeek API Key：提示词优化不可用（生成不受影响）。' +
        '在 config/deepseek.yaml 填 api.key，或设置环境变量 DEEPSEEK_API_KEY 后重启服务。', 'warn', 10000);
    }
    if (cfg.spec && cfg.spec.error) toast('参数规格异常：' + cfg.spec.error, 'err', 12000);
  } catch (e) {
    $('boot-msg').textContent = '连接后端失败：' + e.message;
    const d = $('boot-detail');
    d.hidden = false;
    d.textContent =
      '请确认 WSL 后端正在运行：\n' +
      '  pwsh webui/start.ps1          （Windows 侧一键启动，推荐）\n' +
      '或手动：\n' +
      '  wsl -d Ubuntu -- bash -lc "cd /mnt/d/otherProject/minimax-h3 && ' +
      'python3 webui/backend/server.py"\n\n' + (e.stack || '');
  }
}

function buildSidebarInfo(cfg) {
  const wsl = cfg.wsl || {};
  $('foot-conn').textContent = 'WSL: ' + (wsl.distro || 'Ubuntu');
  $('foot-repo').textContent = wsl.repo_root || '';
  $('pill-model').textContent = '优化模型 ' + ((cfg.deepseek || {}).model || '--');
  $('library-root').textContent = '输出目录：' + (((cfg.server || {}).paths || {}).outputs_dir || '--');
}

/* ------------------------------------------------------------------ 预设与形状 */
const FPS = 24;
const FMIN = () => (((S.spec || {}).shape || {}).frames_min_measured) || 22;
const FMAX = () => (((S.spec || {}).shape || {}).frames_max_measured) || 243;
const validFrames = () => {
  const a = [];
  for (let f = FMIN(); f <= 400; f += 17) a.push(f);
  return a;
};

function buildPresets() {
  const sel = $('sel-preset'), chips = $('preset-chips');
  sel.innerHTML = ''; chips.innerHTML = '';
  const names = Object.keys(S.presets);
  names.forEach((name) => {
    const p = S.presets[name];
    sel.appendChild(new Option(name + (p.seconds ? ' · ' + p.seconds + 's' : ''), name));
    const c = el('button', 'chip');
    c.dataset.preset = name;
    c.innerHTML = '<b>' + esc(name) + '</b><span class="muted">' + (p.width || '?') + '×' +
      (p.height || '?') + ' · ' + (p.num_frames || '?') + '帧 · ' + (p.steps || '?') + '步</span>' +
      (p.denoise_min ? '<span class="tag">' + p.denoise_min + 'min</span>' : '');
    c.title = (p.note || '') + '\n实测/校准 seq=' + (p.seq_len || '?') + '，' + (p.step_s || '?') + ' s/步';
    c.onclick = () => applyPreset(name);
    chips.appendChild(c);
  });
  const def = ((S.cfg.server || {}).defaults || {}).preset || S.spec.default_preset || names[0];
  applyPreset(def, true);
  $('sel-preset').value = def;
}

function applyPreset(name, silent) {
  const p = S.presets[name];
  if (!p) return;
  S.values.preset = name;
  S.values.width = p.width; S.values.height = p.height;
  S.values.num_frames = p.num_frames; S.values.steps = p.steps;
  if (p.ref_image_short_edge) S.values.ref_image_short_edge = p.ref_image_short_edge;
  if (p.ref_video_short_edge) S.values.ref_video_short_edge = p.ref_video_short_edge;
  if (p.ref_video_max_pixels) S.values.ref_video_max_pixels = p.ref_video_max_pixels;
  document.querySelectorAll('#preset-chips .chip').forEach(c =>
    c.classList.toggle('active', c.dataset.preset === name));
  $('sel-preset').value = name;
  syncInputsFromValues();
  refreshShapeControls();
  scheduleEstimate();
  updateSummaries();
  if (!silent) toast('已切到预设 ' + name + '：' + p.width + '×' + p.height + ' · ' +
    p.num_frames + '帧 · ' + p.steps + '步', 'ok', 3200);
}

function buildShapeSelectors() {
  const res = $('sel-res');
  res.innerHTML = '';
  const seen = {};
  Object.keys(S.presets).forEach((n) => {
    const p = S.presets[n];
    const key = p.width + 'x' + p.height;
    if (seen[key]) return;
    seen[key] = 1;
    res.appendChild(new Option(key + '（' + n + '）', key));
  });
  res.appendChild(new Option('自定义…', 'custom'));
}

function refreshShapeControls() {
  const res = $('sel-res'), dur = $('sel-dur');
  if (!res.options.length) buildShapeSelectors();
  const key = num(S.values.width, 640) + 'x' + num(S.values.height, 384);
  res.value = Array.from(res.options).some(o => o.value === key) ? key : 'custom';
  dur.innerHTML = '';
  const cur = num(S.values.num_frames, 73);
  validFrames().forEach(f => dur.appendChild(new Option(
    f + ' 帧 · ' + (f / FPS).toFixed(2) + 's' + (f > FMAX() ? '（超实测上限，可能 OOM）' : ''),
    String(f))));
  if (!Array.from(dur.options).some(o => o.value === String(cur)))
    dur.appendChild(new Option(cur + ' 帧（不合法，会被吸附）', String(cur)));
  dur.value = String(cur);
}

/* ------------------------------------------------------------------ 参数表单 */
const SKIP_IN_GRID = { preset: 1, width: 1, height: 1, num_frames: 1, seconds: 1, seed: 1 };

/* 参数文档渲染：数值常量、范围、提示一览、风险与取值语义表。
   说明文本全部来自 config/params.spec.json（源头是 webui/tools/param_docs.py），
   前端只负责排版，不在这里重复写任何文案。 */
const UNIT_ZH = {
  px: '像素', '帧': '帧', '秒': '秒', '步': '步', GiB: 'GiB', MiB: 'MiB', seed: 'seed',
  a: 'alpha 倍率', alpha: 'alpha', None: '',
};
function fmtUnit(u) { return u ? (UNIT_ZH[u] !== undefined ? UNIT_ZH[u] : u) : ''; }

function constantLine(p) {
  const bits = [];
  const d = p.default;
  if (d && typeof d === 'object' && d.__preset__) {
    bits.push('默认：跟随预设（' + d.__preset__ + '）');
  } else if (d !== undefined && d !== null && d !== '') {
    bits.push('默认：' + (p.type === 'bool' ? (d ? '开' : '关') : d) + (p.unit ? ' ' + p.unit : ''));
  }
  if (p.min !== undefined && p.min !== null) bits.push('范围：' + p.min + ' ~ ' + p.max + (p.step ? '（步长 ' + p.step + '）' : ''));
  if ((p.choices || []).length) bits.push('可选：' + p.choices.join(' / '));
  if ((p.cli || []).length) bits.push('命令行：' + p.cli.join(' '));
  return bits.join('　·　');
}

/* 把 **加粗** 与 \u0060行内代码\u0060 渲染成 HTML（其余一律转义，避免把文档当 HTML 注入） */
function richText(s) {
  let h = esc(s);
  h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  const re = new RegExp("`" + '([^' + "`" + ']+)' + "`", 'g');
  return h.replace(re, '<code>$1</code>');
}

function paramDocPanel(p) {
  const box = el('div', 'pdoc');
  const id = 'pdoc-' + p.id;

  const toggle = el('button', 'doc-toggle');
  toggle.type = 'button';
  toggle.innerHTML = '<span class="caret">▸</span> 详细说明';
  toggle.onclick = () => {
    const wrap = document.getElementById(id);
    const open = wrap.style.display !== 'block';
    wrap.style.display = open ? 'block' : 'none';
    toggle.querySelector('.caret').textContent = open ? '▾' : '▸';
  };
  box.appendChild(toggle);

  const wrap = el('div', 'doc-body');
  wrap.id = id;
  wrap.style.display = 'none';

  const constLine = constantLine(p);
  if (constLine) wrap.appendChild(el('div', 'doc-const', constLine));
  if (p.detail) {
    const d = el('div', 'doc-detail');
    d.innerHTML = richText(p.detail);
    wrap.appendChild(d);
  }
  if ((p.tips || []).length) {
    wrap.appendChild(el('div', 'doc-sub', '经验与推荐'));
    const ul = el('ul', 'doc-ul');
    p.tips.forEach(t => { const li = el('li'); li.innerHTML = richText(t); ul.appendChild(li); });
    wrap.appendChild(ul);
  }
  if ((p.risks || []).length) {
    wrap.appendChild(el('div', 'doc-sub warn', '风险'));
    const ul = el('ul', 'doc-ul risks');
    p.risks.forEach(t => { const li = el('li'); li.innerHTML = richText(t); ul.appendChild(li); });
    wrap.appendChild(ul);
  }
  if ((p.ranges || []).length) {
    wrap.appendChild(el('div', 'doc-sub', '取值语义'));
    const tbl = el('table', 'doc-tbl');
    const tb = document.createElement('tbody');
    p.ranges.forEach(r => {
      const tr = document.createElement('tr');
      tr.appendChild(el('td', 'rng', String(r[0])));
      const td = el('td'); td.innerHTML = richText(String(r[1]));
      tr.appendChild(td);
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    wrap.appendChild(tbl);
  }
  box.appendChild(wrap);
  return box;
}

/* 分组卡片的说明：抬头一句话 + 「本组要点」弹窗 */
function renderPanelDocs() {
  const map = { params: 'card-params', perf: 'card-perf', lora: 'card-lora', cache: 'card-lora', vae: 'card-perf' };
  const docs = (S.spec || {}).panel_docs || {};
  Object.keys(docs).forEach((key) => {
    const card = $(map[key] || '');
    if (!card) return;
    const head = card.querySelector('.card-head');
    if (!head || head.querySelector('.panel-doc-btn')) return;
    const d = docs[key];
    // 抬头补一句「这一组是干什么的」
    const titleEl = head.querySelector('.card-title');
    if (titleEl && d.brief && !titleEl.querySelector('.panel-brief')) {
      const b = el('span', 'panel-brief', '· ' + d.brief);
      titleEl.appendChild(b);
    }
    if (!(d.tips || []).length) return;
    const btn = el('button', 'ghost-btn sm panel-doc-btn', '本组要点');
    btn.onclick = (e) => {
      e.stopPropagation();
      const body = el('div');
      const ul = el('ul', 'doc-ul');
      d.tips.forEach(t => { const li = el('li'); li.innerHTML = richText(t); ul.appendChild(li); });
      body.appendChild(ul);
      const link = el('button', 'ghost-btn sm', '打开完整参数手册');
      link.onclick = () => { $('modal').hidden = true; selectView('about'); };
      body.appendChild(link);
      confirmModal(d.title || key, body, '知道了', null);
    };
    const actions = head.querySelector('.card-actions');
    (actions || head).appendChild(btn);
  });
}

/* 全局：展开/收起所有参数说明 */
function setAllDocs(open) {
  document.querySelectorAll('#card-params .doc-body, #card-perf .doc-body, #card-lora .doc-body')
    .forEach((w) => { w.style.display = open ? 'block' : 'none'; });
  document.querySelectorAll('.doc-toggle').forEach((b) => {
    const caret = b.querySelector('.caret');
    if (caret) caret.textContent = open ? '▾' : '▸';
  });
}


function buildParamFields() {
  const grid = $('params-grid'), perf = $('perf-grid'), lora = $('lora-grid');
  grid.innerHTML = ''; perf.innerHTML = ''; lora.innerHTML = '';
  (S.spec.params || []).forEach((p) => {
    const host = (p.group === 'memory' || p.group === 'vae') ? perf
      : (p.group === 'lora' || p.group === 'scheduler' || p.group === 'cache') ? lora : grid;
    if (SKIP_IN_GRID[p.id]) return;
    host.appendChild(fieldFor(p));
  });
}

function fieldFor(p) {
  const f = el('div', 'field');
  f.dataset.pid = p.id;
  const lab = el('label');
  lab.innerHTML = '<span>' + esc(p.label || p.id) + '</span>' +
    (p.unit ? '<span class="unit">' + esc(p.unit) + '</span>' : '');
  f.appendChild(lab);
  const ctl = el('div', 'ctl');
  let input;
  const id = 'ctl-' + p.id;
  if (p.id === 'lora') {
    input = document.createElement('select'); input.id = id;
    input.appendChild(new Option('（不挂 LoRA）', ''));
    loadLoraOptions(input);
  } else if (p.choices || p.id === 'dit_onload' || p.id === 'sdpa_backend' || p.id === 'scheduler') {
    input = document.createElement('select'); input.id = id;
    const ch = p.choices || (p.id === 'dit_onload' ? ['cpu', 'disk']
      : p.id === 'sdpa_backend' ? ['cudnn', 'flash', 'efficient', 'auto'] : ['auto', 'flow', 'beta']);
    ch.forEach(c => input.appendChild(new Option(c, c)));
  } else if (p.type === 'bool') {
    input = document.createElement('input'); input.type = 'checkbox'; input.id = id;
  } else if (p.type === 'int' || p.type === 'float') {
    input = document.createElement('input'); input.type = 'number'; input.id = id;
    if (p.min !== undefined) input.min = p.min;
    if (p.max !== undefined) input.max = p.max;
    if (p.step !== undefined) input.step = p.step;
  } else {
    input = document.createElement('input'); input.type = 'text'; input.id = id;
  }
  input.addEventListener('change', () => {
    S.values[p.id] = readCtl(p.id);
    if (p.id === 'width' || p.id === 'height' || p.id === 'num_frames') refreshShapeControls();
    scheduleEstimate(); updateSummaries();
  });
  ctl.appendChild(input);
  f.appendChild(ctl);
  if (p.help) {
    const h = el('div', 'hint');
    h.innerHTML = richText(p.help);
    f.appendChild(h);
  }
  f.appendChild(paramDocPanel(p));
  return f;
}

async function loadLoraOptions(sel) {
  try {
    const r = await api('/api/lora');
    (r.files || []).forEach(f => sel.appendChild(new Option(f.name + '（' + f.size_gib + ' GiB）', f.path)));
    const def = ((S.cfg.server || {}).defaults || {}).lora;
    if (def) {
      const base = def.split('/').pop();
      const hit = Array.from(sel.options).find(o => o.value === def || o.textContent.indexOf(base) === 0);
      if (hit) sel.value = hit.value;
    }
  } catch (e) { /* 没 LoRA 也能用 */ }
}

function readCtl(id) {
  const n = $('ctl-' + id);
  if (!n) return S.values[id];
  if (n.type === 'checkbox') return n.checked;
  if (n.type === 'number') return n.value === '' ? null : Number(n.value);
  return n.value;
}
function writeCtl(id, v) {
  const n = $('ctl-' + id);
  if (!n) return;
  if (n.type === 'checkbox') { n.checked = !!v; return; }
  if (n.tagName === 'SELECT') {
    const want = (v === null || v === undefined) ? '' : String(v);
    // 选项是异步加载的（比如 LoRA）：值还不在列表里时补一个临时项，
    // 否则 select.value = x 会被浏览器静默忽略，用户以为挂上了其实没挂。
    if (want && !Array.from(n.options).some(o => o.value === want)) {
      n.appendChild(new Option(want.split('/').pop() + '（来自配置）', want));
    }
    n.value = want;
    return;
  }
  n.value = (v === null || v === undefined) ? '' : v;
}

function initValues() {
  const d = (S.cfg.server || {}).defaults || {};
  const defPreset = d.preset || S.spec.default_preset || Object.keys(S.presets)[0];
  S.values.preset = defPreset;
  (S.spec.params || []).forEach((p) => {
    let v = p.default;
    if (v && typeof v === 'object' && v.__preset__) {
      const pv = S.presets[defPreset] || {};
      v = (pv[v.__preset__] !== undefined && pv[v.__preset__] !== null) ? pv[v.__preset__]
        : (v.fallback !== undefined ? v.fallback : null);
    }
    S.values[p.id] = v;
  });
  const pv = S.presets[defPreset] || {};
  S.values.width = pv.width || 640;
  S.values.height = pv.height || 384;
  S.values.num_frames = pv.num_frames || 73;
  S.values.steps = pv.steps || 20;
  S.values.seed = d.seed === undefined ? 42 : d.seed;
  S.values.scheduler = d.scheduler || 'auto';
  S.values.dit_onload = d.dit_onload || 'cpu';
  S.values.sdpa_backend = d.sdpa_backend || 'cudnn';
  S.values.lora_alpha = 1.0;
  buildShapeSelectors();
  syncInputsFromValues();
  refreshShapeControls();
  updateSummaries();
  scheduleEstimate();
}

function syncInputsFromValues() {
  Object.keys(S.values).forEach(id => writeCtl(id, S.values[id]));
  $('inp-seed').value = S.values.seed;
  $('sel-preset').value = S.values.preset || '';
}

function updateSummaries() {
  const v = S.values;
  $('params-summary').textContent = v.width + '×' + v.height + ' · ' + v.num_frames + '帧 · ' +
    v.steps + '步 · seed ' + v.seed;
  $('perf-summary').textContent = 'vram_limit ' + (v.vram_limit === null || v.vram_limit === undefined
    ? '默认 4.58' : v.vram_limit) + ' GiB · DiT ' + (v.dit_onload || 'cpu') +
    ' · SDPA ' + (v.sdpa_backend || 'cudnn');
  $('lora-summary').textContent = (v.lora ? String(v.lora).split('/').pop() + '（α ' + (v.lora_alpha || 1) + '）'
    : '未挂 LoRA') + ' · 调度 ' + (v.scheduler || 'auto');
}

/* ------------------------------------------------------------------ 参考素材 */
const KIND_ZH = { image: '参考图', video: '参考视频(静音)', video_audio: '视频+音轨', audio: '音频' };
const KIND_ICON = { image: 'IMG', video: 'VID', video_audio: 'V+A', audio: 'AUD' };
const ACCEPT = {
  image: ['.png', '.jpg', '.jpeg', '.webp', '.bmp'],
  video: ['.mp4', '.mov', '.mkv', '.webm', '.avi'],
  video_audio: ['.mp4', '.mov', '.mkv', '.webm', '.avi'],
  audio: ['.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus'],
};

function buildRefButtons() {
  const pick = (kind) => () => pickFiles(kind);
  $('btn-add-image').onclick = pick('image');
  $('btn-add-video').onclick = pick('video');
  $('btn-add-va').onclick = pick('video_audio');
  $('btn-add-audio').onclick = pick('audio');
  $('btn-attach').onclick = pick('image');
  $('btn-add-path').onclick = addByPath;
}

async function pickFiles(kind) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.accept = ACCEPT[kind].join(',');
  inp.onchange = async () => {
    const files = Array.from(inp.files || []);
    inp.remove();
    for (const f of files) await addRefFromFile(f, kind);
  };
  inp.click();
}

async function addRefFromFile(file, kind) {
  const p = file.path;   // Electron / 部分浏览器提供；普通浏览器是 undefined
  if (p) {
    const r = await api('/api/paths', { method: 'POST', body: JSON.stringify({ paths: [p] }) }).catch(() => null);
    if (r && r.paths[0] && r.paths[0].exists) {
      await pushRef({ kind: kind, path: r.paths[0].wsl, name: file.name, bytes: file.size });
      return;
    }
  }
  toast('正在上传 ' + file.name + '（' + fmtBytes(file.size) + '）…', 'info', 2600);
  const resp = await fetch('/api/upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': file.name },
    body: file,
  });
  const data = await resp.json().catch(() => null);
  if (!resp.ok || !data || !data.ok) {
    toast('上传失败：' + ((data && data.error) || resp.status), 'err', 8000);
    return;
  }
  await pushRef({ kind: kind, path: data.path, name: file.name, probe: data.probe, bytes: data.bytes });
}

async function addByPath() {
  const p = prompt('粘贴参考素材路径（Windows 或 WSL 都行）：\n例如 D:\\otherProject\\minimax-h3\\workspace\\pink-harem-ref2va\\materials\\ref-pink-harem.jpg');
  if (!p) return;
  const r = await api('/api/paths', { method: 'POST', body: JSON.stringify({ paths: [p] }) }).catch(() => null);
  if (!r || !r.paths[0] || !r.paths[0].exists) { toast('路径不存在或不可访问：' + p, 'err', 7000); return; }
  const ext = '.' + (p.split('.').pop() || '').toLowerCase();
  let kind = 'image';
  if (ACCEPT.video.indexOf(ext) >= 0) kind = 'video';
  else if (ACCEPT.audio.indexOf(ext) >= 0) kind = 'audio';
  await pushRef({ kind: kind, path: r.paths[0].wsl, name: p.split(/[\\/]/).pop() });
}

async function pushRef(ref) {
  ref.uid = Math.random().toString(36).slice(2, 9);
  const probe = ref.probe;
  if (probe && probe.available) {
    ref.w = probe.w; ref.h = probe.h; ref.frames = probe.frames; ref.seconds = probe.duration_s;
    ref.has_audio = probe.has_audio;
  }
  S.refs.push(ref);
  renderRefs();
  scheduleEstimate();
  updateSummaries();
  if (!probe) {
    // 让后端 ffprobe 一次补上尺寸/帧数（直接影响序列长度与耗时预估）
    api('/api/paths', { method: 'POST', body: JSON.stringify({ paths: [ref.path], probe: true }) })
      .then((r) => {
        const p = r && r.paths && r.paths[0] && r.paths[0].probe;
        if (p && p.available) {
          ref.w = p.w; ref.h = p.h; ref.frames = p.frames;
          ref.seconds = p.duration_s; ref.has_audio = p.has_audio;
          renderRefs();
          scheduleEstimate();
        } else if (p) {
          toast('读不出 ' + (ref.name || ref.path) + ' 的尺寸：' + (p.error || 'ffprobe 无输出') +
            '（预估会按常见尺寸估算）', 'warn', 7000);
        }
      })
      .catch(() => { });
  }
}

/* 标签编号必须与框架一致：image / video / audio 各自独立计数
   （见 diffsynth 的 presentation_ref2va 与 MiniMaxH3Unit_PromptEmbedder.preprocess_ref_blocks），
   而「视频+音轨」会同时占掉一个 <Video n> 和一个 <Audio n>（视频在前，音频在后）。 */
function relabelRefs() {
  const c = { image: 0, video: 0, audio: 0 };
  S.refs.forEach((r) => {
    const labels = [];
    if (r.kind === 'image') { c.image += 1; labels.push('<Picture ' + c.image + '>'); }
    if (r.kind === 'video' || r.kind === 'video_audio') {
      c.video += 1; labels.push('<Video ' + c.video + '>');
    }
    if (r.kind === 'audio' || r.kind === 'video_audio') {
      c.audio += 1; labels.push('<Audio ' + c.audio + '>');
    }
    r.label = labels.join(' + ');
  });
  return c;
}

function renderRefs() {
  relabelRefs();
  const host = $('refs-list');
  host.innerHTML = '';
  $('refs-count').textContent = S.refs.length ? '（' + S.refs.length + '）' : '';
  if (!S.refs.length) {
    host.appendChild(el('div', 'muted tiny',
      '还没有参考素材。纯文字也能生成（T2VA）；加一张参考图可以用它当首帧（I2VA）；' +
      '要做「改视频」就加「视频+音轨」。'));
    return;
  }
  S.refs.forEach((r, i) => {
    const row = el('div', 'ref-item');
    row.dataset.uid = r.uid;
    const th = el('div', 'ref-thumb');
    if (r.kind === 'image') {
      const img = el('img');
      img.src = '/api/file?path=' + encodeURIComponent(r.path);
      img.onerror = () => { img.remove(); th.textContent = KIND_ICON[r.kind]; };
      th.appendChild(img);
    } else th.textContent = KIND_ICON[r.kind] || '?';
    row.appendChild(th);

    const main = el('div', 'ref-main');
    const l1 = el('div', 'ref-line1');
    l1.appendChild(el('span', 'ref-label', r.label));
    l1.appendChild(el('span', 'ref-name', r.name || r.path));
    main.appendChild(l1);
    const dims = [];
    if (r.w && r.h) dims.push(r.w + '×' + r.h);
    if (r.frames) dims.push(r.frames + '帧');
    if (r.seconds) dims.push(Number(r.seconds).toFixed(2) + 's');
    if (r.bytes) dims.push(fmtBytes(r.bytes));
    if (r.kind === 'video_audio' && r.has_audio === false) dims.push('⚠ 没有音轨');
    const m = el('div', 'ref-meta', KIND_ZH[r.kind] + (dims.length ? ' · ' + dims.join(' · ') : '') + ' · ' + r.path);
    m.title = r.path;
    main.appendChild(m);
    row.appendChild(main);

    const ops = el('div', 'ref-ops');
    const mk = (txt, title, fn) => { const b = el('button', null, txt); b.title = title; b.onclick = fn; ops.appendChild(b); };
    mk('↑', '上移（顺序决定编号）', () => { if (i > 0) { const t = S.refs[i - 1]; S.refs[i - 1] = S.refs[i]; S.refs[i] = t; renderRefs(); scheduleEstimate(); } });
    mk('↓', '下移', () => { if (i < S.refs.length - 1) { const t = S.refs[i + 1]; S.refs[i + 1] = S.refs[i]; S.refs[i] = t; renderRefs(); scheduleEstimate(); } });
    mk('⟳', '重新读取尺寸（ffprobe）', async () => {
      const r2 = await api('/api/paths', { method: 'POST', body: JSON.stringify({ paths: [r.path], probe: true }) }).catch(() => null);
      const p = r2 && r2.paths && r2.paths[0] && r2.paths[0].probe;
      if (p && p.available) {
        r.w = p.w; r.h = p.h; r.frames = p.frames; r.seconds = p.duration_s; r.has_audio = p.has_audio;
        renderRefs(); scheduleEstimate();
        toast((r.name || '') + '：' + [p.w && (p.w + '×' + p.h), p.frames && (p.frames + '帧'),
          p.duration_s && (p.duration_s.toFixed(2) + 's')].filter(Boolean).join(' · '), 'ok', 4200);
      } else {
        toast('读不出尺寸：' + ((p && p.error) || '文件不存在或 ffprobe 无输出'), 'err', 6000);
      }
    });
    mk('✕', '移除', () => { S.refs.splice(i, 1); renderRefs(); scheduleEstimate(); updateSummaries(); });
    row.appendChild(ops);
    host.appendChild(row);
  });
}

/* ------------------------------------------------------------------ 预估 */
let estTimer = null;
function scheduleEstimate() { clearTimeout(estTimer); estTimer = setTimeout(runEstimate, 240); }

function estimatePayload() {
  const v = S.values;
  return {
    width: v.width, height: v.height, num_frames: v.num_frames, steps: v.steps,
    ref_image_short_edge: v.ref_image_short_edge,
    ref_video_short_edge: v.ref_video_short_edge,
    ref_video_max_pixels: v.ref_video_max_pixels,
    vram_limit: v.vram_limit, dit_onload: v.dit_onload, lora: v.lora,
    refs: S.refs.map(r => ({ kind: r.kind, w: r.w, h: r.h, frames: r.frames, seconds: r.seconds })),
  };
}

async function runEstimate() {
  try {
    const r = await api('/api/estimate', { method: 'POST', body: JSON.stringify(estimatePayload()) });
    S.estimate = r.estimate; S.risks = r.risks || [];
    renderEstimate(r);
  } catch (e) {
    $('params-summary').textContent = '预估失败：' + e.message;
  }
}

function renderEstimate(r) {
  const e = r.estimate, rows = e.rows, a = r.aligned, risks = r.risks || [];
  const danger = risks.filter(x => x.level === 'danger');
  const warn = risks.filter(x => x.level === 'warn');
  const html = [];
  html.push('<span class="badge">seq ' + rows.seq_len + '</span>');
  html.push('<span class="badge">' + e.step_s + ' s/步 · ' + e.step_confidence + '</span>');
  html.push('<span class="badge">去噪 ' + fmtDur(e.denoise_s) + '</span>');
  html.push('<span class="badge">总约 ' + fmtDur(e.total_s) + '</span>');
  html.push('<span class="badge">实时 ' + (e.realtime_factor || '--') + '×</span>');
  if (danger.length) html.push('<span class="badge err">' + danger.length + ' 项致命风险</span>');
  else if (warn.length) html.push('<span class="badge warn">' + warn.length + ' 项警告</span>');
  $('params-summary').innerHTML = html.join('');

  const notes = [];
  (r.shape_notes || []).forEach(n => notes.push('形状吸附：' + n));
  notes.push('实际执行：' + a.width + '×' + a.height + ' × ' + a.num_frames + ' 帧（' + a.seconds + 's）');
  notes.push('行数：目标视频 ' + rows.target_video + ' + 参考图 ' + rows.ref_image +
    ' + 参考视频 ' + rows.ref_video + ' + 参考音频 ' + rows.ref_audio +
    ' + 目标音频 ' + rows.target_audio + ' + 文本 ' + rows.text + ' = ' + rows.used +
    ' → 对齐 seq ' + rows.seq_len);
  notes.push('固定开销约 ' + e.fixed_s + 's（建流水线 + VAE 解码 + 参考解码；文本编码缓存命中可再省 12s）');
  renderRiskBox(risks, notes);
}

function renderRiskBox(risks, notes) {
  const host = $('risk-box');
  if (!host) return;
  host.innerHTML = '';
  (notes || []).forEach(n => host.appendChild(el('div', 'hint dim', n)));
  risks.forEach((r) => {
    const d = el('div', r.level === 'danger' ? 'errbox' : r.level === 'warn' ? 'warnbox' : 'okbox');
    d.innerHTML = '<b>[' + (r.level === 'danger' ? '会失败' : r.level === 'warn' ? '注意' : '提示') + ']</b> ' +
      esc(r.msg) + (r.hint ? '\n→ ' + esc(r.hint) : '');
    host.appendChild(d);
  });
}

/* ------------------------------------------------------------------ 事件绑定 */
function bindEvents() {
  $('rail-toggle').onclick = () => {
    S.ui.railCollapsed = !S.ui.railCollapsed;
    $('rail').classList.toggle('collapsed', S.ui.railCollapsed);
  };
  $('btn-monitor').onclick = () => toggleMonitor();
  $('btn-monitor-close').onclick = () => toggleMonitor(false);
  $('btn-refresh').onclick = refreshAll;
  document.querySelectorAll('.nav-item').forEach(b => b.onclick = () => selectView(b.dataset.view));
  document.querySelectorAll('.mode-tab').forEach(b => b.onclick = () => {
    document.querySelectorAll('.mode-tab').forEach(x => x.classList.toggle('active', x === b));
    const edit = b.dataset.mode === 'edit';
    if (edit && !S.refs.some(r => r.kind === 'video' || r.kind === 'video_audio')) {
      toast('编辑模式对应官方 tav2va：请添加「视频+音轨」参考。', 'info', 6000);
    }
    if (edit && S.presets['video-edit']) applyPreset('video-edit', true);
  });
  $('sel-preset').onchange = () => applyPreset($('sel-preset').value);
  $('sel-res').onchange = () => {
    const v = $('sel-res').value;
    if (v === 'custom') return;
    const wh = v.split('x');
    S.values.width = Number(wh[0]); S.values.height = Number(wh[1]);
    writeCtl('width', S.values.width); writeCtl('height', S.values.height);
    scheduleEstimate(); updateSummaries();
  };
  $('sel-dur').onchange = () => {
    S.values.num_frames = Number($('sel-dur').value);
    writeCtl('num_frames', S.values.num_frames);
    scheduleEstimate(); updateSummaries();
  };
  $('inp-seed').onchange = () => { S.values.seed = Number($('inp-seed').value) || 0; updateSummaries(); };
  $('sel-mode').onchange = () => { S.mode = $('sel-mode').value; };
  $('btn-optimize').onclick = optimize;
  $('btn-generate').onclick = () => submitJob(false);
  $('btn-preview').onclick = previewCmd;
  $('btn-refresh-jobs').onclick = refreshJobs;
  $('btn-copy-prompt').onclick = () => {
    const txt = S.optimize.en || $('prompt-input').value;
    navigator.clipboard.writeText(txt).then(() => toast('已复制英文提示词', 'ok', 2400))
      .catch(() => toast('复制失败，请手动选择文本', 'err'));
  };
  $('btn-use-prompt').onclick = () => { $('prompt-input').focus(); window.scrollTo({ top: 0, behavior: 'smooth' }); };
  $('btn-close-prompt').onclick = () => { $('prompt-card').hidden = true; };
  $('btn-advanced').onclick = () => {
    ['card-params', 'card-perf', 'card-lora', 'card-cmd'].forEach(id => $(id).classList.remove('collapsed'));
    // 一次点开就把每个参数的详细说明也铺开：这是「高级参数」按钮该有的行为
    setAllDocs(true);
    $('card-params').scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  // 「说明」按钮长在可折叠卡片的抬头里，必须阻止冒泡，否则点它会把整张卡片收起来
  $('btn-doc-all').onclick = (e) => { // eslint-disable-line no-unused-vars
    e.stopPropagation();
    const anyClosed = Array.from(document.querySelectorAll('#card-params .doc-body'))
      .some(w => w.style.display !== 'block');
    setAllDocs(anyClosed);
    toast(anyClosed ? '已展开全部参数说明' : '已收起全部参数说明', 'info', 2400);
  };
  document.querySelectorAll('[data-collapse]').forEach(h => h.onclick = () => h.parentElement.classList.toggle('collapsed'));
  document.querySelectorAll('#prompt-tabs .tab').forEach(t => t.onclick = () => {
    S.ui.promptPane = t.dataset.pane;
    ['en', 'zh', 'notes', 'raw'].forEach(p => { $('pane-' + p).hidden = (p !== S.ui.promptPane); });
  });
  // 运行日志页签
  $('btn-open-logs').onclick = () => selectView('logs');
  $('log-level').onchange = () => { S.ui.logLevel = $('log-level').value; startFullLogStream(true); };
  $('log-source').onchange = () => { S.ui.logSource = $('log-source').value; startFullLogStream(true); };
  $('btn-log-search').onclick = () => { S.ui.logQ = $('log-search').value.trim(); startFullLogStream(true); };
  $('log-search').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-log-search').onclick(); });
  $('btn-log-refresh').onclick = () => { startFullLogStream(true); loadLlmRuns(); toast('日志流已重连', 'ok', 2200); };
  $('btn-log-download').onclick = downloadLogs;
  $('btn-log-clear').onclick = clearLogs;
  $('btn-llm-refresh').onclick = loadLlmRuns;
  $('log-follow').onchange = () => { S.ui.logFollow = $('log-follow').checked; };
  $('log-wrap').onchange = () => $('log-full').classList.toggle('nowrap', !$('log-wrap').checked);
  $('new-session').onclick = () => {
    S.refs = []; renderRefs();
    $('prompt-input').value = '';
    S.optimize = { en: '', zh: '', notes: '', thinking: '', mode: null, running: false, system: '', user: '', runId: '' };
    $('prompt-card').hidden = true;
    scheduleEstimate();
    toast('已清空当前创作（生成参数保持不变）', 'ok', 2600);
  };
  $('modal-close').onclick = () => { $('modal').hidden = true; };
  $('modal').onclick = (e) => { if (e.target === $('modal')) $('modal').hidden = true; };

  let dragDepth = 0;
  window.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer || Array.from(e.dataTransfer.types || []).indexOf('Files') < 0) return;
    dragDepth++; showDropzone(true);
  });
  window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) showDropzone(false); });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', async (e) => {
    e.preventDefault(); dragDepth = 0; showDropzone(false);
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    for (const f of files) {
      const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
      let kind = 'image';
      if (ACCEPT.video.indexOf(ext) >= 0) kind = 'video';
      else if (ACCEPT.audio.indexOf(ext) >= 0) kind = 'audio';
      if (ACCEPT[kind].indexOf(ext) < 0) { toast('不支持的格式：' + f.name, 'err', 5000); continue; }
      await addRefFromFile(f, kind);
    }
  });
  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); submitJob(false); }
  });
  window.addEventListener('beforeunload', saveSession);
}

function showDropzone(on) {
  let d = $('dropzone');
  if (on && !d) {
    d = el('div', 'dropzone', '松开鼠标添加参考素材（图片 / 视频 / 音频）');
    d.id = 'dropzone';
    document.body.appendChild(d);
  } else if (!on && d) d.remove();
}

function toggleMonitor(force) {
  S.ui.monitor = (force === undefined) ? !S.ui.monitor : force;
  $('monitor').classList.toggle('hidden', !S.ui.monitor);
}

async function refreshAll() {
  try {
    const cfg = await api('/api/config');
    S.cfg = cfg;
    buildSidebarInfo(cfg);
    updateSummaries();
    await refreshJobs();
    await runEstimate();
    toast('已刷新配置、作业与预估', 'ok', 2600);
  } catch (e) { toast('刷新失败：' + e.message, 'err'); }
}

function selectView(v) {
  ['create', 'library', 'guide', 'logs', 'about'].forEach(x => { $('view-' + x).hidden = (x !== v); });
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  S.ui.view = v;
  if (v === 'library') renderLibrary();
  if (v === 'logs') { startFullLogStream(false); loadLlmRuns(); }
}

/* ------------------------------------------------------------------ 提示词优化 */
async function optimize() {
  if (S.optimize.running) return;
  const chinese = $('prompt-input').value.trim();
  if (!chinese) { toast('先写一段中文创作意图，再点优化', 'warn'); return; }
  if (!((S.cfg.deepseek || {}).key_present)) {
    toast('没有配置 DeepSeek API Key，无法优化。请编辑 config/deepseek.yaml 的 api.key，' +
      '或在 WSL 里 export DEEPSEEK_API_KEY 后重启服务。', 'err', 10000);
    return;
  }
  S.optimize = { en: '', zh: '', notes: '', thinking: '', mode: null, running: true, system: '', user: '', runId: '' };
  $('prompt-card').hidden = false;
  setPromptPanes();
  $('optimize-status').className = 'optimize-status';
  $('optimize-status').textContent = '正在请求 DeepSeek …';
  const btn = $('btn-optimize');
  btn.disabled = true; btn.textContent = '优化中…';
  switchPane('en');

  const body = Object.assign(estimatePayload(), {
    chinese: chinese,
    seed: S.values.seed,
    lora: S.values.lora,
    mode: S.mode,
    refs: S.refs.map(r => ({ kind: r.kind, name: r.name, label: r.label, w: r.w, h: r.h,
      frames: r.frames, seconds: r.seconds })),
  });

  let resp;
  try {
    resp = await fetch('/api/optimize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  } catch (e) { finishOptimize('连接后端失败：' + e.message, true); return; }
  if (!resp.ok || !resp.body) {
    let msg = 'HTTP ' + resp.status;
    try { const j = await resp.json(); msg = j.error || j.message || msg; } catch (e) { }
    finishOptimize(msg, true); return;
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    let chunk;
    try { chunk = await reader.read(); } catch (e) { break; }
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
      let ev = 'message', data = '';
      raw.split('\n').forEach((l) => {
        if (l.indexOf('event:') === 0) ev = l.slice(6).trim();
        else if (l.indexOf('data:') === 0) data += l.slice(5).trim();
      });
      if (!data) continue;
      let obj = null;
      try { obj = JSON.parse(data); } catch (e) { continue; }
      handleOptimizeEvent(ev, obj);
    }
  }
  if (S.optimize.running) finishOptimize('连接提前结束（可能超时或被中断），可重试一次。', true);
}

function handleOptimizeEvent(ev, obj) {
  const st = $('optimize-status');
  if (ev === 'meta') {
    S.optimize.runId = obj.run_id || '';
    S.optimize.mode = obj.mode_zh || obj.mode;
    S.optimize.system = obj.system_prompt || '';
    S.optimize.user = obj.user_message || '';
    st.className = 'optimize-status';
    st.textContent = '模型 ' + obj.model +
      (obj.thinking ? '（思考模式 ' + (obj.reasoning_effort || '') + '）' : '') +
      ' · 结构 ' + (obj.mode_zh || obj.mode) +
      '\n判定理由：' + (obj.mode_why || '') +
      '\n引用规范：' + (obj.system_sources || []).join(' + ');
    setPromptPanes();
  } else if (ev === 'thinking') {
    S.optimize.thinking += obj.text || '';
    st.textContent = '思考中…（已 ' + S.optimize.thinking.length + ' 字，完成后可在「调试」页签查看）';
  } else if (ev === 'section') {
    st.textContent = '正在输出：' + ({ prompt: '英文提示词', translation: '中文回译', notes: '结构说明' }[obj.section] || obj.section);
    if (obj.section === 'translation') switchPane('zh');
  } else if (ev === 'delta') {
    if (obj.section === 'prompt') S.optimize.en += obj.text || '';
    else if (obj.section === 'translation') S.optimize.zh += obj.text || '';
    else S.optimize.notes += obj.text || '';
    setPromptPanes();
  } else if (ev === 'warn') {
    toast(obj.message || '警告', 'warn', 6000);
  } else if (ev === 'error') {
    finishOptimize((obj.message || '未知错误') + (obj.hint ? '\n→ ' + obj.hint : '') +
      (obj.detail ? '\n\n服务端原始返回：' + obj.detail : ''), true);
  } else if (ev === 'done') {
    S.optimize.en = obj.result || S.optimize.en;
    S.optimize.zh = obj.translation || S.optimize.zh;
    S.optimize.notes = obj.notes || S.optimize.notes;
    setPromptPanes();
    const u = obj.usage || {};
    const miss = obj.missing || [];
    finishOptimize('完成 · 结构 ' + (obj.mode || '') + ' · 时长对齐 ' + obj.duration_s + 's' +
      (u.total_tokens ? ' · tokens ' + u.total_tokens : '') +
      (miss.length ? '\n⚠ 模型没有输出这些段：' + miss.join(' / ') + '（建议重试）' : ''), miss.length > 0);
  }
}

function switchPane(pane) {
  S.ui.promptPane = pane;
  document.querySelectorAll('#prompt-tabs .tab').forEach(x => x.classList.toggle('active', x.dataset.pane === pane));
  ['en', 'zh', 'notes', 'raw'].forEach(p => { $('pane-' + p).hidden = (p !== pane); });
}

function setPromptPanes() {
  $('pane-en').textContent = S.optimize.en || '';
  $('pane-zh').textContent = S.optimize.zh || '';
  $('pane-notes').textContent = S.optimize.notes || '';
  $('pane-raw').textContent =
    '===== system prompt =====\n' + (S.optimize.system || '(尚未返回)') +
    '\n\n===== user message =====\n' + (S.optimize.user || '(尚未返回)') +
    '\n\n===== 思考过程 =====\n' + (S.optimize.thinking || '(无 / 未开启思考模式)');
}

function finishOptimize(msg, isErr) {
  S.optimize.running = false;
  const btn = $('btn-optimize');
  btn.disabled = false; btn.textContent = '✦ 优化提示词';
  const st = $('optimize-status');
  st.className = 'optimize-status ' + (isErr ? 'err' : 'ok');
  st.textContent = msg;
  if (!isErr) {
    // 这次调用的完整记录已经落盘：给一个直达入口，方便回看 system/user/输出/think
    if (S.optimize.runId) {
      const link = el('button', 'link-btn', ' → 查看本次 LLM 调用记录');
      link.onclick = () => openLlmRun(S.optimize.runId);
      st.appendChild(link);
    }
    toast('提示词已生成（' + (S.optimize.mode || '') + '），可直接开始生成', 'ok', 4200);
    if (S.ui.view === 'logs') loadLlmRuns();
  }
}

/* ------------------------------------------------------------------ 提交生成 */
function buildSubmitBody(force) {
  const v = S.values;
  return Object.assign(estimatePayload(), {
    prompt: $('prompt-input').value.trim(),
    steps: v.steps, seed: v.seed, preset: v.preset,
    lora: v.lora || null, lora_alpha: v.lora_alpha,
    vram_limit: v.vram_limit, activation_reserve: v.activation_reserve,
    dit_onload: v.dit_onload, sdpa_backend: v.sdpa_backend, scheduler: v.scheduler,
    beta_alpha: v.beta_alpha, beta_beta: v.beta_beta,
    tile_size: v.tile_size, tile_overlap: v.tile_overlap, no_tiled: v.no_tiled,
    refresh_text_cache: v.refresh_text_cache,
    refs: S.refs.map(r => ({ kind: r.kind, path: r.path, name: r.name,
      w: r.w, h: r.h, frames: r.frames, seconds: r.seconds })),
    force: !!force,
  });
}

async function submitJob(force) {
  const body = buildSubmitBody(force);
  if (!body.prompt) { toast('提示词不能为空', 'warn'); return; }
  const btn = $('btn-generate');
  btn.disabled = true;
  try {
    const r = await api('/api/jobs', { method: 'POST', body: JSON.stringify(body) });
    toast('作业已提交：' + r.job.id, 'ok', 4200);
    (r.warnings || []).forEach(w => toast(w, 'warn', 9000));
    S.activeJob = r.job.id;
    await refreshJobs();
    openJobStream(r.job.id);
    toggleMonitor(true);
  } catch (e) {
    const d = e.data || {};
    if (d.need_force) {
      confirmModal('这个配置被判定为大概率失败', buildRiskHtml(d.risks),
        '仍然要跑（不推荐）', () => submitJob(true));
    } else {
      const errs = (d.errors || []).join('\n');
      toast('提交失败：' + e.message + (errs ? '\n' + errs : ''), 'err', 10000);
    }
  } finally {
    btn.disabled = false;
  }
}

function buildRiskHtml(risks) {
  const wrap = el('div');
  (risks || []).forEach((r) => {
    const d = el('div', r.level === 'danger' ? 'errbox' : r.level === 'warn' ? 'warnbox' : 'okbox');
    d.innerHTML = '<b>' + esc(r.msg) + '</b>' + (r.hint ? '<br>→ ' + esc(r.hint) : '');
    wrap.appendChild(d);
  });
  return wrap;
}

function confirmModal(title, bodyNode, okText, onOk) {
  $('modal-title').textContent = title;
  const body = $('modal-body');
  body.innerHTML = '';
  if (typeof bodyNode === 'string') body.innerHTML = bodyNode; else body.appendChild(bodyNode);
  const foot = $('modal-foot');
  foot.innerHTML = '';
  const cancel = el('button', 'ghost-btn', '取消');
  cancel.onclick = () => { $('modal').hidden = true; };
  const ok = el('button', 'primary-btn', okText || '确定');
  ok.onclick = () => { $('modal').hidden = true; if (onOk) onOk(); };
  foot.appendChild(cancel); foot.appendChild(ok);
  $('modal').hidden = false;
}

async function previewCmd() {
  const body = buildSubmitBody(false);
  try {
    const r = await api('/api/preview', { method: 'POST', body: JSON.stringify(body) });
    const out = $('cmd-out');
    out.hidden = false;
    out.textContent = '# 实际会执行的命令（参数已由后端解析；不会加载任何权重）\n' +
      r.cmd.map(x => (/\s/.test(x) ? '"' + x + '"' : x)).join(' \\\n  ') +
      '\n\n# 预估\n' + JSON.stringify(r.request._estimate, null, 2) +
      (((r.warnings || []).length) ? '\n\n# 提示\n' + r.warnings.join('\n') : '');
    $('card-cmd').classList.remove('collapsed');
    toast('参数解析通过，命令见下方', 'ok', 3200);
  } catch (e) {
    toast('解析失败：' + e.message + (((e.data || {}).errors) ? '\n' + e.data.errors.join('\n') : ''), 'err', 9000);
  }
}

/* ------------------------------------------------------------------ 作业与事件流 */
async function refreshJobs() {
  try {
    const r = await api('/api/jobs');
    S.jobs = r.jobs || [];
    renderJobs(); renderGallery(); renderLibrary();
    const running = S.jobs.filter(j => j.status === 'running' || j.status === 'cancelling').length;
    const queued = ((r.queue || {}).queued || []).length;
    const pill = $('pill-queue');
    pill.textContent = '队列 ' + running + (queued ? ' + 排队 ' + queued : '');
    pill.className = 'pill' + (running ? ' live' : '');
  } catch (e) { /* 忽略 */ }
}

function renderJobs() {
  const host = $('job-monitor');
  host.innerHTML = '';
  const active = S.jobs.filter(j => ['running', 'queued', 'cancelling'].indexOf(j.status) >= 0);
  const recent = S.jobs.filter(j => ['running', 'queued', 'cancelling'].indexOf(j.status) < 0).slice(0, 2);
  const list = active.concat(recent);
  if (!list.length) { host.appendChild(el('div', 'muted tiny', '暂无作业')); return; }
  list.forEach(j => host.appendChild(jobCard(j)));
}

function jobCard(j) {
  const box = el('div', 'jmon');
  const head = el('div', 'jhead');
  head.appendChild(el('span', 'status-dot ' + j.status));
  const r = j.request || {};
  head.appendChild(el('b', null, r.width ? (r.width + '×' + r.height + '×' + r.num_frames + 'f') : j.id));
  head.appendChild(el('span', 'muted tiny', j.stage_zh || j.stage));
  head.appendChild(el('span', 'sp', fmtDur(j.elapsed_s)));
  box.appendChild(head);

  const bar = el('div', 'jbar');
  const fill = el('i');
  fill.style.width = Math.round((j.progress || 0) * 100) + '%';
  bar.appendChild(fill); box.appendChild(bar);

  const meta = el('div', 'jmeta');
  meta.appendChild(el('span', null, '步骤 ' + j.step + '/' + j.total_steps));
  if (j.step_s) meta.appendChild(el('span', null, j.step_s + ' s/步'));
  if (j.eta_s) meta.appendChild(el('span', null, '剩余 ' + fmtDur(j.eta_s)));
  if (j.peak_vram_gib) meta.appendChild(el('span', null, '峰值 ' + j.peak_vram_gib + ' GiB'));
  if (j.estimate && j.estimate.total_min) meta.appendChild(el('span', 'dim', '预估 ' + j.estimate.total_min + ' min'));
  box.appendChild(meta);

  if (j.error) { const e = el('div', 'errbox tiny'); e.textContent = j.error; box.appendChild(e); }

  const ops = el('div', 'gops');
  const focus = el('button', null, '看日志');
  focus.onclick = () => { openJobStream(j.id); toggleMonitor(true); };
  ops.appendChild(focus);
  if (j.status === 'running' || j.status === 'queued') {
    const cancel = el('button', null, '取消');
    cancel.onclick = async () => {
      const res = await api('/api/jobs/' + j.id + '/cancel', { method: 'POST' }).catch(e => ({ message: e.message }));
      toast(res.message || '已请求取消', 'info');
      refreshJobs();
    };
    ops.appendChild(cancel);
  }
  if (j.status === 'done') {
    const open = el('button', null, '播放 / 路径');
    open.onclick = () => openResult(j.id);
    ops.appendChild(open);
  }
  box.appendChild(ops);
  return box;
}

const jobSources = {};
function openJobStream(jobId) {
  S.activeJob = jobId;
  const box = $('log-stream');
  box.textContent = '';
  appendLog({ level: 'info', source: 'job', msg: '—— 关注作业 ' + jobId + ' ——' });
  if (!window.EventSource) return;
  if (jobSources.active) { jobSources.active.close(); }
  const es = new EventSource('/api/jobs/' + jobId + '/stream?from=0');
  jobSources.active = es;
  const handler = (name) => (e) => {
    let obj = {};
    try { obj = JSON.parse(e.data); } catch (err) { }
    if (name === 'log') appendLog({ level: guessLevel(obj.line), source: 'job', msg: obj.line });
    else if (name === 'step') updateJobFromEvent(jobId, obj);
    else if (name === 'stage') appendLog({ level: 'info', source: 'job', msg: '▸ [' + (obj.stage_zh || obj.stage) + '] ' + (obj.info || '') });
    else if (name === 'result') appendLog({ level: 'ok', source: 'job', msg: '产物：' + (obj.out || '(见作业详情)') });
    else if (name === 'warn') appendLog({ level: 'warn', source: 'job', msg: obj.message || JSON.stringify(obj) });
    else if (name === 'error') appendLog({ level: 'error', source: 'job', msg: obj.message || JSON.stringify(obj) });
    else if (name === 'exit') {
      appendLog({ level: obj.code === 0 ? 'ok' : 'error', source: 'job',
        msg: '作业结束：' + obj.status + (obj.error ? ' — ' + obj.error : '') });
      refreshJobs();
      es.close();
    }
  };
  ['log', 'step', 'stage', 'exit', 'error', 'result', 'warn'].forEach(n => es.addEventListener(n, handler(n)));
}

function updateJobFromEvent(jobId, obj) {
  const j = S.jobs.find(x => x.id === jobId);
  if (!j) return;
  j.step = obj.i; j.total_steps = obj.total; j.step_s = obj.s_step;
  j.peak_vram_gib = obj.peak_vram_gib || j.peak_vram_gib;
  j.eta_s = obj.eta_s;
  j.progress = obj.total ? obj.i / obj.total : 0;
  renderJobs();
}

function guessLevel(line) {
  if (!line) return 'info';
  if (/error|traceback|runtimeerror|failed|exception|killed/i.test(line)) return 'error';
  if (/warn|注意|deprecat/i.test(line)) return 'warn';
  if (line.indexOf('[h3 ') === 0) return 'h3';
  return 'info';
}

/* ------------------------------------------------------------------ 运行日志
 * 两条 SSE：监控面板的小窗（只看 info+）、「运行日志」页签的大窗（按等级/来源/关键字
 * 服务端过滤）。LLM 调用记录走 /api/llm/runs，单独渲染成表格 + 详情弹窗。
 */
const logEs = {};
const LOG_KEYS = ['seq', 't', 'iso', 'level', 'source', 'event', 'msg'];

function logsExposed() {
  return ((S.cfg || {}).logging || {}).expose !== false;
}

function shortVal(v) {
  let s = (typeof v === 'string') ? v : JSON.stringify(v);
  if (s === undefined || s === null) s = '';
  return s.length > 110 ? s.slice(0, 110) + '…' : s;
}

function logLine(rec) {
  const level = rec.level || 'info';
  const row = el('div', 'lline l-' + level + (rec.source ? ' src-' + rec.source : ''));
  const iso = String(rec.iso || rec.t || '');
  row.appendChild(el('span', 'lt', iso.length >= 23 ? iso.slice(11, 23) : iso));
  if (rec.source) row.appendChild(el('span', 'ls', rec.source));
  row.appendChild(el('span', 'lm', rec.msg === undefined ? JSON.stringify(rec) : rec.msg));
  const extras = {};
  Object.keys(rec).forEach((k) => {
    if (LOG_KEYS.indexOf(k) >= 0) return;
    if (rec[k] === null || rec[k] === undefined || rec[k] === '') return;
    extras[k] = rec[k];
  });
  const keys = Object.keys(extras);
  if (keys.length) {
    const x = el('span', 'lx', keys.map(k => k + '=' + shortVal(extras[k])).join(' '));
    try { x.title = JSON.stringify(extras, null, 2); } catch (e) { }
    row.appendChild(x);
  }
  return row;
}

function appendLog(rec, boxId) {
  const target = boxId || 'log-stream';
  const box = $(target);
  if (!box) return;
  box.appendChild(logLine(rec));
  while (box.childNodes.length > 1200) box.removeChild(box.firstChild);
  const follow = target === 'log-full'
    ? ($('log-follow') ? $('log-follow').checked : true)
    : ($('chk-autoscroll') ? $('chk-autoscroll').checked : true);
  if (follow) box.scrollTop = box.scrollHeight;
}

function startLogStream() {
  if (!window.EventSource || !logsExposed()) return;
  if ((((S.cfg || {}).server) || {}).expose_server_log === false) return;
  if (logEs.monitor) logEs.monitor.close();
  const es = new EventSource('/api/logs/stream?level=info');
  logEs.monitor = es;
  es.addEventListener('snapshot', (e) => {
    let d = {};
    try { d = JSON.parse(e.data); } catch (err) { }
    updateLogStats(d.stats, d.llm);
  });
  es.addEventListener('log', (e) => {
    let rec = {};
    try { rec = JSON.parse(e.data); } catch (err) { }
    appendLog(rec, 'log-stream');
  });
}

function startFullLogStream(clear) {
  if (!window.EventSource || !logsExposed()) return;
  if (logEs.full) { logEs.full.close(); logEs.full = null; }
  const box = $('log-full');
  if (clear && box) box.textContent = '';
  const qs = new URLSearchParams({ level: S.ui.logLevel || 'all', source: S.ui.logSource || 'all' });
  if (S.ui.logQ) qs.set('q', S.ui.logQ);
  const es = new EventSource('/api/logs/stream?' + qs.toString());
  logEs.full = es;
  es.addEventListener('snapshot', (e) => {
    let d = {};
    try { d = JSON.parse(e.data); } catch (err) { }
    updateLogStats(d.stats, d.llm);
  });
  es.addEventListener('log', (e) => {
    let rec = {};
    try { rec = JSON.parse(e.data); } catch (err) { }
    appendLog(rec, 'log-full');
  });
}

function updateLogStats(stats, llm) {
  if (stats && $('mon-log-level')) $('mon-log-level').textContent = '(' + (stats.level || '') + ')';
  const host = $('log-stats');
  if (!stats || !host) return;
  const c = stats.counts || {};
  host.textContent =
    '等级 ' + stats.level + ' · 内存 ' + stats.retained + '/' + stats.capacity +
    ' 条 · 已滚出 ' + stats.evicted +
    ' · debug ' + (c.debug || 0) + ' / info ' + (c.info || 0) +
    ' / warn ' + (c.warn || 0) + ' / error ' + (c.error || 0) +
    ' · 文件 ' + (stats.files || []).length + ' 个 ' +
    ((stats.file_bytes || 0) / 1024).toFixed(1) + ' KiB' +
    (stats.file_error ? ' · ⚠ 写盘错误 ' + stats.file_error : '') +
    (llm ? ' · LLM 记录 ' + llm.runs + ' 份' : '');
}

function applyLoggingConfig(cfg) {
  const L = (cfg && cfg.logging) || {};
  const expose = L.expose !== false;
  const nav = document.querySelector('.nav-item[data-view="logs"]');
  if (nav) nav.hidden = !expose;
  if ($('log-level') && L.level) $('log-level').value = (L.level === 'debug' ? 'all' : L.level);
  if ($('mon-log-level') && L.level) $('mon-log-level').textContent = '(' + L.level + ')';
  if ($('logs-intro')) {
    $('logs-intro').textContent = expose
      ? '日志同时写入内存环缓冲与磁盘：' + (L.dir || 'cache/webui/logs') +
        '（JSONL，服务重启也不丢）。LLM 每次调用单独一份完整记录，见下方表格。'
      : '运行日志未对外暴露（config/server.yaml → logging.expose=false）。';
  }
}

function downloadLogs() {
  const qs = new URLSearchParams({ level: S.ui.logLevel || 'all',
    source: S.ui.logSource || 'all', format: 'txt', n: '5000' });
  if (S.ui.logQ) qs.set('q', S.ui.logQ);
  const a = document.createElement('a');
  a.href = '/api/logs/download?' + qs.toString();
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
}

async function clearLogs() {
  if (!window.confirm('清空内存里的日志缓冲？（磁盘上的 JSONL 文件保留，仍可下载）')) return;
  try {
    await api('/api/logs/clear', { method: 'POST', body: JSON.stringify({ files: false }) });
  } catch (e) { toast('清空失败：' + e.message, 'err'); return; }
  const box = $('log-full');
  if (box) box.textContent = '';
  toast('日志缓冲已清空', 'ok', 2400);
}

async function loadLlmRuns() {
  if (!logsExposed()) return;
  try {
    const d = await api('/api/llm/runs?limit=50');
    S.llm.runs = d.runs || [];
    S.llm.stats = d.stats || null;
    renderLlmRuns();
  } catch (e) { /* 页签可能被隐藏，忽略 */ }
}

function renderLlmRuns() {
  const tbl = $('llm-table');
  if (!tbl) return;
  const tb = tbl.querySelector('tbody');
  tb.innerHTML = '';
  if (!S.llm.runs.length) {
    const tr = document.createElement('tr');
    const td = el('td', 'muted', '还没有调用记录：点一次「优化提示词」就会出现。');
    td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  S.llm.runs.forEach((r) => {
    const tr = document.createElement('tr');
    const u = r.usage || {};
    const oc = r.output_chars || {};
    [r.started, '', r.model || '', r.mode || '', u.total_tokens || '--',
     (r.duration_s || 0).toFixed(2) + 's',
     (oc.prompt || 0) + ' / ' + (oc.translation || 0) + ' / ' + (oc.notes || 0),
     r.error || ''].forEach(c => tr.appendChild(el('td', null, c)));
    const cls = r.status === 'ok' ? 'ok' : (r.status === 'error' ? 'err' : 'warn');
    tr.children[1].innerHTML = '<span class="badge ' + cls + '">' + esc(r.status) + '</span>';
    const td = el('td');
    const b = el('button', 'ghost-btn sm', '详情');
    b.onclick = () => openLlmRun(r.id);
    td.appendChild(b);
    tr.appendChild(td);
    tb.appendChild(tr);
  });
}

async function openLlmRun(id) {
  let d;
  try { d = await api('/api/llm/runs/' + encodeURIComponent(id)); }
  catch (e) { toast('读取调用记录失败：' + e.message, 'err'); return; }
  const run = d.run || {};
  const req = run.request || {}, res = run.response || {}, out = res.sections || {};
  const box = el('div');
  const sec = (title, text) => {
    if (!text) return;
    box.appendChild(el('h4', null, title));
    box.appendChild(el('pre', 'llm-json', text));
  };
  box.appendChild(el('div', 'muted tiny',
    'run ' + run.id + ' · ' + run.status + ' · ' + (run.duration_s || 0) + 's' +
    (run.usage && run.usage.total_tokens ? ' · tokens ' + run.usage.total_tokens : '') +
    (run.capture ? ' · capture=' + run.capture : '')));
  sec('system prompt（引用：' + (((req.system_sources || []).join(', ')) || '-') + '）',
    req.system || '（未保存全文；字符数 ' + (req.system_chars || 0) + '）');
  sec('user message', req.user || '（未保存全文；字符数 ' + (req.user_chars || 0) + '）');
  sec('英文提示词（送进流水线）', out.prompt);
  sec('中文回译', out.translation);
  sec('结构说明', out.notes);
  if (res.thinking) sec('思考过程', res.thinking);
  else if (res.reasoning_chars) sec('思考过程', '（未保存全文；共 ' + res.reasoning_chars + ' 字符）');
  if ((run.events || []).length) {
    sec('事件', run.events.map(e => e.t + '  [' + e.kind + '] ' + e.message).join('\n'));
  }
  box.appendChild(el('h4', null, '请求参数 / usage'));
  box.appendChild(el('pre', 'llm-json', JSON.stringify({
    model: req.model, stream: req.stream, params: req.params,
    usage: run.usage, missing: res.missing || [],
  }, null, 2)));
  confirmModal('LLM 调用记录 ' + run.id, box, '下载 JSON', () => {
    const blob = new Blob([JSON.stringify(run, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'llm-run-' + run.id + '.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  });
}

/* 前端主动上报：前端异常/关键动作也进同一份运行日志，和用户看到的报错对得上 */
function clientLog(level, msg, extra) {
  if (!logsExposed()) return;
  const rec = Object.assign({
    level: level, source: 'ui', event: 'ui.event', msg: String(msg),
    page: location.pathname,
  }, extra || {});
  try {
    if (navigator.sendBeacon) {
      const blob = new Blob([JSON.stringify(rec)], { type: 'application/json' });
      if (navigator.sendBeacon('/api/logs/client', blob)) return;
    }
  } catch (e) { /* 退回到 fetch */ }
  fetch('/api/logs/client', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rec), keepalive: true,
  }).catch(() => { });
}

/* ------------------------------------------------------------------ 遥测 */
function startTelemetry() {
  const tick = async () => {
    try {
      const t = await api('/api/telemetry?history=120');
      S.telemetry = t;
      renderTelemetry(t);
    } catch (e) { /* 忽略 */ }
    setTimeout(tick, ((S.cfg.server || {}).telemetry_interval_s || 1.5) * 1000);
  };
  tick();
}

function renderTelemetry(t) {
  const g = t.now.gpu || {}, m = t.now.mem || {}, d = t.now.disk || {};
  const gpuU = num(g.util_pct, 0);
  setGauge('g-gpu', gpuU, gpuU.toFixed(0) + '%');
  const vp = g.vram_total_gib ? (g.vram_used_gib / g.vram_total_gib * 100) : 0;
  setGauge('g-vram', vp, (g.vram_used_gib || 0).toFixed(2) + '/' + (g.vram_total_gib || 0).toFixed(2) + 'G');
  const rp = m.total_gib ? (m.used_gib / m.total_gib * 100) : 0;
  setGauge('g-ram', rp, (m.used_gib || 0).toFixed(1) + '/' + (m.total_gib || 0).toFixed(1) + 'G');
  const cpu = num(m.cpu_util_pct, 0);
  setGauge('g-cpu', cpu, cpu.toFixed(0) + '%');
  $('hw-detail').textContent =
    (g.name || 'GPU ?') + (g.temp_c !== null && g.temp_c !== undefined ? ' · ' + g.temp_c + '°C' : '') +
    (g.power_w ? ' · ' + g.power_w.toFixed(1) + 'W' : '') +
    (g.clock_sm_mhz ? ' · ' + g.clock_sm_mhz + '/' + (g.clock_max_mhz || '?') + ' MHz' : '') + '\n' +
    'WSL 内存配额 ' + (m.quota_gib || '?') + ' GiB · swap ' + (m.swap_used_gib || 0) + '/' +
    (m.swap_total_gib || 0) + ' GiB · load ' + ((m.loadavg || []).map(x => x.toFixed(2)).join(' ') || '-') + '\n' +
    '磁盘剩余 ' + (d.free_gib === undefined ? '?' : d.free_gib) + ' GiB' +
    (((g.processes || []).length) ? '\n显存占用：' + g.processes.map(p => p.name + '(' + p.vram_mib + 'MiB)').join(', ') : '');
  if (!g.ok && g.error) $('hw-detail').textContent += '\n⚠ nvidia-smi: ' + g.error;

  const pv = $('pill-vram');
  pv.textContent = '显存 ' + (g.vram_used_gib || 0).toFixed(2) + '/' + (g.vram_total_gib || 0).toFixed(1) + ' GiB';
  pv.className = 'pill' + (vp > 92 ? ' bad' : vp > 78 ? ' warn' : '');
  const pr = $('pill-ram');
  pr.textContent = '内存 ' + (m.used_gib || 0).toFixed(1) + '/' + (m.total_gib || 0).toFixed(1) + ' GiB';
  pr.className = 'pill' + (rp > 92 ? ' bad' : rp > 80 ? ' warn' : '');
  const pg = $('pill-gpu');
  pg.textContent = 'GPU ' + gpuU.toFixed(0) + '% · ' + (g.temp_c === undefined ? '--' : g.temp_c) + '°C';
  const ps = $('pill-server');
  ps.textContent = '服务在线';
  ps.className = 'pill live';
  drawSpark(t.history || []);
}

function setGauge(id, pct, text) {
  const bar = $(id);
  if (!bar) return;
  bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  bar.classList.toggle('hot', pct > 85);
  const v = $(id + '-v');
  if (v) v.textContent = text;
}

function drawSpark(hist) {
  const c = $('spark');
  if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H);
  const now = (S.telemetry && S.telemetry.now) || {};
  const vramTotal = ((now.gpu || {}).vram_total_gib) || 7.93;
  const ramTotal = ((now.mem || {}).total_gib) || 22.9;
  const series = [
    { key: 'gpu_util', color: '#4f7cff', max: 100, label: 'GPU%' },
    { key: 'vram_used_gib', color: '#6fe0c0', max: vramTotal, label: 'VRAM' },
    { key: 'mem_used_gib', color: '#e8a33d', max: ramTotal, label: 'RAM' },
  ];
  ctx.strokeStyle = '#242833'; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = H * i / 4; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  if (hist.length < 2) {
    ctx.fillStyle = '#5f6779'; ctx.font = '11px monospace';
    ctx.fillText('采集历史中…', 8, H / 2 + 4);
    return;
  }
  series.forEach((s) => {
    ctx.beginPath(); ctx.strokeStyle = s.color; ctx.lineWidth = 1.6;
    hist.forEach((h, i) => {
      const v = num(h[s.key], 0) / s.max;
      const x = i / (hist.length - 1) * W;
      const y = H - Math.max(0, Math.min(1, v)) * (H - 6) - 3;
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    });
    ctx.stroke();
  });
  ctx.font = '10px monospace';
  series.forEach((s, i) => { ctx.fillStyle = s.color; ctx.fillText(s.label, 6 + i * 56, 11); });
}

/* ------------------------------------------------------------------ 产物 */
async function openResult(jobId) {
  const r = await api('/api/jobs/' + jobId + '/result').catch(() => null);
  if (!r || !r.output) { toast('还没有产物文件', 'warn'); return; }
  const b = el('div');
  b.innerHTML = '<div class="kv">' +
    '<div>WSL 路径</div><div><code>' + esc(r.output.wsl) + '</code></div>' +
    '<div>Windows 路径</div><div><code>' + esc(r.output.windows) + '</code></div>' +
    '<div>大小</div><div>' + r.output.size_mib + ' MiB</div></div>';
  const v = document.createElement('video');
  v.controls = true;
  v.style.width = '100%';
  v.style.marginTop = '10px';
  v.style.borderRadius = '10px';
  v.src = '/api/file?path=' + encodeURIComponent(r.output.wsl);
  b.appendChild(v);
  confirmModal('生成结果', b, '复制 Windows 路径', () => {
    navigator.clipboard.writeText(r.output.windows);
    toast('已复制：' + r.output.windows, 'ok', 6000);
  });
}

function renderGallery() {
  const host = $('gallery');
  host.innerHTML = '';
  const done = S.jobs.filter(j => j.status === 'done' && j.result && j.result.out);
  const show = (done.length ? done : S.jobs).slice(0, 8);
  if (!show.length) {
    host.appendChild(el('div', 'muted', '还没有生成记录。写好提示词后点「开始生成」。'));
    return;
  }
  show.forEach((j) => {
    const box = el('div', 'gitem');
    const out = j.result && j.result.out;
    const r = j.request || {};
    if (out) {
      const v = document.createElement('video');
      v.controls = true; v.preload = 'metadata';
      v.src = '/api/file?path=' + encodeURIComponent(out);
      box.appendChild(v);
    } else {
      const ph = el('div', 'ref-thumb');
      ph.style.height = '132px'; ph.style.width = '100%'; ph.style.borderRadius = '0';
      ph.textContent = j.stage_zh || j.status;
      box.appendChild(ph);
    }
    const body = el('div', 'gbody');
    body.appendChild(el('div', 'gname', r.prompt_excerpt || j.id));
    const meta = el('div', 'gmeta');
    meta.appendChild(el('span', null, j.stage_zh || j.status));
    meta.appendChild(el('span', null, fmtDur(j.elapsed_s)));
    if (r.width) meta.appendChild(el('span', null, r.width + '×' + r.height + ' ' + r.steps + '步'));
    if (j.peak_vram_gib) meta.appendChild(el('span', null, '峰值 ' + j.peak_vram_gib + ' GiB'));
    body.appendChild(meta);
    const ops = el('div', 'gops');
    if (out) {
      const a = el('button', null, '详情 / 播放');
      a.onclick = () => openResult(j.id);
      ops.appendChild(a);
    }
    const cp = el('button', null, '复制路径');
    cp.onclick = () => api('/api/jobs/' + j.id + '/result').then((res) => {
      if (res.output) { navigator.clipboard.writeText(res.output.windows); toast('已复制', 'ok', 3000); }
    }).catch(() => { });
    ops.appendChild(cp);
    body.appendChild(ops);
    box.appendChild(body);
    host.appendChild(box);
  });
}

function renderLibrary() {
  const tb = $('library-table').querySelector('tbody');
  tb.innerHTML = '';
  S.jobs.forEach((j) => {
    const r = j.request || {};
    const tr = document.createElement('tr');
    [fmtClock(j.created),
     (j.result && j.result.out) ? j.result.out.split('/').pop() : '(未产出)',
     (r.width || '?') + '×' + (r.height || '?') + '×' + (r.num_frames || '?') + 'f',
     r.steps || '?', (r.seed === undefined ? '?' : r.seed),
     fmtDur(j.elapsed_s), (j.peak_vram_gib || '--') + ' GiB'
    ].forEach(c => tr.appendChild(el('td', null, c)));
    const td = el('td');
    if (j.status === 'done') {
      const a = el('button', 'ghost-btn sm', '播放');
      a.onclick = () => openResult(j.id);
      td.appendChild(a);
    } else {
      td.appendChild(el('span', 'muted', j.stage_zh || j.status));
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  });
}

/* ------------------------------------------------------------------ 文档 */
async function loadGuideDoc() {
  const txt = await fetch('/assets/guide.md').then(r => r.text()).catch(() => '');
  $('guide-doc').innerHTML = mdToHtml(txt);
}

async function loadAboutDoc() {
  const h = await api('/api/health').catch(() => null);
  const host = $('about-doc');
  if (!h) { host.textContent = '自检失败：无法访问 /api/health'; return; }
  const rows = (h.checks || []).map(c =>
    '<tr><td>' + esc(c.name) + '</td><td>' + (c.ok ? '<span class="badge ok">正常</span>'
      : '<span class="badge err">异常</span>') + '</td><td><code>' + esc(c.detail || '') + '</code></td></tr>').join('');
  host.innerHTML = '<div class="kv">' +
    '<div>仓库根（WSL）</div><div><code>' + esc(h.root) + '</code></div>' +
    '<div>仓库根（Windows）</div><div><code>' + esc(h.repo_windows) + '</code></div>' +
    '<div>后端 Python</div><div>' + esc(h.python) + '</div>' +
    '<div>WSL 主机</div><div>' + esc(h.host) + '</div>' +
    '<div>已运行</div><div>' + fmtDur(h.uptime_s) + '</div></div>' +
    '<h2>检查项</h2><table class="tbl"><tbody>' + rows + '</tbody></table>';
  renderAboutParams();

  const tb = $('spec-table').querySelector('tbody');
  tb.innerHTML = '';
  (S.spec.params || []).forEach((p) => {
    const tr = document.createElement('tr');
    const def = (p.default && typeof p.default === 'object' && p.default.__preset__)
      ? '跟随预设' : JSON.stringify(p.default === undefined ? null : p.default);
    [p.label || p.id, p.group_title || p.group, p.type, def,
     (p.cli || []).join(' '), p.help || ''].forEach(c => tr.appendChild(el('td', null, c)));
    tb.appendChild(tr);
  });
}

/* 「环境自检」页里的完整参数手册：与卡片上的详细说明同源 */
function renderAboutParams() {
  const host = $('about-params');
  if (!host) return;
  host.innerHTML = '';
  const spec = S.spec || {};
  const groups = [];
  const seen = {};
  (spec.params || []).forEach((p) => {
    const g = p.group_title || p.group || '其它';
    if (!seen[g]) { seen[g] = { title: g, items: [] }; groups.push(seen[g]); }
    seen[g].items.push(p);
  });
  const T = String.fromCharCode(96);
  groups.forEach((g) => {
    const h = el('h3', null, g.title);
    host.appendChild(h);
    g.items.forEach((p) => {
      const box = el('div', 'about-param');
      const head = el('div', 'about-param-head');
      head.innerHTML = '<b>' + esc(p.label || p.id) + '</b> <code>' +
        esc((p.cli || []).join(' ') || p.id) + '</code>';
      box.appendChild(head);
      const cl = constantLine(p);
      if (cl) box.appendChild(el('div', 'doc-const', cl));
      if (p.help) { const d = el('div', 'doc-detail'); d.innerHTML = richText(p.help); box.appendChild(d); }
      if (p.detail) { const d = el('div', 'doc-detail'); d.innerHTML = richText(p.detail); box.appendChild(d); }
      if ((p.tips || []).length) {
        const ul = el('ul', 'doc-ul');
        p.tips.forEach(t => { const li = el('li'); li.innerHTML = richText(t); ul.appendChild(li); });
        box.appendChild(ul);
      }
      if ((p.risks || []).length) {
        box.appendChild(el('div', 'doc-sub warn', '风险'));
        const ul = el('ul', 'doc-ul risks');
        p.risks.forEach(t => { const li = el('li'); li.innerHTML = richText(t); ul.appendChild(li); });
        box.appendChild(ul);
      }
      if ((p.ranges || []).length) {
        const tbl = el('table', 'doc-tbl');
        const tb = document.createElement('tbody');
        p.ranges.forEach(r => {
          const tr = document.createElement('tr');
          tr.appendChild(el('td', 'rng', String(r[0])));
          const td = el('td'); td.innerHTML = richText(String(r[1]));
          tr.appendChild(td); tb.appendChild(tr);
        });
        tbl.appendChild(tb); box.appendChild(tbl);
      }
      host.appendChild(box);
    });
  });
  ((spec.general_notes) || []).forEach((n) => {
    const h = el('h3', null, n.title);
    host.appendChild(h);
    const ul = el('ul', 'doc-ul');
    (n.lines || []).forEach((ln) => { const li = el('li'); li.innerHTML = richText(ln); ul.appendChild(li); });
    host.appendChild(ul);
  });
}

/* 极简 markdown 渲染：标题 / 列表 / 表格 / 代码块 / 粗体 / 链接 */
function mdToHtml(md) {
  if (!md) return '<p class="muted">（文档为空）</p>';
  md = md.replace(/^---\n[\s\S]*?\n---\n/, '');
  const lines = md.split('\n');
  const out = [];
  let inCode = false, inTable = false, list = null;
  const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
  const closeTable = () => { if (inTable) { out.push('</table>'); inTable = false; } };
  const BT3 = String.fromCharCode(96, 96, 96);
  const BT1 = String.fromCharCode(96);
  lines.forEach((ln) => {
    if (ln.trim().indexOf(BT3) === 0) {
      closeList(); closeTable();
      out.push(inCode ? '</pre>' : '<pre>');
      inCode = !inCode; return;
    }
    if (inCode) { out.push(esc(ln)); return; }
    if (/^\s*\|/.test(ln)) {
      closeList();
      const cells = ln.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
      if (!inTable) {
        out.push('<table><tr>' + cells.map(c => '<th>' + inlineMd(c) + '</th>').join('') + '</tr>');
        inTable = true; return;
      }
      if (cells.every(c => /^-{2,}$/.test(c) || c === '')) return;
      out.push('<tr>' + cells.map(c => '<td>' + inlineMd(c) + '</td>').join('') + '</tr>');
      return;
    }
    closeTable();
    const h = /^(#{1,4})\s+(.*)$/.exec(ln);
    if (h) { closeList(); const n = h[1].length; out.push('<h' + n + '>' + inlineMd(h[2]) + '</h' + n + '>'); return; }
    const li = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(ln);
    if (li) {
      const want = /^\s*\d+\./.test(ln) ? 'ol' : 'ul';
      if (list !== want) { closeList(); out.push('<' + want + '>'); list = want; }
      out.push('<li>' + inlineMd(li[1]) + '</li>'); return;
    }
    closeList();
    if (/^\s*>\s?/.test(ln)) { out.push('<blockquote>' + inlineMd(ln.replace(/^\s*>\s?/, '')) + '</blockquote>'); return; }
    if (!ln.trim()) return;
    // 行内代码：用拼出来的反引号正则，避免与外层模板冲突
    const re = new RegExp(BT1 + '([^' + BT1 + ']+)' + BT1, 'g');
    out.push('<p>' + inlineMd(ln).replace(re, '<code>$1</code>') + '</p>');
  });
  closeList(); closeTable();
  return out.join('\n');
}

/* ------------------------------------------------------------------ 本地草稿 */
function saveSession() {
  try {
    localStorage.setItem('h3ui.session', JSON.stringify({
      prompt: $('prompt-input').value, values: S.values,
      refs: S.refs.map(r => ({ kind: r.kind, path: r.path, name: r.name })), ts: Date.now(),
    }));
  } catch (e) { /* 隐私模式等场景忽略 */ }
}
setInterval(saveSession, 10000);

/* 前端异常也进日志面板：用户看到的报错和页面上的日志能对上，不用去猜。 */
window.addEventListener('error', (e) => {
  const msg = '[前端异常] ' + (e.message || e.type) +
    (e.filename ? ' @ ' + String(e.filename).split('/').pop() + ':' + e.lineno : '');
  appendLog({ level: 'error', source: 'ui', msg: msg });
  clientLog('error', msg, { event: 'ui.js_error', stack: e.error && e.error.stack ? String(e.error.stack).slice(0, 1500) : '' });
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  const msg = '[前端 Promise 未处理] ' + ((r && (r.message || r)) || 'unknown');
  appendLog({ level: 'error', source: 'ui', msg: msg });
  clientLog('error', msg, { event: 'ui.unhandled_rejection', stack: r && r.stack ? String(r.stack).slice(0, 1500) : '' });
});

document.addEventListener('DOMContentLoaded', boot);
