// Blockly <-> AMADEUS sequence JSON.
//
// Blockly is only the editor. Everything saved or run is the sequence format
// (client/blocks/model.py), so a script never depends on Blockly. Each MCP
// tool is a block type ("tool_<name>") generated from the tool catalog. The
// control blocks map 1:1 onto the sequence node types.

import { argToText, baseType, defaultText, parseArg } from './args.js';

const B = window.Blockly;
const DEFAULT = '__default__';
const CONTROL_HUE = 210;
const CATEGORY_HUES = {
  Acquire: 20, Stage: 120, Optics: 260, Focus: 290, Column: 330, Live: 45,
  Spectroscopy: 0, 'EDS analysis': 15, Analysis: 180, Files: 60, Windows: 75,
  Session: 160, Operator: 195, Scripting: 300, Admin: 0, Unknown: 0,
};

const toolMeta = new Map();   // tool name -> catalog entry used to define its block

function fieldKind(prop) {
  if (prop && prop.enum) return 'enum';
  if (baseType(prop) === 'boolean') return 'bool';
  return 'text';
}

function makeField(prop) {
  const kind = fieldKind(prop);
  const dflt = defaultText(prop);
  if (kind === 'enum') {
    return new B.FieldDropdown([[`(default${dflt ? ': ' + dflt : ''})`, DEFAULT],
      ...prop.enum.map(v => [String(v), String(v)])]);
  }
  if (kind === 'bool') {
    return new B.FieldDropdown([[`(default${dflt ? ': ' + dflt : ''})`, DEFAULT],
      ['true', 'true'], ['false', 'false']]);
  }
  return new B.FieldTextInput('');
}

export function defineToolBlock(tool) {
  toolMeta.set(tool.name, tool);
  const props = (tool.input_schema && tool.input_schema.properties) || {};
  const hue = CATEGORY_HUES[tool.category] ?? 0;
  B.Blocks['tool_' + tool.name] = {
    init() {
      this.appendDummyInput('HEAD').appendField(tool.name);
      for (const [arg, prop] of Object.entries(props)) {
        const d = defaultText(prop);
        const role = prop['x-role'] ? ` @${prop['x-role']}` : '';
        const label = fieldKind(prop) === 'text' && d ? `${arg} [${d}]${role}` : `${arg}${role}`;
        this.appendDummyInput('ROW_' + arg).appendField('   ' + label)
          .appendField(makeField(prop), 'ARG_' + arg);
      }
      if (tool.mode) {
        this.appendDummyInput('ENSURE_ROW').appendField(`   switch to ${tool.mode} if needed`)
          .appendField(new B.FieldCheckbox('FALSE'), 'ENSURE');
      }
      this.appendDummyInput('AS_ROW').appendField('   store result as')
        .appendField(new B.FieldTextInput(''), 'AS_VAR');
      this.setPreviousStatement(true, null);
      this.setNextStatement(true, null);
      this.setColour(tool.available === false || tool.unknown ? '#5a5a5a' : hue);
      const missing = tool.available === false
        ? `\n\nNot available on this microscope (still saveable for others): ${tool.missing.join(', ')}` : '';
      this.setTooltip(`${tool.summary || tool.name}${tool.mode ? `\nNeeds ${tool.mode} mode.` : ''}` +
        '\n\nArguments: empty = default, =expr = expression, @ROLE = detector/camera role.' + missing);
    },
  };
}

const EXPR_HELP = 'Expression: variables (img.sharpness[0]), + - * / < > == and or not, ' +
  "has('cap'), mode(), len, min, max, abs, round.";

export function defineControlBlocks() {
  B.common.defineBlocksWithJsonArray([
    { type: 'amd_program', message0: 'sequence %1', args0: [
      { type: 'field_input', name: 'NAME', text: 'untitled' }],
      message1: 'on error %1', args1: [{ type: 'field_dropdown', name: 'ON_ERROR',
        options: [['abort', 'abort'], ['continue', 'continue']] }],
      message2: '%1', args2: [{ type: 'input_statement', name: 'BODY' }],
      colour: 230, tooltip: 'The program. Blocks outside it are ignored.' },
    { type: 'amd_if', message0: 'if %1', args0: [{ type: 'field_input', name: 'COND', text: 'True' }],
      message1: 'then %1', args1: [{ type: 'input_statement', name: 'THEN' }],
      message2: 'else %1', args2: [{ type: 'input_statement', name: 'ELSE' }],
      previousStatement: null, nextStatement: null, colour: CONTROL_HUE, tooltip: EXPR_HELP },
    { type: 'amd_if_has', message0: 'if microscope has %1', args0: [
      { type: 'field_input', name: 'CAP', text: 'calib:focus.STEM' }],
      message1: 'then %1', args1: [{ type: 'input_statement', name: 'THEN' }],
      message2: 'else %1', args2: [{ type: 'input_statement', name: 'ELSE' }],
      previousStatement: null, nextStatement: null, colour: 170,
      tooltip: 'Run the first branch only where the instrument has this capability ' +
        '(e.g. cmd:tem3_call, mode:STEM, detector:HAADF, calib:image_mapping.STEM). ' +
        'Its blocks are then not required on microscopes without it, which keeps the ' +
        'sequence portable.' },
    { type: 'amd_repeat', message0: 'repeat %1 times, index %2', args0: [
      { type: 'field_number', name: 'COUNT', value: 3, min: 0, precision: 1 },
      { type: 'field_input', name: 'VAR', text: 'i' }],
      message1: '%1', args1: [{ type: 'input_statement', name: 'BODY' }],
      previousStatement: null, nextStatement: null, colour: CONTROL_HUE },
    { type: 'amd_while', message0: 'while %1 at most %2 times', args0: [
      { type: 'field_input', name: 'COND', text: 'True' },
      { type: 'field_number', name: 'MAX', value: 10, min: 1, precision: 1 }],
      message1: '%1', args1: [{ type: 'input_statement', name: 'BODY' }],
      previousStatement: null, nextStatement: null, colour: CONTROL_HUE, tooltip: EXPR_HELP },
    { type: 'amd_foreach', message0: 'for each %1 in %2 at most %3', args0: [
      { type: 'field_input', name: 'VAR', text: 'item' },
      { type: 'field_input', name: 'ITEMS', text: '[1, 2, 3]' },
      { type: 'field_number', name: 'MAX', value: 100, min: 1, precision: 1 }],
      message1: '%1', args1: [{ type: 'input_statement', name: 'BODY' }],
      previousStatement: null, nextStatement: null, colour: CONTROL_HUE, tooltip: EXPR_HELP },
    { type: 'amd_set', message0: 'set %1 = %2', args0: [
      { type: 'field_input', name: 'VAR', text: 'x' },
      { type: 'field_input', name: 'VALUE', text: '0' }],
      previousStatement: null, nextStatement: null, colour: 330, tooltip: EXPR_HELP },
    { type: 'amd_ask', message0: 'ask operator %1', args0: [
      { type: 'field_input', name: 'QUESTION', text: 'Continue?' }],
      message1: 'options %1 store as %2', args1: [
        { type: 'field_input', name: 'OPTIONS', text: 'yes, no' },
        { type: 'field_input', name: 'VAR', text: 'answer' }],
      previousStatement: null, nextStatement: null, colour: 195,
      tooltip: 'Shows the question in the Agent terminal and waits for an answer.' },
    { type: 'amd_pause', message0: 'pause queue %1', args0: [
      { type: 'field_input', name: 'MESSAGE', text: '' }],
      previousStatement: null, nextStatement: null, colour: 45,
      tooltip: 'Pauses the queue here until the operator presses Resume.' },
    { type: 'amd_fail', message0: 'fail %1', args0: [
      { type: 'field_input', name: 'MESSAGE', text: 'stopped' }],
      previousStatement: null, nextStatement: null, colour: 0 },
  ]);
}

export function defineTheme() {
  return B.Theme.defineTheme('amadeus-dark', {
    base: B.Themes.Classic,
    componentStyles: {
      workspaceBackgroundColour: '#15171c', toolboxBackgroundColour: '#1d2027',
      toolboxForegroundColour: '#d5d9e0', flyoutBackgroundColour: '#23272f',
      flyoutForegroundColour: '#d5d9e0', flyoutOpacity: 0.97, scrollbarColour: '#4a505c',
      insertionMarkerColour: '#ffffff', insertionMarkerOpacity: 0.3, cursorColour: '#d0d0d0',
    },
  });
}

export function defineToolBlocks(catalog) {
  for (const t of catalog) defineToolBlock(t);
}

export function toolbox(catalog) {
  const cats = new Map();
  for (const t of catalog) {
    if (!cats.has(t.category)) cats.set(t.category, []);
    cats.get(t.category).push({ kind: 'block', type: 'tool_' + t.name });
  }
  const control = ['amd_program', 'amd_if', 'amd_if_has', 'amd_repeat', 'amd_while',
    'amd_foreach', 'amd_set', 'amd_ask', 'amd_pause', 'amd_fail'];
  return {
    kind: 'categoryToolbox',
    contents: [
      { kind: 'category', name: 'Control', colour: String(CONTROL_HUE),
        contents: control.map(type => ({ kind: 'block', type })) },
      { kind: 'sep' },
      ...[...cats.entries()].map(([name, contents]) => ({
        kind: 'category', name, colour: String(CATEGORY_HUES[name] ?? 0), contents })),
    ],
  };
}

// ---------------------------------------------------------------------------
// workspace -> sequence
// ---------------------------------------------------------------------------

function chain(block) {
  const out = [];
  for (let b = block; b; b = b.getNextBlock()) {
    if (b.isEnabled && !b.isEnabled()) continue;
    const node = toNode(b);
    if (node) out.push(node);
  }
  return out;
}

function stmt(block, name) { return chain(block.getInputTargetBlock(name)); }

function toNode(b) {
  const f = n => b.getFieldValue(n);
  switch (b.type) {
    case 'amd_if': return { type: 'if', cond: f('COND'), then: stmt(b, 'THEN'), else: stmt(b, 'ELSE') };
    case 'amd_if_has': return { type: 'if', cond: `has(${JSON.stringify(f('CAP').trim())})`,
      then: stmt(b, 'THEN'), else: stmt(b, 'ELSE') };
    case 'amd_repeat': return { type: 'repeat', count: Number(f('COUNT')),
      ...(f('VAR').trim() ? { var: f('VAR').trim() } : {}), body: stmt(b, 'BODY') };
    case 'amd_while': return { type: 'while', cond: f('COND'), max_iter: Number(f('MAX')),
      body: stmt(b, 'BODY') };
    case 'amd_foreach': return { type: 'for_each', var: f('VAR').trim(), items: f('ITEMS'),
      max_iter: Number(f('MAX')), body: stmt(b, 'BODY') };
    case 'amd_set': return { type: 'set', var: f('VAR').trim(), value: f('VALUE') };
    case 'amd_ask': return { type: 'ask', question: f('QUESTION'),
      options: f('OPTIONS').split(',').map(s => s.trim()).filter(Boolean),
      ...(f('VAR').trim() ? { as_var: f('VAR').trim() } : {}) };
    case 'amd_pause': return { type: 'pause', message: f('MESSAGE') };
    case 'amd_fail': return { type: 'fail', message: f('MESSAGE') };
    case 'amd_program': return { type: 'seq', children: stmt(b, 'BODY') };
    default:
      if (b.type.startsWith('tool_')) return toolNode(b);
      return null;
  }
}

function toolNode(b) {
  const name = b.type.slice(5);
  const tool = toolMeta.get(name) || { input_schema: { properties: {} } };
  const props = tool.input_schema.properties || {};
  const args = {};
  for (const [arg, prop] of Object.entries(props)) {
    const raw = b.getFieldValue('ARG_' + arg);
    const kind = fieldKind(prop);
    if (raw === null || raw === undefined || raw === DEFAULT) continue;
    let v;
    if (kind === 'bool') v = raw === 'true';
    else if (kind === 'enum') v = raw;
    else v = parseArg(raw, prop);
    if (v !== undefined) args[arg] = v;
  }
  const node = { type: 'call', tool: name, args };
  const asVar = (b.getFieldValue('AS_VAR') || '').trim();
  if (asVar) node.as_var = asVar;
  if (b.getField('ENSURE') && b.getFieldValue('ENSURE') === 'TRUE') node.ensure_mode = true;
  return node;
}

export function workspaceToSequence(ws, extra = {}) {
  const tops = ws.getTopBlocks(true);
  const program = tops.find(b => b.type === 'amd_program');
  let body;
  let name = extra.name || 'untitled';
  let onError = 'abort';
  if (program) {
    body = stmt(program, 'BODY');
    name = program.getFieldValue('NAME') || name;
    onError = program.getFieldValue('ON_ERROR');
  } else {
    body = tops.filter(b => !b.previousConnection || !b.previousConnection.isConnected())
      .flatMap(b => chain(b));
  }
  let meta = {};
  try { meta = program && program.data ? JSON.parse(program.data) : {}; } catch { meta = {}; }
  return { schema_version: 1, name, description: meta.description || '',
    params: meta.params || [], requires: meta.requires || [], on_error: onError, body };
}

// ---------------------------------------------------------------------------
// sequence -> workspace
// ---------------------------------------------------------------------------

function chainJSON(nodes) {
  let first = null;
  let prev = null;
  for (const n of nodes || []) {
    const j = nodeJSON(n);
    if (!j) continue;
    if (prev) prev.next = { block: j }; else first = j;
    prev = j;
  }
  return first;
}

function withInputs(j, inputs) {
  const out = {};
  for (const [k, nodes] of Object.entries(inputs)) {
    const c = chainJSON(nodes);
    if (c) out[k] = { block: c };
  }
  if (Object.keys(out).length) j.inputs = out;
  return j;
}

const HAS_RE = /^has\((['"])([^'"]+)\1\)$/;

function nodeJSON(n) {
  switch (n.type) {
    case 'if': {
      const m = HAS_RE.exec((n.cond || '').trim());
      if (m) return withInputs({ type: 'amd_if_has', fields: { CAP: m[2] } },
        { THEN: n.then, ELSE: n.else });
      return withInputs({ type: 'amd_if', fields: { COND: n.cond } }, { THEN: n.then, ELSE: n.else });
    }
    case 'repeat': return withInputs({ type: 'amd_repeat', fields: { COUNT: n.count, VAR: n.var || '' } },
      { BODY: n.body });
    case 'while': return withInputs({ type: 'amd_while', fields: { COND: n.cond, MAX: n.max_iter ?? 100 } },
      { BODY: n.body });
    case 'for_each': return withInputs({ type: 'amd_foreach',
      fields: { VAR: n.var, ITEMS: n.items, MAX: n.max_iter ?? 1000 } }, { BODY: n.body });
    case 'set': return { type: 'amd_set', fields: { VAR: n.var, VALUE: n.value } };
    case 'ask': return { type: 'amd_ask', fields: { QUESTION: n.question,
      OPTIONS: (n.options || []).join(', '), VAR: n.as_var || '' } };
    case 'pause': return { type: 'amd_pause', fields: { MESSAGE: n.message || '' } };
    case 'fail': return { type: 'amd_fail', fields: { MESSAGE: n.message || '' } };
    case 'seq': return chainJSON(n.children);   // flattened into the parent chain
    case 'call': return callJSON(n);
    default: return null;
  }
}

function callJSON(n) {
  let tool = toolMeta.get(n.tool);
  if (!tool) {
    // Not registered on this client: keep the block (and its arguments) so
    // the script survives a round trip, greyed out.
    const props = {};
    for (const k of Object.keys(n.args || {})) props[k] = { type: 'string' };
    tool = { name: n.tool, category: 'Unknown', unknown: true, available: false,
      missing: [`tool:${n.tool}`], summary: `Unknown tool ${n.tool}`,
      input_schema: { properties: props } };
    defineToolBlock(tool);
  }
  const props = tool.input_schema.properties || {};
  const fields = {};
  for (const [k, v] of Object.entries(n.args || {})) {
    const kind = fieldKind(props[k]);
    fields['ARG_' + k] = kind === 'text' ? argToText(v) : String(v);
  }
  if (n.as_var) fields.AS_VAR = n.as_var;
  if (n.ensure_mode && tool.mode) fields.ENSURE = 'TRUE';
  return { type: 'tool_' + n.tool, fields };
}

export function sequenceToState(seq) {
  const program = withInputs({
    type: 'amd_program', x: 24, y: 24,
    fields: { NAME: seq.name || 'untitled', ON_ERROR: seq.on_error || 'abort' },
    data: JSON.stringify({ description: seq.description || '', params: seq.params || [],
      requires: seq.requires || [] }),
  }, { BODY: seq.body });
  return { blocks: { languageVersion: 0, blocks: [program] } };
}

export function blockJSON(node) { return nodeJSON(node); }
