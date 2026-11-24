// Agent terminal: the operator's side of the conversation with the sidecar
// Claude Code session. Instructions go to the agent's wait_for_operator
// call, or ride along on its next tool result if it is busy. Questions from
// ask_operator (or an Ask block in a flow) show up here with answer buttons.
// The agent's queue blocks are interleaved so the conversation reads in order.

import { api, argSummary, fmtTime } from '../api.js';
import { useStore } from '../store.js';

const { html, useEffect, useMemo, useRef, useState } = window.htmPreact;

function Question({ e }) {
  const [text, setText] = useState('');
  const send = async answer => {
    if (!answer.trim()) return;
    try { await api.answer(e.id, answer); } catch (err) { alert(err.message); }
  };
  return html`
    <div class=${`msg question ${e.answered ? 'answered' : 'open'}`}>
      <div class="who">${e.who === 'sequence' ? 'flow asks' : 'agent asks'} · ${fmtTime(e.t)}</div>
      <div class="text">${e.text}</div>
      ${e.answered
        ? html`<div class="answer">${e.answer === null ? '(no answer: timed out)' : `→ ${e.answer}`}</div>`
        : html`<div class="answerbox">
            ${e.options.map(o => html`<button onClick=${() => send(o)}>${o}</button>`)}
            <input type="text" placeholder="Type an answer…" value=${text}
              onInput=${ev => setText(ev.target.value)}
              onKeyDown=${ev => ev.key === 'Enter' && send(text)} />
            <button class="primary" onClick=${() => send(text)}>Answer</button>
          </div>`}
    </div>`;
}

// A command held for the operator's go-ahead (client/confirm.py): Allow/Deny.
export function Confirm({ e, compact = false }) {
  const send = async answer => {
    try { await api.answer(e.id, answer); } catch (err) { alert(err.message); }
  };
  const [head, ...rest] = e.text.split('\n');
  return html`
    <div class=${`msg confirm ${e.answered ? 'answered' : 'open'} ${compact ? 'compact' : ''}`}>
      ${!compact && html`<div class="who">needs your go-ahead · ${fmtTime(e.t)}</div>`}
      <div class="text"><b>${head}</b>${rest.length ? html`<div class="muted">${rest.join(' ')}</div>` : ''}</div>
      ${e.answered
        ? html`<div class="answer">${e.answer === null ? '(no answer: declined)' : `→ ${e.answer}`}</div>`
        : html`<div class="answerbox">
            <button class="primary" onClick=${() => send('Allow')}>Allow</button>
            <button class="danger" onClick=${() => send('Deny')}>Deny</button>
          </div>`}
    </div>`;
}

function Presence() {
  const seen = useStore(s => s.agentSeen);
  const waiting = useStore(s => s.agentWaiting);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick(x => x + 1), 5000); return () => clearInterval(t); }, []);
  let label = 'no agent attached';
  let cls = 'off';
  if (waiting) { label = 'agent is listening'; cls = 'ok'; }
  else if (seen && Date.now() / 1000 - seen < 600) { label = 'agent is working'; cls = 'busy'; }
  else if (seen) { label = `agent last seen ${fmtTime(seen)}`; cls = 'off'; }
  return html`<span class=${`presence p-${cls}`}><i></i>${label}</span>`;
}

export function TerminalPanel() {
  const chat = useStore(s => s.chat);
  const blocks = useStore(s => s.blocks);
  const order = useStore(s => s.order);
  const [text, setText] = useState('');
  const [showCalls, setShowCalls] = useState(true);
  const listRef = useRef(null);

  const items = useMemo(() => {
    const out = chat.map(e => ({ t: e.t, kind: 'chat', e }));
    if (showCalls) {
      for (const id of order) {
        const b = blocks[id];
        if (b && b.origin === 'agent') out.push({ t: b.created, kind: 'call', b });
      }
    }
    return out.sort((a, b) => a.t - b.t);
  }, [chat, blocks, order, showCalls]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length]);

  const send = async () => {
    const t = text.trim();
    if (!t) return;
    try { await api.say(t); setText(''); } catch (e) { alert(e.message); }
  };

  return html`
    <div class="panel terminal">
      <div class="toolbar">
        <${Presence} />
        <span class="spacer"></span>
        <label class="check"><input type="checkbox" checked=${showCalls}
          onChange=${e => setShowCalls(e.target.checked)} /> agent tool calls</label>
      </div>
      <div class="chat" ref=${listRef}>
        ${items.length === 0 && html`<div class="empty">
          Attach Claude Code to <code>${location.origin}/mcp</code> (see the operator
          folder), then type an instruction below. The agent picks it up through${' '}
          <code>wait_for_operator</code>.</div>`}
        ${items.map(it => {
          if (it.kind === 'call') {
            const b = it.b;
            return html`<div class=${`msg call s-${b.status}`} key=${'c' + b.id}>
              <span class="who">${fmtTime(b.created)}</span>
              <code>${b.tool}</code> <span class="muted">${argSummary(b.args, 70)}</span>
              <span class=${`status s-${b.status}`}>${b.status}</span></div>`;
          }
          const e = it.e;
          if (e.kind === 'question') return html`<${Question} key=${e.id} e=${e} />`;
          if (e.kind === 'confirm') return html`<${Confirm} key=${e.id} e=${e} />`;
          if (e.kind === 'answer') return null;
          return html`<div key=${e.id} class=${`msg ${e.who} k-${e.kind}`}>
            <div class="who">${e.who}${e.kind === 'instruction'
              ? (e.delivered ? ' · delivered' : ' · waiting for the agent') : ''} · ${fmtTime(e.t)}</div>
            <div class="text">${e.text}</div></div>`;
        })}
      </div>
      <div class="composer">
        <textarea rows="2" placeholder="Instruction for the agent… (Enter to send, Shift+Enter for a new line)"
          value=${text} onInput=${e => setText(e.target.value)}
          onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}></textarea>
        <button class="primary" onClick=${send}>Send</button>
      </div>
    </div>`;
}
