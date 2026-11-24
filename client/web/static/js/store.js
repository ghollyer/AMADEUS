// Global client state, fed by the server's SSE stream (/events).
// Components subscribe with useStore(selector) and re-render on change.

const { useState, useEffect } = window.htmPreact;

const state = {
  connected: false,
  relayOk: null,
  queue: { state: { paused: false, current: null, queued: 0 } },
  blocks: {},          // id -> block
  order: [],           // top-level block ids, oldest first
  caps: { caps: [], roles: {}, notes: [], known: false },
  catalog: [],         // tool entries from /api/catalog
  params: {},          // monitor readouts (mode, mag, stage...)
  chat: [],
  agentSeen: null,
  agentWaiting: false,
  live: null,          // {frame_index, mtime}
  imagesVersion: 0,    // bump to make the image list refetch
  selectedImage: null, // image_id shown in the viewer
  flowLoad: null,      // {seq, nonce}: a sequence for the flow editor to load
  flowAppend: null,    // {tool, args, nonce}: a block for the flow editor to add
};

const listeners = new Set();

export function getState() { return state; }

export function setState(patch) {
  Object.assign(state, typeof patch === 'function' ? patch(state) : patch);
  for (const fn of listeners) fn();
}

export function useStore(selector) {
  const [value, setValue] = useState(() => selector(state));
  useEffect(() => {
    const update = () => setValue(() => selector(state));
    listeners.add(update);
    update();
    return () => listeners.delete(update);
  }, []);
  return value;
}

function upsertBlock(b) {
  const blocks = { ...state.blocks, [b.id]: b };
  let order = state.order;
  if (!b.parent && !order.includes(b.id)) order = [...order, b.id];
  setState({ blocks, order });
}

const handlers = {
  hello(d) {
    const blocks = {};
    const order = [];
    for (const b of d.queue.blocks) {
      blocks[b.id] = b;
      if (!b.parent) order.push(b.id);
    }
    setState({ blocks, order, queue: { state: d.queue.state }, caps: d.capabilities,
      relayOk: d.relay_ok, params: d.params || {}, chat: d.chat, agentSeen: d.agent_seen,
      agentWaiting: d.agent_waiting, connected: true });
  },
  block: upsertBlock,
  queue_state(d) { setState({ queue: { state: d } }); },
  capabilities(d) { setState({ caps: d }); refreshCatalog(); },
  relay(d) { setState({ relayOk: d.ok }); },
  status(d) { setState({ params: d.params || {} }); },
  live(d) { setState({ live: d }); },
  image(d) {
    setState(s => ({ imagesVersion: s.imagesVersion + 1,
      selectedImage: s.followNew === false ? s.selectedImage : d.image_id }));
  },
  chat(entry) {
    const chat = state.chat.slice();
    const i = chat.findIndex(e => e.id === entry.id);
    if (i >= 0) chat[i] = entry; else chat.push(entry);
    setState({ chat });
  },
  agent_presence(d) { setState({ agentSeen: d.seen, agentWaiting: d.waiting }); },
  sequence_event() {},
};

export async function refreshCatalog() {
  try {
    const r = await fetch('/api/catalog');
    const d = await r.json();
    setState({ catalog: d.tools, caps: d.capabilities });
  } catch (e) { console.warn('catalog', e); }
}

export function connectEvents() {
  const es = new EventSource('/events');
  for (const kind of Object.keys(handlers)) {
    es.addEventListener(kind, ev => {
      try { handlers[kind](JSON.parse(ev.data)); } catch (e) { console.error(kind, e); }
    });
  }
  es.onerror = () => setState({ connected: false });
  es.onopen = () => refreshCatalog();
  return es;
}
