/*
  app.js - the control panel.

  The sidebar is not hand-written. On connect the server sends the schema from
  settings.py - every field, its type, its bounds, its help text, whether the
  browser is allowed to touch it, and what changing it forces the running
  pipeline to rebuild - and this file renders controls from that. Adding an
  option to settings.py makes it appear here, correctly typed and documented,
  with no edit to this file. That is the whole point: the old guided launcher
  could only ever offer the flags somebody had remembered to copy into it.

  Everything else here is a subscriber to the same event stream the console and
  the overlay see: transcript, meter, perf, state, log, benchmark.
*/

'use strict';

// ---------------------------------------------------------------- state
const S = {
  ws: null,
  connected: false,
  everConnected: false,    // so the first connect does not flash "offline"
  retry: 0,
  schema: null,
  fields: new Map(),        // key -> {field, el, setValue}
  settings: {},
  status: {},
  devices: { loopback: [], input: [], errors: [] },
  models: { ggml: [], faster_whisper: [] },
  presets: [],
  engine: {},              // whisper-server as seen from the port, not guessed
  busy: '',                // the lifecycle command in flight, by name
  lastMeter: {},
  unseenLogs: 0,
  mic: { stream: null, ctx: null, node: null, sent: 0, started: 0 },
  langSeen: new Map(),
};

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const SVGNS = 'http://www.w3.org/2000/svg';
/* One <use> into the sprite in index.html. Built with createElementNS because
   SVG children created through the HTML namespace render as nothing at all -
   they exist in the DOM and are simply never painted, which looks like a CSS
   problem and is not one. */
function icon(name, cls) {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

/* Buttons here carry an icon as well as a word, so the obvious
   btn.textContent = 'Resume' silently deletes the icon. */
function setLabel(node, iconName, text) {
  node.innerHTML = '';
  if (iconName) node.append(icon(iconName));
  if (text) node.append(document.createTextNode(text));
}

const GROUP_ICONS = {
  audio: 'wave', chunking: 'scissors', gate: 'shield',
  transcription: 'chip', decoding: 'sliders', output: 'file',
  overlay: 'layers', web: 'globe',
};

// ---------------------------------------------------------------- socket
function wsURL() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = new URLSearchParams(location.search).get('token');
  return proto + '//' + location.host + '/ws' + (token ? '?token=' + encodeURIComponent(token) : '');
}

function connect() {
  setConn('busy');
  let ws;
  try {
    ws = new WebSocket(wsURL());
  } catch (e) {
    return scheduleRetry();
  }
  ws.binaryType = 'arraybuffer';
  S.ws = ws;

  ws.onopen = () => {
    S.connected = true;
    S.everConnected = true;
    S.retry = 0;
    setConn('on');
    send({ type: 'hello', role: 'control' });
  };
  ws.onclose = () => {
    S.connected = false;
    S.busy = '';
    setConn('off');
    stopMic('connection lost');
    scheduleRetry();
  };
  ws.onerror = () => { /* onclose always follows; nothing useful to add */ };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handle(msg.type, msg.data);
  };
}

function scheduleRetry() {
  // Backoff, but capped low: the usual reason for a drop is that the Python
  // process was restarted on purpose, and waiting 30 s to notice it came back
  // makes the panel feel broken.
  S.retry = Math.min(S.retry + 1, 6);
  setTimeout(connect, Math.min(400 * S.retry, 2500));
}

function setConn(state) {
  const dot = $('#connDot');
  dot.className = 'dot' + (state === 'on' ? ' on' : state === 'busy' ? ' busy' : '');
  dot.title = state === 'on' ? 'connected' : state === 'busy' ? 'connecting…' : 'disconnected';
  // A 6px dot is not enough. Everything on this page is a snapshot of the last
  // message that arrived, so a dead socket looks exactly like a working one
  // that is idle - the meters keep their last values, the button still says
  // Stop, and clicking it does nothing at all with nothing saying why.
  document.body.classList.toggle('offline', state !== 'on');
  applyRunState();
  renderEngine();
}

function send(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(obj));
}
function command(name, args) { send({ type: 'command', name, args: args || {} }); }
function patch(data) { send({ type: 'config', data }); }

// ---------------------------------------------------------------- events
function handle(type, data) {
  switch (type) {
    case 'hello':
      $('#version').textContent = 'v' + data.version;
      S.schema = data.schema;
      S.settings = data.settings;
      S.devices = data.devices || S.devices;
      S.models = data.models || S.models;
      S.presets = data.presets || [];
      S.engine = data.engine || {};
      S.busy = data.busy || '';
      buildControls();
      renderPresets();
      renderEngine();
      applyStatus(data.status || {});
      (data.history || []).forEach(e => addCaption(e, true));
      trimTranscript();
      break;
    case 'settings':
      S.settings = Object.assign({}, S.settings, data);
      refreshControls();
      break;
    case 'state': applyStatus(data); break;
    case 'meter': applyMeter(data); break;
    case 'perf': applyPerf(data); break;
    case 'transcript': addCaption(data, false); break;
    case 'dropped': break;   // the duplicate filter working as intended
    case 'log': addLog(data.level, data.msg); break;
    case 'devices': S.devices = data; refreshControls(); toast('Devices rescanned', 'good'); break;
    case 'models': S.models = data; refreshControls(); break;
    case 'presets': S.presets = data; renderPresets(); break;
    case 'engine':
      S.engine = data;
      S.busy = data.busy || '';
      renderEngine();
      applyRunState();
      break;
    case 'history': $('#transcript').innerHTML = ''; data.forEach(e => addCaption(e, true)); break;
    case 'benchmark': renderBench(data); break;
    case 'export': downloadExport(data); break;
    case 'ack': handleAck(data); break;
    case 'error': toast(data.msg, 'error'); break;
  }
}

function handleAck(data) {
  if (data.errors && data.errors.length) data.errors.forEach(e => toast(e, 'error'));
  if (data.error) toast(data.error, 'error');
  if (data.saved) toast('Preset "' + data.saved + '" saved', 'good');
  if (data.deleted) toast('Preset "' + data.deleted + '" deleted');
  if (data.loaded) toast('Preset "' + data.loaded + '" loaded', 'good');
  if (data.presets) { S.presets = data.presets; renderPresets(); }
  // Lifecycle commands report through the log and through 'engine', not
  // through their ack: they finish seconds after it, and an ack that said
  // "done" the instant the button was pressed was the lie worth removing.
  if (data.queued === '') toast('Something else is still running', 'warn');
}

// ---------------------------------------------------------------- controls
function buildControls() {
  const host = $('#controls');
  host.innerHTML = '';
  S.fields.clear();
  const byGroup = new Map();
  S.schema.fields.forEach(f => {
    if (!byGroup.has(f.group)) byGroup.set(f.group, []);
    byGroup.get(f.group).push(f);
  });

  S.schema.groups.forEach((g, i) => {
    const fields = byGroup.get(g.key) || [];
    if (!fields.length) return;
    const box = el('details', 'group');
    box.open = i < 4;
    const sum = el('summary');
    sum.title = g.help;
    sum.append(icon('chevron', 'chev'));
    const glyph = el('span', 'gGlyph');
    glyph.append(icon(GROUP_ICONS[g.key] || 'sliders'));
    sum.append(glyph);
    sum.append(el('span', 'gName', g.label));
    sum.append(el('span', 'gCount', String(fields.length)));
    box.append(sum);
    const body = el('div', 'groupBody');
    fields.forEach(f => body.append(makeField(f)));
    box.append(body);
    host.append(box);
  });
  refreshControls();
}

function makeField(f) {
  const wrap = el('div', 'field');
  wrap.dataset.key = f.key;
  wrap.dataset.group = f.group;
  if (f.advanced) wrap.dataset.advanced = '1';
  if (f.rebuild && f.rebuild !== 'none' && f.rebuild !== 'restart') wrap.classList.add('rebuilds');
  const locked = (S.schema.remoteLocked || []).includes(f.key);
  if (locked) wrap.classList.add('locked');

  const head = el('div', 'head');
  const label = el('label', 'name', f.label);
  label.htmlFor = 'f_' + f.key;
  head.append(label);
  if (f.unit) head.append(el('span', 'unit', f.unit));
  const valOut = el('span', 'val');
  head.append(valOut);
  if (f.help) {
    const why = el('button', 'why', '?');
    why.type = 'button';
    why.title = 'Why this exists';
    why.onclick = () => wrap.classList.toggle('showHelp');
    head.append(why);
  }
  wrap.append(head);

  const ctl = el('div', 'ctl');
  let input, read, write;

  if (f.kind === 'bool') {
    input = el('input');
    input.type = 'checkbox';
    input.id = 'f_' + f.key;
    label.prepend(input, document.createTextNode(' '));
    read = () => input.checked;
    write = v => { input.checked = !!v; };
    ctl.remove();
  } else if (f.kind === 'choice') {
    input = el('select');
    input.id = 'f_' + f.key;
    read = () => input.value;
    write = v => {
      fillChoices(input, f);
      input.value = v == null ? '' : String(v);
      if (input.selectedIndex < 0 && input.options.length) {
        // The saved value is not in the list any more - a device that was
        // unplugged, most often. Show it rather than silently snapping to the
        // first option, which would look like the setting changed itself.
        const opt = el('option', null, String(v) + '  (not available)');
        opt.value = String(v);
        input.append(opt);
        input.value = String(v);
      }
    };
    ctl.append(input);
  } else if (f.kind === 'color') {
    input = el('input');
    input.type = 'color';
    input.id = 'f_' + f.key;
    const text = el('input');
    text.type = 'text';
    text.className = 'grow';
    input.oninput = () => { text.value = input.value; };
    text.onchange = () => { input.value = text.value; onEdit(f, wrap); };
    read = () => input.value;
    write = v => { input.value = v; text.value = v; };
    ctl.append(input, text);
  } else if ((f.kind === 'int' || f.kind === 'float') && f.min != null && f.max != null) {
    input = el('input');
    input.type = 'range';
    input.id = 'f_' + f.key;
    input.min = f.min;
    input.max = f.max;
    input.step = f.step != null ? f.step : (f.kind === 'int' ? 1 : 0.01);
    const num = el('input');
    num.type = 'number';
    num.min = f.min; num.max = f.max; num.step = input.step;
    num.style.width = '5.5rem'; num.style.flex = '0 0 auto';
    input.oninput = () => { num.value = input.value; valOut.textContent = ''; };
    num.onchange = () => { input.value = num.value; onEdit(f, wrap); };
    read = () => f.kind === 'int' ? parseInt(input.value, 10) : parseFloat(input.value);
    write = v => { input.value = v; num.value = v; };
    ctl.append(input, num);
  } else {
    input = el('input');
    input.type = f.kind === 'int' || f.kind === 'float' ? 'number' : 'text';
    input.id = 'f_' + f.key;
    if (f.placeholder) input.placeholder = f.placeholder;
    if (f.min != null) input.min = f.min;
    if (f.max != null) input.max = f.max;
    read = () => (f.kind === 'int' ? parseInt(input.value, 10)
      : f.kind === 'float' ? parseFloat(input.value) : input.value);
    write = v => { input.value = v == null ? '' : v; };
    ctl.append(input);
  }

  if (ctl.parentNode !== wrap && ctl.childNodes.length) wrap.append(ctl);
  if (locked) $$('input,select', wrap).forEach(n => { n.disabled = true; });
  else {
    input.addEventListener('change', () => onEdit(f, wrap));
  }

  if (f.help) {
    const help = el('div', 'help', f.help);
    if (f.cli) {
      help.append(el('br'));
      help.append(el('span', 'cli', f.cli));
    }
    wrap.append(help);
  }
  const extra = el('div', 'extra');
  wrap.append(extra);

  S.fields.set(f.key, { field: f, wrap, read, write, valOut, extra });
  return wrap;
}

function fillChoices(select, f) {
  const want = choicesFor(f);
  const sig = want.map(c => c.value + '\u0000' + c.label).join('\u0001');
  if (select.dataset.sig === sig) return;
  select.dataset.sig = sig;
  select.innerHTML = '';
  want.forEach(c => {
    const o = el('option', null, c.label);
    o.value = c.value;
    select.append(o);
  });
}

function choicesFor(f) {
  if (f.choices === 'languages') return S.schema.languages;
  if (f.choices === 'devices') {
    const mode = S.settings.capture;
    const list = [{ value: '', label: '(system default)' }];
    if (mode === 'loopback') {
      (S.devices.loopback || []).forEach(d =>
        list.push({ value: d.id, label: d.name + (d.default ? '  ← Windows default' : '') }));
    } else if (mode === 'input') {
      (S.devices.input || []).forEach(d =>
        list.push({ value: d.id, label: d.id + ': ' + d.name + ' (' + d.hostapi + ', ' + d.samplerate + ' Hz)' }));
    } else {
      list[0].label = mode === 'browser' ? '(streamed from this page)' : '(not used for a file)';
    }
    return list;
  }
  if (f.key === 'model') {
    const list = (S.models.faster_whisper || []).map(m => ({ value: m.path, label: m.name }));
    if (!list.some(c => c.value === S.settings.model)) list.unshift({ value: S.settings.model, label: S.settings.model });
    return list;
  }
  return f.choices || [];
}

let editTimer = null;
function onEdit(f, wrap) {
  const entry = S.fields.get(f.key);
  const value = entry.read();
  if (value === S.settings[f.key]) return;
  wrap.classList.add('changed');
  clearTimeout(editTimer);
  editTimer = setTimeout(() => patch({ [f.key]: value }), 60);
  if (f.rebuild && f.rebuild !== 'none') noteRebuild(f);
}

function noteRebuild(f) {
  const note = $('#applyNote');
  const what = {
    source: 'the capture device is being re-opened',
    backend: 'the transcription backend is being rebuilt - the CPU path takes a few seconds to load',
    gate: 'the speech gate is being retuned',
    strategy: 'the chunking is being rebuilt, so the window in progress is dropped',
    save: 'the transcript file is being reopened',
    overlay: 'the overlay is being restyled',
    restart: 'this one only takes effect when the program is started again',
  }[f.rebuild];
  if (!what) return;
  setLabel(note, 'warn', f.label + ': ' + what + '.');
  note.hidden = false;
  clearTimeout(note._t);
  note._t = setTimeout(() => { note.hidden = true; }, 4000);
}

function refreshControls() {
  S.fields.forEach((entry, key) => {
    const { field, wrap, write, valOut } = entry;
    const value = S.settings[key];
    write(value);
    wrap.classList.remove('changed');
    if (field.kind === 'int' || field.kind === 'float') {
      valOut.textContent = typeof value === 'number'
        ? (Number.isInteger(value) ? value : value.toFixed(2)) : '';
    } else valOut.textContent = '';
    wrap.classList.toggle('hidden', !visible(field));
  });
  applyFilter();
}

function visible(f) {
  if (f.advanced && !$('#chkAdvanced').checked) return false;
  const cond = f.showIf || {};
  for (const key in cond) {
    if (!cond[key].includes(S.settings[key])) return false;
  }
  return true;
}

function applyFilter() {
  const q = $('#filter').value.trim().toLowerCase();
  $$('.group').forEach(g => {
    let shown = 0;
    $$('.field', g).forEach(w => {
      const key = w.dataset.key;
      const f = S.fields.get(key).field;
      const hit = !q || key.includes(q) || f.label.toLowerCase().includes(q)
        || (f.help || '').toLowerCase().includes(q) || (f.cli || '').includes(q);
      const show = hit && visible(f);
      w.classList.toggle('hidden', !show);
      if (show) shown++;
    });
    g.style.display = shown ? '' : 'none';
    if (q && shown) g.open = true;
  });
}

// ---------------------------------------------------------------- status
function applyStatus(st) {
  S.status = Object.assign({}, S.status, st);
  const b = $('#pillBackend');
  const name = st.backend;
  b.className = 'pill ' + (name === 'server' ? 'gpu' : name === 'local' ? 'cpu' : st.error ? 'bad' : '');
  b.querySelector('b').textContent = name === 'server' ? 'GPU' : name === 'local' ? 'CPU' : '—';
  b.title = st.backend_name || st.error || 'no backend';

  const s = $('#pillSource');
  s.className = 'pill ' + (st.source_alive ? 'good' : st.source ? 'bad' : '');
  s.querySelector('b').textContent = st.source ? shortSource(st.source) : '—';
  s.title = st.source || 'no capture';

  setLabel($('#btnPause'), st.paused ? 'play' : 'pause',
    st.paused ? 'Resume' : 'Pause');
  $('#btnPause').classList.toggle('on', !!st.paused);

  applyRunState();

  if (st.stats && st.stats.xrt != null) {
    const p = $('#pillRt');
    p.querySelector('b').textContent = st.stats.xrt.toFixed(2) + 'x';
    p.className = 'pill ' + (st.stats.xrt < 0.9 ? 'good' : 'bad');
  }
  maybeSuggestModel(st);
}

function shortSource(name) {
  const cut = name.indexOf(':');
  const tail = cut >= 0 ? name.slice(cut + 1).trim() : name;
  return tail.length > 22 ? tail.slice(0, 21) + '…' : tail;
}

function applyMeter(m) {
  S.lastMeter = m;
  const lvl = Math.min(1, Math.sqrt(m.level / 0.25));      // sqrt: speech lives low
  $('#levelBar').style.width = (lvl * 100).toFixed(1) + '%';
  $('#levelNum').textContent = m.level.toFixed(4);
  const t = Math.min(1, Math.sqrt(m.threshold / 0.25));
  $('#levelThresh').style.left = (t * 100).toFixed(1) + '%';

  const need = m.min_frames || 0;
  if (m.vad && need) {
    const frames = m.speech_frames == null ? null : m.speech_frames;
    const scale = Math.max(need * 3, 24);
    $('#vadBar').style.width = frames == null ? '0%' : Math.min(100, frames / scale * 100).toFixed(1) + '%';
    $('#vadThresh').style.left = Math.min(100, need / scale * 100).toFixed(1) + '%';
    $('#vadNum').textContent = frames == null ? '—' : frames + '/' + need;
  } else {
    $('#vadBar').style.width = '0%';
    $('#vadNum').textContent = m.vad ? '—' : 'off';
  }

  const g = $('#gateState');
  if (m.gated === 'silence') {
    g.className = 'gateState silence';
    setLabel(g, 'warn', 'below the silence threshold — nothing is being transcribed');
  } else if (m.gated === 'no-speech') {
    g.className = 'gateState nospeech';
    setLabel(g, 'warn', 'audible, but Silero does not hear speech in it');
  } else if (m.level > m.threshold) {
    g.className = 'gateState live';
    setLabel(g, 'check', 'listening');
  } else {
    g.className = 'gateState';
    g.innerHTML = '';
  }

  $('#pendingInfo').textContent = m.pending_s ? ('buffered ' + m.pending_s.toFixed(1) + ' s'
    + (m.queue ? '  ·  queue ' + m.queue : '')) : '';
}

function applyPerf(p) {
  const pill = $('#pillRt');
  pill.querySelector('b').textContent = p.xrt.toFixed(2) + 'x';
  pill.className = 'pill ' + (p.sustainable ? 'good' : 'bad');
  pill.title = p.infer_s.toFixed(2) + ' s for a ' + p.window_s + ' s window, step ' + p.slide_s + ' s';
}

/* When the GPU cannot hold pace, the useful next move is a smaller GGML model -
   and the panel already knows which ones are sitting in _models. Offering it
   here is the difference between a warning and a fix. */
let modelHintShown = false;
function maybeSuggestModel(st) {
  if (modelHintShown || !st.stats || st.stats.xrt == null) return;
  if (st.stats.xrt < 1.0 || st.stats.windows < 3) return;
  const ggml = S.models.ggml || [];
  if (!ggml.length) return;
  modelHintShown = true;
  const smallest = ggml.slice().sort((a, b) => a.size_mb - b.size_mb)[0];
  const box = el('div', 'toast warn');
  box.append(el('div', null,
    'Inference is slower than real time (' + st.stats.xrt.toFixed(2) + 'x). '
    + 'A smaller model is the biggest single lever.'));
  const row = el('div', 'row');
  row.style.marginTop = '8px';
  const sel = el('select');
  ggml.forEach(m => {
    const o = el('option', null, m.name + '  (' + m.size_mb + ' MB)');
    o.value = m.name;
    sel.append(o);
  });
  sel.value = smallest.name;
  const go = el('button', 'btn tiny primary', 'Restart server with it');
  // 'restart', not 'start': the whole point is that one is already running,
  // and 'start' would refuse because the port is taken.
  go.onclick = () => { command('whisper_server', { action: 'restart', model: sel.value }); box.remove(); };
  const bench = el('button', 'btn tiny', 'Benchmark instead');
  bench.onclick = () => { showTab('bench'); box.remove(); };
  row.append(sel, go, bench);
  box.append(row);
  $('#toasts').append(box);
  setTimeout(() => box.remove(), 30000);
}

// ---------------------------------------------------------------- lifecycle
/*
  Start / Stop / Restart, for the session and for the GPU server.

  These exist because every other control here is a settings patch, and a
  settings patch can leave something down. Change the capture device to one
  that has been unplugged, or switch the backend to the server before the
  server is up, and the component fails to rebuild - the Python side logs it
  and carries on with that attribute set to nothing. Nothing retries. Before
  these buttons the only cure was closing the program and starting it again,
  which is a walk to the machine it runs on if you are reading this over the
  network.

  Both cards are driven from one place: `state` says whether the session is
  running, `engine` says whether whisper-server is, and `busy` names the
  command in flight so the buttons cannot be pressed twice into a race.
*/
function blocked() {
  return S.busy || (S.connected ? '' : 'offline');
}

function applyRunState() {
  const st = S.status || {};
  const busy = blocked();
  const running = !!st.running;

  const run = $('#btnRun');
  setLabel(run, running ? 'stop' : 'play', running ? 'Stop' : 'Start');
  run.title = running ? 'Stop capture and transcription altogether'
    : 'Build everything again and start capturing';
  run.classList.toggle('primary', !running);
  run.disabled = !!busy;
  $('#btnRestart').disabled = !!busy;
  $('#btnPause').disabled = !!busy || !running;

  const sRun = $('#btnSessionRun');
  setLabel(sRun, running ? 'stop' : 'play', running ? 'Stop' : 'Start');
  sRun.disabled = !!busy;
  $('#btnSessionRestart').disabled = !!busy;
  $('#btnReconnect').disabled = !!busy || !running;

  const dot = $('#sessionDot');
  dot.className = 'engineDot ' + (busy && !busy.startsWith('engine') ? 'busy'
    : running ? (st.source_alive ? 'on' : 'off') : 'off');
  $('#sessionState').textContent = running
    ? (st.source_alive ? 'capturing' : 'running, but nothing is being captured')
    : 'stopped';
  $('#sessionSource').textContent = st.source || 'no capture';
  $('#sessionWho').textContent = [
    st.backend_name ? 'backend: ' + st.backend_name : 'backend: none',
    st.strategy ? 'chunking: ' + st.strategy : null,
    st.gate ? 'speech gate on' : 'speech gate off',
    st.saving ? 'writing ' + st.saving : null,
  ].filter(Boolean).join('  ·  ');

  renderBanner();
}

/* One line above the meters, because that is where the eye already is when
   nothing is happening - "the meters are flat" and "nothing is running" look
   identical until something says which. */
function renderBanner() {
  const st = S.status || {};
  const box = $('#runBanner');
  const busy = S.busy || '';
  box.innerHTML = '';
  box.className = 'runBanner';

  // Silent during the opening handshake - the dot already says "connecting",
  // and a red banner on every page load would be noise. Once we have been
  // connected, or two retries have failed, the silence is the wrong answer.
  if (!S.connected && (S.everConnected || S.retry >= 2)) {
    box.className = 'runBanner bad';
    box.append(icon('warn'));
    box.append(el('span', 'grow', S.everConnected
      ? 'Lost the connection to the program. Everything below is the last '
        + 'thing it said, and no button here can reach it. If its window has '
        + 'closed, start it again — this page reconnects on its own.'
      : 'Cannot reach the program on this address. Check that it is running '
        + 'with --web, and that the port matches.'));
    box.hidden = false;
    return;
  }

  const say = (kind, iconName, text, actions) => {
    box.className = 'runBanner ' + kind;
    box.append(icon(iconName));
    box.append(el('span', 'grow', text));
    (actions || []).forEach(([label, fn, primary]) => {
      const b = el('button', 'btn tiny' + (primary ? ' primary' : ''), label);
      b.onclick = fn;
      box.append(b);
    });
    box.hidden = false;
  };

  if (busy) {
    say('busy', 'refresh', busy.charAt(0).toUpperCase() + busy.slice(1)
      + ' in progress — this takes a few seconds.');
    return;
  }
  if (!st.running) {
    say('bad', 'warn', 'Nothing is capturing or transcribing.', [
      ['Start', () => command('start'), true],
      ['Open Engine', () => showTab('engine')],
    ]);
    return;
  }
  if (!st.source_alive) {
    say('bad', 'warn', 'The capture device is not running'
      + (st.error ? ' — ' + st.error : '.'), [
      ['Restart', () => command('restart'), true],
      ['Rescan devices', () => command('devices')],
    ]);
    return;
  }
  if (!st.backend) {
    say('bad', 'warn', 'No transcription backend'
      + (st.error ? ' — ' + st.error : '.'), [
      ['Reconnect', () => command('rebuild', { what: ['backend'] }), true],
      ['Open Engine', () => showTab('engine')],
    ]);
    return;
  }
  box.hidden = true;
}

function renderEngine() {
  const e = S.engine || {};
  const busy = (S.busy || '').startsWith('engine') ? S.busy : '';
  const anyBusy = !!blocked();

  $('#engineDot').className = 'engineDot '
    + (busy ? 'busy' : e.reachable ? 'on' : 'off');
  $('#engineState').textContent = busy ? busy + '…'
    : e.reachable ? 'answering'
      : e.pid ? 'port taken, no answer yet'
        : 'not running';
  $('#engineUrl').textContent = e.url || '—';

  const who = [];
  if (e.pid) {
    who.push('PID ' + e.pid + ' holds ' + e.host + ':' + e.port
      + ' — ' + (e.image || 'image unknown'));
    if (!e.ours) {
      who.push('that is not a whisper server, so Stop will refuse to kill it');
    }
  } else {
    who.push('nothing is listening on ' + (e.host || '?') + ':' + (e.port || '?'));
  }
  if (e.canStart === false) {
    who.push('start_whisper_server.cmd is not next to app.py, so it cannot be started from here');
  }
  if (e.backend === 'local') {
    who.push('the backend setting is "local", so this session will not use it even when it is up');
  }
  $('#engineWho').textContent = who.join('  ·  ');

  fillEngineModels(e.models || []);
  $('#btnEngineStart').disabled = anyBusy || !!e.pid || e.canStart === false;
  $('#btnEngineRestart').disabled = anyBusy || e.canStart === false;
  $('#btnEngineStop').disabled = anyBusy || !e.pid;
  $('#btnEngineCheck').disabled = anyBusy;
}

function fillEngineModels(models) {
  const sel = $('#engineModel');
  const sig = models.map(m => m.name + m.size_mb).join('|');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  const cur = sel.value;
  sel.innerHTML = '';
  sel.append(el('option', null, models.length
    ? '(whatever start_whisper_server.cmd defaults to)'
    : 'no .bin files in _models'));
  models.forEach(m => {
    const o = el('option', null, m.name + '  (' + m.size_mb + ' MB)');
    o.value = m.name;
    sel.append(o);
  });
  if (cur) sel.value = cur;
}

function engineArgs() {
  return { model: $('#engineModel').value || '' };
}

// ---------------------------------------------------------------- transcript
function addCaption(entry, quiet) {
  const host = $('#transcript');
  $('#emptyHint').style.display = 'none';
  const line = el('div', 'line' + (quiet ? '' : ' fresh'));

  const when = new Date((entry.wall || Date.now() / 1000) * 1000);
  const meta = el('span', 'meta', when.toTimeString().slice(0, 8) + '  ' + fmtClock(entry.t_start));
  meta.title = 'at ' + fmtClock(entry.t_start) + ' into the session · '
    + entry.duration + ' s window · ' + entry.processing_time + ' s to transcribe';
  line.append(meta);

  const body = el('span', 'body');
  const tag = el('span', 'tag ' + (entry.backend || ''),
    (entry.language || '??') + (entry.translated ? '→EN' : ''));
  tag.title = describeDetection(entry);
  body.append(tag);

  const warn = S.settings.confidence_warn != null ? S.settings.confidence_warn : 0.6;
  const words = entry.words || [];
  if (words.length) {
    words.forEach(w => {
      const span = el('span', 'w', w.word);
      const p = w.probability;
      if (typeof p === 'number') {
        span.classList.add(p < warn ? 'low' : p < (warn + 1) / 2 ? 'mid' : 'hi');
        span.title = 'confidence ' + (p * 100).toFixed(0) + '%';
      }
      body.append(span);
    });
  } else {
    body.append(document.createTextNode(entry.text));
  }
  line.append(body);
  host.append(line);
  trimTranscript();
  if ($('#chkAutoscroll').checked) host.parentElement.scrollTop = host.parentElement.scrollHeight;
  noteLanguage(entry);
  checkPin(entry);
}

function fmtClock(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function describeDetection(entry) {
  const d = entry.detection || {};
  if (d.pinned) {
    let out = 'language pinned to ' + (entry.language || '??') + ' — not detected';
    if (d.detected) {
      out += '\nthe detector heard ' + d.detected.language
        + (d.detected.probability != null
          ? ' (' + (d.detected.probability * 100).toFixed(0) + '%)' : '');
    }
    return out;
  }
  let out = 'language ' + (entry.language || '??');
  if (d.probability != null) out += '  (' + (d.probability * 100).toFixed(0) + '%)';
  if (d.candidates && d.candidates.length > 1) {
    out += '\nalso considered: ' + d.candidates.slice(1)
      .map(c => c[0] + ' ' + (c[1] * 100).toFixed(0) + '%').join(', ');
  }
  return out;
}

/* A pin the audio disagrees with is silent damage: Whisper will decode Malay
   out of English rather than argue, and the transcript looks like the model
   simply got worse. The backend only sets `disagrees` when the detector is
   confident, so this cannot nag over noisy audio - which is what pinning was
   chosen to stop in the first place. */
let pinWarned = false;
function checkPin(entry) {
  const d = entry.detection || {};
  if (pinWarned || !d.disagrees || !d.detected) return;
  pinWarned = true;
  const box = el('div', 'toast warn');
  box.append(el('div', null,
    'Language is pinned to ' + entry.language + ', but this audio reads as '
    + d.detected.language + ' at '
    + ((d.detected.probability || 0) * 100).toFixed(0) + '% confidence. '
    + 'Whisper will decode it as ' + entry.language + ' regardless.'));
  const row = el('div', 'row');
  row.style.marginTop = '8px';
  const swap = el('button', 'btn tiny primary', 'Pin ' + d.detected.language);
  swap.onclick = () => { patch({ language: d.detected.language }); box.remove(); };
  const auto = el('button', 'btn tiny', 'Back to auto-detect');
  auto.onclick = () => { patch({ language: 'auto' }); box.remove(); };
  const keep = el('button', 'btn tiny', 'Keep it');
  keep.onclick = () => box.remove();
  row.append(swap, auto, keep);
  box.append(row);
  $('#toasts').append(box);
}

function trimTranscript() {
  // A long meeting is thousands of lines and the DOM is the only thing that
  // gets slower. The Python side keeps the full history; this is only the view.
  const host = $('#transcript');
  while (host.childNodes.length > 600) host.removeChild(host.firstChild);
}

/* Auto-detect reruns on every buffer, so a session can wander between
   languages. When it does, say so and offer the one-click fix, since pinning
   is exactly what stops it. */
function noteLanguage(entry) {
  const code = entry.language;
  if (!code || code === '??' || S.settings.language !== 'auto') return;
  S.langSeen.set(code, (S.langSeen.get(code) || 0) + 1);
  const pill = $('#pillLang');
  pill.querySelector('b').textContent = code;
  if (S.langSeen.size < 2 || pill.dataset.warned) return;
  const total = Array.from(S.langSeen.values()).reduce((a, b) => a + b, 0);
  if (total < 6) return;
  pill.dataset.warned = '1';
  pill.className = 'pill bad';
  const top = Array.from(S.langSeen.entries()).sort((a, b) => b[1] - a[1]);
  const box = el('div', 'toast warn');
  box.append(el('div', null,
    'Auto-detect has landed on ' + top.length + ' different languages this session ('
    + top.map(([c, n]) => c + '×' + n).join(', ')
    + '). Detection reruns per buffer, and the transcript follows it.'));
  const row = el('div', 'row');
  row.style.marginTop = '8px';
  const btn = el('button', 'btn tiny primary', 'Pin ' + top[0][0]);
  btn.onclick = () => { patch({ language: top[0][0] }); box.remove(); };
  const dismiss = el('button', 'btn tiny', 'Leave it');
  dismiss.onclick = () => box.remove();
  row.append(btn, dismiss);
  box.append(row);
  $('#toasts').append(box);
}

// ---------------------------------------------------------------- log
function addLog(level, msg) {
  const host = $('#log');
  const line = el('div', level || 'info');
  line.append(el('span', 't', new Date().toTimeString().slice(0, 8)));
  line.append(document.createTextNode(msg));
  host.append(line);
  while (host.childNodes.length > 500) host.removeChild(host.firstChild);
  if (host.parentElement.classList.contains('active')) {
    host.parentElement.scrollTop = host.parentElement.scrollHeight;
  } else if (level === 'warn' || level === 'error') {
    S.unseenLogs++;
    const b = $('#logBadge');
    b.textContent = S.unseenLogs;
    b.classList.add('show');
  }
  if (level === 'error') toast(msg, 'error');
}

// ---------------------------------------------------------------- benchmark
function renderBench(data) {
  const out = $('#benchOut');
  if (data.status === 'error') { out.innerHTML = ''; toast(data.msg, 'error'); return; }
  if (data.status === 'running') {
    out.innerHTML = '';
    out.append(el('p', 'dim', 'Measuring ' + data.sizes.join(', ') + ' s through the live backend. '
      + 'Transcription is paused while this runs.'));
    if (data.synthetic) {
      out.append(el('p', 'warn tiny',
        'Synthetic tones decode to almost no text, so this measures the encoder and '
        + 'little else. Point it at a WAV of real speech for numbers you can act on.'));
    }
    out.append(el('div', null, '…'));
    return;
  }
  const rows = data.rows || [];
  const table = el('table', 'bench');
  const head = el('tr');
  ['buffer', 'median', 'min', 'max', 'xRT', 'chars', 'slide'].forEach(h => {
    head.append(el('th', null, h));
  });
  table.append(head);
  const best = data.recommend;
  rows.forEach(r => {
    const tr = el('tr', best && r.buffer === best.buffer ? 'win' : '');
    [r.buffer + 's', r.median.toFixed(2) + 's', r.min.toFixed(2) + 's', r.max.toFixed(2) + 's',
    r.xrt.toFixed(2) + 'x', r.chars].forEach(v => tr.append(el('td', null, String(v))));
    tr.append(el('td', r.slide ? '' : 'no', r.slide ? r.slide + 's' : '—'));
    table.append(tr);
  });
  const out2 = el('div');
  out2.append(table);

  if (data.status === 'done') {
    if (best) {
      const rec = el('div', 'recommend');
      rec.append(el('b', null, '--buffer ' + best.buffer + ' --slide ' + best.slide));
      rec.append(el('span', 'tiny',
        'about ' + best.buffer + ' s from speech to caption, with ' + (best.buffer - best.slide)
        + ' s of overlap for the duplicate filter'));
      const go = el('button', 'btn primary', 'Apply to the running session');
      go.onclick = () => {
        // Sent together: the validator rejects slide > buffer, so applying them
        // one at a time would bounce whichever arrived first.
        patch({ strategy: 'sliding_window', buffer: best.buffer, slide: best.slide });
        toast('Applied buffer ' + best.buffer + ' s, slide ' + best.slide + ' s', 'good');
      };
      rec.append(go);
      out2.append(rec);
    } else {
      const bad = el('div', 'recommend bad');
      bad.append(icon('warn'));
      bad.append(el('span', null,
        'Nothing tested holds real time. A smaller or more-quantized model is the '
        + 'biggest lever; after that, a larger buffer (cost is near-flat up to 30 s) '
        + 'and a lower audio context.'));
      out2.append(bad);
    }
    if (data.synthetic) {
      out2.append(el('p', 'warn tiny',
        'Measured on synthetic tones. Real speech decodes more tokens, and decoding '
        + 'is the part that varies - re-run with a WAV before trusting these.'));
    }
    out2.append(el('p', 'dim tiny',
      'Measured with nothing else running, on the ' + (data.backend === 'server' ? 'GPU server' : 'CPU')
      + '. Live capture competes with whatever is playing the audio, so treat it as a '
      + 'starting point: if the log prints a pace warning during real use, raise the slide.'));
  }
  out.innerHTML = '';
  out.append(out2);
}

// ---------------------------------------------------------------- browser mic
async function startMic(kind) {
  if (S.settings.capture !== 'browser') {
    patch({ capture: 'browser' });
    toast('Capture mode switched to "This browser"', 'good');
  }
  try {
    const stream = kind === 'tab'
      ? await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { echoCancellation: false, autoGainControl: false, noiseSuppression: false },
      })
      : await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, autoGainControl: false, noiseSuppression: true },
      });
    if (!stream.getAudioTracks().length) {
      stream.getTracks().forEach(t => t.stop());
      throw new Error('That share has no audio track - tick "Share tab audio" in the picker.');
    }
    stream.getVideoTracks().forEach(t => t.stop());   // we only ever wanted the sound

    const ctx = new AudioContext();
    await ctx.audioWorklet.addModule('audio-worklet.js');
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, 'realtime-audio-processor', {
      processorOptions: { targetRate: 16000 },
    });
    node.port.onmessage = (ev) => {
      if (ev.data && ev.data.silent) {
        $('#micWarn').textContent = 'No audio is arriving on that track - if you shared a tab, it may be muted.';
        $('#micWarn').hidden = false;
        return;
      }
      if (!(ev.data instanceof ArrayBuffer)) return;
      $('#micWarn').hidden = true;
      if (S.ws && S.ws.readyState === WebSocket.OPEN) {
        S.ws.send(ev.data);
        S.mic.sent += ev.data.byteLength;
        updateMicStats();
      }
    };
    src.connect(node);
    // Connect to a silent gain so the graph is pulled even with no output.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    node.connect(sink).connect(ctx.destination);

    stream.getAudioTracks()[0].addEventListener('ended', () => stopMic('the share ended'));
    S.mic = { stream, ctx, node, sent: 0, started: Date.now() };
    $('#micState').textContent = 'streaming at ' + ctx.sampleRate + ' Hz → 16 kHz';
    setLabel($('#btnMic'), 'pause', 'Stop');
    $('#btnMic').classList.add('on');
    $('#btnTabAudio').disabled = true;
  } catch (e) {
    toast('Could not capture: ' + e.message, 'error');
  }
}

function stopMic(why) {
  const m = S.mic;
  if (m.stream) m.stream.getTracks().forEach(t => t.stop());
  if (m.node) try { m.node.disconnect(); } catch (e) { /* already gone */ }
  if (m.ctx) m.ctx.close().catch(() => { });
  S.mic = { stream: null, ctx: null, node: null, sent: 0, started: 0 };
  $('#micState').textContent = why ? 'stopped (' + why + ')' : 'not streaming';
  setLabel($('#btnMic'), 'mic', 'Start microphone');
  $('#btnMic').classList.remove('on');
  $('#btnTabAudio').disabled = false;
  $('#micStats').textContent = '';
}

function updateMicStats() {
  const m = S.mic;
  if (!m.started) return;
  const secs = (Date.now() - m.started) / 1000;
  $('#micStats').textContent =
    (m.sent / 1024).toFixed(0) + ' KB sent · ' + (m.sent / 2 / 16000).toFixed(1)
    + ' s of audio · ' + secs.toFixed(0) + ' s elapsed';
}

// ---------------------------------------------------------------- misc UI
function toast(msg, kind) {
  const box = el('div', 'toast ' + (kind || ''), msg);
  $('#toasts').append(box);
  setTimeout(() => box.remove(), kind === 'error' ? 9000 : 4500);
}

function downloadExport(data) {
  if (!data.count) { toast('Nothing to export yet', 'warn'); return; }
  const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
  const a = el('a');
  a.href = URL.createObjectURL(blob);
  a.download = data.filename;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast(data.count + ' captions exported as ' + data.format, 'good');
}

function renderPresets() {
  const sel = $('#presetSelect');
  const cur = sel.value;
  sel.innerHTML = '';
  // The empty value is load-bearing. el() sets className and textContent
  // only, so without it the placeholder's value becomes its own label and
  // the onchange handler below - which fires on any truthy name - would ask
  // the engine to load a preset called 'Presets…'. index.html's static
  // markup has value="" for the same reason; this rebuild has to match it.
  const placeholder = el('option', null, 'Presets…');
  placeholder.value = '';
  sel.append(placeholder);
  S.presets.forEach(p => {
    const o = el('option', null, p);
    o.value = p;
    sel.append(o);
  });
  // Only restore a selection that still exists - a deleted preset would
  // otherwise leave selectedIndex at -1, which renders as a blank control
  // rather than showing the placeholder.
  sel.value = cur && S.presets.includes(cur) ? cur : '';
}

function showTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'log') {
    S.unseenLogs = 0;
    $('#logBadge').classList.remove('show');
    const host = $('#log');
    host.parentElement.scrollTop = host.parentElement.scrollHeight;
  }
}

// ---------------------------------------------------------------- wiring
function wire() {
  $$('.tab').forEach(t => t.onclick = () => showTab(t.dataset.tab));

  $('#btnRun').onclick = () => command(S.status.running ? 'stop' : 'start');
  $('#btnRestart').onclick = () => command('restart');
  $('#btnPause').onclick = () => command(S.status.paused ? 'resume' : 'pause');

  $('#btnSessionRun').onclick = () => command(S.status.running ? 'stop' : 'start');
  $('#btnSessionRestart').onclick = () => command('restart');
  $('#btnReconnect').onclick = () => command('rebuild', { what: ['backend'] });

  $('#btnEngineStart').onclick = () => command('whisper_server', Object.assign({ action: 'start' }, engineArgs()));
  $('#btnEngineRestart').onclick = () => command('whisper_server', Object.assign({ action: 'restart' }, engineArgs()));
  $('#btnEngineStop').onclick = () => {
    if (S.engine.ours === false && S.engine.pid) {
      toast('PID ' + S.engine.pid + ' is not a whisper server — it will not be killed', 'warn');
    }
    command('whisper_server', { action: 'stop' });
  };
  $('#btnEngineCheck').onclick = () => command('whisper_server', { action: 'status' });
  $('#btnClear').onclick = () => {
    command('clear');
    $('#transcript').innerHTML = '';
    $('#emptyHint').style.display = '';
    S.langSeen.clear();
  };
  $('#btnExport').onclick = (e) => {
    e.stopPropagation();
    $('#exportMenu').classList.toggle('open');
  };
  $$('#exportMenu button').forEach(b => b.onclick = () => {
    $('#exportMenu').classList.remove('open');
    command('export', { format: b.dataset.fmt });
  });
  document.addEventListener('click', () => $('#exportMenu').classList.remove('open'));

  $('#btnTheme').onclick = () => {
    const now = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = now;
    try { localStorage.setItem('lat.theme', now); } catch (e) { /* private mode */ }
  };

  $('#chkAdvanced').onchange = refreshControls;
  $('#filter').oninput = applyFilter;
  $('#chkConfidence').onchange = () =>
    document.body.classList.toggle('showConfidence', $('#chkConfidence').checked);
  $('#chkTimes').onchange = () =>
    document.body.classList.toggle('showTimes', $('#chkTimes').checked);

  $('#btnDefaults').onclick = () => {
    if (!confirm('Reset every setting to its default?')) return;
    patch(S.schema.defaults);
  };
  $('#btnRefreshDevices').onclick = () => command('devices');

  $('#presetSelect').onchange = () => {
    const name = $('#presetSelect').value;
    if (name) command('preset_load', { name });
  };
  $('#btnPresetSave').onclick = () => {
    const name = prompt('Save the current settings as:', $('#presetSelect').value || 'my-setup');
    if (name) command('preset_save', { name });
  };
  $('#btnPresetDelete').onclick = () => {
    const name = $('#presetSelect').value;
    if (name && confirm('Delete preset "' + name + '"?')) command('preset_delete', { name });
  };

  $('#btnBench').onclick = () => command('benchmark', {
    buffers: $('#benchSizes').value.split(',').map(s => parseInt(s.trim(), 10)).filter(n => n > 0),
    reps: parseInt($('#benchReps').value, 10) || 2,
    wav: $('#benchWav').value.trim() || null,
  });

  $('#btnMic').onclick = () => S.mic.stream ? stopMic() : startMic('mic');
  $('#btnTabAudio').onclick = () => startMic('tab');

  document.body.classList.add('showConfidence');
  try {
    const saved = localStorage.getItem('lat.theme');
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (e) { /* private mode */ }

  setInterval(updateMicStats, 1000);
}

wire();
connect();
