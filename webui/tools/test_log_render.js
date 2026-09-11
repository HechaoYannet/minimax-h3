/* test_log_render.js -- 用最小 DOM 桩验证 app.js 里「运行日志」渲染函数的运行期正确性。

   与 test_docs_render.js 同一套路：从 app.js 抽出函数源码注入桩环境，
   喂真实记录形状，检查时间/来源/正文/附加字段分别落在正确的位置。
   纯离线，不连后端：node webui/tools/test_log_render.js webui/web/assets/app.js
*/
const fs = require('fs');

const src = fs.readFileSync(process.argv[2], 'utf8');

function extract(name) {
  const re = new RegExp('(?:^|\\n)(?:function\\s+' + name + '\\b[\\s\\S]*?\\n\\}|' +
                        'const\\s+' + name + '\\s*=[\\s\\S]*?\\n\\};)');
  const m = src.match(re);
  if (!m) throw new Error('cannot extract ' + name);
  return m[0];
}

const keysConst = src.match(/const LOG_KEYS = \[[^\]]*\];/);
if (!keysConst) throw new Error('cannot extract LOG_KEYS');

const byId = {};
function makeEl(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], style: {}, _text: '', _html: '',
    className: '', id: '', title: '',
    appendChild(c) { this.children.push(c); if (c && c.id) byId[c.id] = c; return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = String(v); }, get textContent() { return this._text; },
  };
  return node;
}
const documentStub = { createElement: makeEl, getElementById: (id) => byId[id] || null };

const helpers = [
  'const $ = (id) => document.getElementById(id);',
  keysConst[0],
  extract('el'), extract('shortVal'), extract('logLine'), extract('updateLogStats'),
].join('\n');

const factory = new Function('document',
  helpers + '\nreturn { logLine, shortVal, updateLogStats, LOG_KEYS };');
const api = factory(documentStub);

let problems = 0;
const check = (cond, msg) => { if (!cond) { console.log('  X ' + msg); problems++; } else { console.log('  ok ' + msg); } };

console.log('LOG_KEYS =', api.LOG_KEYS.join(','));

// --- 一条 llm 记录：时间 / 来源 / 正文 / 附加字段各就各位 ---
const rec = {
  seq: 12, t: 1757600000.1, iso: '2026-09-11 20:57:25.499', level: 'info', source: 'llm',
  event: 'llm.request', msg: 'promptopt: 请求 DeepSeek', run: 'r1', model: 'deepseek-flash',
  system_chars: 4651, user_chars: 364, query: { level: 'info' },
};
const row = api.logLine(rec);
check(row.className === 'lline l-info src-llm', '根节点 class 含等级与来源');
check(row.children.length === 4, '4 个子节点：时间 / 来源 / 正文 / 附加');
check(row.children[0].textContent === '20:57:25.499', '时间取到毫秒');
check(row.children[1].textContent === 'llm', '来源标签');
check(row.children[2].textContent === 'promptopt: 请求 DeepSeek', '正文');
const extra = row.children[3].textContent;
check(extra.indexOf('run=r1') >= 0, '附加字段 run');
check(extra.indexOf('system_chars=4651') >= 0, '附加字段 system_chars');
check(extra.indexOf('iso=') < 0 && extra.indexOf('msg=') < 0, '核心字段不重复进附加区');
check(String(row.children[3].title).indexOf('"run"') >= 0, '附加字段 title 是可读 JSON');

// --- 无附加字段时不应产生空尾巴 ---
const bare = api.logLine({ level: 'warn', source: 'job', msg: '作业失败', iso: '2026-09-11 21:00:00.000' });
check(bare.children.length === 3, '没有附加字段就只渲染 3 段');

// --- 缺 iso 的旧记录（兼容老格式）不能崩 ---
const old = api.logLine({ level: 'error', msg: 'boom', t: 1757600000 });
check(old.children.length >= 2, '缺 iso 也能渲染');

// --- shortVal 截断 ---
check(api.shortVal({ a: 1 }).indexOf('"a"') >= 0, '对象转 JSON');
check(api.shortVal('x'.repeat(300)).length <= 120, '过长字符串被截断');
check(api.shortVal('x'.repeat(300)).slice(-1) === '…', '截断带省略号');

// --- updateLogStats 文案含关键计数 ---
const box = makeEl('div');
byId['log-stats'] = box;
api.updateLogStats({
  level: 'info', capacity: 2000, retained: 42, evicted: 3,
  counts: { debug: 1, info: 2, warn: 3, error: 4 },
  files: [{ name: 'webui.jsonl' }], file_bytes: 4096, file_error: null,
}, { runs: 7 });
const text = box.textContent;
check(text.indexOf('等级 info') >= 0, '统计含等级');
check(text.indexOf('42/2000') >= 0, '统计含内存占用');
check(text.indexOf('error 4') >= 0, '统计含错误数');
check(text.indexOf('LLM 记录 7 份') >= 0, '统计含 LLM 记录数');
api.updateLogStats({ level: 'debug', retained: 1, capacity: 10, evicted: 0,
  counts: {}, files: [], file_bytes: 0, file_error: 'disk full' }, null);
check(box.textContent.indexOf('disk full') >= 0, '写盘错误要显示出来');

console.log(problems ? ('结果：' + problems + ' 处问题') : '结果：全部通过');
process.exit(problems ? 1 : 0);
