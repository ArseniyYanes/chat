import Chart from 'chart.js/auto';
import {
  getLatest, getHistory, getRequests, restartService, testRequest,
  getSettings, putSettings, getActions, notifyTest,
  getKeys, createKey, blockKey, unblockKey, deleteKey, getKeyStats, getKeyUsage, getKeysSummary,
  fmtBytes, fmtRate, fmtMs, fmtPct, fmtTs, fmtNum,
} from './api.js';

const METRICS = [
  ['cpu_pct', 'CPU, %'],
  ['gpu_util', 'GPU утилизация, %'],
  ['gpu_temp', 'GPU температура, °C'],
  ['ram_pct', 'RAM, %'],
  ['net_rx', 'Сеть RX'],
  ['net_tx', 'Сеть TX'],
  ['disk_read', 'Диск чтение'],
  ['disk_write', 'Диск запись'],
  ['vllm_active', 'vLLM активные'],
  ['vllm_tokens_in', 'vLLM ток/с (вход)'],
  ['vllm_tokens_out', 'vLLM ток/с (выход)'],
  ['vllm_ttft', 'vLLM TTFT, ms'],
  ['vllm_tpot', 'vLLM TPOT, ms'],
];

const state = {
  tab: 'dashboard',
  metric: 'cpu_pct',
  range: '24h',
  page: 0,
  pageSize: 50,
  filters: { q: '', user: '', model: '', status: '' },
};

let chart = null;
let keysCache = [];
let keyCharts = {};
let summaryChart = null;
let proxyUrl = '';

const $ = (sel) => document.querySelector(sel);

function esc(v) {
  if (v == null) return '';
  return String(v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

let toastTimer = null;
function toast(msg, kind = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 4000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch { /* ignore */ }
    ta.remove();
    return true;
  }
}

function row(k, v) {
  return `<div class="row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
}

function bar(pct) {
  const cls = pct >= 90 ? 'err' : pct >= 75 ? 'warn' : '';
  const w = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="bar"><i class="${cls}" style="width:${w}%"></i></div>`;
}

function card(title, bodyHtml) {
  return `<div class="card"><h3>${title}</h3>${bodyHtml}</div>`;
}

function gpuCards(gpu) {
  if (!gpu || !gpu.length) {
    return card('GPU', row('—', 'NVIDIA GPU не обнаружены'));
  }
  return gpu.map((g) => {
    const util = Number(g.util);
    const mem = g.mem_total_mib
      ? `${fmtBytes(g.mem_used_mib * 1048576)} / ${fmtBytes(g.mem_total_mib * 1048576)}`
      : '—';
    const title = g.index != null ? `GPU ${g.index}` : 'GPU';
    const sub = g.name ? ` · ${esc(g.name)}` : '';
    return card(
      `<span>${title}${sub}</span>`,
      `<div class="big">${util != null ? `${fmtPct(util)}` : '—'}</div>` +
      row('Температура', fmtNum(g.temp) != '—' ? `${fmtNum(g.temp)} °C` : '—') +
      row('VRAM', mem) +
      row('Мощность', g.power_w != null ? `${fmtNum(g.power_w)} W` : '—') +
      bar(util),
    );
  }).join('');
}

function cpuCard(c) {
  if (!c) return card('CPU', row('—', 'нет данных'));
  const pct = Number(c.pct);
  return card(
    'CPU',
    `<div class="big">${fmtPct(pct)}</div>` +
    row('Load (1/5/15)', `${fmtNum(c.load1)} / ${fmtNum(c.load5)} / ${fmtNum(c.load15)}`) +
    row('Ядра', (c.per_core || []).length || '—') +
    bar(pct),
  );
}

function ramCard(r) {
  if (!r) return card('RAM', row('—', 'нет данных'));
  const pct = Number(r.pct);
  return card(
    'RAM',
    `<div class="big">${fmtPct(pct)}</div>` +
    row('Использовано', `${fmtBytes(r.used_mb * 1048576)} / ${fmtBytes(r.total_mb * 1048576)}`) +
    row('Доступно', fmtBytes(r.available_mb * 1048576)) +
    row('Swap', r.swap_total_mb ? `${fmtBytes(r.swap_used_mb * 1048576)} / ${fmtBytes(r.swap_total_mb * 1048576)}` : '—') +
    bar(pct),
  );
}

function diskCard(d) {
  if (!d) return card('Диск', row('—', 'нет данных'));
  const pct = Number(d.usage_pct);
  return card(
    'Диск /',
    `<div class="big">${fmtPct(pct)}</div>` +
    row('Занято', `${fmtBytes(d.used_gb * 1073741824)} / ${fmtBytes(d.total_gb * 1073741824)}`) +
    row('Чтение', fmtRate(d.read_bps)) +
    row('Запись', fmtRate(d.write_bps)) +
    bar(pct),
  );
}

function netCard(n) {
  if (!n) return card('Сеть', row('—', 'нет данных'));
  return card(
    'Сеть',
    `<div class="big">${fmtRate(n.rx_bps)}</div>` +
    row('Входящий', fmtRate(n.rx_bps)) +
    row('Исходящий', fmtRate(n.tx_bps)),
  );
}

function vllmCard(v) {
  if (!v) return card('vLLM', row('—', 'нет данных'));
  if (v.error) {
    return card('vLLM', `<div class="big" style="color:var(--err)">недоступен</div>` + row('Ошибка', esc(v.error)));
  }
  return card(
    `vLLM ${v.version ? `· v${esc(v.version)}` : ''}`,
    row('Активные / ожидание', `${fmtNum(v.active, 0)} / ${fmtNum(v.waiting, 0)}`) +
    row('KV cache', fmtPct(v.kv_cache_pct)) +
    row('Prefix hit', fmtPct(v.prefix_hit_pct)) +
    row('Токены/с (вход)', fmtNum(v.tokens_in_s)) +
    row('Токены/с (выход)', fmtNum(v.tokens_out_s)) +
    row('TTFT', fmtMs(v.ttft_ms)) +
    row('TPOT', fmtMs(v.tpot_ms)) +
    row('E2E', fmtMs(v.e2e_ms)),
  );
}

function renderCards(d) {
  $('#cards').innerHTML =
    gpuCards(d.gpu) + cpuCard(d.cpu) + ramCard(d.ram) +
    diskCard(d.disk) + netCard(d.net) + vllmCard(d.vllm);
}

function serviceRow(s) {
  const up = s.up ? '<span class="badge ok">up</span>' : '<span class="badge err">down</span>';
  const restart =
    ['vllm', 'openwebui', 'db', 'redis'].includes(s.name)
      ? `<button class="btn small" data-restart="${esc(s.name)}">restart</button>`
      : '';
  return (
    `<tr><td>${esc(s.name)}</td><td>${up}</td>` +
    `<td>${s.latency_ms != null ? fmtMs(s.latency_ms) : '—'}</td>` +
    `<td>${esc(s.version || '—')}</td>` +
    `<td>${fmtTs(s.last_ok)}</td><td>${fmtTs(s.last_check)}</td><td>${restart}</td></tr>`
  );
}

function renderServices(services) {
  const tb = $('#services-table tbody');
  tb.innerHTML = (services || []).map(serviceRow).join('') ||
    '<tr><td colspan="7" class="muted">нет данных</td></tr>';
  tb.querySelectorAll('[data-restart]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.restart;
      if (!confirm(`Запустить «docker compose restart ${name}»?`)) return;
      try {
        const r = await restartService(name);
        toast(`Перезапуск ${name}: ${r.detail || 'выполняется'}`, 'ok');
      } catch (e) {
        toast(`Ошибка: ${e.message}`, 'err');
      }
    });
  });
}

const CHART_UNIT = {
  cpu_pct: '%', ram_pct: '%', gpu_util: '%',
  vllm_ttft: ' ms', vllm_tpot: ' ms',
};

function valueSuffix(metric, v) {
  if (v == null) return null;
  if (['net_rx', 'net_tx', 'disk_read', 'disk_write'].includes(metric)) return fmtBytes(v);
  return fmtNum(v) + (CHART_UNIT[metric] || '');
}

async function loadChart() {
  const canvas = $('#history-chart');
  try {
    const d = await getHistory(state.metric, state.range);
    if (!chart) {
      chart = new Chart(canvas, {
        type: 'line',
        data: { labels: [], datasets: [] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) =>
                  ` ${valueSuffix(state.metric, ctx.parsed.y) ?? ''}`,
              },
            },
          },
          scales: {
            x: { ticks: { color: '#7d8a9e', maxTicksLimit: 10 }, grid: { color: '#252d3d' } },
            y: { ticks: { color: '#7d8a9e' }, grid: { color: '#252d3d' } },
          },
        },
      });
    }
    chart.data.labels = d.labels;
    chart.data.datasets = [
      {
        label: state.metric,
        data: d.values,
        borderColor: '#4f8cff',
        backgroundColor: 'rgba(79, 140, 255, 0.12)',
        fill: true,
        pointRadius: 0,
        borderWidth: 2,
        tension: 0.25,
        spanGaps: true,
      },
    ];
    chart.update();
  } catch (e) {
    toast(`График: ${e.message}`, 'err');
  }
}

function setConn(ok) {
  const dot = $('#conn-dot');
  dot.className = `brand-dot ${ok ? 'ok' : 'err'}`;
}

async function refreshLatest() {
  try {
    const d = await getLatest();
    setConn(true);
    $('#updated-at').textContent = d.ts ? `обновлено ${fmtTs(d.ts)}` : 'нет данных';
    renderCards(d);
    renderServices(d.services);
  } catch (e) {
    setConn(false);
    $('#updated-at').textContent = e.message === 'auth required'
      ? 'нет доступа: введите логин/пароль'
      : 'нет соединения';
  }
}

async function loadRequests() {
  const p = {
    limit: state.pageSize,
    offset: state.page * state.pageSize,
    q: state.filters.q,
    user: state.filters.user,
    model: state.filters.model,
    status: state.filters.status,
  };
  const tb = $('#requests-table tbody');
  tb.innerHTML = '<tr><td colspan="8" class="muted">загрузка…</td></tr>';
  try {
    const d = await getRequests(p);
    const pages = Math.max(1, Math.ceil((d.total || 0) / state.pageSize));
    $('#req-total').textContent = `всего: ${d.total}`;
    $('#req-page').textContent = `${state.page + 1} / ${pages}`;
    $('#req-prev').disabled = state.page === 0;
    $('#req-next').disabled = state.page + 1 >= pages;
    tb.innerHTML = (d.items || []).map((r) => (
      `<tr>` +
      `<td>${fmtTs(r.ts)}</td>` +
      `<td>${esc(r.source)}</td>` +
      `<td>${esc(r.user || '—')}</td>` +
      `<td>${esc(r.model || '—')}</td>` +
      `<td class="wrap" title="${esc(r.prompt)}">${esc(r.prompt ? (r.prompt.length > 120 ? r.prompt.slice(0, 120) + '…' : r.prompt) : '—')}</td>` +
      `<td>${r.prompt_tokens != null ? r.prompt_tokens : '—'} / ${r.completion_tokens != null ? r.completion_tokens : '—'}</td>` +
      `<td>${r.latency_ms != null ? fmtMs(r.latency_ms) : '—'}</td>` +
      `<td><span class="badge ${r.status === 'ok' ? 'ok' : 'err'}">${esc(r.status)}</span></td>` +
      `</tr>`
    )).join('') || '<tr><td colspan="8" class="muted">запросов не найдено</td></tr>';
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="8" class="muted">${esc(e.message)}</td></tr>`;
  }
}

function bindRequests() {
  const metricSel = $('#metric-select');
  metricSel.innerHTML = METRICS.map(([v, label]) =>
    `<option value="${v}" ${v === state.metric ? 'selected' : ''}>${esc(label)}</option>`
  ).join('');
  metricSel.addEventListener('change', () => { state.metric = metricSel.value; loadChart(); });
  $('#range-select').addEventListener('change', () => { state.range = $('#range-select').value; loadChart(); });
  $('#refresh-chart').addEventListener('click', loadChart);

  $('#apply-filters').addEventListener('click', () => {
    state.filters = {
      q: $('#f-q').value.trim(),
      user: $('#f-user').value.trim(),
      model: $('#f-model').value.trim(),
      status: $('#f-status').value,
    };
    state.page = 0;
    loadRequests();
  });
  const onEnter = (e) => { if (e.key === 'Enter') $('#apply-filters').click(); };
  ['f-q', 'f-user', 'f-model'].forEach((id) => $('#' + id).addEventListener('keydown', onEnter));
  $('#req-prev').addEventListener('click', () => { state.page = Math.max(0, state.page - 1); loadRequests(); });
  $('#req-next').addEventListener('click', () => { state.page += 1; loadRequests(); });

  $('#t-send').addEventListener('click', async () => {
    const prompt = $('#t-prompt').value.trim();
    if (!prompt) { toast('Введите prompt', 'err'); return; }
    const btn = $('#t-send');
    const res = $('#t-result');
    btn.disabled = true;
    res.textContent = 'Выполняется запрос…';
    res.className = 'muted';
    try {
      const r = await testRequest({
        prompt,
        model: $('#t-model').value.trim(),
        max_tokens: Number($('#t-max').value) || 256,
        temperature: Number($('#t-temp').value) || 0.7,
      });
      res.textContent = `OK, задержка ${fmtMs(r.latency_ms)}`;
      res.className = 'muted';
      res.style.color = 'var(--ok)';
    } catch (e) {
      res.textContent = `Ошибка: ${e.message}`;
      res.style.color = 'var(--err)';
    }
    btn.disabled = false;
  });
}

async function loadAdmin() {
  try {
    const s = await getSettings();
    $('#s-gpu').value = s.gpu_threshold;
    $('#s-err').value = s.error_rate;
    $('#s-notif').checked = !!s.notifications_enabled;
    $('#s-tg').value = s.telegram_chat_id || '';
  } catch (e) {
    toast(`Настройки: ${e.message}`, 'err');
  }
  try {
    const a = await getActions(50);
    const tb = $('#actions-table tbody');
    tb.innerHTML = (a.items || []).map((x) => (
      `<tr><td>${fmtTs(x.ts)}</td><td>${esc(x.user)}</td><td>${esc(x.action)}</td>` +
      `<td class="wrap muted">${esc(x.details ? JSON.stringify(x.details) : '')}</td></tr>`
    )).join('') || '<tr><td colspan="4" class="muted">пока нет действий</td></tr>';
  } catch (e) {
    /* optional */
  }
}

function bindAdmin() {
  $('#s-save').addEventListener('click', async () => {
    const payload = {
      gpu_threshold: Number($('#s-gpu').value),
      error_rate: Number($('#s-err').value),
      notifications_enabled: $('#s-notif').checked,
      telegram_chat_id: $('#s-tg').value.trim(),
    };
    const msg = $('#s-msg');
    try {
      await putSettings(payload);
      msg.textContent = 'Сохранено';
      msg.style.color = 'var(--ok)';
      toast('Настройки сохранены', 'ok');
    } catch (e) {
      msg.textContent = e.message;
      msg.style.color = 'var(--err)';
    }
  });
  $('#s-notify-test').addEventListener('click', async () => {
    const btn = $('#s-notify-test');
    btn.disabled = true;
    try {
      const r = await notifyTest();
      toast(r.sent ? 'Уведомление отправлено' : 'Не отправлено (проверьте TELEGRAM_* / кулдаун)', r.sent ? 'ok' : 'err');
    } catch (e) {
      toast(`Ошибка: ${e.message}`, 'err');
    }
    btn.disabled = false;
  });
}

// ---------------------------------------------------------------------------
// API keys tab
// ---------------------------------------------------------------------------
function keyCard(k) {
  const active = k.is_active;
  const status = active
    ? '<span class="key-status ok">● активен</span>'
    : '<span class="key-status err">● заблокирован</span>';
  const toggle = active
    ? `<button class="btn small" data-action="block" data-id="${k.id}">Заблокировать</button>`
    : `<button class="btn small" data-action="unblock" data-id="${k.id}">Разблокировать</button>`;
  const masked = `${esc(k.prefix || '—')}${'•'.repeat(16)}`;
  return `<div class="key-card${active ? '' : ' blocked'}">
    <div class="key-card-head">
      <div class="key-id-block">
        <div class="key-name">${esc(k.name)}</div>
        <div class="key-id muted mono" title="Ключ показывается один раз">${masked}</div>
      </div>
      ${status}
    </div>
    <div class="key-grid">
      <div class="key-metric"><span>Создан</span><b>${fmtTs(k.created_at)}</b></div>
      <div class="key-metric"><span>Последний запрос</span><b>${fmtTs(k.last_used_at)}</b></div>
      <div class="key-metric"><span>Запросов всего</span><b>${fmtNum(k.total_requests, 0)}</b></div>
      <div class="key-metric"><span>Токенов всего</span><b>${fmtNum(k.total_tokens, 0)}</b></div>
      <div class="key-metric"><span>Лимит, req/мин</span><b>${fmtNum(k.rate_limit, 0)}</b></div>
      <div class="key-metric"><span>Лимит, ток/день</span><b>${fmtNum(k.daily_token_limit, 0)}</b></div>
      <div class="key-metric"><span>Ср. скорость ответа</span><b title="Токены/сек за всё время ключа">${k.avg_tokens_per_s != null ? `${fmtNum(k.avg_tokens_per_s, 1)} ток/с` : '—'}</b></div>
      <div class="key-metric"><span>Ср. задержка</span><b title="Среднее время ответа за всё время ключа">${k.avg_latency_ms != null ? fmtMs(k.avg_latency_ms) : '—'}</b></div>
    </div>
    <div class="key-chart-wrap"><canvas id="kchart-${k.id}"></canvas></div>
    <div class="key-usage-head muted">История запросов (последние 30)</div>
    <div id="kusage-${k.id}" class="key-usage">загрузка…</div>
    <div class="key-actions">
      ${toggle}
      <button class="btn small" data-action="copy" data-id="${k.id}" title="Скопировать маскированную форму">Копировать</button>
      <button class="btn small danger" data-action="delete" data-id="${k.id}">Удалить</button>
    </div>
  </div>`;
}

async function drawKeyChart(k) {
  const cv = document.getElementById(`kchart-${k.id}`);
  if (!cv) return;
  try {
    const s = await getKeyStats(k.id);
    if (keyCharts[k.id]) { keyCharts[k.id].destroy(); keyCharts[k.id] = null; }
    keyCharts[k.id] = new Chart(cv, {
      type: 'bar',
      data: {
        labels: (s.days || []).map((d) => d.slice(5)),
        datasets: [{
          label: 'токены',
          data: s.tokens || [],
          backgroundColor: 'rgba(79, 140, 255, 0.55)',
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => ` ${fmtNum(ctx.parsed.y, 0)} токенов / ${(s.requests || [])[ctx.dataIndex] ?? 0} запросов`,
            },
          },
        },
        scales: {
          x: { ticks: { color: '#7d8a9e', maxRotation: 0 }, grid: { display: false } },
          y: { ticks: { color: '#7d8a9e' }, grid: { color: '#252d3d' } },
        },
      },
    });
  } catch (e) {
    /* chart is optional */
  }
}

async function drawKeyUsage(k) {
  const box = document.getElementById(`kusage-${k.id}`);
  if (!box) return;
  try {
    const s = await getKeyUsage(k.id, 30);
    const items = s.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="muted">Запросов пока нет.</div>';
      return;
    }
    box.innerHTML = `<table class="table usage-tbl"><thead><tr>
      <th>Время</th><th>in</th><th>out</th><th>всего</th><th>задержка</th><th>статус</th><th>IP</th>
    </tr></thead><tbody>` +
      items.map((r) => `<tr>
      <td>${fmtTs(r.request_time)}</td>
      <td>${fmtNum(r.input_tokens, 0)}</td>
      <td>${fmtNum(r.output_tokens, 0)}</td>
      <td>${fmtNum(r.total_tokens, 0)}</td>
      <td>${r.latency_ms != null ? fmtMs(r.latency_ms) : '—'}</td>
      <td><span class="badge ${r.status_code < 400 ? 'ok' : 'err'}">${r.status_code ?? '—'}</span></td>
      <td class="mono muted">${esc(r.ip_address || '—')}</td>
    </tr>`).join('') +
      '</tbody></table>';
  } catch (e) {
    box.innerHTML = `<div class="muted">нет данных (${esc(e.message)})</div>`;
  }
}

function renderKeys() {
  const wrap = $('#keys-list');
  Object.values(keyCharts).forEach((c) => { if (c) c.destroy(); });
  keyCharts = {};
  if (!keysCache.length) {
    wrap.innerHTML = '<div class="muted">Ключей пока нет — сгенерируйте первый.</div>';
    return;
  }
  wrap.innerHTML = keysCache.map(keyCard).join('');
  keysCache.forEach((k) => { drawKeyChart(k); drawKeyUsage(k); });
}

async function loadKeys() {
  const wrap = $('#keys-list');
  wrap.innerHTML = '<div class="muted">загрузка…</div>';
  try {
    const d = await getKeys();
    keysCache = d.items || [];
    renderKeys();
  } catch (e) {
    keysCache = [];
    wrap.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
  loadKeysSummary();
}

async function loadKeysSummary() {
  try {
    const s = await getKeysSummary();
    const t = s.totals || {};
    $('#ks-total-keys').textContent = fmtNum(t.total_keys, 0);
    $('#ks-active-keys').textContent = `${fmtNum(t.active_keys, 0)} / ${fmtNum(t.blocked_keys, 0)}`;
    $('#ks-total-req').textContent = fmtNum(t.total_requests, 0);
    $('#ks-total-tok').textContent = fmtNum(t.total_tokens, 0);
    $('#ks-today-req').textContent = fmtNum(t.today_requests, 0);
    $('#ks-today-tok').textContent = fmtNum(t.today_tokens, 0);

    if (s.proxy && s.proxy.chat_completions) {
      proxyUrl = s.proxy.chat_completions;
      const el = $('#proxy-url');
      if (el) el.textContent = proxyUrl;
    }

    const cv = document.getElementById('keys-summary-chart');
    if (cv && s.series) {
      if (summaryChart) { summaryChart.destroy(); summaryChart = null; }
      summaryChart = new Chart(cv, {
        type: 'line',
        data: {
          labels: (s.series.days || []).map((d) => d.slice(5)),
          datasets: [
            {
              label: 'токены',
              data: s.series.tokens || [],
              borderColor: '#4f8cff',
              backgroundColor: 'rgba(79,140,255,0.18)',
              tension: 0.3, fill: true, pointRadius: 2,
            },
            {
              label: 'запросы',
              data: s.series.requests || [],
              borderColor: '#2fd0a5',
              backgroundColor: 'rgba(47,208,165,0.15)',
              tension: 0.3, fill: false, pointRadius: 2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: { legend: { labels: { color: '#9fb0c6' } } },
          scales: {
            x: { ticks: { color: '#7d8a9e', maxRotation: 0 }, grid: { display: false } },
            y: { ticks: { color: '#7d8a9e' }, grid: { color: '#252d3d' } },
          },
        },
      });
    }
  } catch (e) {
    /* summary is best-effort */
  }
}

function openGenerateModal() {
  const ov = document.createElement('div');
  ov.className = 'modal-overlay';
  ov.innerHTML = `<div class="modal">
    <div class="modal-head"><h3>Новый API-ключ</h3><button class="modal-close" type="button">×</button></div>
    <div class="modal-body">
      <label class="field">Название ключа
        <input id="nk-name" type="text" placeholder="например, dev-team" />
      </label>
      <div class="field-row">
        <label class="field">Лимит, запросов/мин
          <input id="nk-rate" type="number" value="60" min="1" />
        </label>
        <label class="field">Лимит, токенов/день
          <input id="nk-tokens" type="number" value="1000000" min="1" />
        </label>
      </div>
      <label class="field">Мастер-пароль
        <input id="nk-master" type="password" placeholder="********" autocomplete="off" />
      </label>
      <div id="nk-err" class="field-err"></div>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" data-close>Отмена</button>
      <button class="btn primary" type="button" id="nk-generate">Сгенерировать</button>
    </div>
  </div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.querySelector('.modal-close').addEventListener('click', close);
  ov.querySelector('[data-close]').addEventListener('click', close);
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#nk-generate').addEventListener('click', async () => {
    const body = {
      name: ov.querySelector('#nk-name').value.trim(),
      rate_limit: Number(ov.querySelector('#nk-rate').value) || 60,
      daily_token_limit: Number(ov.querySelector('#nk-tokens').value) || 1000000,
      master_password: ov.querySelector('#nk-master').value,
    };
    const err = ov.querySelector('#nk-err');
    if (!body.name) { err.textContent = 'Введите название ключа'; return; }
    const btn = ov.querySelector('#nk-generate');
    btn.disabled = true;
    try {
      const r = await createKey(body);
      close();
      openKeyReveal(r);
      loadKeys();
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
    }
  });
}

function openKeyReveal(r) {
  const ov = document.createElement('div');
  ov.className = 'modal-overlay';
  ov.innerHTML = `<div class="modal reveal">
    <div class="modal-head"><h3>Ключ создан</h3></div>
    <div class="modal-body">
      <p class="reveal-note">Скопируйте ключ сейчас — он хранится только как хеш и больше
      не будет показан никому, включая вас.</p>
      <div class="key-reveal mono">${esc(r.key)}</div>
    </div>
    <div class="modal-foot">
      <button class="btn primary" type="button" id="rk-copy">Копировать ключ</button>
      <button class="btn" type="button" data-close>Понятно</button>
    </div>
  </div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.querySelector('[data-close]').addEventListener('click', close);
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#rk-copy').addEventListener('click', async () => {
    await copyText(r.key);
    const b = ov.querySelector('#rk-copy');
    b.textContent = 'Скопировано';
    setTimeout(() => { b.textContent = 'Копировать ключ'; }, 1500);
  });
}

function bindKeys() {
  $('#keys-generate').addEventListener('click', openGenerateModal);
  const pc = $('#proxy-copy');
  if (pc) {
    pc.addEventListener('click', async () => {
      if (!proxyUrl) { toast('URL ещё не загружен', 'err'); return; }
      await copyText(proxyUrl);
      const prev = pc.textContent;
      pc.textContent = '✓ Скопировано';
      setTimeout(() => { pc.textContent = prev; }, 1500);
    });
  }
  $('#keys-list').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const id = btn.dataset.id;
    const action = btn.dataset.action;
    const item = keysCache.find((k) => k.id === id);
    try {
      if (action === 'copy') {
        const masked = item ? `${item.prefix || '—'}${'•'.repeat(16)}` : id;
        await copyText(masked);
        toast('Скопирована маскированная форма ключа', 'ok');
      } else if (action === 'block') {
        await blockKey(id);
        toast('Ключ заблокирован', 'ok');
        loadKeys();
      } else if (action === 'unblock') {
        await unblockKey(id);
        toast('Ключ разблокирован', 'ok');
        loadKeys();
      } else if (action === 'delete') {
        if (!confirm(`Удалить ключ «${item ? item.name : id}»? Это необратимо.`)) return;
        await deleteKey(id);
        toast('Ключ удалён', 'ok');
        loadKeys();
      }
    } catch (err) {
      toast(`Ошибка: ${err.message}`, 'err');
    }
  });
}

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.toggle('active', p.id === `tab-${name}`));
  if (name === 'requests') loadRequests();
  if (name === 'keys') loadKeys();
  if (name === 'admin') loadAdmin();
}

export function init() {
  document.querySelectorAll('.tab').forEach((b) => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });
  bindRequests();
  bindAdmin();
  bindKeys();
  refreshLatest();
  loadChart();
  setInterval(() => {
    if (document.visibilityState === 'visible') refreshLatest();
  }, 10000);
}


