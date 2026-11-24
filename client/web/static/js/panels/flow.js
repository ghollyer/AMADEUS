// Flow programmer: Blockly workspace for building sequences with if/else,
// loops and variables. Validate checks the sequence against this
// microscope's capabilities. Run queues it, and Save writes
// <data_dir>/sequences/<name>.amseq.json.

import { api } from '../api.js';
import { blockJSON, defineControlBlocks, defineTheme, defineToolBlocks, sequenceToState,
  toolbox, workspaceToSequence } from '../flow_blocks.js';
import { getState, useStore } from '../store.js';

const { html, useEffect, useRef, useState } = window.htmPreact;
const B = window.Blockly;
const DRAFT_KEY = 'amadeus.flow.draft';

let controlsDefined = false;

const STARTER = {
  name: 'example: focus check, then capture', on_error: 'abort', body: [
    { type: 'set', var: 'threshold', value: '0.02' },
    { type: 'call', tool: 'take_stem_image', args: { mode: 'preview' }, as_var: 'img',
      ensure_mode: true },
    { type: 'call', tool: 'image_sharpness', args: { image_ids: { $expr: '[img.image_id]' } },
      as_var: 'sharp' },
    { type: 'if', cond: 'sharp.ranked[0].sharpness < threshold',
      then: [{ type: 'call', tool: 'stem_autofocus', args: {} }], else: [] },
    { type: 'call', tool: 'take_stem_image', args: { mode: 'capture' } },
  ],
};

function saveDraft(ws) {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(workspaceToSequence(ws))); } catch { /* */ }
}

function loadDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch { return null; }
}

function Report({ report }) {
  if (!report) return null;
  if (report.error) return html`<div class="report bad">${report.error}</div>`;
  return html`<div class=${`report ${report.ok ? 'good' : 'bad'}`}>
    <b>${report.ok ? '✓ Compatible with this microscope' : '✕ Not compatible with this microscope'}</b>
    ${report.capabilities_known === false && html`<span class="muted"> (capabilities unknown:
      relay not reachable, so only tools and expressions were checked)</span>`}
    ${report.missing && report.missing.length > 0 && html`<ul>
      ${report.missing.map(m => html`<li><code>${m.cap}</code> for ${m.tool || 'the sequence'}
        <small class="muted">${m.path}</small></li>`)}</ul>`}
    ${report.mode_guards && report.mode_guards.length > 0 && html`<div class="muted">Mode checks at
      run time: ${report.mode_guards.map(g => `${g.tool} → ${g.mode}${g.auto_switch ? ' (auto-switch)' : ''}`).join(', ')}</div>`}
    ${report.warnings && report.warnings.map(w => html`<div class="muted">⚠ ${w}</div>`)}
  </div>`;
}

export function FlowPanel() {
  const catalog = useStore(s => s.catalog);
  const flowLoad = useStore(s => s.flowLoad);
  const flowAppend = useStore(s => s.flowAppend);
  const host = useRef(null);
  const wsRef = useRef(null);
  const [file, setFile] = useState('');
  const [report, setReport] = useState(null);
  const [msg, setMsg] = useState('');

  // Inject once the catalog has arrived (tool blocks need their definitions).
  useEffect(() => {
    if (!catalog.length) return;
    if (!controlsDefined) { defineControlBlocks(); controlsDefined = true; }
    defineToolBlocks(catalog);
    if (wsRef.current) { wsRef.current.updateToolbox(toolbox(catalog)); return; }
    const ws = B.inject(host.current, {
      toolbox: toolbox(catalog), theme: defineTheme(), trashcan: true, sounds: false,
      grid: { spacing: 24, length: 2, colour: '#2a2e36', snap: true },
      zoom: { controls: true, wheel: true, startScale: 0.8 }, move: { scrollbars: true, wheel: false },
    });
    wsRef.current = ws;
    B.serialization.workspaces.load(sequenceToState(loadDraft() || STARTER), ws);
    ws.addChangeListener(e => { if (!e.isUiEvent) { saveDraft(ws); setReport(null); } });
    const ro = new ResizeObserver(() => B.svgResize(ws));
    ro.observe(host.current);
    return undefined;
  }, [catalog]);

  useEffect(() => {
    if (!flowLoad || !wsRef.current) return;
    wsRef.current.clear();
    B.serialization.workspaces.load(sequenceToState(flowLoad.seq), wsRef.current);
    setFile(flowLoad.file || '');
    setMsg(`loaded ${flowLoad.file || flowLoad.seq.name}`);
  }, [flowLoad && flowLoad.nonce]);

  useEffect(() => {
    const ws = wsRef.current;
    if (!flowAppend || !ws) return;
    const json = blockJSON({ type: 'call', tool: flowAppend.tool, args: flowAppend.args });
    const block = B.serialization.blocks.append(json, ws);
    let program = ws.getTopBlocks(true).find(b => b.type === 'amd_program');
    if (!program) {
      program = B.serialization.blocks.append({ type: 'amd_program', x: 24, y: 24 }, ws);
    }
    let last = program.getInputTargetBlock('BODY');
    if (!last) program.getInput('BODY').connection.connect(block.previousConnection);
    else {
      while (last.getNextBlock()) last = last.getNextBlock();
      last.nextConnection.connect(block.previousConnection);
    }
    block.select && block.select();
    setMsg(`added ${flowAppend.tool}`);
  }, [flowAppend && flowAppend.nonce]);

  const sequence = () => workspaceToSequence(wsRef.current, { name: file || 'untitled' });

  const validate = async () => {
    try { setReport(await api.checkSequence(sequence())); } catch (e) { setReport({ error: e.message }); }
  };
  const run = async () => {
    try {
      await api.enqueueSequence(sequence(), {});
      setMsg('queued');
      window.amadeus?.focusPanel('queue');
    } catch (e) {
      if (e.data && e.data.report) setReport({ ...e.data.report, capabilities_known: true });
      setMsg(e.message);
    }
  };
  const save = async () => {
    const seq = sequence();
    let name = file || seq.name;
    name = prompt('Save script as', name) || '';
    if (!name) return;
    try {
      const r = await api.saveSequence(name, seq);
      setFile(name);
      setReport({ ...r.report, capabilities_known: getState().caps.known });
      setMsg(`saved ${r.saved}`);
      window.dispatchEvent(new Event('amadeus:sequences-changed'));
    } catch (e) { setMsg(e.message); }
  };
  const fresh = () => {
    if (!confirm('Clear the flow editor?')) return;
    wsRef.current.clear();
    B.serialization.workspaces.load(sequenceToState({ name: 'untitled', body: [] }), wsRef.current);
    setFile('');
  };
  const exportJSON = () => {
    const seq = sequence();
    const blob = new Blob([JSON.stringify(seq, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${(file || seq.name).replace(/[^\w .-]/g, '_')}.amseq.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return html`
    <div class="panel flow">
      <div class="toolbar">
        <button onClick=${fresh}>New</button>
        <button onClick=${validate}>Validate</button>
        <button class="primary" onClick=${run}>▶ Run</button>
        <button onClick=${save}>Save…</button>
        <button onClick=${exportJSON}>Export</button>
        <span class="muted">${file ? `script: ${file}` : 'unsaved draft'} · ${msg}</span>
      </div>
      <${Report} report=${report} />
      ${!catalog.length && html`<div class="empty">Loading tool catalog…</div>`}
      <div class="blockly" ref=${host}></div>
    </div>`;
}
