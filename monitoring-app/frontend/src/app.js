import Chart from 'chart.js/auto';
import {
  getLatest, getHistory, getRequests, restartService, testRequest,
  getSettings, putSettings, getActions, notifyTest,
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
    $('#updated-at').textContent = 'нет соединения';
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

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tabpane').forEach((p) => p.classList.toggle('active', p.id === `tab-${name}`));
  if (name === 'requests') loadRequests();
  if (name === 'admin') loadAdmin();
}

export function init() {
  document.querySelectorAll('.tab').forEach((b) => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });
  bindRequests();
  bindAdmin();
  refreshLatest();
  loadChart();
  setInterval(() => {
    if (document.visibilityState === 'visible') refreshLatest();
  }, 10000);
}


