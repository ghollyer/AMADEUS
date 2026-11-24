// Thin wrappers over the amadeus-web REST API.

async function call(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    err.data = data;
    err.status = r.status;
    throw err;
  }
  return data;
}

export const api = {
  enqueueCall: (tool, args, label) => call('POST', '/api/queue/call', { tool, args, label }),
  enqueueSequence: (sequence, params, force) =>
    call('POST', '/api/queue/sequence', { sequence, params, force }),
  control: action => call('POST', '/api/queue/control', { action }),
  remove: id => call('DELETE', `/api/queue/${encodeURIComponent(id)}`),
  stopStage: () => call('POST', '/api/stop_stage'),
  sequences: () => call('GET', '/api/sequences'),
  getSequence: name => call('GET', `/api/sequences/${encodeURIComponent(name)}`),
  saveSequence: (name, seq) => call('PUT', `/api/sequences/${encodeURIComponent(name)}`, seq),
  deleteSequence: name => call('DELETE', `/api/sequences/${encodeURIComponent(name)}`),
  checkSequence: seq => call('POST', '/api/sequences/check', seq),
  images: () => call('GET', '/api/images'),
  refreshCapabilities: () => call('GET', '/api/capabilities?refresh=1'),
  say: text => call('POST', '/api/chat', { text }),
  answer: (question_id, answer) => call('POST', '/api/chat/answer', { question_id, answer }),
};

// Short "k=v, k=v" summary of tool arguments for block cards.
export function argSummary(args, max = 90) {
  const parts = Object.entries(args || {}).map(([k, v]) => {
    let s;
    if (v && typeof v === 'object' && '$role' in v) s = '@' + v.$role.split(':')[1];
    else if (v && typeof v === 'object' && '$expr' in v) s = '=' + v.$expr;
    else s = typeof v === 'string' ? v : JSON.stringify(v);
    return `${k}=${s}`;
  });
  const text = parts.join(', ');
  return text.length > max ? text.slice(0, max - 1) + '…' : text;
}

export function fmtDuration(b) {
  if (!b.started) return '';
  const end = b.finished || Date.now() / 1000;
  const s = end - b.started;
  return s < 1 ? `${Math.round(s * 1000)} ms` : s < 120 ? `${s.toFixed(1)} s`
    : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

export function fmtTime(t) {
  if (!t) return '';
  const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
  return d.toLocaleTimeString([], { hour12: false });
}
