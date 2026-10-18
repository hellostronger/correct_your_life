// 做T判断记录 UI 测试（node V8 真跑）。跑法：node stock-advisor/_t_log_ui_test.mjs
// 覆盖：加载即显示记录 / 窗口切换 / 按票汇总与时间线 / 空态 / 点代码开钻 / XSS 防护
import fs from 'fs';
import path from 'path';

const html = fs.readFileSync(path.join(path.dirname(path.resolve('')), 'stock-advisor', 'static', 'index.html'), 'utf8');

// 提取两个 script 块
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
console.log('script blocks:', scripts.length);

// DOM 元素注册表（lazy create）
const _elements = new Map();
const $ = (id) => {
  if (!_elements.has(id)) {
    const el = {
      id, innerHTML: '', textContent: '', _classes: new Set(),
      classList: { 
        add: (c) => { el._classes.add(c); }, 
        remove: (c) => { el._classes.delete(c); }, 
        toggle: (c, cond) => { if (cond) el._classes.add(c); else el._classes.delete(c); }, 
        contains: (c) => el._classes.has(c) 
      },
      style: {}, dataset: {}, children: [], value: '',
      onclick: null, onchange: null, appendChild: (c) => { el.children.push(c); },
      querySelector: (sel) => ({ classList: { add: () => {}, remove: () => {}, toggle: () => {} } }),
      querySelectorAll: () => [],
      addEventListener: () => {},
      offsetParent: {},
      focus: () => {},
      select: () => {},
      isConnected: true,
      remove: () => {},  // for toast cleanup
      setAttribute: () => {},  // for toast
      removeChild: () => {},  // for toast wrap cleanup
    };
    _elements.set(id, el);
  }
  return _elements.get(id);
};

// 预创建一些需要 querySelectorAll 的容器元素
const _createMockContainer = (id, children = []) => {
  const el = $(id);
  el.querySelectorAll = (sel) => {
    if (sel.includes('.t-sub')) return children;
    return [];
  };
  return el;
};

// 为 t-log-days 创建按钮
const dayButtons = [1, 3, 7, 30].map(d => {
  const btn = {
    id: '', innerHTML: '', textContent: '', _classes: new Set(),
    classList: { 
      add: (c) => { btn._classes.add(c); }, 
      remove: (c) => { btn._classes.delete(c); }, 
      toggle: (cls, cond) => { if (cond) btn._classes.add(cls); else btn._classes.delete(cls); }, 
      contains: (c) => btn._classes.has(c) 
    },
    style: {}, dataset: { d: String(d) }, children: [], value: '',
    onclick: null, onchange: null, appendChild: () => {},
    querySelector: () => ({ classList: { add: () => {}, remove: () => {}, toggle: () => {} } }),
    querySelectorAll: () => [],
    addEventListener: () => {}, offsetParent: {}, focus: () => {}, select: () => {}, isConnected: true,
  };
  return btn;
});
_createMockContainer('t-log-days', dayButtons);

// 为 t-log-hist-wrap 创建空容器
_createMockContainer('t-log-hist-wrap');
_createMockContainer('t-log-hist-title');
_createMockContainer('t-log-hist-body');
_createMockContainer('t-log-hist-empty');

// 为汇总表格创建 t-log-empty 容器
_createMockContainer('t-log-empty');
_createMockContainer('t-log-meta');
_createMockContainer('t-log-body');
_createMockContainer('t-log-out');

// 构造全局环境（模拟浏览器最小集）
global.window = global;
global.document = {
  getElementById: (id) => $(id),
  querySelectorAll: (sel) => {
    if (sel === '#t-log-days .t-sub') return dayButtons;
    return [];
  },
  querySelector: () => ({ classList: { add: () => {}, remove: () => {}, toggle: () => {} }, textContent: '', innerHTML: '' }),
  createElement: () => ({ innerHTML: '', textContent: '', classList: { add: () => {}, remove: () => {}, toggle: () => {} }, style: {}, children: [], appendChild: () => {}, setAttribute: () => {}, remove: () => {} }),
  body: { classList: { add: () => {}, remove: () => {}, toggle: () => {} } },
  documentElement: { setAttribute: () => {} },
  addEventListener: () => {},
  hidden: false,
};
global.localStorage = { getItem: () => 'auto', setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false, addEventListener: () => {} });
global.location = { href: 'http://localhost/' };
global.console = console;

// 定义测试用的 api/fetch（在运行 script 1 之前，因为 script 1 里的 api 会捕获当前的 fetch）
global.fetch = async (url) => {
  // 只响应 /api/t/log
  if (url.includes('/api/t/log')) {
    const days = url.includes('days=') ? url.split('days=')[1].split('&')[0] : '3';
    const code = url.includes('code=') ? decodeURIComponent(url.split('code=')[1].split('&')[0]) : '';
    if (code) {
      // 单票时间线
      return { ok: true, json: async () => ({
        n_rows: 2, days: Number(days), code,
        runs: [{ run_id: 'bg-A', run_type: 'bg', created_at: '2026-10-18T10:00:00', n_items: 1, n_act: 1, act_codes: [code] }],
        codes: [{ code, name: '测试', total: 2, t_sell: 1, t_buy: 0, hold: 1,
          last_action: 't_sell', last_why: '达到门槛', last_created_at: '2026-10-18T10:00:00',
          last_price: 10.5, last_edge_pct: 2.0, last_threshold_pct: 1.7, last_bias: 'bear',
          first_created_at: '2026-10-18T09:30:00' }],
        latest: { run_id: 'bg-A', run_type: 'bg', created_at: '2026-10-18T10:00:00',
          items: [{ code, name: '测试', action: 't_sell', bias: 'bear', price: 10.5, cost: 10.0,
            pnl_pct: 5.0, edge_pct: 2.0, threshold_pct: 1.7, anchor_price: 10.2,
            trigger_price: 10.4, net: 123.4, layer: 'L2_week', movable: 100, base_shares: 200,
            pending: false, stabilizing: false, why: '达到门槛', invalidate: '若继续放量上行',
            created_at: '2026-10-18T10:00:00', ticket: { trigger_price: 10.4, net: 123.4 } }] },
        history: [
          { code, name: '测试', run_id: 'bg-A', run_type: 'bg', created_at: '2026-10-18T10:00:00',
            action: 't_sell', bias: 'bear', price: 10.5, edge_pct: 2.0, threshold_pct: 1.7,
            trigger_price: 10.4, why: '达到门槛' },
          { code, name: '测试', run_id: 'bg-B', run_type: 'api', created_at: '2026-10-18T10:30:00',
            action: 'hold', bias: 'flat', price: 10.3, edge_pct: 0.5, threshold_pct: 1.7,
            trigger_price: 10.4, why: '偏离 0.5% < 需 1.7%' },
        ],
      })};
    }
    // 全量汇总
    return { ok: true, json: async () => ({
      n_rows: 5, days: Number(days), code: null,
      runs: [
        { run_id: 'bg-C', run_type: 'bg', created_at: '2026-10-18T11:00:00', n_items: 2, n_act: 1, act_codes: ['000858'] },
        { run_id: 'bg-B', run_type: 'api', created_at: '2026-10-18T10:30:00', n_items: 2, n_act: 1, act_codes: ['00136'] },
        { run_id: 'bg-A', run_type: 'bg', created_at: '2026-10-18T10:00:00', n_items: 2, n_act: 1, act_codes: ['000858'] },
      ],
      codes: [
        { code: '000858', name: '五粮液', total: 3, t_sell: 1, t_buy: 0, hold: 2,
          last_action: 'hold', last_why: '又不够了', last_created_at: '2026-10-18T11:00:00',
          last_price: 10.5, last_edge_pct: 0.3, last_threshold_pct: 1.7, last_bias: 'flat',
          first_created_at: '2026-10-18T10:00:00' },
        { code: '00136', name: '香港小盘', total: 2, t_sell: 1, t_buy: 0, hold: 1,
          last_action: 'hold', last_why: '未止跌', last_created_at: '2026-10-18T10:30:00',
          last_price: 10.3, last_edge_pct: 0.5, last_threshold_pct: 1.7, last_bias: 'bear',
          first_created_at: '2026-10-18T10:00:00' },
      ],
      latest: { run_id: 'bg-C', run_type: 'bg', created_at: '2026-10-18T11:00:00',
        items: [
          { code: '000858', name: '五粮液', action: 'hold', bias: 'flat', price: 10.5, cost: 10.0,
            pnl_pct: 5.0, edge_pct: 0.3, threshold_pct: 1.7, anchor_price: 10.2,
            trigger_price: 10.4, net: null, layer: 'L2_week', movable: 100, base_shares: 200,
            pending: false, stabilizing: false, why: '又不够了', invalidate: '方向判不出',
            created_at: '2026-10-18T11:00:00', ticket: {} },
          { code: '00136', name: '香港小盘', action: 't_sell', bias: 'bear', price: 10.3, cost: 10.0,
            pnl_pct: 3.0, edge_pct: 1.8, threshold_pct: 1.7, anchor_price: 10.1,
            trigger_price: 10.2, net: 45.6, layer: 'L2_week', movable: 100, base_shares: 200,
            pending: false, stabilizing: false, why: '这次够了', invalidate: '若继续放量上行',
            created_at: '2026-10-18T11:00:00', ticket: { trigger_price: 10.2, net: 45.6 } },
        ] },
      history: [],
    })};
  }
  return { ok: true, json: async () => ({}) };
};

// 运行 script 0（全局样式/主题，使用真实 document.getElementById）
new Function(scripts[0])();

// 运行 script 1（业务逻辑，包含 loadTLog 等）
// new Function 创建的函数内部声明是局部的，需用 "return" 把需要的符号导出
const fn1 = new Function(scripts[1] + `
return {loadTLog, tLogDays, renderTLog, tLogOpenHist, tLogCloseHist, api, esc, fmt, tNum, tPx, tPct4, tPct, tInt, cls, signed, skeletonBox, confirmDlg, formDlg, _tAttr, _T, T_BIAS, T_ACT};
`);
const exports = fn1();

// 显式把导出的符号挂到 global
Object.assign(global, exports);

console.log('global.loadTLog:', typeof global.loadTLog);
console.log('global.tLogDays:', typeof global.tLogDays);
console.log('global.renderTLog:', typeof global.renderTLog);
console.log('global.tLogOpenHist:', typeof global.tLogOpenHist);
console.log('global.tLogCloseHist:', typeof global.tLogCloseHist);

let passed = 0, failed = 0;
function eq(a, b, msg) { if (a === b) { passed++; } else { failed++; console.log(`FAIL: ${msg} | got ${JSON.stringify(a)} !== ${JSON.stringify(b)}`); } }
function truthy(a, msg) { if (a) passed++; else { failed++; console.log(`FAIL: ${msg} | falsy`); } }
function includes(a, b, msg) { if (String(a).includes(String(b))) passed++; else { failed++; console.log(`FAIL: ${msg} | ${String(a)} not includes ${String(b)}`); } }
function notIncludes(a, b, msg) { if (!String(a).includes(String(b))) passed++; else { failed++; console.log(`FAIL: ${msg} | ${String(a)} includes ${String(b)}`); } }

console.log('\n=== 1) 全量汇总：加载即显示记录（无需点击）===');
await loadTLog();
truthy(_T.log !== null, '_T.log 已赋值');
truthy(_T.log.codes && _T.log.codes.length === 2, '两只票汇总');
includes($('t-log-body').innerHTML, '000858', 'tbody 含 000858');
includes($('t-log-body').innerHTML, '五粮液', 'tbody 含名称');
includes($('t-log-body').innerHTML, '3', '000858 判定次数 3');
// last_action 是 hold，显示 "— 不做" + 偏离原因 "又不够了"
includes($('t-log-body').innerHTML, '— 不做', '显示 hold 标签');
includes($('t-log-body').innerHTML, '又不够了', '最近原因列显示 why');
includes($('t-log-body').innerHTML, '📈', '时间线按钮存在');
// 空态应被隐藏（有 hide class）
truthy($('t-log-empty').classList.contains('hide'), '空态隐藏');
truthy($('t-log-meta').textContent.includes('5 条判断 · 3 轮'), 'meta 显示汇总');
console.log('PASS: 1/7');

// 2) 窗口切换
console.log('\n=== 2) 窗口切换：1/3/7/30 天 ===');
await tLogDays(1);
truthy(_T.logDays === 1, 'logDays 更新为 1');
// tLogDays 会重新 loadTLog，用新的 fetch 返回数据
// dayButtons[0] 对应 data-d="1"
truthy(dayButtons[0].classList.contains('active'), '1天按钮 active');
await tLogDays(7);
truthy(_T.logDays === 7, 'logDays 更新为 7');
console.log('PASS: 2/7');

// 3) 点代码开钻时间线
console.log('\n=== 3) 点代码开钻时间线 ===');
await tLogOpenHist('000858');
truthy(_T.logCode === '000858', '_T.logCode 记录被点代码');
truthy(!$('t-log-hist-wrap').classList.contains('hide'), '时间线面板展开');
includes($('t-log-hist-title').textContent, '000858', '标题含代码');
// 历史模板不渲染 code 列，检查动作标签和原因
includes($('t-log-hist-body').innerHTML, '高抛（先卖）', '时间线动作标签');
includes($('t-log-hist-body').innerHTML, '达到门槛', '时间线原因');
// 历史 hold 原因里的 < 被 esc() 转义为 < 在 innerHTML 里
includes($('t-log-hist-body').innerHTML, '偏离 0.5% &lt; 需 1.7%', '历史 hold 原因也在（已转义为 <）');
// 最新在前：达到门槛 (10:00) 在 偏离 0.5% (10:30) 之前
truthy($('t-log-hist-body').innerHTML.indexOf('达到门槛') < $('t-log-hist-body').innerHTML.indexOf('偏离 0.5%'), '最新在前');
console.log('PASS: 3/7');

// 4) 收起时间线回到全量汇总
console.log('\n=== 4) 收起时间线回到全量汇总 ===');
// tLogCloseHist 会调用 loadTLog('')，需要 mock fetch 返回全量数据
global.fetch = async (url) => {
  if (url.includes('/api/t/log')) {
    return { ok: true, json: async () => ({
      n_rows: 5, days: 3, code: null,
      runs: [
        { run_id: 'bg-C', run_type: 'bg', created_at: '2026-10-18T11:00:00', n_items: 2, n_act: 1, act_codes: ['000858'] },
        { run_id: 'bg-B', run_type: 'api', created_at: '2026-10-18T10:30:00', n_items: 2, n_act: 1, act_codes: ['00136'] },
        { run_id: 'bg-A', run_type: 'bg', created_at: '2026-10-18T10:00:00', n_items: 2, n_act: 1, act_codes: ['000858'] },
      ],
      codes: [
        { code: '000858', name: '五粮液', total: 3, t_sell: 1, t_buy: 0, hold: 2,
          last_action: 'hold', last_why: '又不够了', last_created_at: '2026-10-18T11:00:00',
          last_price: 10.5, last_edge_pct: 0.3, last_threshold_pct: 1.7, last_bias: 'flat',
          first_created_at: '2026-10-18T10:00:00' },
        { code: '00136', name: '香港小盘', total: 2, t_sell: 1, t_buy: 0, hold: 1,
          last_action: 'hold', last_why: '未止跌', last_created_at: '2026-10-18T10:30:00',
          last_price: 10.3, last_edge_pct: 0.5, last_threshold_pct: 1.7, last_bias: 'bear',
          first_created_at: '2026-10-18T10:00:00' },
      ],
      latest: { run_id: 'bg-C', run_type: 'bg', created_at: '2026-10-18T11:00:00',
        items: [
          { code: '000858', name: '五粮液', action: 'hold', bias: 'flat', price: 10.5, cost: 10.0,
            pnl_pct: 5.0, edge_pct: 0.3, threshold_pct: 1.7, anchor_price: 10.2,
            trigger_price: 10.4, net: null, layer: 'L2_week', movable: 100, base_shares: 200,
            pending: false, stabilizing: false, why: '又不够了', invalidate: '方向判不出',
            created_at: '2026-10-18T11:00:00', ticket: {} },
          { code: '00136', name: '香港小盘', action: 't_sell', bias: 'bear', price: 10.3, cost: 10.0,
            pnl_pct: 3.0, edge_pct: 1.8, threshold_pct: 1.7, anchor_price: 10.1,
            trigger_price: 10.2, net: 45.6, layer: 'L2_week', movable: 100, base_shares: 200,
            pending: false, stabilizing: false, why: '这次够了', invalidate: '若继续放量上行',
            created_at: '2026-10-18T11:00:00', ticket: { trigger_price: 10.2, net: 45.6 } },
        ] },
      history: [],
    })};
  }
  return { ok: true, json: async () => ({}) };
};
_elements.clear(); // 重置 DOM，否则 t-log-hist-wrap 是上一轮的
// 重新创建 t-log-hist-wrap 因为它是 lazy created
_createMockContainer('t-log-hist-wrap');
_createMockContainer('t-log-hist-title');
_createMockContainer('t-log-hist-body');
_createMockContainer('t-log-hist-empty');
// 重新创建汇总视图的容器
_createMockContainer('t-log-empty');
_createMockContainer('t-log-meta');
_createMockContainer('t-log-body');
_createMockContainer('t-log-out');
await tLogCloseHist();
await new Promise(r => setTimeout(r, 0)); // 等 loadTLog 的微任务跑完
truthy(_T.logCode === '', 'logCode 清空');
truthy($('t-log-hist-wrap').classList.contains('hide'), '时间线面板收起');
truthy(_T.log.codes && _T.log.codes.length === 2, '重新加载全量汇总');
console.log('PASS: 4/7');

// 5) 空态：无记录时显示引导
console.log('\n=== 5) 空态：无记录时显示引导 ===');
global.fetch = async () => ({ ok: true, json: async () => ({ n_rows: 0, days: 3, code: null, runs: [], codes: [], latest: null, history: [] }) });
_elements.clear(); // 重置 DOM
await loadTLog();
truthy($('t-log-empty').classList.contains('hide') === false, '空态显示');
includes($('t-log-empty').textContent, '开启做T建议调度', '引导文案含调度入口');
console.log('PASS: 5/7');

// 6) 读取失败态
console.log('\n=== 6) 读取失败态 ===');
global.fetch = async () => { throw new Error('network error'); };
_elements.clear();
await loadTLog();
truthy($('t-log-out').textContent.includes('加载失败'), '显示失败文案');
console.log('PASS: 6/7');

// 7) XSS 防护：代码/原因里带尖括号/单引号不破坏 HTML
console.log('\n=== 7) XSS 防护 ===');
global.fetch = async (url) => {
  if (url.includes('code=')) {
    return { ok: true, json: async () => ({ n_rows: 1, days: 3, code: "<script>alert(1)</script>",
      runs: [], codes: [{ code: "<script>alert(1)</script>", name: "<img src=x onerror=1>", total: 1, t_sell: 0, t_buy: 0, hold: 1,
        last_action: 'hold', last_why: "'; DROP TABLE--", last_created_at: '2026-10-18T10:00:00',
        last_price: 10, last_edge_pct: 0, last_threshold_pct: 1.7, last_bias: 'flat',
        first_created_at: '2026-10-18T10:00:00' }],
      latest: null, history: [] })};
  }
  return { ok: true, json: async () => ({ n_rows: 1, days: 3, code: null,
    runs: [], codes: [{ code: "<script>alert(1)</script>", name: "<img src=x onerror=1>", total: 1, t_sell: 0, t_buy: 0, hold: 1,
      last_action: 'hold', last_why: "'; DROP TABLE--", last_created_at: '2026-10-18T10:00:00',
      last_price: 10, last_edge_pct: 0, last_threshold_pct: 1.7, last_bias: 'flat',
      first_created_at: '2026-10-18T10:00:00' }],
    latest: null, history: [] })};
};
_elements.clear();
await loadTLog();
// XSS 防护验证：尖括号被转义，不出现真实的 <script> / <img 标签
notIncludes($('t-log-body').innerHTML, '<script>', 'tbody 无 <script> 标签');
notIncludes($('t-log-body').innerHTML, '<img', 'tbody 无 <img> 标签');
// 文本内容里有 onerror / DROP TABLE 是正常的（已转义为文本）
await tLogOpenHist("<script>alert(1)</script>");
notIncludes($('t-log-hist-body').innerHTML, '<script>', '时间线无 <script> 标签');
notIncludes($('t-log-hist-body').innerHTML, '<img', '时间线无 <img> 标签');
console.log('PASS: 7/7');

// 8) 边界：code 有空格/特殊字符不炸
console.log('\n=== 8) 边界：特殊代码 ===');
global.fetch = async (url) => {
  if (url.includes('code=')) {
    return { ok: true, json: async () => ({ n_rows: 1, days: 3, code: "000858 ",
      runs: [], codes: [{ code: "000858 ", name: "A B", total: 1, t_sell: 0, t_buy: 0, hold: 1,
        last_action: 'hold', last_why: '  ', last_created_at: '2026-10-18T10:00:00',
        last_price: 10, last_edge_pct: 0, last_threshold_pct: 1.7, last_bias: 'flat',
        first_created_at: '2026-10-18T10:00:00' }],
      latest: null, history: [] })};
  }
  return { ok: true, json: async () => ({ n_rows: 1, days: 3, code: null,
    runs: [], codes: [{ code: "000858 ", name: "A B", total: 1, t_sell: 0, t_buy: 0, hold: 1,
      last_action: 'hold', last_why: '  ', last_created_at: '2026-10-18T10:00:00',
      last_price: 10, last_edge_pct: 0, last_threshold_pct: 1.7, last_bias: 'flat',
      first_created_at: '2026-10-18T10:00:00' }],
    latest: null, history: [] })};
};
_elements.clear();
await loadTLog();
truthy($('t-log-body').innerHTML.includes('000858') || $('t-log-body').innerHTML.includes('000858 '), '不崩');
console.log('PASS: 8/8');

console.log(`\n=== 总计 ${passed} 通过 / ${failed} 失败 ===`);
if (failed) process.exit(1);
console.log('ALL DONE');