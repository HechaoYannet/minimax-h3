/* webui.tools.test_disk_ui -- 网盘页签的静态自检（node，不需要浏览器）。
 *
 * 前端没有构建步骤，改完刷新就是最终形态 —— 代价是「JS 里用 $('xxx') 取的 id 在 HTML 里
 * 根本不存在」这类错要到运行时才发现。这个脚本把 app.js 里出现的所有 id 取值
 * 与 index.html 里的 id 对一遍，另外检查网盘页签的关键约定。
 *
 * 跑法： node webui/tools/test_disk_ui.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..', '..');
const html = fs.readFileSync(path.join(root, 'webui/web/index.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'webui/web/assets/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'webui/web/assets/app.css'), 'utf8');

const htmlIds = new Set([...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]));
const jsIds = new Set([...js.matchAll(/\$\('([^']+)'\)/g)].map(m => m[1]));

let bad = 0;
// 运行时才出现在 DOM 里的 id：要么是脚本自己 createElement 出来的，要么前缀就说明是动态的
const DYNAMIC_IDS = new Set(['dropzone']);
const dynamic = (id) => DYNAMIC_IDS.has(id) || id.startsWith('dtask-');
const missing = [...jsIds].filter(id => !htmlIds.has(id) && !dynamic(id));
if (missing.length) { bad++; console.log('x JS 引用了 HTML 里不存在的 id：' + missing.join(', ')); }
else console.log('ok  JS 里引用的 id 都能在 index.html 找到（' + jsIds.size + ' 个）');

/* 动态创建的 id（任务卡里的日志面板）单独确认 */
if (!/dtask-log-/.test(js)) { bad++; console.log('x 缺少动态任务卡日志面板 id'); }
else console.log('ok  任务卡日志面板 id 是动态生成的');

/* 账号徽章：初始必须是「加载中…」，且渲染逻辑要区分「没查过」和「未授权」 */
const badgeInit = (html.match(/id="disk-auth-badge">([^<]*)</) || [])[1];
if (badgeInit !== '加载中…') { bad++; console.log('x 账号徽章初始文案应为「加载中…」，实际=' + JSON.stringify(badgeInit)); }
else console.log('ok  账号徽章初始是「加载中…」（不预设结论）');
if (!/auth_known !== false/.test(js)) { bad++; console.log('x 渲染没有区分「没查过」和「未授权」'); }
else console.log('ok  渲染区分 auth_known（没查过 -> 加载中，查过才下结论）');

/* 网盘页签必须在导航里，并且有对应 section */
const nav = /data-view="disk"/.test(html);
const section = /id="view-disk"/.test(html);
if (!nav || !section) { bad++; console.log('x 导航项或 view-disk 缺失'); }
else console.log('ok  导航项与 view-disk 都在');

/* selectView 必须把 disk 列进去，否则切过去其它视图不会隐藏 */
if (!/'create', 'library', 'disk'/.test(js)) { bad++; console.log('x selectView 没带 disk'); }
else console.log('ok  selectView 覆盖 disk');

/* 关键控件必须都绑了事件，不然按钮点了没反应 */
const wired = ['btn-disk-refresh', 'btn-disk-login', 'btn-disk-login-token', 'btn-disk-publish',
  'btn-disk-tasks-refresh', 'btn-disk-root', 'btn-disk-up', 'btn-disk-search'];
const notWired = wired.filter(id => !new RegExp("\\$\\('" + id + "'\\)\\.onclick").test(js));
if (notWired.length) { bad++; console.log('x 这些控件没有绑定 onclick：' + notWired.join(', ')); }
else console.log('ok  网盘页签的按钮全部绑定了事件（' + wired.length + ' 个）');

/* 产物弹窗里那个入口：网络慢时的出口不能丢 */
if (!/useJobForDisk/.test(js)) { bad++; console.log('x 缺少「上传到网盘」入口'); }
else console.log('ok  生成结果弹窗里有「加密打包上传到网盘」入口');

/* CSS 里任务卡/表单类名要对得上 */
const cssNeed = ['disk-form', 'dtask', 'disk-file-actions'];
const cssMissing = cssNeed.filter(c => !css.includes('.' + c));
if (cssMissing.length) { bad++; console.log('x app.css 缺少：' + cssMissing.join(', ')); }
else console.log('ok  样式类名齐备');

/* 后端契约：前端调的每个 /api/disk/* 都得在后端路由里有 */
const srv = fs.readFileSync(path.join(root, 'webui/backend/server.py'), 'utf8');
const calls = new Set([...js.matchAll(/['"](\/api\/disk\/[a-z/]+)/g)].map(m => m[1]));
const badCalls = [...calls].filter(p => {
  const seg = p.replace('/api/disk/', '').split('/')[0];
  return !new RegExp('"disk", "' + seg + '"').test(srv);
});
if (badCalls.length) { bad++; console.log('x 前端调了后端没有的端点：' + badCalls.join(', ')); }
else console.log('ok  前端调用的网盘端点后端都有（' + calls.size + ' 个）');

/* 账号卡片的渲染：get-user-info 是嵌套结构，拍平逻辑单独测一下（不用起浏览器） */
try {
  const i0 = js.indexOf('const DISK_VIP_ZH');
  const i1 = js.indexOf('async function diskLogin');
  if (i0 < 0 || i1 < 0) throw new Error('找不到账号拍平函数');
  const fmtBytes = (b) => {
    const u = ['B', 'KiB', 'MiB', 'GiB']; let i = 0, v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 2 : 1) + ' ' + u[i];
  };
  const flatten = new Function('fmtBytes', js.slice(i0, i1) + '; return flattenDiskAccount;')(fmtBytes);
  const rows = flatten({ vipInfo: { vipType: 'NORMAL', used: 394756543, capacity: 10737418240 },
                         userInfo: { nickname: '某人', userId: 'abc' } });
  const want = [['昵称', '某人'], ['账号 ID', 'abc'], ['会员类型', '普通用户'],
                ['总容量', '10.0 GiB'], ['已用', '376.5 MiB']];
  const missing = want.filter(w => !rows.some(r => r[0] === w[0] && String(r[1]) === w[1]));
  if (missing.length) { bad++; console.log('x 账号卡片漏了/译错了：' + JSON.stringify(missing) + ' 实际=' + JSON.stringify(rows)); }
  else console.log('ok  账号卡片能把嵌套的 vipInfo/userInfo 拍平并翻译（' + rows.length + ' 行）');
} catch (e) { bad++; console.log('x 账号卡片测试失败：' + e.message); }

console.log(bad ? '\n有 ' + bad + ' 项不合格' : '\n全部通过');
process.exit(bad ? 1 : 0);
