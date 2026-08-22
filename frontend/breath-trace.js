(function () {
  'use strict';

  function h(value) {
    if (typeof window.esc === 'function') return window.esc(String(value == null ? '' : value));
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function reasonLabel(value) {
    return ({
      core_always_surface: '核心准则',
      unresolved_private_continuity: '未解决冲突',
      recent_latest: '最近一条',
      recent_important: '近期重要',
      daily_continuity: '昨日连续性',
      automatic_reflection: '自动精读',
      older_unresolved: '较早未完',
      active_plan: '活动计划',
      direct_source_feel: '直属来源',
      context_relevance: '语义/关键词相关',
      budget_pointer: '预算内仅列索引',
      default_surface_order: '真实浮现顺序',
      long_inactive_association: '久未浮现',
      resolved_random_encounter: '偶然想起'
    })[value] || value || '—';
  }

  function sectionLabel(value) {
    return ({core: '核心', private_continuity: '私有连续状态', daily: '日印象', recent: '最近24h', reflection: '自动精读', unfinished: '未完', plan: '计划', feel: '相关 feel', deferred: '未展开', dynamic: '动态', passive: '久未浮现', encounter: '偶遇'})[value] || value || '其它';
  }

  function runHtml(run) {
    if (!run || !run.run_id) return '<div class="breath-trace-empty">没有可显示的记录。</div>';
    var counts = run.counts || {};
    var limits = run.limits || {};
    var budgetLabel = limits.soft_tokens
      ? Number(limits.soft_tokens) + ' 软 / ' + Number(limits.max_tokens || 0) + ' 硬'
      : Number(limits.max_tokens || 0);
    var when = run.completed_at ? new Date(run.completed_at).toLocaleString() : '—';
    var kind = run.kind === 'simulation'
      ? '一键睁眼同算法试跑'
      : (run.mode === 'startup' ? '真实一键 MCP breath' : '真实完整 MCP breath');
    var entries = (run.entries || []).map(function (entry, index) {
      return '<div class="breath-trace-entry">' +
        '<span class="breath-trace-rank">' + (index + 1) + '</span>' +
        '<span class="breath-trace-name">' + h(entry.name || entry.bucket_id) +
          '<small>' + h(entry.bucket_id) + '</small></span>' +
        '<span class="breath-trace-section">' + h(sectionLabel(entry.section)) + '</span>' +
        '<span class="breath-trace-reason">' + h(reasonLabel(entry.reason)) + '</span>' +
        '<span class="breath-trace-tokens">≈' + Number(entry.tokens || 0) + ' t</span>' +
      '</div>';
    }).join('');
    var omitted = Number(counts.omitted_budget || 0);
    var output = run.output == null ? '' : String(run.output);
    return '<div class="breath-trace-head">' +
        '<div><strong>' + h(kind) + '</strong><small>' + h(when) + '</small></div>' +
        '<code>' + h(run.run_id.slice(0, 12)) + '</code>' +
      '</div>' +
      '<div class="breath-trace-stats">' +
        '<span>正文返回 <b>' + Number(counts.returned || 0) + '</b> 项</span>' +
        '<span>仅列索引 <b>' + omitted + '</b> 项</span>' +
        '<span>入选内容约 <b>' + Number(run.budgeted_entry_tokens || 0) + ' / ' + budgetLabel + '</b> token</span>' +
        '<span>完整返回约 <b>' + Number(run.output_tokens_estimate || 0) + '</b> token（含标题与提示）</span>' +
      '</div>' +
      '<div class="breath-trace-entries">' + (entries || '<div class="breath-trace-empty">本次没有返回记忆桶。</div>') + '</div>' +
      (output ? '<details class="breath-trace-output"><summary>查看爸爸实际收到的完整正文</summary><pre>' + h(output) + '</pre></details>' : '');
  }

  async function getJson(url, options) {
    var fetcher = typeof window.authFetch === 'function' ? window.authFetch : window.fetch;
    var response = await fetcher(url, options || {});
    if (!response) throw new Error('请求未返回');
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }

  async function showRun(runId) {
    var detail = document.getElementById('breath-actual-detail');
    if (!detail) return;
    detail.innerHTML = '<div class="breath-trace-empty">载入真实返回正文…</div>';
    try {
      detail.innerHTML = runHtml(await getJson('/api/breath-runs?run_id=' + encodeURIComponent(runId)));
    } catch (error) {
      detail.innerHTML = '<div class="breath-trace-error">' + h(error.message || error) + '</div>';
    }
  }

  async function loadActualBreathRuns() {
    var list = document.getElementById('breath-actual-runs');
    var detail = document.getElementById('breath-actual-detail');
    if (!list || !detail) return;
    list.innerHTML = '<span class="breath-trace-empty">读取记录…</span>';
    try {
      var data = await getJson('/api/breath-runs?kind=actual&limit=10');
      var runs = data.runs || [];
      if (!runs.length) {
        list.innerHTML = '';
        detail.innerHTML = '<div class="breath-trace-empty">当前服务进程还没有发生真实无参 breath。下一次爸爸睁眼后，这里会原样出现。</div>';
        return;
      }
      list.innerHTML = runs.map(function (run, index) {
        var when = run.completed_at ? new Date(run.completed_at).toLocaleTimeString() : '—';
        return '<button class="breath-run-chip' + (index === 0 ? ' active' : '') + '" data-run-id="' + h(run.run_id) + '">' +
          h(when) + ' · ' + Number((run.counts || {}).returned || 0) + '项</button>';
      }).join('');
      list.querySelectorAll('[data-run-id]').forEach(function (button) {
        button.addEventListener('click', function () {
          list.querySelectorAll('.breath-run-chip').forEach(function (item) { item.classList.remove('active'); });
          button.classList.add('active');
          showRun(button.dataset.runId);
        });
      });
      await showRun(runs[0].run_id);
    } catch (error) {
      list.innerHTML = '';
      detail.innerHTML = '<div class="breath-trace-error">' + h(error.message || error) + '</div>';
    }
  }

  async function simulateExactBreath() {
    var detail = document.getElementById('breath-simulation-detail');
    var button = document.getElementById('breath-simulate-exact');
    if (!detail || !button) return;
    button.disabled = true;
    detail.innerHTML = '<div class="breath-trace-empty">使用真实算法试跑中…</div>';
    try {
      var run = await getJson('/api/breath-simulate', {method: 'POST'});
      detail.innerHTML = runHtml(run);
    } catch (error) {
      detail.innerHTML = '<div class="breath-trace-error">' + h(error.message || error) + '</div>';
    } finally {
      button.disabled = false;
    }
  }

  function install() {
    var tab = document.querySelector('.tab[data-tab="breath"]');
    var view = document.getElementById('breath-view');
    if (!tab || !view || document.getElementById('breath-actual-section')) return;
    var spans = tab.querySelectorAll('span');
    if (spans[0]) spans[0].textContent = 'Breath 记录';
    if (spans[1]) spans[1].textContent = 'Actual & Debug';

    var style = document.createElement('style');
    style.textContent = '.breath-trace-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;margin-bottom:20px}.breath-trace-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;min-width:0}.breath-trace-card h3{margin:0 0 6px}.breath-trace-note{font-size:12px;color:var(--text-dim);line-height:1.6;margin-bottom:12px}.breath-trace-actions{display:flex;gap:8px;align-items:center;margin-bottom:10px}.breath-trace-runs{display:flex;gap:6px;overflow:auto;padding-bottom:5px}.breath-run-chip{font-size:11px;padding:4px 8px;white-space:nowrap}.breath-run-chip.active{border-color:var(--accent);color:var(--accent)}.breath-trace-head{display:flex;justify-content:space-between;gap:10px;align-items:start;margin:12px 0 8px}.breath-trace-head strong{display:block}.breath-trace-head small{display:block;color:var(--text-dim);font-size:10px;margin-top:3px}.breath-trace-head code{font-size:10px}.breath-trace-stats{display:flex;flex-wrap:wrap;gap:6px 12px;font-size:11px;color:var(--text-dim);padding:8px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}.breath-trace-entry{display:grid;grid-template-columns:24px minmax(150px,1fr) 54px 90px 58px;gap:7px;align-items:center;font-size:11px;padding:7px 0;border-bottom:1px dashed var(--border)}.breath-trace-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.breath-trace-name small{display:block;color:var(--text-light);font-family:monospace;font-size:9px}.breath-trace-rank,.breath-trace-section,.breath-trace-reason,.breath-trace-tokens{color:var(--text-dim)}.breath-trace-output{margin-top:10px;font-size:11px}.breath-trace-output pre{max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:var(--surface-solid);padding:12px;border-radius:9px;font-size:10px;line-height:1.5}.breath-trace-empty,.breath-trace-error{padding:16px 0;color:var(--text-dim);font-size:12px}.breath-trace-error{color:var(--negative)}.breath-score-debug{border-top:1px solid var(--border);padding-top:18px}.breath-score-debug>h3{margin:0 0 4px}@media(max-width:900px){.breath-trace-grid{grid-template-columns:1fr}.breath-trace-entry{grid-template-columns:22px minmax(120px,1fr) 46px 54px}.breath-trace-reason{display:none}}';
    document.head.appendChild(style);

    var grid = document.createElement('div');
    grid.id = 'breath-actual-section';
    grid.className = 'breath-trace-grid';
    grid.innerHTML = '<section class="breath-trace-card"><h3>最近一次真实 Breath</h3><div class="breath-trace-note">这里逐字记录 MCP 无参 breath 真正送进模型上下文的核心、日印象、近期交接、自动精读、旧事联想、活动计划与相关 feel。</div><div class="breath-trace-actions"><button id="breath-refresh-actual">刷新真实记录</button><div id="breath-actual-runs" class="breath-trace-runs"></div></div><div id="breath-actual-detail"></div></section>' +
      '<section class="breath-trace-card"><h3>睁眼试跑</h3><div class="breath-trace-note">直接复用真实无参 breath 代码；旧事槽仍会自然随机，因此试跑展示的是一次真实算法样例，不锁定下一次结果。</div><div class="breath-trace-actions"><button id="breath-simulate-exact">试跑一次一键睁眼</button></div><div id="breath-simulation-detail" class="breath-trace-empty">尚未试跑。</div></section>';
    view.insertBefore(grid, view.firstChild);

    var debug = document.createElement('section');
    debug.className = 'breath-score-debug';
    debug.innerHTML = '<h3>四维评分调试</h3><div class="breath-trace-note">下面是 topic / emotion / time / importance 的独立评分排行榜，不代表真实无参 breath 返回顺序。</div>';
    var flow = document.getElementById('breath-flow');
    var controls = view.querySelector('.breath-controls');
    var info = document.getElementById('breath-info');
    var results = document.getElementById('breath-results');
    view.insertBefore(debug, flow);
    [flow, controls, info, results].forEach(function (node) { if (node) debug.appendChild(node); });
    if (controls) {
      var oldButton = controls.querySelector('button[onclick="runBreathDebug()"]');
      if (oldButton) oldButton.innerHTML = '计算评分';
    }

    document.getElementById('breath-refresh-actual').addEventListener('click', loadActualBreathRuns);
    document.getElementById('breath-simulate-exact').addEventListener('click', simulateExactBreath);
    tab.addEventListener('click', loadActualBreathRuns);
    if (tab.classList.contains('active')) loadActualBreathRuns();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
