// Actions queue: every tool call, oldest at the top, new blocks appended at
// the bottom. The running block has a marker in the left gutter and a
// highlight ring, and the list follows it unless the operator scrolls away.

import { api, argSummary, fmtDuration, fmtTime } from '../api.js';
import { setState, useStore } from '../store.js';

const { html, useEffect, useRef, useState } = window.htmPreact;

const ORIGIN = { agent: 'agent', manual: 'manual', sequence: 'seq' };

function useTick(active) {
  const [, set] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(() => set(x => x + 1), 500);
    return () => clearInterval(t);
  }, [active]);
}

function Block({ b, current, child }) {
  const [open, setOpen] = useState(false);
  const running = b.status === 'running' || b.id === current;
  useTick(running);
  const remove = async e => {
    e.stopPropagation();
    try { await api.remove(b.id); } catch (err) { alert(err.message); }
  };
  const showImage = e => {
    e.stopPropagation();
    setState({ selectedImage: b.image_id });
    window.amadeus?.focusPanel('images');
  };
  return html`
    <div class=${`qblock st-${b.status} ${running ? 'running' : ''} ${child ? 'child' : ''}`}
         data-id=${b.id} onClick=${() => setOpen(!open)}>
      <div class="gutter">${running ? html`<span class="marker">▶</span>` : ''}</div>
      <div class="qbody">
        <div class="qhead">
          <span class=${`origin o-${b.origin}`}>${ORIGIN[b.origin] || b.origin}</span>
          <span class="qtitle">${b.kind === 'sequence' ? '⧉ ' : ''}${b.label}</span>
          <span class="qargs">${b.kind === 'call' ? argSummary(b.args) : ''}</span>
          <span class="spacer"></span>
          ${b.image_id && html`<button class="mini" title="Show image" onClick=${showImage}>▣</button>`}
          <span class=${`status s-${b.status}`}>${b.status}</span>
          <span class="dur">${fmtDuration(b)}</span>
          ${b.status === 'queued' && html`<button class="mini" title="Remove from queue"
              onClick=${remove}>✕</button>`}
        </div>
        ${b.error && html`<div class="qerr">${b.error}</div>`}
        ${open && html`<div class="qdetail">
          <div><b>id</b> ${b.id}${b.path ? ` · path ${b.path}` : ''} · queued ${fmtTime(b.created)}</div>
          ${b.kind === 'call' && html`<pre>${JSON.stringify(b.args, null, 2)}</pre>`}
          ${b.result && html`<div><b>result</b></div><pre>${b.result}</pre>`}
        </div>`}
      </div>
    </div>`;
}

function SequenceGroup({ b, blocks, current }) {
  const [collapsed, setCollapsed] = useState(false);
  const kids = (b.children || []).map(id => blocks[id]).filter(Boolean);
  return html`
    <div class="qgroup">
      <${Block} b=${b} current=${current} />
      ${kids.length > 0 && html`
        <button class="linkbtn" onClick=${() => setCollapsed(!collapsed)}>
          ${collapsed ? '▸' : '▾'} ${kids.length} step${kids.length === 1 ? '' : 's'}
        </button>`}
      ${!collapsed &&
        kids.map(k => html`<${Block} key=${k.id} b=${k} current=${current} child=${true} />`)}
    </div>`;
}

export function QueuePanel() {
  const order = useStore(s => s.order);
  const blocks = useStore(s => s.blocks);
  const qs = useStore(s => s.queue.state);
  const listRef = useRef(null);
  const [follow, setFollow] = useState(true);

  const runningId = (() => {
    const cur = qs.current && blocks[qs.current];
    if (!cur) return null;
    if (cur.kind === 'sequence') {
      const kids = (cur.children || []).map(id => blocks[id]).filter(Boolean);
      const r = kids.reverse().find(k => k.status === 'running');
      return r ? r.id : cur.id;
    }
    return cur.id;
  })();

  useEffect(() => {
    const el = listRef.current;
    if (!el || !follow) return;
    const target = runningId && el.querySelector(`[data-id="${runningId}"]`);
    if (target) target.scrollIntoView({ block: 'nearest' });
    else el.scrollTop = el.scrollHeight;
  }, [order.length, runningId, blocks, follow]);

  // Only the operator's own scrolling turns following off (our scrollIntoView
  // calls fire scroll events too); reaching the bottom turns it back on.
  const userScrolled = e => {
    if (e.type === 'wheel' && e.deltaY > 0) return;
    if (follow) setFollow(false);
  };
  const onScroll = () => {
    const el = listRef.current;
    if (!follow && el.scrollHeight - el.scrollTop - el.clientHeight < 8) setFollow(true);
  };

  const act = a => api.control(a).catch(e => alert(e.message));
  const stop = () => api.stopStage().catch(e => alert(e.message));

  return html`
    <div class="panel queue">
      <div class="toolbar">
        ${qs.paused
          ? html`<button class="primary" onClick=${() => act('resume')}>▶ Resume</button>`
          : html`<button onClick=${() => act('pause')}>⏸ Pause</button>`}
        <button onClick=${() => act('step')} title="Run exactly one block, then pause">⤼ Step</button>
        <button onClick=${() => act('abort')} disabled=${!qs.current}
          title="Abort the running sequence at its next block">⏹ Abort</button>
        <button onClick=${() => act('clear')} disabled=${!qs.queued}
          title="Remove every block still waiting">Clear queued</button>
        <span class="spacer"></span>
        <span class="qstate">${qs.paused ? 'PAUSED' : qs.current ? 'running' : 'idle'}
          · ${qs.queued} queued</span>
        <button class="danger" onClick=${stop} title="Stop the stage now (bypasses the queue)">
          STOP STAGE</button>
      </div>
      <div class="qlist" ref=${listRef} onScroll=${onScroll} onWheel=${userScrolled}
        onTouchMove=${userScrolled}>
        ${order.length === 0 && html`<div class="empty">No blocks yet. Add one from the
          Blocks pane, run a flow, or let the agent work.</div>`}
        ${order.map(id => {
          const b = blocks[id];
          if (!b) return null;
          return b.kind === 'sequence'
            ? html`<${SequenceGroup} key=${id} b=${b} blocks=${blocks} current=${runningId} />`
            : html`<${Block} key=${id} b=${b} current=${runningId} />`;
        })}
      </div>
      ${!follow && html`<button class="jump" onClick=${() => setFollow(true)}>
        ↓ follow running block</button>`}
    </div>`;
}
