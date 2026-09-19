/* webui.tools.test_prompt_flow -- 「优化出来的英文提示词到底有没有进流水线」的静态自检。
 *
 * 背景：buildSubmitBody 曾经直接读输入框，于是「✦ 优化提示词」生成的英文只停在页面上，
 * 真正提交给 run_h3.sh 的却是输入框里的中文原文 —— 页面显示的和实际生成的是两份东西，
 * 而且页面上完全看不出来。这个脚本把这条链路钉住：
 *   1. 提交体必须取自 effectivePrompt()，不能再直接读输入框；
 *   2. effectivePrompt() 的四条分支（新鲜 / 输入框改过 / 选了中文 / 没优化过）行为正确；
 *   3. 输入框下面必须有可见的「本次生成使用哪一份」提示元素。
 *
 * 跑法： node webui/tools/test_prompt_flow.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const js = fs.readFileSync(path.join(root, 'webui/web/assets/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'webui/web/index.html'), 'utf8');

let bad = 0;
const ok = (m) => console.log('ok  ' + m);
const no = (m) => { bad++; console.log('x ' + m); };

/* ---- 1. 提交路径 ---------------------------------------------------------- */
const bodyStart = js.indexOf('function buildSubmitBody');
const bodyEnd = js.indexOf('async function submitJob');
const body = bodyStart >= 0 && bodyEnd > bodyStart ? js.slice(bodyStart, bodyEnd) : '';
/* prompt / prompt_zh / prompt_source 必须来自**同一次** effectivePrompt()。
   允许写成 const eff = effectivePrompt(); prompt: eff.text（作品库恢复也要用同一个 eff.zh），
   但不允许任何一段绕过它去读输入框 —— 那正是「页面显示的」和「实际提交的」错开的老 bug。 */
const effVar = body.match(/const\s+(\w+)\s*=\s*effectivePrompt\(\)/);
const fromEff = (field) => {
  const direct = new RegExp(field + ':\\s*effectivePrompt\\(\\)\\.\\w+').test(body);
  const viaVar = effVar && new RegExp(field + ':\\s*' + effVar[1] + '\\.\\w+').test(body);
  return direct || viaVar;
};
if (!body) no('找不到 buildSubmitBody');
else if (!fromEff('prompt')) {
  no('buildSubmitBody 没有用 effectivePrompt() 取提示词，优化出来的英文不会进流水线');
} else if (/prompt:\s*\$\('prompt-input'\)\.value/.test(body)) {
  no('buildSubmitBody 仍在直接读输入框（会和页面显示的那份错开）');
} else if (!fromEff('prompt_zh') || !fromEff('prompt_source')) {
  no('buildSubmitBody 的 prompt_zh / prompt_source 没有和 prompt 取自同一次 effectivePrompt()' +
     '（作品库恢复时中文原文会与英文错位）');
} else ok('提交体的 prompt / prompt_zh / prompt_source 来自同一次 effectivePrompt()');

/* 优化时必须记下「这段英文是给哪份中文写的」 */
if (!/source:\s*chinese/.test(js)) no('optimize() 没有记录 source（无法判断优化结果是否已失效）');
else ok('optimize() 记录了 source = 中文原文');

/* 「用中文原文重来」必须真的能把提交切回中文 */
if (!/S\.optimize\.useZh = true/.test(js)) no('「用中文原文重来」没有把 useZh 打开');
else ok('「用中文原文重来」会切回中文原文');

/* ---- 2. effectivePrompt 的四条分支（不用浏览器） ------------------------- */
try {
  const i0 = js.indexOf('function effectivePrompt');
  const i1 = js.indexOf('function updatePromptUse');
  if (i0 < 0 || i1 < 0) throw new Error('找不到 effectivePrompt');
  const S = { optimize: { en: '', source: '', useZh: false } };
  let textarea = '';
  const $ = () => ({ value: textarea });
  const effectivePrompt = new Function('$', 'S', js.slice(i0, i1) + '; return effectivePrompt;')($, S);

  const EN = 'integrated_multimodal_description: [Shot 1] ...';
  const cases = [
    ['新鲜：优化后没动输入框 -> 用英文', () => {
      textarea = '雨夜的街道'; S.optimize = { en: EN, source: '雨夜的街道', useZh: false };
      const e = effectivePrompt();
      return e.source === 'en' && e.text === EN && e.stale === false;
    }],
    ['输入框改过 -> 回落到中文并标 stale', () => {
      textarea = '换一个：白天的海滩'; S.optimize = { en: EN, source: '雨夜的街道', useZh: false };
      const e = effectivePrompt();
      return e.source === 'zh' && e.text === '换一个：白天的海滩' && e.stale === true;
    }],
    ['点了「用中文原文重来」-> 用中文（保留英文供查看）', () => {
      textarea = '雨夜的街道'; S.optimize = { en: EN, source: '雨夜的街道', useZh: true };
      const e = effectivePrompt();
      return e.source === 'zh' && e.text === '雨夜的街道' && e.stale === false;
    }],
    ['没优化过 -> 用中文', () => {
      textarea = '随便写点'; S.optimize = { en: '', source: '', useZh: false };
      const e = effectivePrompt();
      return e.source === 'zh' && e.text === '随便写点';
    }],
    ['输入框被清空但英文还在 -> 仍用英文（英文才是那份提示词）', () => {
      textarea = ''; S.optimize = { en: EN, source: '雨夜的街道', useZh: false };
      const e = effectivePrompt();
      return e.source === 'en' && e.text === EN;
    }],
  ];
  cases.forEach(([name, fn]) => {
    let pass = false;
    try { pass = !!fn(); } catch (e) { pass = false; }
    if (pass) ok('effectivePrompt：' + name); else no('effectivePrompt：' + name);
  });
} catch (e) { no('effectivePrompt 测试失败：' + e.message); }

/* ---- 3. 页面上必须能看见「用哪一份」 ------------------------------------- */
if (!/id="prompt-use"/.test(html)) no('index.html 缺少 #prompt-use（用户看不出用的是哪一份提示词）');
else if (!/\$\('prompt-use'\)/.test(js)) no('app.js 没有更新 #prompt-use');
else if (!/addEventListener\('input', updatePromptUse\)/.test(js)) {
  no('输入框没有监听 input：优化后改字不会把「已失效」显示出来');
} else ok('#prompt-use 一直在，且输入框改动会即时反映');

console.log(bad ? '\n有 ' + bad + ' 项不合格' : '\n全部通过');
process.exit(bad ? 1 : 0);
