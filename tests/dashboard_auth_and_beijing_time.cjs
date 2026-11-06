const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../frontend/dashboard.html'), 'utf8');
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const auth = section('async function checkAuth()', 'async function doLogout()');
const bootstrap = html.match(/^checkAuth\(\)\.then[^\n]+/m)[0];
const times = section('function parseBucketDate(', 'function daysSince(');
const daily = section('function _dailyTime(', 'function renderDailyDayList(');

async function authCase(flow, ok, existing = false) {
  const elements = new Map();
  const loads = [];
  const document = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      value: id.includes('pwd') ? 'synthetic-password-123' : '',
      style: {display: id === 'auth-overlay' ? 'flex' : 'none'},
      innerHTML: id === 'bucket-list' ? 'Loading' : '', textContent: ''
    });
    return elements.get(id);
  }};
  const context = vm.createContext({document, loadBuckets() {
    loads.push('buckets'); document.getElementById('bucket-list').innerHTML = 'synthetic bucket';
  }, loadStatusBanner() { loads.push('status'); }, fetch: async url => {
    assert.ok(['/auth/status', '/auth/login', '/auth/setup', '/auth/recover'].includes(url));
    return {ok, json: async () => url === '/auth/status'
      ? {authenticated: existing, setup_needed: flow === 'doSetup'}
      : {error: 'synthetic rejection'}};
  }});
  vm.runInContext(auth, context);
  await vm.runInContext(bootstrap, context);
  if (existing) {
    assert.deepEqual(loads, ['buckets', 'status']);
  } else {
    assert.deepEqual(loads, []);
    await vm.runInContext(flow + '()', context);
    assert.deepEqual(loads, ok ? ['buckets', 'status'] : []);
    assert.equal(document.getElementById('bucket-list').innerHTML, ok ? 'synthetic bucket' : 'Loading');
    if (!ok) assert.equal(document.getElementById('auth-error').style.display, 'block');
  }
  if (ok || existing) assert.equal(document.getElementById('auth-overlay').style.display, 'none');
}

function timeCases(viewerTimezone) {
  const RealDate = Date;
  const now = '2026-12-31T18:30:00Z';
  function localPart(date, type) {
    return Number(new Intl.DateTimeFormat('en-GB', {timeZone: viewerTimezone,
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
      minute: '2-digit', hourCycle: 'h23'}).formatToParts(date).find(p => p.type === type).value);
  }
  class ViewerDate extends RealDate {
    constructor(...args) {
      if (!args.length) args = [now];
      if (typeof args[0] === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(args[0])) {
        args[0] = args[0].replace(' ', 'T') + (viewerTimezone === 'America/Los_Angeles' ? '-08:00' : 'Z');
      }
      super(...args);
    }
    getFullYear() { return localPart(this, 'year'); }
    getMonth() { return localPart(this, 'month') - 1; }
    getDate() { return localPart(this, 'day'); }
    getHours() { return localPart(this, 'hour'); }
    getMinutes() { return localPart(this, 'minute'); }
    toLocaleString(locale, options = {}) { return super.toLocaleString(locale, {timeZone: viewerTimezone, ...options}); }
  }
  const context = vm.createContext({Date: ViewerDate});
  vm.runInContext(times + daily, context);
  const evaluate = code => vm.runInContext(code, context);
  const instant = RealDate.parse('2026-12-31T18:05:00Z');
  for (const value of ['2026-12-31T18:05:00Z', '2027-01-01T02:05:00+08:00',
    '2027-01-01T02:05:00', '2027-01-01 02:05:00']) {
    assert.equal(evaluate(`parseBucketDate(${JSON.stringify(value)}).getTime()`), instant);
  }
  assert.equal(evaluate("parseBucketDate('2027-01-01 02:05:00.123456').getTime()"), instant + 123);
  assert.equal(evaluate("parseBucketDate('2027-01-01').getTime()"), RealDate.parse('2026-12-31T16:00:00Z'));
  assert.equal(evaluate("formatCompactBucketTime('2026-12-31T18:05:00Z')"), '01-01 02:05');
  assert.equal(evaluate("formatCompactBucketTime('2025-12-31T18:05:00Z')"), '2026-01-01 02:05');
  assert.equal(evaluate("formatCompactBucketTime('2026-12-31T16:00:00Z')"), '01-01 00:00');
  assert.match(evaluate("formatExactBucketTime('2026-12-31T16:00:00Z')"), /00:00:00.*北京时间$/);
  assert.equal(evaluate("parseBucketDate(0).getTime()"), 0);
  assert.equal(evaluate("firstValidBucketTime(0, '2000-01-01T00:00:00').getTime()"), 0);
  assert.equal(evaluate("parseBucketDate(new Date('2026-12-31T18:05:00Z')).getTime()"), instant);
  assert.equal(evaluate("formatTimeAgo('2026-12-31T16:30:00Z')"), '2h前');
  assert.equal(evaluate("_dailyTime('2027-01-01T02:05:00')"), evaluate("formatExactBucketTime('2026-12-31T18:05:00Z')"));
  for (const value of ['null', 'undefined', 'NaN', "''", "'invalid'"]) {
    assert.equal(evaluate(`parseBucketDate(${value})`), null);
  }
  assert.equal(evaluate("formatExactBucketTime('invalid')"), '—');
  return 21;
}

module.exports = (async () => {
  await authCase('doLogin', true, true);
  for (const flow of ['doLogin', 'doSetup', 'doRecover']) {
    await authCase(flow, true);
    await authCase(flow, false);
  }
  const timeAssertions = timeCases('America/Los_Angeles') + timeCases('UTC');
  return {authScenarios: 7, timeAssertions, syntheticOnly: true};
})();
if (require.main === module) module.exports.then(result => console.log(JSON.stringify(result)));
