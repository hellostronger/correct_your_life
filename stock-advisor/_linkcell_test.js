// linkCell 渲染单测：喂真实的 /api/crypto/links payload。
// 关键：真实 payload 可能是新鲜的(status=ok)也可能是降级的(stale，源抖动)，
// 两种都出现过。所以这里不假设它是哪一种，而是「拿真实数据跑渲染 + 从真实数据
// 合成另一种状态」，断言只针对「输入什么状态就该渲染成什么样」。
//
// 跑法（必须在 stock-advisor 目录下，脚本用相对路径读源码和 payload）：
//   curl -s http://127.0.0.1:8686/api/crypto/links -o _links_probe.json
//   node _linkcell_test.js
// _links_probe.json 是运行时抓的、不入库，所以每次先 curl 一份。
const fs = require('fs');
const h = fs.readFileSync('static/index.html', 'utf8');

const PROBE = '_links_probe.json';
if (!fs.existsSync(PROBE)) {
  console.error('缺少 ' + PROBE + '。先抓一份真实 payload：\n'
    + '  curl -s http://127.0.0.1:8686/api/crypto/links -o ' + PROBE);
  process.exit(2);
}
const payload = JSON.parse(fs.readFileSync(PROBE, 'utf8'));

function cut(name) {
  const s = h.indexOf('function ' + name);
  if (s < 0) throw new Error('切不出 ' + name);
  const e = h.indexOf('\n}\n', s) + 3;
  if (e < 3) throw new Error(name + ' 没有行首 }');
  return h.slice(s, e);
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const signed = v => (v > 0 ? '+' : '') + Number(v).toFixed(2);
const cls = v => (v > 0 ? 'up' : v < 0 ? 'down' : '');
const _cryptoLinks = {};
// eslint-disable-next-line no-eval
eval(cut('sparkDual') + cut('linkCell'));

const fails = [];
function chk(n, c, x) {
  console.log('  ' + (c ? 'PASS' : 'FAIL') + '  ' + n + '  ' + (x || ''));
  if (!c) fails.push(n);
}
// 合成 stale：把真实 payload 改成「最新重算失败、回退到旧值」的样子
function asStale(it) {
  const c = JSON.parse(JSON.stringify(it));
  c.stock_code = it.stock_code;
  const s = c.stats;
  s.status = 'insufficient'; s.error = '日K 取不到（币圈 45 根 / 股票 0 根）：已尝试腾讯K全源失败';
  s.stale = true; s.latest_attempt = '2026-10-09'; s.data_stat_date = '2026-10-02';
  return c;
}
const realStale = payload.items.filter(i => i.stats.stale);
console.log(`线上 payload 状态：${payload.items.map(i =>
  `${i.contract}(${i.stats.status}${i.stats.stale ? '/stale' : ''})`).join(' ')}`);

console.log('');
console.log('=== 1. 真实 payload：有数据就必须画出迷你双线图 ===');
for (const it of payload.items) (_cryptoLinks[it.contract] = it);
for (const it of payload.items) {
  const out = linkCell(it.contract);
  const stale = it.stats.status !== 'ok';
  chk(it.contract + ' 画出迷你双线图（2 条 path）',
    (out.match(/<path/g) || []).length === 2, (out.match(/<path/g) || []).length + ' 条');
  chk(it.contract + ' 不退化成「无数据」占位', out.indexOf('badge b-warn" title=') < 0);
  chk(it.contract + ' 数值无 undefined/NaN', !/undefined|NaN/.test(out),
    (out.match(/相关 [-\d.]+/) || [''])[0]);
  chk(it.contract + ' 陈旧标记与实际状态一致（线上 ' + (stale ? 'stale' : 'ok') + '）',
    stale ? out.indexOf('陈旧·') > 0 : out.indexOf('陈旧') < 0);
  chk(it.contract + ' badge 配色随状态（' + (stale ? 'b-warn' : 'b-info') + '）',
    out.indexOf(stale ? 'b-warn' : 'b-info') > 0 && out.indexOf(stale ? 'b-info' : 'b-warn') < 0);
}

console.log('');
console.log('=== 2. 合成 stale（源抖动）：有效统计不能被丢掉 ===');
for (const it of payload.items) {
  const c = asStale(it);
  const key = '__st_' + it.contract;
  _cryptoLinks[key] = c;
  const out = linkCell(key);
  chk(it.contract + ' 仍画图（回退值有效）', (out.match(/<path/g) || []).length === 2);
  chk(it.contract + ' corr 仍显示', out.indexOf('旧 相关') > 0,
    (out.match(/旧 相关 [-\d.]+/) || [''])[0]);
  chk(it.contract + ' 旧值日期可见（非仅 tooltip）',
    out.indexOf('陈旧·2026-10-02') > 0);
  chk(it.contract + ' tooltip 写明失败原因',
    out.indexOf('重算') > 0 && out.indexOf('失败') > 0);
  chk(it.contract + ' tooltip 指向最新尝试日', out.indexOf('2026-10-09') > 0);
  chk(it.contract + ' 无 undefined/NaN', !/undefined|NaN/.test(out));
}

console.log('');
console.log('=== 3. 真没数据才显示「无数据」 ===');
_cryptoLinks.__none = { contract: '__none', stock_code: '99999',
  stats: { status: 'error', error: '取日K失败：ConnectionError' } };
const outNone = linkCell('__none');
chk('显示无数据', outNone.indexOf('无数据') > 0);
chk('不带 svg', outNone.indexOf('<svg') < 0);
chk('tooltip 带原因', outNone.indexOf('取日K失败') > 0);
_cryptoLinks.__nos = { contract: '__nos', stock_code: '99999',
  stats: { status: 'insufficient', error: '重叠交易日仅 5 天', corr: null,
           series: { dates: [], crypto: [], stock: [] } } };
chk('corr=null 且 series 空 -> 无数据', linkCell('__nos').indexOf('无数据') > 0);
_cryptoLinks.__s = { contract: '__s', stock_code: '88888', quote: { name: '测试' },
  stats: { status: 'ok', corr: null, stock_code: '88888', n_obs: 20,
           series: payload.items[0].stats.series } };
chk('只有 series 没 corr -> 仍画图', linkCell('__s').indexOf('<svg') > 0);
chk('  且不吐 undefined', !/undefined|NaN/.test(linkCell('__s')));

console.log('');
console.log('=== 4. 边界 ===');
_cryptoLinks.__empty = { contract: '__empty', stock_code: '88888', stats: null };
chk('stats=null 不炸', linkCell('__empty').indexOf('无数据') > 0);
chk('未关联的合约', linkCell('__missing__').indexOf('设关联') > 0);

console.log('');
console.log(fails.length ? '失败 ' + fails.length + ': ' + fails.join(' | ') : '全部通过');
process.exit(fails.length ? 1 : 0);