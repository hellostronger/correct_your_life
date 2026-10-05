// sparkDual 行内迷你双线图 单测。
// 从 index.html 里切出真实函数体来测（顶层函数以行首 '}' 结束，按行切最省事）。
//
// 跑法（必须在 stock-advisor 目录下，脚本用相对路径读源码）：
//   node _spark_test.js
// 零依赖，不需要起服务；纯构造数据，覆盖缩放/缺口/降级/XSS。
const fs = require('fs');
const h = fs.readFileSync('static/index.html', 'utf8');

const start = h.indexOf('function sparkDual');
const end = h.indexOf('\n}\n', start) + 3;
if (start < 0 || end < 3) { console.error('FAIL: 没能切出 sparkDual'); process.exit(1); }
const src = h.slice(start, end);

// sparkDual 只依赖 esc；页面里用的是同一份实现，这里补一个等价物即可
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// eslint-disable-next-line no-eval
eval(src);

const fails = [];
function chk(name, cond, extra) {
  const line = '  ' + (cond ? 'PASS' : 'FAIL') + '  ' + name + '  ' + (extra || '');
  console.log(line);
  if (!cond) fails.push(name);
}

const N = 33;
function mk(cMul, sMul) {
  const dates = [], crypto = [], stock = [];
  for (let i = 0; i < N; i++) {
    dates.push('2026-09-' + String(i + 1).padStart(2, '0'));
    crypto.push(80 * (1 + Math.sin(i / 3) * 0.03 * cMul));
    stock.push(630 * (1 + Math.sin(i / 3) * 0.03 * sMul));
  }
  return { dates: dates, crypto: crypto, stock: stock };
}
function ds(svg) {
  const out = [];
  const re = /<path d="([^"]+)"/g;
  let m;
  while ((m = re.exec(svg)) !== null) out.push(m[1]);
  return out;
}
function pts(pathD) {
  const chunks = pathD.trim().split(/(?=[ML])/);
  const out = [];
  for (const c of chunks) {
    const g = c.match(/[ML]([\d.]+) ([\d.]+)/);
    if (g) out.push([parseFloat(g[1]), parseFloat(g[2])]);
  }
  return out;
}
function allPts(svg) {
  const all = [];
  for (const d of ds(svg)) for (const p of pts(d)) all.push(p);
  return all;
}
function inBox(pts_, w, h) {
  return pts_.every(p => p[0] >= -0.01 && p[0] <= w + 0.01 && p[1] >= -0.7 && p[1] <= h + 0.7);
}

console.log('=== 1. 正常输入 ===');
let svg = sparkDual(mk(1, 1));
chk('产出 svg', svg.indexOf('<svg') === 0 && svg.slice(-6) === '</svg>', svg.length + ' 字符');
chk('两条 path', ds(svg).length === 2);
chk('合约蓝实线', /stroke="var\(--accent\)"[^>]*stroke-width="1\.4"/.test(svg));
chk('股票红虚线', /stroke="var\(--up\)"[^>]*stroke-dasharray="3 2"/.test(svg));
chk('viewBox 匹配宽高',
  svg.indexOf('width="88"') > 0 && svg.indexOf('height="24"') > 0 &&
  svg.indexOf('viewBox="0 0 88 24"') > 0);
chk('d 只含 M/L 指令',
  ds(svg).every(d => /^[ML][0-9. ML]+$/.test(d.trim())),
  ds(svg).map(d => d.slice(0, 14)));
chk('坐标都在画布内', inBox(allPts(svg), 88, 24), allPts(svg).length + ' 点');
chk('每条线 N 个点（含首日起笔）', pts(ds(svg)[0]).length === N, pts(ds(svg)[0]).length);

console.log('');
console.log('=== 2. 停牌缺口必须断开 ===');
const gapped = mk(1, 1);
gapped.stock[10] = null; gapped.stock[11] = null; gapped.stock[12] = null;
svg = sparkDual(gapped);
const sp = ds(svg)[0];   // 股票线在 SVG 里先输出
chk('股票线断成 2 段', (sp.match(/M/g) || []).length === 2, 'M x' + (sp.match(/M/g) || []).length);
chk('缺 3 天则少 3 个点', pts(sp).length === N - 3, pts(sp).length);
chk('合约线不受影响', pts(ds(svg)[1]).length === N, pts(ds(svg)[1]).length);

console.log('');
console.log('=== 3. 降级：缺数据返回空串，不炸 ===');
chk('null', sparkDual(null) === '');
chk('undefined', sparkDual(undefined) === '');
chk('{}', sparkDual({}) === '');
chk('只有 1 个点', sparkDual({ dates: ['a'], crypto: [1], stock: [1] }) === '');
chk('价格全 null',
  sparkDual({ dates: ['a', 'b', 'c'], crypto: [null, null, null], stock: [null, null, null] }) === '');
chk('价格全 0（找不到 base）',
  sparkDual({ dates: ['a', 'b'], crypto: [0, 0], stock: [0, 0] }) === '');
chk('字符串价格被严格拒绝（与大图 drawLinkChart 口径一致，不静默强转）',
  sparkDual({ dates: ['a', 'b', 'c'], crypto: ['1', '2', '1.5'], stock: ['2', '4', '3'] }) === '');
chk('混入一个字符串则整条线断开而不是画错',
  (function () {
    const g = mk(1, 1); g.crypto[5] = '80';
    const d = ds(sparkDual(g))[1];
    return (d.match(/M/g) || []).length === 2;
  })());

console.log('');
console.log('=== 4. 首日=100 归一化（与大图口径一致）===');
const flat = { dates: ['a', 'b', 'c', 'd'], crypto: [80, 80, 80, 80], stock: [630, 630, 630, 630] };
svg = sparkDual(flat);
chk('画 100 基准线', (svg.match(/stroke="var\(--border\)"/g) || []).length === 1);
chk('恒定序列两条线重合（点全在同 y）', (function () {
  const a = pts(ds(svg)[0]), b = pts(ds(svg)[1]);
  return a.length === 4 && b.length === 4 && Math.abs(a[0][1] - b[0][1]) < 1e-9;
})());
chk('量纲差 7 倍也能画出（归一化生效）',
  sparkDual({ dates: ['a', 'b'], crypto: [80, 81], stock: [630, 700] }).indexOf('<svg') === 0);

console.log('');
console.log('=== 5. 缩放不破图 ===');
const wide = sparkDual(mk(1, 1), 200, 60);
chk('200x60 坐标在范围内', inBox(allPts(wide), 200, 60), allPts(wide).length + ' 点');
const tiny = sparkDual(mk(1, 1), 40, 12);
chk('40x12 坐标在范围内', inBox(allPts(tiny), 40, 12));

console.log('');
console.log('=== 6. XSS：title 走 esc ===');
const evil = mk(1, 1);
evil.dates[0] = '<img src=x onerror=alert(1)>';
const out = sparkDual(evil);
chk('title 内插值已转义', out.indexOf('<img') < 0,
  (out.match(/<title>[\s\S]*?<\/title>/) || [''])[0].slice(0, 70));

console.log('');
console.log(fails.length ? '失败 ' + fails.length + ': ' + fails.join(' | ') : '全部通过');
process.exit(fails.length ? 1 : 0);