// AMADEUS web console shell: dockable / floating panes (dockview) plus the
// status header. Each pane is a Preact component mounted into a dockview
// panel. The layout is remembered per browser.

import { api } from './api.js';
import { FlowPanel } from './panels/flow.js';
import { ImagesPanel } from './panels/images.js';
import { PalettePanel } from './panels/palette.js';
import { QueuePanel } from './panels/queue.js';
import { Confirm, TerminalPanel } from './panels/terminal.js';
import { connectEvents, refreshCatalog, useStore } from './store.js';

const { html, render, useState } = window.htmPreact;
const { DockviewComponent, themeDark } = window['dockview-core'];
const LAYOUT_KEY = 'amadeus.layout.v1';

const PANELS = {
  queue: { title: 'Actions queue', comp: QueuePanel },
  palette: { title: 'Blocks & scripts', comp: PalettePanel },
  terminal: { title: 'Agent terminal', comp: TerminalPanel },
  images: { title: 'Image viewer', comp: ImagesPanel },
  flow: { title: 'Flow programmer', comp: FlowPanel },
};

class FloatAction {
  constructor(group) {
    this.group = group;
    this.element = document.createElement('div');
    this.element.className = 'dv-actions';
    const btn = document.createElement('button');
    btn.title = 'Float this pane (drag its tab back into the grid to dock it)';
    btn.textContent = '⧉';
    btn.onclick = () => {
      if (group.api.location.type !== 'floating') dock.addFloatingGroup(group);
    };
    this.element.appendChild(btn);
  }
  init() {}
  dispose() {}
}

const dock = new DockviewComponent(document.getElementById('dock'), {
  theme: themeDark,
  floatingGroupBounds: 'boundedWithinViewport',
  createComponent(options) {
    const element = document.createElement('div');
    element.className = 'dvpanel';
    const spec = PANELS[options.name];
    return {
      element,
      init() { render(html`<${spec.comp} />`, element); },
      dispose() { render(null, element); },
    };
  },
  createRightHeaderActionComponent: group => new FloatAction(group),
});

function defaultLayout() {
  dock.clear();
  dock.addPanel({ id: 'queue', component: 'queue', title: PANELS.queue.title });
  dock.addPanel({ id: 'palette', component: 'palette', title: PANELS.palette.title,
    position: { referencePanel: 'queue', direction: 'left' }, initialWidth: 380 });
  dock.addPanel({ id: 'flow', component: 'flow', title: PANELS.flow.title,
    position: { referencePanel: 'queue', direction: 'right' } });
  dock.addPanel({ id: 'images', component: 'images', title: PANELS.images.title,
    position: { referencePanel: 'flow', direction: 'within' }, inactive: true });
  dock.addPanel({ id: 'terminal', component: 'terminal', title: PANELS.terminal.title,
    position: { referencePanel: 'queue', direction: 'below' } });
}

function restoreLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || 'null');
    if (saved) {
      dock.fromJSON(saved);
      for (const id of Object.keys(PANELS)) if (!dock.getGroupPanel(id)) openPanel(id);
      return;
    }
  } catch (e) { console.warn('layout restore failed, using default', e); }
  defaultLayout();
}

function openPanel(id) {
  const existing = dock.getGroupPanel(id);
  if (existing) { existing.api.setActive(); return; }
  dock.addPanel({ id, component: id, title: PANELS[id].title,
    floating: { width: 640, height: 480, x: 80, y: 80 } });
}

restoreLayout();
dock.onDidLayoutChange(() => {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(dock.toJSON())); } catch { /* */ }
});

window.amadeus = { focusPanel: openPanel, dock };

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function fmtNum(v, unit, digits = 4) {
  if (v === undefined || v === null || v === '') return '—';
  return `${typeof v === 'number' ? +v.toPrecision(digits) : v}${unit || ''}`;
}

function Header() {
  const connected = useStore(s => s.connected);
  const relayOk = useStore(s => s.relayOk);
  const caps = useStore(s => s.caps);
  const p = useStore(s => s.params);
  const qs = useStore(s => s.queue.state);
  // Commands waiting for the operator's go-ahead, shown whatever pane is open.
  const pending = useStore(s => s.chat).filter(e => e.kind === 'confirm' && !e.answered);
  const [menu, setMenu] = useState(false);
  // The column driver's instrument name (or its kind), from the per-driver manifest.
  const src = caps.sources || {};
  const columnName = ((src.routing || {}).column || [])[0];
  const hw = ((src.drivers || {})[columnName]) || {};
  const instrument = hw.instrument || hw.kind || (caps.known ? 'instrument' : 'capabilities unknown');
  const stage = p.stage || {};
  const refresh = async () => { await api.refreshCapabilities().catch(() => {}); refreshCatalog(); };
  return html`
    <header>
      <span class="brand">AMADEUS</span>
      <span class=${`chip ${connected ? 'ok' : 'bad'}`} title="Browser ↔ amadeus-web">
        <i></i>${connected ? 'console' : 'disconnected'}</span>
      <span class=${`chip ${relayOk ? 'ok' : relayOk === false ? 'bad' : ''}`} title="amadeus-web ↔ relay">
        <i></i>relay</span>
      <button class=${`chip ${caps.known ? '' : 'warn'}`} onClick=${refresh}
        title=${['Refresh capabilities', ...(caps.notes || [])].join('\n')}>
        ${instrument}${caps.notes && caps.notes.length ? ' ⚠' : ''}</button>
      <span class="readouts">
        <span><small>mode</small>${p.tem_stem_mode || p.operation_mode || '—'}</span>
        <span><small>mag</small>${p.magnification ? Math.round(p.magnification).toLocaleString() + '×' : '—'}</span>
        <span><small>HT</small>${fmtNum(p.high_tension_kV, ' kV')}</span>
        <span><small>stage</small>${stage.x_m !== undefined
          ? `${fmtNum(stage.x_m * 1e6, '', 5)}, ${fmtNum(stage.y_m * 1e6, '', 5)} µm` : '—'}</span>
        <span><small>beam</small>${p.beam_blanked === undefined ? '—' : p.beam_blanked ? 'blanked' : 'on'}</span>
      </span>
      <span class="spacer"></span>
      <span class=${`chip ${qs.paused ? 'warn' : ''}`}>${qs.paused ? 'queue paused' : qs.current ? 'running' : 'idle'}</span>
      <div class="menu">
        <button onClick=${() => setMenu(!menu)}>Panes ▾</button>
        ${menu && html`<div class="menulist" onClick=${() => setMenu(false)}>
          ${Object.entries(PANELS).map(([id, s]) => html`
            <button onClick=${() => openPanel(id)}>${s.title}</button>`)}
          <hr />
          <button onClick=${() => { localStorage.removeItem(LAYOUT_KEY); defaultLayout(); }}>
            Reset layout</button>
        </div>`}
      </div>
    </header>
    ${pending.length > 0 && html`<div class="confirmbar">
      ${pending.map(e => html`<${Confirm} key=${e.id} e=${e} compact=${true} />`)}
    </div>`}`;
}

render(html`<${Header} />`, document.getElementById('header'));
connectEvents();
