// Tool-argument helpers shared by the Blocks form and the flow editor.
//
// Text entered for an argument means:
//   ""            leave the argument out (the tool's default applies)
//   "=expr"       {"$expr": "expr"}: evaluated when the block runs
//   "@HAADF"      {"$role": "<kind>:HAADF"}: this microscope's index for a role
//   anything else parsed by the argument's JSON type

export function baseType(prop) {
  if (!prop) return 'string';
  if (prop.type) return prop.type;
  const alt = (prop.anyOf || []).map(a => a.type).filter(t => t && t !== 'null');
  return alt[0] || 'string';
}

export function nullable(prop) {
  return !!(prop && (prop.anyOf || []).some(a => a.type === 'null'));
}

export function defaultText(prop) {
  if (!prop || prop.default === undefined || prop.default === null) return '';
  return typeof prop.default === 'string' ? prop.default : JSON.stringify(prop.default);
}

export function parseArg(text, prop) {
  if (text === undefined || text === null) return undefined;
  if (typeof text !== 'string') return text;
  const raw = text.trim();
  if (raw === '') return undefined;
  if (raw.startsWith('=')) return { $expr: raw.slice(1).trim() };
  const role = prop && prop['x-role'];
  if (raw.startsWith('@')) return { $role: `${role || 'detector'}:${raw.slice(1)}` };
  const t = baseType(prop);
  if (t === 'integer' || t === 'number') {
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  if (t === 'boolean') return raw === 'true' ? true : raw === 'false' ? false : raw;
  if (t === 'array' || t === 'object') {
    try { return JSON.parse(raw); } catch { return raw; }
  }
  return raw;
}

// Inverse of parseArg, for loading a saved sequence into editors.
export function argToText(value) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'object' && !Array.isArray(value)) {
    if ('$expr' in value) return '=' + value.$expr;
    if ('$role' in value) return '@' + String(value.$role).split(':').slice(1).join(':');
  }
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function buildArgs(values, schema) {
  const props = (schema && schema.properties) || {};
  const out = {};
  for (const [name, text] of Object.entries(values)) {
    const v = parseArg(text, props[name]);
    if (v !== undefined) out[name] = v;
  }
  return out;
}

export function rolesFor(caps, kind) {
  return Object.keys(((caps && caps.roles) || {})[kind + 's'] || {});
}
