// --- API helpers -------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401) {
    // Browser will have shown the Basic-auth dialog; let it retry.
    throw new Error('auth required');
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

export const getLatest = () => api('/api/latest');
export const getHistory = (metric, range) => {
  // minutes offset of local tz vs UTC (e.g. 180 for UTC+3) so server-side
  // chart labels match the user's clock
  const tz = -new Date().getTimezoneOffset();
  return api(`/api/history?metric=${metric}&range=${range}&tz=${tz}`);
};
export const getRequests = (p) => {
  const q = new URLSearchParams(p).toString();
  return api(`/api/requests?${q}`);
};
export const getStatus = () => api('/api/status');
export const restartService = (name) =>
  api(`/api/status/${name}/restart`, { method: 'POST' });
export const testRequest = (body) =>
  api('/api/test-request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
export const getSettings = () => api('/api/settings');
export const putSettings = (body) =>
  api('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
export const getActions = (limit = 50) => api(`/api/actions?limit=${limit}`);
export const notifyTest = () => api('/api/admin/notify-test', { method: 'POST' });

// --- API keys ---------------------------------------------------------------
export const getKeys = () => api('/api/keys');
export const createKey = (body) =>
  api('/api/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
export const blockKey = (id) => api(`/api/keys/${id}/block`, { method: 'POST' });
export const unblockKey = (id) => api(`/api/keys/${id}/unblock`, { method: 'POST' });
export const deleteKey = (id) => api(`/api/keys/${id}`, { method: 'DELETE' });
export const getKeyStats = (id) => api(`/api/keys/${id}/stats`);

// --- formatters ----------------------------------------------------------------

export function fmtBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtRate(n) {
  if (n == null || isNaN(n)) return '—';
  return `${fmtBytes(n)}/s`;
}

export function fmtMs(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1000) return `${(n / 1000).toFixed(2)} s`;
  return `${Math.round(n)} ms`;
}

export function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  return `${Number(n).toFixed(1)}%`;
}

export function fmtTs(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  if (isNaN(d)) return '—';
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtNum(n, digits = 1) {
  if (n == null || isNaN(n)) return '—';
  return Number(n).toFixed(digits).replace(/\.0+$/, '');
}
