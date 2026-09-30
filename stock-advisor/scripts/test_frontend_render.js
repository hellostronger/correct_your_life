/* Frontend render test.
 *
 * Loads the WHOLE page script into a vm sandbox with a minimal DOM stub, feeds it
 * real API payloads, and calls every render function in the new jq tab.
 *
 * Why bother (static checks are not enough)
 * -----------------------------------------
 * node --check / grep for class names / check CSS vars only prove you did not
 * misspell a name. They prove nothing about runtime. The things that actually
 * break render code are all runtime:
 *   - wrong field name  (it.hasCode vs it.has_code) -> undefined, then .length dies
 *   - null not guarded   (p.risks is null -> p.risks.length throws TypeError)
 *   - number arrives as string (signed() does v > 0, which behaves oddly on strings)
 * Fake data lets all three through; real data exposes them. So: real data.
 *
 * Two earlier versions tried to EXTRACT the helper functions by name from the
 * page. Both broke, for reasons worth remembering:
 *   1) the depth scan stopped at the ')' of the parameter list, never reaching
 *      the '=' branch  -> "Unexpected token const"
 *   2) the '"' inside the regex literal /[&<>"]/g was read as a string opener,
 *      so the scan ran past the end of the expression
 * A quote inside a regex literal cannot be handled by "skip over strings", and
 * writing a real JS parser is too much. So: load the entire page instead. That
 * tests the real code and the real helpers, which is strictly better anyway.
 *
 * NOTE: this file is intentionally ASCII-only. Editing it via PowerShell
 * Set-Content -Encoding UTF8 adds a BOM and double-encodes CJK text (hit twice).
 * Use the write/edit tools.
 */
const fs = require('fs');
const vm = require('vm');

const page = fs.readFileSync('static/index.html', 'utf8');
const js = [...page.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).join('\n');
const fx = JSON.parse(fs.readFileSync(process.env.JQ_FIXTURES || 'C:\\Users\\Strong\\AppData\\Local\\Temp\\opencode\\jq_fixtures.json', 'utf8'));
console.log('  page script: ' + js.length + ' chars, fixtures: ' +
            Object.keys(fx).length + ' keys');

/* ---------- minimal DOM stub ---------- */
const calls = [];                  // every innerHTML write, for assertions
const ctx2d = new Proxy({}, {
  get(_, k) {
    if (k === 'createLinearGradient') return () => ({ addColorStop() {} });
    if (k === 'measureText') return () => ({ width: 10 });
    return () => {};                // every drawing method is a no-op
  },
  set() { return true; },
});
function mkEl(id) {
  return {
    id, _html: '', textContent: '', value: '', checked: false, disabled: false,
    style: {}, dataset: {}, files: [], children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c);
                     if (f) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    set innerHTML(v) { this._html = String(v); calls.push([id, this._html]); },
    get innerHTML() { return this._html; },
    getContext: () => ctx2d,
    querySelector: () => mkEl('x'), querySelectorAll: () => [],
    closest: () => null, matches: () => false,
    /* real child bookkeeping: the page's toast() prunes with
     * `while (wrap.children.length > 3) wrap.removeChild(wrap.firstChild)`.
     * A stub whose appendChild is a no-op leaves children undefined and that
     * line throws -- which is how a *stub* gap masquerades as a page bug. */
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    get firstChild() { return this.children[0] || null; },
    get lastChild() { return this.children[this.children.length - 1] || null; },
    insertBefore(c) { this.children.unshift(c); return c; },
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 900, height: 260 }),
    scrollIntoView() {},
    get clientWidth() { return 900; }, get clientHeight() { return 260; },
    get offsetWidth() { return 900; }, get offsetHeight() { return 260; },
    get parentElement() { return mkEl('p'); },
    focus() {}, click() {}, blur() {},
  };
}
const els = new Map();
const document = {
  documentElement: {}, body: mkEl('body'), head: mkEl('head'),
  getElementById(id) { if (!els.has(id)) els.set(id, mkEl(id)); return els.get(id); },
  querySelector() { return mkEl('q'); }, querySelectorAll: () => [],
  createElement(t) { return mkEl(t); },
  createTextNode(t) { return mkEl('#text'); },
  createDocumentFragment() { return mkEl('#frag'); },
  addEventListener() {}, removeEventListener() {},
  execCommand() {},
};

/* ---------- api stub: serve the captured real payloads ---------- */
const apiCalls = [];
function pickByUrl(url) {
  /* ORDER MATTERS. '/api/strategy-lib/article' is a PREFIX of
   * '/api/strategy-lib/articles', so checking the singular first makes every
   * list request resolve to the single-article payload -- which has no `items`
   * key, so the list rendered "no articles" with 65 articles in the fixture.
   * Longest prefix first. */
  if (url.startsWith('/api/strategy-lib/articles')) return fx.articles;
  if (url.startsWith('/api/strategy-lib/article')) return fx.article;
  if (url.startsWith('/api/strategy-lib/stats')) return fx.stats;
  if (url.startsWith('/api/sandbox/health')) return fx.health;
  if (url.startsWith('/api/strategy-digest/list')) return fx.digest;
  if (url.startsWith('/api/backtest/runs')) return fx.backtest_runs;
  if (url.startsWith('/api/backtest/curve')) return fx.curve;
  if (url.startsWith('/api/sandbox/compare')) return fx.compare;
  if (url.startsWith('/api/sandbox/precheck')) return fx.precheck;
  if (url.startsWith('/api/sandbox/runs'))
    return /only_failed=true/.test(url) ? fx.sandbox_runs_failed : fx.sandbox_runs;
  if (url.startsWith('/api/sandbox/run')) return {
    ok: true, run_id: 99, where: 'local', post_id: 'x', title: 't',
    slice: { codes: 400, days: 651, bytes: 2400000, val_codes: 380 },
    result: { ok: true, elapsed: 42.3, n_trades_total: 456, n_rejected: 210,
      n_callback_errors: 1,
      metrics: { total_return: 0.2308, annual_return: 0.0809,
                 max_drawdown: -0.1612, sharpe: 0.5432 },
      trade_stats: { win_rate: 0.578 },
      warnings: ['w1', 'w2'], rejected: [{ why: 'x' }] } };
  if (url.startsWith('/api/strategy-lib/fetch')) return { fetched: 3 };
  if (url.startsWith('/api/strategy-digest/extract')) return { ok: true };
  if (url.startsWith('/api/sandbox/batch')) return { n: 1, items: [
    { post_id: 'p', title: 'tt', ok: true, total_return: 1.5, annual_return: 0.4,
      max_drawdown: -3.2, n_trades: 10, error: '' } ] };
  throw new Error('api stub missed: ' + url);
}

/* ---------- sandbox ---------- */
let timerId = 0;
const sandbox = {
  document, console,
  window: {
    devicePixelRatio: 2, innerWidth: 1400, innerHeight: 900,
    scrollTo() {}, addEventListener() {}, removeEventListener() {},
    matchMedia: () => ({ matches: false, addListener() {}, removeListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    location: { href: 'http://127.0.0.1:8686/', reload() {} },
  },
  /* timers must be inert, otherwise the page's polling loops keep the process
   * alive forever and the test never exits */
  setTimeout: (fn, ms) => 0, clearTimeout() {},
  setInterval: () => 0, clearInterval() {},
  requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  Promise, Math, JSON, Number, String, Array, Object, Date, RegExp, Error, Map, Set,
  isNaN, isFinite, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  /* matchMedia / devicePixelRatio must exist as BARE globals, not just on
   * window. The page calls matchMedia(...) at top level, and a throw there
   * aborts the whole script before `const $` and my `let JQ_POST` ever get
   * initialised -- which then shows up as a dozen of misleading
   * "Cannot access 'JQ_POST' before initialization" failures downstream. */
  matchMedia: () => ({ matches: false, media: '', onchange: null,
                       addEventListener() {}, removeEventListener() {},
                       addListener() {}, removeListener() {}, dispatchEvent() {} }),
  devicePixelRatio: 2,
  innerWidth: 1400, innerHeight: 900, scrollX: 0, scrollY: 0,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: 'node' },
  location: { href: 'http://127.0.0.1:8686/', hostname: '127.0.0.1' },
  CSS: { escape: (s) => s }, DOMParser: class { parseFromString() { return mkEl('#html'); } },
  FormData: class { constructor() { this.d = {}; } append(k, v) { this.d[k] = v; } },
  Blob: class {}, URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  alert() {}, confirm: () => true, prompt: () => '',
  fetch: async (url, opts) => {
    apiCalls.push([String(url), (opts && opts.method) || 'GET']);
    const u = String(url);
    if (!u.startsWith('/api/')) {
      return { ok: true, status: 200, statusText: 'OK', text: async () => '' };
    }
    return { ok: true, status: 200, statusText: 'OK', json: async () => pickByUrl(u) };
  },
};
sandbox.window.document = document;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

const script = new vm.Script(js, { filename: 'page.js' });
vm.createContext(sandbox);
try {
  script.runInContext(sandbox);
  console.log('  page script loaded with no top-level throw');
} catch (e) {
  console.log('  NOTE top-level threw: ' + e.message);
  console.log('       ' + (e.stack || '').split('\n')[1].trim().slice(0, 100));
}

/* sanity: the functions we test must exist */
const NEED = ['loadJqLib', 'loadJqList', 'openJqArticle', 'jqPrecheck', 'jqRun',
              'jqBatch', 'jqCompare', 'jqSwitch', 'showJqCurve', 'drawJqCurve',
              'renderJqRun', 'jqShowRun', 'jqFetch', 'jqExtract', 'jqEsc',
              'jqSyntaxBadge', 'jqScoreBadge'];
const absent = NEED.filter(n => typeof sandbox[n] !== 'function');
if (absent.length) {
  console.log('  !! these are not callable: ' + absent.join(', '));
  process.exit(1);
}
console.log('  all ' + NEED.length + ' jq-tab functions present');

/* ---------- drive every render path ---------- */
const results = [];
/* jqSwitch() and openJqArticle() call the async renderers WITHOUT awaiting
 * them (fire-and-forget, which is normal in a browser). So the step returns
 * before the html exists, and a late write from the previous step leaks into
 * the next one. Drain the microtask/macrotask queues before asserting. */
const drain = () => new Promise(r => setImmediate(() => setImmediate(r)));
async function step(name, fn, mustContain) {
  calls.length = 0;
  try {
    await fn();
    await drain(); await drain();
    const html = calls.map(c => c[1]).join('\n');
    const miss = (mustContain || []).filter(t => !html.includes(t));
    if (miss.length) {
      /* print what actually got written -- guessing from the assertion alone
       * is how you end up "fixing" code that was never broken */
      const where = calls.map(c => c[0]).join(',') || '(nothing written)';
      results.push(['FAIL ' + name,
        'missing ' + miss.map(x => JSON.stringify(x)).join(', ') +
        ' | wrote: ' + where + ' | ' + JSON.stringify(html.slice(0, 220))]);
    } else {
      results.push(['ok   ' + name, calls.length + ' writes, ' + html.length + ' chars']);
    }
  } catch (e) {
    results.push(['FAIL ' + name,
      e.message + '  @ ' + (e.stack || '').split('\n')[1].trim().slice(0, 78)]);
  }
}
/* `const $ = ...` at the top level of a vm script is a lexical binding, NOT a
 * property of the context object (only `var` and `function` declarations are).
 * So the test cannot call sandbox.$ -- it has to go through document. */
const g = id => sandbox.document.getElementById(id);

(async () => {
  /* Assertions check for things that actually appear in the CONTENT.
   * My first version asserted on element ids ("jq-stats"), which of course
   * never appear inside the html they were written to -- ten FAILs against
   * perfectly working code. Assert on classes/markup instead. */
  await step('loadJqLib', () => sandbox.loadJqLib(), ['jq-stat', 'jq-sandbox']);
  await step('list: articles', () => sandbox.loadJqList(), ['<table', 'badge']);
  /* `let JQ_VIEW` is lexical, so it cannot be poked from outside: assigning
   * sandbox.JQ_VIEW just creates an unrelated property and the page keeps
   * reading its own 'lib'. jqSwitch() is a function declaration (reachable)
   * and sets the real binding -- and it is the path the UI actually takes. */
  await step('list: digest', () => sandbox.jqSwitch('digest'), ['<table']);
  await step('list: backtest', () => sandbox.jqSwitch('backtest'), ['<table']);
  await step('list: runs', () => sandbox.jqSwitch('runs'), ['<table']);
  const pid = (fx.articles.items.find(a => a.has_code) || {}).post_id;
  await step('openJqArticle', () => sandbox.openJqArticle(pid),
             ['font-size:14px', 'badge', 'target="_blank"']);
  await step('jqPrecheck', () => sandbox.jqPrecheck(), ['<table', 'badge']);
  await step('jqRun local', async () => {
    g('jq-start').value = '2024-01-01';
    g('jq-end').value = '';
    g('jq-limit').value = '400';
    await sandbox.jqRun('local', null);
  }, ['jq-metric', 'badge']);
  await step('jqBatch', async () => {
    g('jq-batch-n').value = '3';
    await sandbox.jqBatch(null);
  }, ['<table']);
  await step('jqCompare', () => sandbox.jqCompare(null), []);
  const rid = (fx.backtest_runs.items[0] || {}).id;
  await step('showJqCurve', () => sandbox.showJqCurve(rid), ['jq-curve-cv']);
  await step('drawJqCurve direct', () =>
    sandbox.drawJqCurve(fx.curve.curve), []);
  await step('jqSyntaxBadge all states', () => {
    ['ok', 'fragment', 'syntax_error', 'empty', 'weird_state'].forEach(
      s => { if (typeof sandbox.jqSyntaxBadge(s) !== 'string') throw new Error(s); });
  }, []);
  await step('jqScoreBadge all ranges', () => {
    [null, 0, 1, 2, 3, 4, 5, '3'].forEach(
      v => { if (typeof sandbox.jqScoreBadge(v) !== 'string') throw new Error('' + v); });
  }, []);
  await step('jqEsc hostile input', () => {
    const out = sandbox.jqEsc('<script>alert("x")&\'</script>');
    if (out.includes('<')) throw new Error('esc did not escape <');
  }, []);

  console.log();
  let bad = 0;
  for (const [a, b] of results) {
    if (a.startsWith('FAIL')) bad++;
    console.log('  ' + a.padEnd(34) + b);
  }
  console.log();
  console.log('  ' + (bad ? 'FAIL ' + bad + ' problem(s)' :
    'all ' + results.length + ' steps passed'));
  console.log('  api calls: ' + apiCalls.length + ' -> ' +
    [...new Set(apiCalls.map(c => c[0].split('?')[0].replace('/api/', '/')))].join(' '));
  process.exit(bad ? 1 : 0);
})();
