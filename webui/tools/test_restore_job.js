/* webui.tools.test_restore_job -- 「从作品库恢复完整会话与参数」的静态 + 行为自检。
 *
 * 为什么值得单独钉住：
 *   - 作品库里一条失败作业（比如爆显存）以前只能「看」，要重试只能凭记忆把提示词、
 *     参考素材和几十个参数再抄一遍；恢复链路一旦断在某一环，用户看到的是「按钮在、
 *     点了没反应」或者「参数回来了但提交的还是旧英文」——两者都不会报错，只会默默坑人。
 *   - 后端不存上下文时，恢复出来的只是一份英文孤本。所以这里同时检查提交时确实把
 *     中文原文 / 优化结果 / 模式带进了作业记录。
 *   - 作品库那两列（产出文件名 / 失败原因）可能非常长，必须默认折叠：否则一个长路径
 *     就能把整张表撑变形，把别的列全挤出去。
 *
 * 跑法： node webui/tools/test_restore_job.js
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

/* ---- 1. 界面接线：入口、提示条、作品库列 ------------------------------- */
if (!/id="restore-banner"/.test(html)) no('index.html 缺少 #restore-banner（恢复后没有提示条）');
else if (!/id="restore-banner"[^>]*hidden/.test(html)) no('#restore-banner 默认没有 hidden');
else ok('#restore-banner 存在且默认隐藏');

if (!/id="lib-only-failed"/.test(html)) no('index.html 缺少 #lib-only-failed（无法只看失败）');
else ok('作品库有「只看未完成 / 失败」开关');

if (!/id="btn-library-refresh"/.test(html)) no('index.html 缺少 #btn-library-refresh');
else ok('作品库有刷新按钮');

if (!/<th>状态<\/th>/.test(html)) no('作品库表头没有「状态」列，失败原因看不到');
else ok('作品库表头含「状态」列');

/* ---- 2. 函数齐备 ------------------------------------------------------- */
const fns = ['specDefaults', 'valuesFromJobRequest', 'optimizeFromJobRequest',
  'refsFromJobRequest', 'sessionFromJob', 'restoreJob', 'renderRestoreBanner',
  'openJobDetail', 'jobStatusBadge', 'currentModeTab'];
const missing = fns.filter(f => !new RegExp('function ' + f + '\\b').test(js));
if (missing.length) no('缺少恢复相关函数：' + missing.join(', '));
else ok('恢复相关函数齐备（' + fns.length + ' 个）');

/* ---- 3. 按钮真的接到恢复上（而不是只画了个按钮） ----------------------- */
if (!/rs\.onclick = \(\) => restoreJob\(j\.id\)/.test(js)) no('作品库「恢复参数」没有接到 restoreJob');
else ok('作品库「恢复参数」接到 restoreJob');

if (!/onOk[\s\S]{0,40}restoreJob\(job\.id\)/.test(js) && !/restoreJob\(job\.id\)/.test(js)) {
  no('作业详情弹窗没有恢复入口');
} else ok('作业详情弹窗带恢复入口');

if (!/\$\('lib-only-failed'\)\.onchange/.test(js)) no('「只看未完成 / 失败」没有绑定');
else ok('「只看未完成 / 失败」已绑定');

if (!/\$\('btn-library-refresh'\)\.onclick = refreshJobs/.test(js)) no('作品库刷新按钮没接 refreshJobs');
else ok('作品库刷新按钮接到 refreshJobs');

if (!/renderRestoreBanner\(s\)/.test(js)) no('applySession 没有刷新恢复提示条');
else ok('切换/恢复对话都会刷新提示条');

/* ---- 4. 提交时把「完整会话」带进作业记录 ------------------------------- */
if (!/prompt_zh: eff\.zh/.test(js)) no('提交体没有带 prompt_zh（中文原文会丢）');
else ok('提交体带 prompt_zh（中文原文）');
if (!/prompt_source: eff\.source/.test(js)) no('提交体没有带 prompt_source');
else ok('提交体带 prompt_source');
if (!/optimize: \{ en: S\.optimize\.en/.test(js)) no('提交体没有带 optimize（英文优化结果会丢）');
else ok('提交体带 optimize（英文/回译/结构说明）');
if (!/mode_tab: currentModeTab\(\)/.test(js)) no('提交体没有带 mode_tab（编辑页签会丢）');
else ok('提交体带 mode_tab（生成 / 编辑页签）');

/* ---- 5. CSS ------------------------------------------------------------ */
if (!/\.restore-banner\{/.test(css)) no('CSS 缺少 .restore-banner');
else if (!/\.restore-banner\.err/.test(css)) no('CSS 缺少 .restore-banner.err（恢复的失败作业与普通提示同色）');
else ok('恢复提示条有正常 / 失败两种配色');
if (!/\.lib-toolbar\{/.test(css)) no('CSS 缺少 .lib-toolbar');
else ok('作品库工具栏有样式');
if (!/\.cell-err \.clamp-text\{/.test(css)) no('CSS 缺少 .cell-err .clamp-text（失败原因会被撑爆表格）');
else ok('失败原因单元格有折叠样式');

/* ---- 5b. 长文件名 / 长报错默认折叠：不折叠会把整张表撑变形 ------------ */
if (!/function collapsible\(/.test(js)) no('缺少 collapsible（长文件名 / 报错没有折叠）');
else ok('长文件名 / 报错走 collapsible 折叠');
if (!/collapsible\(j\.error, 'cell-err'/.test(js)) no('失败原因没有走折叠');
else ok('失败原因默认折叠');
if (!/collapsible\(j\.result\.out[^;]*'cell-file'/.test(js)) no('产出文件名没有走折叠');
else ok('产出文件名默认折叠');
if (!/\.clamp-text\{[^}]*text-overflow:ellipsis/.test(css)) no('CSS 缺少 .clamp-text 的省略号（折叠态没生效）');
else ok('折叠态：一行 + 省略号');
if (!/\.clamp-text\{[^}]*max-width:70px/.test(css)) no('CSS 折叠态不是 70px（默认露出的文字量应为原来的 1/4）');
else ok('折叠态只露 70px（约为原 280px 的四分之一文字量）');
if (!/\.clamp\.open \.clamp-text\{[^}]*max-width:280px/.test(css)) no('CSS 展开态没有放宽宽度（全文会被 70px 硬折行）');
else ok('展开态放宽到 280px，全文可读');
if (!/\.clamp\.open \.clamp-text\{[^}]*white-space:normal/.test(css)) no('CSS 缺少 .clamp.open 的换行（展开后仍看不到全文）');
else ok('展开态：允许换行看全文');

/* ---- 6. 行为：作业 request -> 会话与参数 ------------------------------ */
function extract(a, b) {
  const i = js.indexOf(a), j = js.indexOf(b);
  if (i < 0 || j < 0 || j <= i) throw new Error('找不到代码段：' + a);
  return js.slice(i, j);
}
try {
  const srcDefaults = extract('function specDefaults', 'function initValues');
  const srcSessions = extract('function sessUid', 'function loadSessions');
  const srcEff = extract('function effectivePrompt', 'function updatePromptUse');

  const S = {
    cfg: { server: { defaults: { preset: 'draft', seed: 42 } } },
    spec: {
      default_preset: 'draft',
      params: [
        { id: 'preset', default: 'draft' },
        { id: 'width', default: 640 }, { id: 'height', default: 384 },
        { id: 'num_frames', default: 73 }, { id: 'steps', default: 20 },
        { id: 'seed', default: 42 }, { id: 'seconds', default: 3.04 },
        { id: 'vram_limit', default: 4.58 }, { id: 'text_cache', default: true },
      ],
    },
    presets: { draft: { width: 640, height: 384, num_frames: 73, steps: 20 } },
    values: { width: 999, steps: 20 },   // 故意放一个「当前界面上的值」，恢复时不许泄漏
  };
  const f = new Function('S', 'FPS', srcDefaults + srcSessions +
    '; return { specDefaults, valuesFromJobRequest, optimizeFromJobRequest, sessionFromJob };')(S, 24);

  const req = {
    width: 1344, height: 768, num_frames: 192, steps: 40, seed: 7,
    preset: 'max-native', vram_limit: 4.2, dit_onload: 'disk', lora: null,
    refresh_text_cache: false, prompt: 'EN BODY', prompt_zh: '中文原文',
    prompt_source: 'en', mode: 'ref2va', mode_tab: 'edit',
    optimize: { en: 'EN BODY', zh: '回译', notes: 'notes', mode: 'ref2va',
                source: '中文原文', runId: 'llm-1' },
    refs: [{ kind: 'image', path: '/mnt/d/a.png', name: 'a.png' }],
  };

  const v = f.valuesFromJobRequest(req);
  if (v.width === 1344 && v.num_frames === 192 && v.dit_onload === 'disk' &&
      v.vram_limit === 4.2 && v.seconds === 8) {
    ok('valuesFromJobRequest：作业参数覆盖默认值，时长按帧数重算');
  } else no('valuesFromJobRequest 没还原参数：' + JSON.stringify(v));

  if (v.text_cache === true && v.width !== 999) {
    ok('valuesFromJobRequest：没被作业记录的参数回默认，且不粘当前界面值');
  } else no('valuesFromJobRequest 泄漏了当前界面值 / 丢了默认值：text_cache=' + v.text_cache + ' width=' + v.width);

  if (v.preset === '') ok('valuesFromJobRequest：已不存在的预设名不会让下拉框悬空');
  else no('valuesFromJobRequest 对未知预设处理不对：' + JSON.stringify(v.preset));

  const o = f.optimizeFromJobRequest(req);
  if (o.en === 'EN BODY' && o.zh === '回译' && o.source === '中文原文' &&
      o.mode === 'ref2va' && o.runId === 'llm-1') {
    ok('optimizeFromJobRequest：英文/回译/runId 全部还原，source 对齐中文原文');
  } else no('optimizeFromJobRequest 还原不对：' + JSON.stringify(o));

  const old = f.optimizeFromJobRequest({ prompt: 'OLD EN', prompt_source: 'en' });
  if (old.en === 'OLD EN' && old.source === '') ok('optimizeFromJobRequest：老记录（无 optimize）也能退化成英文原文');
  else no('optimizeFromJobRequest 对老记录处理不对：' + JSON.stringify(old));

  const job = { id: 'j1', status: 'failed', stage_zh: '失败', error: 'CUDA out of memory',
                created: '2026-09-19 07:43:25', request: req };
  const s = f.sessionFromJob(job);
  if (s.prompt === '中文原文' && s.mode === 'ref2va' && s.modeTab === 'edit' &&
      s.refs.length === 1 && !!s.refs[0].uid && s.values.width === 1344) {
    ok('sessionFromJob：中文原文/模式/页签/参考素材/参数全部铺回');
  } else no('sessionFromJob 还原不完整：' + JSON.stringify({ p: s.prompt, m: s.mode, t: s.modeTab, n: s.refs.length, w: s.values.width }));

  if (s.restoreInfo && s.restoreInfo.jobId === 'j1' && s.restoreInfo.error === 'CUDA out of memory' &&
      s.restoreInfo.promptSource === 'en') {
    ok('sessionFromJob：带上原作业号 / 失败原因 / 提示词来源（提示条要用）');
  } else no('sessionFromJob 没有带 restoreInfo');

  const oldS = f.sessionFromJob({ id: 'j0', status: 'done', request: { prompt: 'OLD EN' } });
  if (oldS.prompt === 'OLD EN' && oldS.optimize.en === '' && oldS.restoreInfo.promptSource === 'legacy') {
    ok('sessionFromJob：老记录把当时用的那一份放回输入框，且不谎称它是中文原文');
  } else no('sessionFromJob 对老记录处理不对：' + JSON.stringify({ p: oldS.prompt, en: oldS.optimize.en, src: oldS.restoreInfo && oldS.restoreInfo.promptSource }));

  /* 恢复之后提交，用的必须还是那份英文（source 对得上输入框）——这是恢复最容易断的一环 */
  const S2 = { optimize: s.optimize };
  const eff = new Function('S', '$', srcEff + '; return effectivePrompt;')(S2, () => ({ value: s.prompt }));
  const e = eff();
  if (e.source === 'en' && e.text === 'EN BODY') ok('恢复后 effectivePrompt：英文仍是新鲜结果，会重新送进流水线');
  else no('恢复后 effectivePrompt 没有选中英文：' + JSON.stringify(e));

  const stale = new Function('S', '$', srcEff + '; return effectivePrompt;')(
    { optimize: s.optimize }, () => ({ value: '改过的中文' }))();
  if (stale.source === 'zh') ok('恢复后改中文原文：英文正确失效，回落到中文（不悄悄用旧英文）');
  else no('恢复后改输入框，英文没有被判失效：' + JSON.stringify(stale));

  /* 原作业当初用的就是中文（英文在提交时已经失效）：恢复后不能被「洗白」成新鲜英文 */
  const staleReq = { prompt: '中文正文', prompt_zh: '中文正文', prompt_source: 'zh',
                     optimize: { en: 'STALE EN', source: '更早的中文', mode: 't2va' } };
  const staleOpt = f.optimizeFromJobRequest(staleReq);
  const eff2 = new Function('S', '$', srcEff + '; return effectivePrompt;')(
    { optimize: staleOpt }, () => ({ value: '中文正文' }))();
  if (eff2.source === 'zh' && eff2.text === '中文正文') {
    ok('恢复「当初用中文」的作业：失效英文不会被洗白成新鲜结果');
  } else no('恢复把失效英文洗白了：' + JSON.stringify(eff2));
} catch (e) {
  no('恢复行为测试异常：' + e.message);
}


/* ---- 7. 行为：折叠组件（长文本收起 / 展开、短文本不显示按钮） ---------- */
try {
  const srcEl = extract('const el = (tag, cls, text)', 'function toast');
  const srcClamp = extract('const clampOpen = new Set()', 'function renderLibrary');
  let created = 0;
  const make = () => {
    const node = {
      tagName: '', className: '', textContent: '', title: '', hidden: false,
      type: '', children: [], scrollWidth: 0, clientWidth: 0,
      appendChild(c) { node.children.push(c); return c; },
    };
    const set = new Set();
    node.classList = {
      add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
      toggle: (c) => (set.has(c) ? (set.delete(c), false) : (set.add(c), true)),
    };
    created++;
    return node;
  };
  const fakeDoc = { createElement: make };
  const ros = [];
  function FakeRO(cb) { this.cb = cb; this.observe = () => ros.push(cb); }
  const fires = () => ros.forEach(cb => cb());
  const ev = { preventDefault() {}, stopPropagation() {} };

  const f = new Function('document', 'ResizeObserver', srcEl + srcClamp +
    '; return { collapsible, clampOpen };')(fakeDoc, FakeRO);

  const long = f.collapsible('h3_20260919_' + 'x'.repeat(80) + '.mp4', 'cell-file', 'j1:file');
  const ltxt = long.children[0], lbtn = long.children[1];
  ltxt.scrollWidth = 900; ltxt.clientWidth = 240;   // 超长：被截断
  fires();
  if (!lbtn.hidden && lbtn.textContent === '展开' && !long.classList.contains('open')) {
    ok('长文件名默认折叠成一行，并给出「展开」入口');
  } else no('长文件名折叠不对：' + JSON.stringify({ hidden: lbtn.hidden, txt: lbtn.textContent }));

  const short = f.collapsible('a.mp4', 'cell-file', 'j2:file');
  const stxt = short.children[0], sbtn = short.children[1];
  stxt.scrollWidth = 40; stxt.clientWidth = 240;    // 没被截断
  fires();
  if (sbtn.hidden) ok('短文件名不显示多余的展开按钮');
  else no('短文件名也冒出了展开按钮');

  lbtn.onclick(ev);
  if (long.classList.contains('open') && lbtn.textContent === '收起' && f.clampOpen.has('j1:file')) {
    ok('点「展开」换行看全文，并按 key 记住');
  } else no('展开切换不对：' + JSON.stringify({ open: long.classList.contains('open'), txt: lbtn.textContent }));

  const again = f.collapsible('h3_20260919_x.mp4', 'cell-file', 'j1:file');
  if (again.classList.contains('open') && again.children[1].textContent === '收起') {
    ok('列表重绘之后仍然保持展开（不会看一半被折回去）');
  } else no('重绘把展开状态弄丢了');

  lbtn.onclick(ev);
  if (!long.classList.contains('open') && lbtn.textContent === '展开' && !f.clampOpen.has('j1:file')) {
    ok('点「收起」回到一行，并清掉记忆');
  } else no('收起切换不对：' + JSON.stringify({ open: long.classList.contains('open'), txt: lbtn.textContent }));
} catch (e) {
  no('折叠行为测试异常：' + e.message);
}

console.log(bad ? '\n有 ' + bad + ' 项不合格' : '\n全部通过');
process.exit(bad ? 1 : 0);
