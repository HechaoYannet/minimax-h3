/* 用最小 DOM 桩验证 app.js 里「参数文档」相关函数的运行期正确性：
   从 app.js 抽出这几段函数源码注入桩环境，再用真实的 /api/config 载荷跑一遍。 */
const fs = require('fs');
const http = require('http');

const src = fs.readFileSync(process.argv[2], 'utf8');

function extract(name) {
  const re = new RegExp('(?:^|\\n)(?:function\\s+' + name + '\\b[\\s\\S]*?\\n\\}|const\\s+' + name + '\\s*=[\\s\\S]*?\\n\\};)');
  const m = src.match(re);
  if (!m) throw new Error('cannot extract ' + name);
  return m[0];
}

const helpers = ['el', 'esc', 'constantLine', 'richText', 'paramDocPanel', 'fmtUnit']
  .map(extract).join('\n');

const UNIT_ZH = { px: '像素' };
const S = { spec: null };

const byId = {};
function makeEl(tag) {
  const node = {
    tagName: String(tag).toUpperCase(), children: [], style: {}, dataset: {},
    _text: '', _html: '', className: '', id: '', type: '', title: '', onclick: null,
    appendChild(c) { this.children.push(c); if (c && c.id) byId[c.id] = c; return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    // 真的 querySelector 会找到子节点；桩里至少要让 .caret 查得到，
    // 否则 toggle.onclick 的 caret 更新会在这里断掉（浏览器里是好的，桩里也得像）
    querySelector(sel) {
      if (sel === '.caret') {
        if (!this._caret) { this._caret = makeEl('span'); this._caret.className = 'caret'; }
        return this._caret;
      }
      return null;
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html || ''; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
  };
  Object.defineProperty(node, 'id', {
    get() { return this._id || ''; },
    set(v) { this._id = v; if (v) byId[v] = this; },
  });
  return node;
}
// 真实的 getElementById 会按 id 找到节点；桩也必须这样，否则 onclick 里查不到展开区
const documentStub = { createElement: makeEl, getElementById: (id) => byId[id] || null };

const factory = new Function('document', 'UNIT_ZH', 'S',
  helpers + '\nreturn {constantLine, richText, paramDocPanel, fmtUnit};');
const api = factory(documentStub, UNIT_ZH, S);

http.get('http://127.0.0.1:8765/api/config', (res) => {
  let body = '';
  res.on('data', (c) => body += c);
  res.on('end', () => {
    const spec = JSON.parse(body).spec;
    S.spec = spec;
    const byId = {};
    spec.params.forEach(p => { byId[p.id] = p; });

    let problems = 0;
    const check = (cond, msg) => { if (!cond) { console.log('  X ' + msg); problems++; } };
    const BT = String.fromCharCode(96);

    console.log('参数数:', spec.params.length, '| 有 detail 的:', spec.params.filter(p => p.detail).length);

    const c1 = api.constantLine(byId.vram_limit);
    console.log('vram_limit 常量行:', c1);
    check(c1.includes('默认'), '常量行应含默认值');
    check(c1.includes('范围'), '常量行应含范围');
    const c2 = api.constantLine(byId.width);
    console.log('width 常量行:', c2);
    check(c2.includes('跟随预设'), 'width 默认应标注跟随预设');
    const c3 = api.constantLine(byId.scheduler);
    console.log('scheduler 常量行:', c3);
    check(c3.includes('可选'), 'scheduler 应显示可选值');
    console.log('no_tiled 常量行:', api.constantLine(byId.no_tiled));

    const r1 = api.richText('**重点** 与 ' + BT + 'code' + BT + ' 以及 <script>');
    console.log('richText:', r1);
    check(r1.includes('<b>重点</b>'), '加粗未渲染');
    check(r1.includes('<code>code</code>'), '行内代码未渲染');
    check(!r1.includes('<script>'), 'HTML 未转义！');

    let withTips = 0, withRisks = 0, withRanges = 0;
    spec.params.forEach((p) => {
      const panel = api.paramDocPanel(p);
      check(panel.children.length >= 2, p.id + ' 面板结构不完整');
      const bodyNode = panel.children[1];
      check(bodyNode.id === 'pdoc-' + p.id, p.id + ' 展开区 id 不对');
      if (p.detail) check(bodyNode.children.some(c => c.className === 'doc-detail'), p.id + ' 缺 detail');
      if ((p.tips || []).length) { withTips++; check(bodyNode.children.some(c => c.className === 'doc-ul'), p.id + ' 缺 tips'); }
      if ((p.risks || []).length) { withRisks++; check(bodyNode.children.some(c => c.className === 'doc-ul risks'), p.id + ' 缺 risks'); }
      if ((p.ranges || []).length) { withRanges++; check(bodyNode.children.some(c => c.className === 'doc-tbl'), p.id + ' 缺 ranges'); }
    });
    const p0 = api.paramDocPanel(byId.steps);
    const toggle = p0.children[0];
    check(toggle.className === 'doc-toggle', '折叠按钮 class 不对');
    check(String(toggle.innerHTML).includes('详细说明'), '折叠按钮文案不对');
    toggle.onclick();
    check(p0.children[1].style.display === 'block', '点击后未展开');
    toggle.onclick();
    check(p0.children[1].style.display === 'none', '再点未收起');

    console.log('带 tips 的参数:', withTips, '| 带 risks 的:', withRisks, '| 带取值语义表的:', withRanges);
    console.log(problems ? ('结果：' + problems + ' 处问题') : '结果：全部通过');
    process.exit(problems ? 1 : 0);
  });
}).on('error', (e) => { console.log('无法访问后端:', e.message); process.exit(2); });
