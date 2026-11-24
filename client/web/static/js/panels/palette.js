// Blocks & scripts: pick a tool, fill its arguments, add it to the queue
// (or to the flow editor). Saved scripts (*.amseq.json) can be run,
// opened in the flow editor, imported and exported here.

import { api } from '../api.js';
import { baseType, buildArgs, defaultText, rolesFor } from '../args.js';
import { setState, useStore } from '../store.js';

const { html, useEffect, useMemo, useState } = window.htmPreact;

function ArgField({ name, prop, value, onInput, caps }) {
  const t = baseType(prop);
  const role = prop['x-role'];
  const placeholder = defaultText(prop) || (t === 'array' ? '[…]' : '');
  const id = `arg-${name}`;
  let input;
  if (prop.enum) {
    input = html`<select id=${id} value=${value} onChange=${e => onInput(e.target.value)}>
      <option value="">(default${placeholder ? `: ${placeholder}` : ''})</option>
      ${prop.enum.map(v => html`<option value=${v}>${v}</option>`)}
    </select>`;
  } else if (t === 'boolean') {
    input = html`<select id=${id} value=${value} onChange=${e => onInput(e.target.value)}>
      <option value="">(default${placeholder ? `: ${placeholder}` : ''})</option>
      <option value="true">true</option><option value="false">false</option>
    </select>`;
  } else {
    const roles = role ? rolesFor(caps, role) : [];
    input = html`<span class="argrow">
      <input id=${id} type="text" value=${value} placeholder=${placeholder}
        onInput=${e => onInput(e.target.value)} />
      ${roles.length > 0 && html`<select class="roles" value=""
          title=${`Use a ${role} role, so the block is portable between microscopes`}
          onChange=${e => e.target.value && onInput('@' + e.target.value)}>
        <option value="">${role} role…</option>
        ${roles.map(r => html`<option value=${r}>${r}</option>`)}
      </select>`}
    </span>`;
  }
  return html`
    <label class="arg" for=${id} title=${prop.description || ''}>
      <span class="argname">${name}<small>${t}</small></span>
      ${input}
      ${prop.description && html`<small class="arghelp">${prop.description}</small>`}
    </label>`;
}

function ToolForm({ tool, caps }) {
  const [values, setValues] = useState({});
  const [msg, setMsg] = useState('');
  useEffect(() => { setValues({}); setMsg(''); }, [tool.name]);
  const props = (tool.input_schema && tool.input_schema.properties) || {};
  const args = buildArgs(values, tool.input_schema);
  const enqueue = async () => {
    try {
      const b = await api.enqueueCall(tool.name, args);
      setMsg(`queued as ${b.id}`);
    } catch (e) { setMsg(e.message); }
  };
  const toFlow = () => {
    setState({ flowAppend: { tool: tool.name, args, nonce: Date.now() } });
    window.amadeus?.focusPanel('flow');
  };
  return html`
    <div class="toolform">
      <div class="formhead">
        <b>${tool.name}</b>
        ${tool.mode && html`<span class="tag">needs ${tool.mode}</span>`}
        ${!tool.hardware && html`<span class="tag soft">analysis</span>`}
      </div>
      <p class="summary">${tool.summary}</p>
      ${!tool.available && html`<div class="warn">Not available on this instrument:
        ${tool.missing.join(', ')}</div>`}
      ${Object.keys(props).length === 0 && html`<p class="muted">No arguments.</p>`}
      ${Object.entries(props).map(([name, prop]) => html`
        <${ArgField} key=${name} name=${name} prop=${prop} caps=${caps}
          value=${values[name] || ''} onInput=${v => setValues({ ...values, [name]: v })} />`)}
      <div class="hint">Empty = tool default · <code>=expr</code> = expression ·
        <code>@ROLE</code> = detector/camera role</div>
      <div class="actions">
        <button class="primary" onClick=${enqueue} disabled=${!tool.available}>Add to queue</button>
        <button onClick=${toFlow}>Add to flow</button>
        <span class="muted">${msg}</span>
      </div>
    </div>`;
}

function Scripts() {
  const caps = useStore(s => s.caps);
  const [list, setList] = useState([]);
  const [msg, setMsg] = useState('');
  const load = () => api.sequences().then(d => setList(d.sequences)).catch(e => setMsg(e.message));
  useEffect(() => { load(); }, [caps]);
  useEffect(() => {
    const h = () => load();
    window.addEventListener('amadeus:sequences-changed', h);
    return () => window.removeEventListener('amadeus:sequences-changed', h);
  }, []);

  const open = async name => {
    const seq = await api.getSequence(name);
    setState({ flowLoad: { seq, file: name, nonce: Date.now() } });
    window.amadeus?.focusPanel('flow');
  };
  const run = async name => {
    const seq = await api.getSequence(name);
    try {
      await api.enqueueSequence(seq, {});
      setMsg(`queued ${name}`);
    } catch (e) {
      const miss = e.data?.report?.missing?.map(m => m.cap).join(', ');
      setMsg(miss ? `not compatible: missing ${miss}` : e.message);
    }
  };
  const del = async name => {
    if (!confirm(`Delete script "${name}"?`)) return;
    await api.deleteSequence(name);
    load();
  };
  const exportFile = async name => {
    const seq = await api.getSequence(name);
    const blob = new Blob([JSON.stringify(seq, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${name}.amseq.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const importFile = async e => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const seq = JSON.parse(await file.text());
      const name = file.name.replace(/\.amseq\.json$|\.json$/i, '');
      const r = await api.saveSequence(name, seq);
      setMsg(r.report.ok ? `imported ${name}` :
        `imported ${name} (missing here: ${r.report.missing.map(m => m.cap).join(', ')})`);
      load();
    } catch (err) { setMsg(`import failed: ${err.message}`); }
    e.target.value = '';
  };

  return html`
    <div class="scripts">
      <div class="sechead">
        <b>Scripts</b><span class="spacer"></span>
        <label class="button">Import…<input type="file" accept=".json" hidden
          onChange=${importFile} /></label>
      </div>
      ${list.length === 0 && html`<p class="muted">No saved scripts. Build one in the Flow
        pane and save it.</p>`}
      ${list.map(s => html`
        <div class=${`script ${s.ok === false ? 'bad' : ''}`} key=${s.name}>
          <div class="sname" title=${s.description || ''}>
            ${s.ok === false ? '⚠ ' : ''}${s.name}
            ${s.missing && s.missing.length > 0 &&
              html`<small class="muted"> needs ${s.missing.join(', ')}</small>`}
            ${s.error && html`<small class="err"> ${s.error}</small>`}
          </div>
          <button class="mini" onClick=${() => run(s.name)} disabled=${s.ok === false}>Run</button>
          <button class="mini" onClick=${() => open(s.name)}>Edit</button>
          <button class="mini" onClick=${() => exportFile(s.name)}>Export</button>
          <button class="mini" onClick=${() => del(s.name)}>✕</button>
        </div>`)}
      <div class="muted">${msg}</div>
    </div>`;
}

export function PalettePanel() {
  const catalog = useStore(s => s.catalog);
  const caps = useStore(s => s.caps);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(null);
  const [showUnavailable, setShowUnavailable] = useState(true);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = new Map();
    for (const t of catalog) {
      if (!showUnavailable && !t.available) continue;
      if (q && !(t.name.includes(q) || (t.summary || '').toLowerCase().includes(q))) continue;
      if (!out.has(t.category)) out.set(t.category, []);
      out.get(t.category).push(t);
    }
    return [...out.entries()];
  }, [catalog, query, showUnavailable]);

  const tool = selected && catalog.find(t => t.name === selected);

  return html`
    <div class="panel palette">
      <div class="toolbar">
        <input type="search" placeholder="Find a tool…" value=${query}
          onInput=${e => setQuery(e.target.value)} />
        <label class="check"><input type="checkbox" checked=${showUnavailable}
          onChange=${e => setShowUnavailable(e.target.checked)} /> unavailable</label>
      </div>
      <div class="split">
        <div class="toollist">
          ${groups.map(([cat, tools]) => html`
            <div class="cat" key=${cat}>
              <div class="catname">${cat}</div>
              ${tools.map(t => html`
                <div key=${t.name}
                  class=${`tool ${t.available ? '' : 'na'} ${selected === t.name ? 'sel' : ''}`}
                  title=${t.available ? t.summary : `Missing: ${t.missing.join(', ')}`}
                  onClick=${() => setSelected(t.name)}>
                  ${t.name}${t.mode ? html`<small> ${t.mode}</small>` : ''}
                </div>`)}
            </div>`)}
        </div>
        <div class="toolside">
          ${tool ? html`<${ToolForm} tool=${tool} caps=${caps} />`
            : html`<p class="muted pad">Pick a tool to configure a block.</p>`}
          <${Scripts} />
        </div>
      </div>
    </div>`;
}

