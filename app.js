'use strict';

// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 the Klepsydra authors

// Klepsydra, an interval timer. Timers come from this browser's localStorage, or the bundled
// timers.json until the user saves their own. The file is hand-edited, so everything in it is
// validated before use and only ever reaches the page through textContent / style properties.

const CONFIG_URL = 'timers.json';
const STORAGE_KEY = 'klepsydra:timers';
const KEEP_AWAKE_KEY = 'klepsydra:keep-awake';
const LEGACY_KEY = 'interval-timer:last-file';  // earlier versions remembered an opened file here
const MAX_FILE_BYTES = 1024 * 1024;
const REPO_URL = 'https://github.com/n03l/klepsydra';

const LIMITS = {
  timers: 200,
  intervals: 2000,        // per timer, after expanding repeats
  depth: 5,               // nesting of repeat blocks
  repeat: 100,
  duration: 24 * 3600,    // seconds
  countdown: 10,
  prepare: 120,
  name: 60,
  description: 200,
  comment: 100,
};

const DEFAULTS = { countdown: 3, halfway: false, prepare: 0 };
const PALETTE = ['#2196f3', '#e53935', '#43a047', '#fb8c00', '#8e24aa', '#00897b', '#6d4c41', '#3949ab'];
const PALETTE_NAMES = ['Blue', 'Red', 'Green', 'Orange', 'Purple', 'Teal', 'Brown', 'Indigo'];
const PREPARE_COLOR = '#546e7a';
const DONE_COLOR = '#263238';

const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- config parsing

class ConfigError extends Error {}

function parseDuration(v, where) {
  let s;
  if (typeof v === 'number') {
    s = v;
  } else if (typeof v === 'string' && /^\d{1,5}(:\d{1,2}){0,2}$/.test(v.trim())) {
    s = v.trim().split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
  } else {
    throw new ConfigError(`${where}: duration must be seconds or "m:ss"`);
  }
  if (!Number.isFinite(s) || s < 1 || s > LIMITS.duration) {
    throw new ConfigError(`${where}: duration must be between 1 s and 24 h`);
  }
  return Math.round(s * 1000);
}

function parseInt0(v, min, max, where, what) {
  if (!Number.isInteger(v) || v < min || v > max) {
    throw new ConfigError(`${where}: ${what} must be a whole number from ${min} to ${max}`);
  }
  return v;
}

function parseBool(v, where, what) {
  if (typeof v !== 'boolean') throw new ConfigError(`${where}: ${what} must be true or false`);
  return v;
}

// Free text shown on one line of the UI (description, comment): whitespace collapsed, cut to the limit.
function parseShortText(v, where, what) {
  if (typeof v !== 'string') throw new ConfigError(`${where}: ${what} must be a string`);
  const chars = Array.from(v.trim().replace(/\s+/g, ' '));
  return chars.length > LIMITS[what] ? chars.slice(0, LIMITS[what] - 1).join('') + '…' : chars.join('');
}

function parseName(v, where) {
  if (typeof v !== 'string' || !v.trim()) throw new ConfigError(`${where}: name must be a non-empty string`);
  return Array.from(v.trim()).slice(0, LIMITS.name).join('');
}

// Let the browser parse the colour (so names like "tomato" work), accept only opaque results.
const colorCtx = document.createElement('canvas').getContext('2d');
function parseColor(v, where) {
  if (typeof v !== 'string' || v.length > 40) throw new ConfigError(`${where}: color must be a CSS colour string`);
  colorCtx.fillStyle = '#000000';
  colorCtx.fillStyle = v;
  const a = colorCtx.fillStyle;
  colorCtx.fillStyle = '#ffffff';
  colorCtx.fillStyle = v;
  const b = colorCtx.fillStyle;
  if (a !== b || !/^#[0-9a-f]{6}$/.test(a)) throw new ConfigError(`${where}: "${v}" is not an opaque colour`);
  return a;
}

// Per-interval options that inherit from the enclosing block / timer.
function parseOptions(obj, inherited, where) {
  const o = { ...inherited };
  if (hasOwn(obj, 'countdown')) o.countdown = parseInt0(obj.countdown, 0, LIMITS.countdown, where, 'countdown');
  if (hasOwn(obj, 'halfway')) o.halfway = parseBool(obj.halfway, where, 'halfway');
  return o;
}

// Flattens `list` into `out` (the intervals as they run). When `outline` is given, also records the
// structure as written ({name, duration, color} steps and {repeat, items} blocks) for the details view;
// every interval's `step` points at its entry there.
function expand(list, opts, depth, where, out, ctx, outline) {
  if (!Array.isArray(list) || list.length === 0) throw new ConfigError(`${where}: intervals must be a non-empty list`);
  if (depth > LIMITS.depth) throw new ConfigError(`${where}: repeat blocks nested too deeply`);
  list.forEach((item, i) => {
    const at = `${where} #${i + 1}`;
    if (!isPlainObject(item)) throw new ConfigError(`${at}: must be an object`);
    const itemOpts = parseOptions(item, opts, at);
    if (hasOwn(item, 'repeat')) {
      const n = parseInt0(item.repeat, 1, LIMITS.repeat, at, 'repeat');
      const block = { repeat: n, items: [] };
      if (outline) outline.push(block);
      let first = 0;
      let size = 0;
      for (let r = 1; r <= n; r++) {
        const start = out.length;
        expand(item.intervals, itemOpts, depth + 1, at, out, ctx, r === 1 && outline ? block.items : null);
        if (r === 1) {
          first = start;
          size = out.length - start;
        } else {
          for (let k = 0; k < size; k++) out[start + k].step = out[first + k].step;  // same steps as round 1
        }
        // The innermost repeat that covers an interval labels its round.
        for (let k = start; k < out.length; k++) if (!out[k].round) out[k].round = `${r}/${n}`;
      }
      return;
    }
    const name = parseName(item.name, at);
    const duration = parseDuration(item.duration, at);
    let color;
    if (hasOwn(item, 'color')) {
      color = parseColor(item.color, at);
    } else {
      if (!ctx.colorByName.has(name)) ctx.colorByName.set(name, PALETTE[ctx.colorByName.size % PALETTE.length]);
      color = ctx.colorByName.get(name);
    }
    const comment = hasOwn(item, 'comment') ? parseShortText(item.comment, at, 'comment') : '';
    const step = { name, duration, color, comment };
    if (outline) outline.push(step);
    out.push({ ...step, countdown: itemOpts.countdown, halfway: itemOpts.halfway, round: '', step });
    if (out.length > LIMITS.intervals) throw new ConfigError(`${where}: more than ${LIMITS.intervals} intervals`);
  });
}

function parseTimer(raw, index) {
  let where = `Timer ${index + 1}`;
  if (!isPlainObject(raw)) throw new ConfigError(`${where}: must be an object`);
  const name = parseName(raw.name, where);
  where = `"${name}"`;
  const description = hasOwn(raw, 'description') ? parseShortText(raw.description, where, 'description') : '';
  const opts = parseOptions(raw, DEFAULTS, where);
  const prepare = hasOwn(raw, 'prepare') ? parseInt0(raw.prepare, 0, LIMITS.prepare, where, 'prepare') : DEFAULTS.prepare;
  const intervals = [];
  const outline = [];
  expand(raw.intervals, opts, 0, where, intervals, { colorByName: new Map() }, outline);
  if (prepare > 0) {
    const step = { name: 'Get ready', duration: prepare * 1000, color: PREPARE_COLOR, comment: '' };
    outline.unshift(step);
    intervals.unshift({ ...step, countdown: opts.countdown, halfway: false, round: '', prepare: true, step });
  }
  return { name, description, intervals, outline, total: intervals.reduce((s, it) => s + it.duration, 0) };
}

// Returns [{timer} | {name, error}] so one bad timer doesn't hide the others.
function parseConfig(data) {
  const list = Array.isArray(data) ? data : isPlainObject(data) && hasOwn(data, 'timers') ? data.timers : null;
  if (!Array.isArray(list)) throw new ConfigError('The file must be a list of timers or {"timers": [...]}');
  if (list.length > LIMITS.timers) throw new ConfigError(`The file has more than ${LIMITS.timers} timers`);
  return list.map((raw, i) => {
    try {
      return { timer: parseTimer(raw, i) };
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      const name = isPlainObject(raw) && typeof raw.name === 'string' && raw.name.trim()
        ? Array.from(raw.name.trim()).slice(0, LIMITS.name).join('') : `Timer ${i + 1}`;
      return { name, error: e.message };
    }
  });
}

// ---------------------------------------------------------------- formatting

function fmt(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000 - 1e-6));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function textOn(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.45 ? '#111111' : '#ffffff';
}

// ---------------------------------------------------------------- sound

let audio = null;

// Must be called from a user gesture the first time (browsers block audio otherwise).
function unlockAudio() {
  try {
    if (!audio) {
      audio = new (window.AudioContext || window.webkitAudioContext)();
      tone(440, 30, 0, 0);  // iOS only unlocks audio once something plays inside the gesture
    }
    if (audio.state === 'suspended') audio.resume();
  } catch (e) {
    audio = null;
  }
}

function tone(freq, durMs, delayMs = 0, volume = 0.5) {
  if (!audio) return;
  const t0 = audio.currentTime + delayMs / 1000;
  const t1 = t0 + durMs / 1000;
  const osc = audio.createOscillator();
  const gain = audio.createGain();
  osc.type = 'square';
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0, t0);
  gain.gain.linearRampToValueAtTime(volume * 0.3, t0 + 0.005);
  gain.gain.setValueAtTime(volume * 0.3, t1 - 0.02);
  gain.gain.linearRampToValueAtTime(0, t1);
  osc.connect(gain).connect(audio.destination);
  osc.start(t0);
  osc.stop(t1 + 0.01);
}

const sounds = {
  countdown: () => tone(880, 120),
  halfway: () => { tone(660, 100); tone(660, 100, 180); },
  intervalEnd: () => tone(1320, 500),
  finish: () => { tone(1320, 250); tone(1320, 250, 350); tone(1760, 700, 700); },
};

// ---------------------------------------------------------------- wake lock

// The screen stays on while a timer runs, if the user turned the setting on (off by default).
let keepAwakeSetting = false;
let timerRunning = false;
let wakeLock = null;
let requestingLock = false;

function wakeLockSupported() { return 'wakeLock' in navigator; }

function keepAwake(on) {
  timerRunning = on;
  syncWakeLock();
}

// Takes or releases the lock to match the state; call after anything that changes it.
async function syncWakeLock() {
  const want = timerRunning && keepAwakeSetting && document.visibilityState === 'visible';
  if (want && !wakeLock && !requestingLock && wakeLockSupported()) {
    requestingLock = true;
    try {
      const lock = await navigator.wakeLock.request('screen');
      lock.addEventListener('release', () => { if (wakeLock === lock) wakeLock = null; });
      wakeLock = lock;
    } catch (e) {
      return;  // refused (battery saver, not HTTPS): the timer still works
    } finally {
      requestingLock = false;
    }
    syncWakeLock();  // the timer may have been paused while we waited
  } else if (!want && wakeLock) {
    const lock = wakeLock;
    wakeLock = null;
    lock.release().catch(() => {});
  }
}

function initKeepAwakeSetting() {
  const box = $('keep-awake');
  try { keepAwakeSetting = localStorage.getItem(KEEP_AWAKE_KEY) === 'on'; } catch (e) { keepAwakeSetting = false; }
  box.checked = keepAwakeSetting;
  if (!wakeLockSupported()) {
    box.disabled = true;
    $('keep-awake-note').textContent = 'Not available here (needs HTTPS and a recent browser)';
  }
  box.addEventListener('change', () => {
    keepAwakeSetting = box.checked;
    try { localStorage.setItem(KEEP_AWAKE_KEY, keepAwakeSetting ? 'on' : 'off'); } catch (e) { /* private mode */ }
    syncWakeLock();
  });
}

// ---------------------------------------------------------------- timer engine

const run = {
  timer: null,
  idx: 0,
  remaining: 0,     // ms left in the current interval while paused
  endAt: 0,         // performance.now() at which the current interval ends while running
  running: false,
  done: false,
  lastSec: null,    // last whole second shown, to fire each countdown beep once
  halfwayDone: false,
  after: [],        // after[i] = total ms of the intervals after i
};

function current() { return run.timer.intervals[run.idx]; }

function remainingNow() {
  return run.running ? run.endAt - performance.now() : run.remaining;
}

function enterInterval(i) {
  run.idx = i;
  run.lastSec = null;
  run.halfwayDone = false;
}

function goTo(i) {
  run.done = false;
  enterInterval(i);
  run.remaining = current().duration;
  if (run.running) run.endAt = performance.now() + run.remaining;
  render();
}

function play() {
  unlockAudio();
  if (run.done) goTo(0);
  if (run.running) return;
  run.running = true;
  run.endAt = performance.now() + run.remaining;
  keepAwake(true);
  render();
}

function pause() {
  if (!run.running) return;
  run.remaining = Math.max(0, run.endAt - performance.now());
  run.running = false;
  keepAwake(false);
  render();
}

function togglePlay() { run.running ? pause() : play(); }

function next() {
  unlockAudio();
  if (run.idx + 1 < run.timer.intervals.length) goTo(run.idx + 1);
  else finish(false);
}

function prev() {
  unlockAudio();
  goTo(run.done ? run.timer.intervals.length - 1 : Math.max(0, run.idx - 1));
}

function reset() {
  run.running = false;
  keepAwake(false);
  goTo(0);
}

function finish(withSound) {
  run.running = false;
  run.done = true;
  run.remaining = 0;
  keepAwake(false);
  if (withSound) sounds.finish();
  render();
}

function tick() {
  if (!run.timer || !run.running) return;
  const now = performance.now();
  // Catch up on every interval that ended since the last tick (the tab may have been in the background).
  let crossed = false;
  while (run.endAt <= now) {
    if (run.idx + 1 >= run.timer.intervals.length) { finish(true); return; }
    enterInterval(run.idx + 1);
    run.endAt += current().duration;
    crossed = true;
  }
  if (crossed) sounds.intervalEnd();

  const it = current();
  const rem = run.endAt - now;
  const sec = Math.ceil(rem / 1000);
  if (sec !== run.lastSec) {
    // Skip the beep on the first tick of an interval: that second was announced by the interval-end tone.
    if (run.lastSec !== null && sec >= 1 && sec <= it.countdown) sounds.countdown();
    run.lastSec = sec;
  }
  const half = it.duration / 2;
  if (it.halfway && !run.halfwayDone && rem <= half) {
    run.halfwayDone = true;
    // Don't let it collide with the countdown beeps of a short interval.
    if (half > (it.countdown + 1) * 1000) sounds.halfway();
  }
  render();
}

function loop() {
  tick();
  requestAnimationFrame(loop);
}

// ---------------------------------------------------------------- rendering

const view = {};
const shown = {};  // last value written per element, to avoid touching the DOM 60 times a second

function set(key, value, apply) {
  if (shown[key] === value) return;
  shown[key] = value;
  apply(value);
}

function setText(el, text) { set(el.id, text, (v) => { el.textContent = v; }); }

// Sizes the countdown digits to fill the width (fonts differ per phone, so measure rather than guess),
// capped by the screen height. Digits are tabular, so this only changes with the format or the screen.
const TIME_FILL = 1;  // the digits' own side bearings leave a small margin
const RING_LENGTH = 2 * Math.PI * 46;  // circumference of the ring (r=46 in index.html)
const sideways = window.matchMedia('(orientation: landscape) and (max-height: 560px)');
function fitTime() {
  const el = view.time;
  if (!el.clientWidth) return;  // hidden
  el.style.fontSize = '100px';
  const range = document.createRange();
  range.selectNodeContents(el);
  const textWidth = range.getBoundingClientRect().width;
  if (!textWidth) return;
  const byWidth = (100 * el.clientWidth * TIME_FILL) / textWidth;
  const byHeight = window.innerHeight * (sideways.matches ? 0.38 : 0.28);
  el.style.fontSize = `${Math.floor(Math.min(byWidth, byHeight))}px`;
}

function render() {
  if (!run.timer) return;
  const ivs = run.timer.intervals;
  const it = current();
  const rem = run.done ? 0 : remainingNow();
  const left = run.done ? 0 : rem + run.after[run.idx];

  const time = fmt(rem);
  setText(view.time, time);
  set('time-format', time.length, fitTime);  // "mm:ss" ↔ "h:mm:ss"
  // The ring empties with the current interval; rounded so the DOM is only touched when it visibly moves.
  const emptied = run.done ? 0 : Math.round((1 - rem / it.duration) * 1000) / 1000;
  set('ring', emptied, (v) => { view.ringArc.style.strokeDashoffset = String(RING_LENGTH * v); });

  const nextIt = ivs[run.idx + 1];
  if (run.done) {
    setText(view.curName, 'Done');
    setText(view.curDur, '');
    setText(view.summary, `Total ${fmt(run.timer.total)}`);
    setText(view.curComment, '');
  } else {
    setText(view.curName, it.name);
    setText(view.curDur, `of ${fmt(it.duration)}`);
    setText(view.summary,
      `${it.round ? `Round ${it.round} · ` : ''}Set ${run.idx + 1}/${ivs.length} · ${fmt(left)} left`);
    setText(view.curComment, it.comment);
  }
  set('next-hidden', run.done, (v) => { view.nextCard.hidden = v; });
  setText(view.nextName, nextIt ? nextIt.name : 'Finish');
  setText(view.nextDur, nextIt ? fmt(nextIt.duration) : '');
  setText(view.nextComment, nextIt ? nextIt.comment : '');
  set('next-edge', nextIt ? nextIt.color : '', (c) => { view.nextCard.style.borderLeftColor = c; });

  const bg = run.done ? DONE_COLOR : it.color;
  set('bg', bg, (c) => {
    view.timer.style.backgroundColor = c;
    view.timer.style.setProperty('--fg', textOn(c));
    view.themeColor.setAttribute('content', c);
  });
  set('state', `${run.running}/${run.done}`, () => {
    view.timer.classList.toggle('running', run.running);
    view.timer.classList.toggle('done', run.done);
    view.play.setAttribute('aria-label', run.running ? 'Pause' : 'Play');
  });
}

// ---------------------------------------------------------------- screens

let entries = [];

// `ok` shows it as news (e.g. what was imported) rather than as an error.
function showMessage(text, ok = false) {
  view.message.textContent = text;
  view.message.classList.toggle('ok', ok);
}

function metaText(t) {
  return `${fmt(t.total)} · ${t.intervals.length} interval${t.intervals.length === 1 ? '' : 's'}`;
}

// The coloured timeline of a timer, each interval as wide as it is long.
function stripEl(intervals) {
  const strip = el('span', 'strip');
  for (const it of intervals) {
    const seg = el('span');
    seg.style.flexGrow = String(it.duration);
    seg.style.backgroundColor = it.color;
    strip.append(seg);
  }
  return strip;
}

function entryName(entry) { return entry.timer ? entry.timer.name : entry.name; }

function renderList() {
  view.list.replaceChildren();
  view.empty.hidden = entries.length > 0 || view.message.textContent !== '';
  entries.forEach((entry, i) => view.list.append(editMode ? manageCard(entry, i) : timerCard(entry, i)));
}

function timerCard(entry, i) {
  const li = document.createElement('li');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'timer-item';
  const name = document.createElement('span');
  name.className = 'name';
  btn.append(name);
  if (entry.error) {
    name.textContent = entry.name;
    const err = document.createElement('span');
    err.className = 'error';
    err.textContent = entry.error;
    btn.append(err);
    btn.disabled = true;
  } else {
    const t = entry.timer;
    name.textContent = t.name;
    if (t.description) {
      const desc = document.createElement('span');
      desc.className = 'description';
      desc.textContent = t.description;
      btn.append(desc);
    }
    btn.append(el('span', 'meta', metaText(t)), stripEl(t.intervals));
    btn.addEventListener('click', () => openTimer(i));
  }
  li.append(btn);
  if (entry.timer) {
    // A sibling of the card, not inside it: a button can't contain another button.
    const info = infoButton(`Details of ${entry.timer.name}`);
    info.classList.add('card-info');
    info.addEventListener('click', () => openDetails(entry.timer, null));
    li.append(info);
  }
  return li;
}

// In edit mode: reorder, edit, download and delete, also for timers with an error.
function manageCard(entry, i) {
  const name = entryName(entry);
  const li = el('li', 'manage-card');
  const move = el('div', 'manage-move');
  const up = iconButton('up', `Move ${name} up`, () => moveTimer(i, -1));
  const down = iconButton('down', `Move ${name} down`, () => moveTimer(i, 1));
  up.classList.add('move-up');
  down.classList.add('move-down');
  up.disabled = i === 0;
  down.disabled = i === entries.length - 1;
  move.append(up, down);
  const body = el('div', 'manage-body');
  body.append(el('span', 'name', name));
  if (entry.error) body.append(el('span', 'error', entry.error));
  else body.append(el('span', 'meta', metaText(entry.timer)), stripEl(entry.timer.intervals));
  const acts = el('div', 'manage-acts');
  const del = iconButton('trash', `Delete ${name}`, () => deleteTimer(i));
  del.classList.add('danger');
  acts.append(iconButton('edit', `Edit ${name}`, () => openTimerEditor(i)),
              iconButton('download', `Download ${name}`, () => downloadTimer(i)), del);
  li.append(move, body, acts);
  return li;
}

// ---------------------------------------------------------------- details

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function infoButton(label) {
  const btn = view.infoTemplate.content.firstElementChild.cloneNode(true);
  btn.setAttribute('aria-label', label);
  return btn;
}

function stepTotal(node) {
  return node.repeat ? node.repeat * node.items.reduce((sum, child) => sum + stepTotal(child), 0) : node.duration;
}

// The routine as written: steps, and repeat blocks with their steps indented under them.
function outlineList(items, currentStep) {
  const ul = el('ul', 'outline');
  for (const node of items) {
    const li = el('li');
    if (node.repeat) {
      li.className = 'outline-block';
      const head = el('div', 'outline-row outline-head');
      head.append(el('span', 'outline-name', `${node.repeat} ${node.repeat === 1 ? 'round' : 'rounds'}`),
                  el('span', 'outline-dur', fmt(stepTotal(node))));
      li.append(head, outlineList(node.items, currentStep));
    } else {
      li.className = 'outline-row';
      const dot = el('span', 'outline-dot');
      dot.style.backgroundColor = node.color;
      const text = el('span', 'outline-text');
      text.append(el('span', 'outline-name', node.name));
      if (node.comment) text.append(el('span', 'outline-comment', node.comment));
      li.append(dot, text, el('span', 'outline-dur', fmt(node.duration)));
      if (node === currentStep) {
        li.classList.add('is-current');
        li.setAttribute('aria-current', 'step');
      }
    }
    ul.append(li);
  }
  return ul;
}

// `currentStep` (from the timer screen) is highlighted; the timer keeps running underneath.
function openDetails(timer, currentStep) {
  if (view.details.open) return;
  view.detailsName.textContent = timer.name;
  view.detailsDesc.textContent = timer.description;
  view.detailsDesc.hidden = !timer.description;
  view.detailsMeta.textContent = metaText(timer);
  view.detailsOutline.replaceChildren(outlineList(timer.outline, currentStep));
  openDialog(view.details);
  const cur = view.detailsOutline.querySelector('.is-current');
  if (cur) cur.scrollIntoView({ block: 'center' });
}

// ---------------------------------------------------------------- timers file

// The timers file lives in this browser's localStorage once the user saves, uploads or restores it.
// Until then the bundled timers.json is used, so a redeploy with new defaults still reaches them.

let currentText = '';  // JSON text behind the list, what Edit and Download work on

function parseText(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new ConfigError(`Not valid JSON: ${e.message}`);
  }
  return parseConfig(data);
}

function writeStored(text) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ text, savedAt: new Date().toISOString() }));
  // Ask the browser not to evict our data under storage pressure (best effort, may be refused).
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
}

function readStored() {
  try {
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (legacy !== null) {
      const old = JSON.parse(legacy);
      if (localStorage.getItem(STORAGE_KEY) === null && isPlainObject(old) && typeof old.text === 'string') {
        writeStored(old.text);
      }
      localStorage.removeItem(LEGACY_KEY);
    }
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
    return isPlainObject(saved) && typeof saved.text === 'string' ? saved.text : null;
  } catch (e) {
    return null;  // storage blocked or corrupt: fall back to the default
  }
}

async function fetchDefault() {
  const res = await fetch(CONFIG_URL, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

// Shows `text` in the list; a broken file still becomes currentText so Edit can fix it.
function showTimers(text) {
  currentText = text;
  view.download.disabled = !text;
  try {
    entries = parseText(text);
    showMessage('');
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    entries = [];
    showMessage(e.message);
  }
  renderList();
  updateSettings();
}

// The timers file as data, for changing single timers: `list` is the array inside `data`, so writing
// `data` back keeps the file's shape. Null while the file is broken (only the JSON editor can fix that).
function timersFile() {
  if (!currentText.trim()) {
    const data = { timers: [] };  // the default couldn't be loaded: start an empty file
    return { data, list: data.timers };
  }
  let data;
  try {
    data = JSON.parse(currentText);
  } catch (e) {
    return null;
  }
  const list = Array.isArray(data) ? data
    : isPlainObject(data) && hasOwn(data, 'timers') && Array.isArray(data.timers) ? data.timers : null;
  return list && list.length <= LIMITS.timers ? { data, list } : null;
}

function writeTimersFile(file, report) {
  return saveTimers(`${JSON.stringify(file.data, null, 2)}\n`, report);
}

async function loadConfig() {
  let text = readStored();
  if (text === null) {
    try {
      text = await fetchDefault();
    } catch (e) {
      showTimers('');
      showMessage(`Couldn't load the default timers. Use Upload or Edit to add your own.`);
      return;
    }
  }
  showTimers(text);
}

// Stores and shows `text`; returns false (after telling `report` why) if the browser won't store it.
function saveTimers(text, report) {
  try {
    writeStored(text);
  } catch (e) {
    report(`Couldn't save in this browser (${e.name}). Download your timers to keep a copy.`);
    return false;
  }
  showTimers(text);
  return true;
}

// Restore from backup: replaces every timer with the file's.
async function onUpload() {
  const file = view.fileInput.files && view.fileInput.files[0];
  view.fileInput.value = '';
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) { settingsMessage('That file is larger than 1 MB.'); return; }
  const text = await file.text();
  try {
    parseText(text);
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    settingsMessage(`${file.name}: ${e.message}`);
    return;
  }
  if (!confirm(`Replace all your timers with ${file.name}?`)) return;
  if (saveTimers(text, settingsMessage)) leaveSettings();
}

// Import a timer: adds the valid timers of a file (one or many) after the existing ones.
async function onImport() {
  const file = view.importInput.files && view.importInput.files[0];
  view.importInput.value = '';
  if (!file) return;
  if (file.size > MAX_FILE_BYTES) { settingsMessage('That file is larger than 1 MB.'); return; }
  const text = await file.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    settingsMessage(`${file.name}: Not valid JSON: ${e.message}`);
    return;
  }
  const raws = Array.isArray(data) ? data : isPlainObject(data) && hasOwn(data, 'timers') ? data.timers : null;
  if (!Array.isArray(raws) || raws.length === 0) {
    settingsMessage(`${file.name}: The file must be a list of timers or {"timers": [...]}`);
    return;
  }
  const target = timersFile();
  if (!target) return;
  const taken = new Set(entries.map(entryName));
  const added = [];
  const skipped = [];
  raws.forEach((raw, i) => {
    let timer;
    try {
      timer = parseTimer(raw, i);
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      skipped.push(e.message);
      return;
    }
    const copy = JSON.parse(JSON.stringify(raw));
    copy.name = freeName(timer.name, taken);
    taken.add(copy.name);
    added.push(copy);
  });
  if (added.length === 0) {
    settingsMessage(`${file.name}: nothing to import.\n${skipped.join('\n')}`);
    return;
  }
  if (target.list.length + added.length > LIMITS.timers) {
    settingsMessage(`${file.name}: you can have at most ${LIMITS.timers} timers.`);
    return;
  }
  target.list.push(...added);
  if (!writeTimersFile(target, settingsMessage)) return;
  leaveSettings();
  showMessage(`Imported ${added.map((t) => `"${t.name}"`).join(', ')}.`
    + (skipped.length ? `\nSkipped, with errors: ${skipped.join('; ')}` : ''), true);
}

// `name`, or "name (2)", "name (3)"… if a timer already has it.
function freeName(name, taken) {
  for (let n = 2, candidate = name; ; n++) {
    if (!taken.has(candidate)) return candidate;
    const suffix = ` (${n})`;
    candidate = Array.from(name).slice(0, LIMITS.name - suffix.length).join('') + suffix;
  }
}

function offerDownload(filename, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// Back up all timers.
function download() {
  if (!currentText) return;
  offerDownload(`klepsydra-backup-${new Date().toISOString().slice(0, 10)}.json`, currentText);
}

// Forgets the user's timers, so the bundled default (and later updates to it) is used again.
async function restoreDefaultTimers() {
  if (!confirm('Replace all your timers with the default timers?')) return;
  let text;
  try {
    text = await fetchDefault();
  } catch (e) {
    settingsMessage(`Couldn't load the default timers (${e.message}).`);
    return;
  }
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (e) {
    settingsMessage(`Couldn't change this browser's storage (${e.name}).`);
    return;
  }
  showTimers(text);
  leaveSettings();
}

// Shows the deployed commit (version.txt is written by build.sh; absent when run from the sources).
async function showVersion() {
  try {
    const res = await fetch('version.txt', { cache: 'no-cache' });
    const commit = res.ok ? (await res.text()).trim() : '';
    if (!/^[0-9a-f]{7,40}$/.test(commit)) return;
    for (const link of document.querySelectorAll('.build-info .version')) {
      link.textContent = commit.slice(0, 7);
      link.href = `${REPO_URL}/commit/${commit}`;
      link.title = `Commit ${commit}`;
      link.hidden = false;
    }
  } catch (e) {
    // offline and never cached: nothing to show
  }
}

// ---------------------------------------------------------------- editor

let editorOriginal = '';

function editorDirty() { return view.editorText.value !== editorOriginal; }
function editorError(text) { view.editorError.textContent = text; }

// Opened from Settings, in its place: Save, Cancel and the back button all return to the list.
function openEditor() {
  editorOriginal = currentText;
  view.editorText.value = currentText;
  editorError('');
  openScreen('json', { replace: true });
}

function saveEditor() {
  const text = view.editorText.value;
  try {
    parseText(text);
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    editorError(e.message);
    return;
  }
  if (!saveTimers(text, editorError)) return;
  editorOriginal = text;
  history.back();
}

function formatEditor() {
  try {
    view.editorText.value = JSON.stringify(JSON.parse(view.editorText.value), null, 2);
    editorError('');
  } catch (e) {
    editorError(`Not valid JSON: ${e.message}`);
  }
}

async function restoreDefault() {
  if (!confirm('Replace the editor contents with the default timers? Nothing is saved until you press Save.')) return;
  try {
    view.editorText.value = await fetchDefault();
    editorError('');
  } catch (e) {
    editorError(`Couldn't load the default timers (${e.message}).`);
  }
}

function openTimer(i) {
  const entry = entries[i];
  if (!entry || !entry.timer) return;
  unlockAudio();
  run.timer = entry.timer;
  run.running = false;
  const ivs = run.timer.intervals;
  run.after = new Array(ivs.length);
  let acc = 0;
  for (let k = ivs.length - 1; k >= 0; k--) { run.after[k] = acc; acc += ivs[k].duration; }
  for (const k of Object.keys(shown)) delete shown[k];
  goTo(0);
  openScreen('timer');
  fitTime();  // it couldn't be measured while hidden
  view.play.focus();
}

// Leaves the timer screen; called by the close button (via history.back) and by the phone's back button.
function closeTimer() {
  pause();
  run.timer = null;
  view.themeColor.setAttribute('content', '#121212');
}

// ---------------------------------------------------------------- navigation

// Screens open over the list, innermost last. Each has one history entry, so the phone's back button,
// and every Cancel / ✕ / Back (they call history.back()), closes the top one.
const screens = [];
let editMode = false;  // the list shows the reorder / edit / download / delete buttons
let dialogInHistory = null;  // the open <dialog>; it has a history entry too

function topScreen() { return screens.length ? screens[screens.length - 1] : 'list'; }

function screenView(name) {
  const views = { list: view.listView, manage: view.listView, settings: view.settings, json: view.editor,
                  edit: view.timerEditor, timer: view.timer };
  return views[name];
}

// `replace` takes the place of the top screen (Settings → Edit timers): back then skips Settings.
function openScreen(name, { replace = false } = {}) {
  if (replace && screens.length) {
    screens[screens.length - 1] = name;
    history.replaceState({ screen: name }, '');
  } else {
    screens.push(name);
    history.pushState({ screen: name }, '');
  }
  showTop();
}

function showTop() {
  const top = topScreen();
  const current = screenView(top);
  for (const v of [view.listView, view.settings, view.editor, view.timerEditor, view.timer]) v.hidden = v !== current;
  view.openHelp.hidden = view.openSettings.hidden = top === 'manage';
  view.manageDone.hidden = top !== 'manage';
  if (editMode !== (top === 'manage')) {
    editMode = top === 'manage';
    renderList();
  }
}

// Called on popstate for the top screen; false keeps it open (the user kept their unsaved changes).
function leaveScreen(name) {
  if (name === 'json') return !editorDirty() || confirm('Discard your changes?');
  if (name === 'edit') return !teDirty() || confirm('Discard your changes?');
  if (name === 'timer') closeTimer();
  return true;
}

function onPopState() {
  if (dialogInHistory) {
    const d = dialogInHistory;
    dialogInHistory = null;
    if (d.open) d.close();
    return;
  }
  if (!screens.length) return;
  const top = topScreen();
  if (!leaveScreen(top)) {
    history.pushState({ screen: top }, '');
    return;
  }
  screens.pop();
  showTop();
}

function openDialog(d) {
  d.showModal();
  history.pushState({ dialog: d.id }, '');
  dialogInHistory = d;
}

// ---------------------------------------------------------------- icons

// Icons live in index.html's <template id="icons">, cloned by name.
function icon(name) {
  const svg = view.icons.content.querySelector(`svg[data-name="${name}"]`).cloneNode(true);
  svg.removeAttribute('data-name');
  svg.setAttribute('aria-hidden', 'true');
  return svg;
}

function iconButton(name, label, onClick) {
  const btn = el('button', 'icon-btn');
  btn.type = 'button';
  btn.setAttribute('aria-label', label);
  btn.append(icon(name));
  btn.addEventListener('click', onClick);
  return btn;
}

// ---------------------------------------------------------------- settings

const BROKEN_FILE = 'Your timers file has an error (see the list). Fix it with Edit all timers as JSON, '
  + 'or restore a backup or the default timers.';

function settingsMessage(text) { view.settingsMessage.textContent = text; }

function updateSettings() {
  const ok = timersFile() !== null;
  for (const btn of [view.manage, view.newTimer, view.import]) btn.disabled = !ok;
  settingsMessage(ok ? '' : BROKEN_FILE);
}

// After a change made from Settings, show the list with the result.
function leaveSettings() {
  if (topScreen() === 'settings') history.back();
}

function openSettings() {
  updateSettings();
  openScreen('settings');
}

// ---------------------------------------------------------------- edit mode

// Each change is saved at once; entries[i] is always the parsed form of the file's timer i.
function moveTimer(i, by) {
  const file = timersFile();
  const j = i + by;
  if (!file || j < 0 || j >= file.list.length) return;
  [file.list[i], file.list[j]] = [file.list[j], file.list[i]];
  if (!writeTimersFile(file, showMessage)) return;
  // Keep the keyboard on the card that moved.
  const card = view.list.children[j];
  const btn = card.querySelector(by < 0 ? '.move-up' : '.move-down');
  (btn.disabled ? card.querySelector(by < 0 ? '.move-down' : '.move-up') : btn).focus();
}

function deleteTimer(i) {
  const file = timersFile();
  if (!file || !confirm(`Delete "${entryName(entries[i])}"?`)) return;
  file.list.splice(i, 1);
  writeTimersFile(file, showMessage);
}

// A file with just this timer, which Import a timer (here or on another phone) adds back.
function downloadTimer(i) {
  const file = timersFile();
  if (!file) return;
  const slug = entryName(entries[i]).normalize('NFKD').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').slice(0, 40).replace(/^-+|-+$/g, '') || 'timer';
  offerDownload(`klepsydra-${slug}.json`, `${JSON.stringify({ timers: [file.list[i]] }, null, 2)}\n`);
}

// ---------------------------------------------------------------- timer editor

// Edits a copy of the timer as written in the file (so fields the form doesn't know about survive),
// in a form or as JSON. Only Save writes it back.
const te = { index: null, draft: null, original: '', tab: 'form' };
const sheet = { list: null, index: 0, opts: null };  // the interval or block open in the sheet

function teError(text) { view.teError.textContent = text; }

function textValue(v) { return typeof v === 'string' || typeof v === 'number' ? String(v) : ''; }

function cleanName(v) {
  return typeof v === 'string' && v.trim() ? Array.from(v.trim()).slice(0, LIMITS.name).join('') : '';
}

// The countdown / halfway an item gets, like parseOptions but ignoring invalid values.
function effectiveOpts(obj, inherited) {
  const o = { ...inherited };
  if (Number.isInteger(obj.countdown) && obj.countdown >= 0 && obj.countdown <= LIMITS.countdown) o.countdown = obj.countdown;
  if (typeof obj.halfway === 'boolean') o.halfway = obj.halfway;
  return o;
}

function durationMs(v) {
  try {
    return parseDuration(v, '');
  } catch (e) {
    return 0;
  }
}

// How a duration shows in the form: seconds under a minute, "m:ss" / "h:mm:ss" above.
function durationText(v) {
  if (typeof v === 'string') return v;
  if (typeof v !== 'number') return '';
  if (v < 60 || !Number.isInteger(v)) return String(v);
  const [h, m, sec] = [Math.floor(v / 3600), Math.floor((v % 3600) / 60), v % 60];
  const ss = String(sec).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

// What a typed duration is stored as; typos are kept as typed so the error says what's wrong.
function durationValue(text) {
  const t = text.trim();
  try {
    const sec = parseDuration(/^\d+(\.\d+)?$/.test(t) ? Number(t) : t, '') / 1000;
    return sec < 60 || !Number.isInteger(sec) ? sec : durationText(sec);
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    return t;
  }
}

function itemsTotal(list) {
  return list.reduce((sum, it) => sum + (hasOwn(it, 'repeat')
    ? (Number.isInteger(it.repeat) ? it.repeat : 0) * itemsTotal(Array.isArray(it.intervals) ? it.intervals : [])
    : durationMs(it.duration)), 0);
}

// The form can show any timer whose structure is right (the values may still be wrong).
function itemsFormable(list, depth) {
  return Array.isArray(list) && depth <= LIMITS.depth
    && list.every((it) => isPlainObject(it) && (!hasOwn(it, 'repeat') || itemsFormable(it.intervals, depth + 1)));
}
function formable(d) { return isPlainObject(d) && itemsFormable(d.intervals, 0); }

function choicesCountdown(inheritLabel) {
  const numbers = Array.from({ length: LIMITS.countdown + 1 }, (_, n) => [String(n), n === 0 ? 'Off' : `${n} s`]);
  return [['', inheritLabel], ...numbers];
}
function choicesHalfway(inheritLabel) { return [['', inheritLabel], ['true', 'On'], ['false', 'Off']]; }
function onOff(b) { return b ? 'on' : 'off'; }
function countdownLabel(n) { return n === 0 ? 'off' : `${n} s`; }

function fillSelect(select, choices, value) {
  select.replaceChildren(...choices.map(([v, label]) => {
    const option = el('option', null, label);
    option.value = v;
    return option;
  }));
  const current = value === undefined ? '' : String(value);
  select.value = choices.some(([v]) => v === current) ? current : '';
}

// '' (inherit) removes the key.
function setOption(obj, key, value) {
  if (value === '') delete obj[key];
  else obj[key] = key === 'halfway' ? value === 'true' : Number(value);
}

function openTimerEditor(index, { replace = false } = {}) {
  const file = timersFile();
  if (!file) return;
  te.index = index;
  te.draft = index === null
    ? { name: '', intervals: [{ name: 'Work', duration: 30 }, { name: 'Rest', duration: 10 }] }
    : JSON.parse(JSON.stringify(file.list[index]));
  te.original = JSON.stringify(te.draft, null, 2);
  view.teTitle.textContent = index === null ? 'New timer' : 'Edit timer';
  teError('');
  if (formable(te.draft)) {
    showForm();
  } else {
    showJson();
    teError(entries[index] && entries[index].error ? entries[index].error : '');
  }
  openScreen('edit', { replace });
  if (index === null) view.teName.focus();
}

function draftText() {
  if (te.tab === 'form') return JSON.stringify(te.draft, null, 2);
  try {
    return JSON.stringify(JSON.parse(view.teJson.value), null, 2);
  } catch (e) {
    return view.teJson.value;
  }
}
function teDirty() { return draftText() !== te.original; }

function setTab(tab) {
  te.tab = tab;
  view.teForm.hidden = tab !== 'form';
  view.teJson.hidden = tab !== 'json';
  view.teJsonTools.hidden = tab !== 'json';
  view.teTabForm.setAttribute('aria-pressed', String(tab === 'form'));
  view.teTabJson.setAttribute('aria-pressed', String(tab === 'json'));
}

function showForm() {
  setTab('form');
  const d = te.draft;
  view.teName.value = textValue(d.name);
  view.teDesc.value = textValue(d.description);
  view.tePrepare.value = textValue(d.prepare);
  fillSelect(view.teCountdown, choicesCountdown(`Default (${countdownLabel(DEFAULTS.countdown)})`), d.countdown);
  fillSelect(view.teHalfway, choicesHalfway(`Default (${onOff(DEFAULTS.halfway)})`), d.halfway);
  teChanged();
}

function showJson() {
  setTab('json');
  view.teJson.value = JSON.stringify(te.draft, null, 2);
}

function switchToForm() {
  if (te.tab === 'form') return;
  let d;
  try {
    d = JSON.parse(view.teJson.value);
  } catch (e) {
    teError(`Not valid JSON: ${e.message}`);
    return;
  }
  if (!formable(d)) {
    teError('The form needs a timer object with an "intervals" list of objects. Fix that here first.');
    return;
  }
  te.draft = d;
  teError('');
  showForm();
}

function switchToJson() {
  if (te.tab === 'json') return;
  teError('');
  showJson();
}

// Re-draws everything that depends on the draft, except the field being typed in.
function teChanged() {
  const d = te.draft;
  const opts = effectiveOpts(d, DEFAULTS);
  const prepare = Number.isInteger(d.prepare) ? d.prepare : 0;
  view.teOptionsSummary.textContent = `Options: get ready ${prepare} s · countdown ${countdownLabel(opts.countdown)}`
    + ` · halfway ${onOff(opts.halfway)}`;
  try {
    const t = parseTimer(d, te.index === null ? entries.length : te.index);
    view.teSummary.replaceChildren(el('span', 'meta', metaText(t)), stripEl(t.intervals));
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    view.teSummary.replaceChildren(el('span', 'error', e.message));
  }
  view.teItems.replaceChildren(itemsEl(d.intervals, 0, opts, { colorByName: new Map() }));
}

function itemsEl(list, depth, opts, ctx) {
  const box = el('div', 'te-items');
  list.forEach((item, i) => {
    box.append(hasOwn(item, 'repeat') ? blockEl(list, i, depth, opts, ctx) : rowEl(list, i, opts, ctx));
  });
  const adders = el('div', 'te-adders');
  const addInterval = el('button', 'btn add', '+ Interval');
  addInterval.type = 'button';
  addInterval.addEventListener('click', () => {
    list.push({ name: 'Work', duration: 30 });
    teChanged();
    openSheet(list, list.length - 1, opts);
  });
  adders.append(addInterval);
  if (depth < LIMITS.depth) {
    const addBlock = el('button', 'btn add', '+ Repeat block');
    addBlock.type = 'button';
    addBlock.addEventListener('click', () => {
      list.push({ repeat: 2, intervals: [{ name: 'Work', duration: 30 }, { name: 'Rest', duration: 10 }] });
      teChanged();
    });
    adders.append(addBlock);
  }
  box.append(adders);
  return box;
}

// Same colours as the timer will get: explicit ones, else the palette by name in order of appearance.
function itemColor(item, ctx) {
  if (hasOwn(item, 'color')) {
    try {
      return parseColor(item.color, '');
    } catch (e) {
      return 'transparent';
    }
  }
  const name = cleanName(item.name);
  if (!ctx.colorByName.has(name)) ctx.colorByName.set(name, PALETTE[ctx.colorByName.size % PALETTE.length]);
  return ctx.colorByName.get(name);
}

function rowEl(list, i, opts, ctx) {
  const item = list[i];
  const name = cleanName(item.name);
  const row = el('button', 'te-row');
  row.type = 'button';
  const dot = el('span', 'te-dot');
  dot.style.backgroundColor = itemColor(item, ctx);
  const text = el('span', 'te-text');
  text.append(el('span', 'te-name', name || 'No name'));
  if (typeof item.comment === 'string' && item.comment.trim()) text.append(el('span', 'te-comment', item.comment.trim()));
  const ms = durationMs(item.duration);
  row.append(dot, text, el('span', 'te-dur', ms ? fmt(ms) : '—'), icon('chevron'));
  row.addEventListener('click', () => openSheet(list, i, opts));
  return row;
}

function blockEl(list, i, depth, opts, ctx) {
  const item = list[i];
  const n = item.repeat;
  const valid = Number.isInteger(n) && n >= 1 && n <= LIMITS.repeat;
  const box = el('div', 'te-block');
  const head = el('div', 'te-block-head');
  head.append(el('span', 'te-block-label', `Repeat · ${fmt(itemsTotal([item]))}`));
  const stepper = el('div', 'stepper');
  const fewer = el('button', null, '−');
  const more = el('button', null, '+');
  fewer.type = more.type = 'button';
  fewer.setAttribute('aria-label', 'Fewer rounds');
  more.setAttribute('aria-label', 'More rounds');
  fewer.disabled = valid && n <= 1;
  more.disabled = valid && n >= LIMITS.repeat;
  const step = (by) => {
    item.repeat = valid ? n + by : 1;
    teChanged();
  };
  fewer.addEventListener('click', () => step(-1));
  more.addEventListener('click', () => step(1));
  stepper.append(fewer, el('span', 'te-count', valid ? `× ${n}` : '× ?'), more);
  head.append(stepper, iconButton('more', 'Repeat block options', () => openSheet(list, i, opts)));
  box.append(head, itemsEl(item.intervals, depth + 1, effectiveOpts(item, opts), ctx));
  return box;
}

// ---------------------------------------------------------------- interval / block sheet

function sheetItem() { return sheet.list[sheet.index]; }

function sheetProblem(item) {
  try {
    if (hasOwn(item, 'repeat')) {
      parseInt0(item.repeat, 1, LIMITS.repeat, 'Rounds', 'repeat');
    } else {
      parseName(item.name, 'Name');
      parseDuration(item.duration, 'Duration');
      if (hasOwn(item, 'color')) parseColor(item.color, 'Colour');
      if (hasOwn(item, 'comment')) parseShortText(item.comment, 'Cue', 'comment');
    }
    parseOptions(item, DEFAULTS, 'Options');
    return '';
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    return e.message;
  }
}

// Every edit in the sheet goes straight into the draft.
function sheetChanged() {
  teChanged();
  view.sheetError.textContent = sheetProblem(sheetItem());
  view.sheetUp.disabled = sheet.index === 0;
  view.sheetDown.disabled = sheet.index === sheet.list.length - 1;
}

function openSheet(list, index, opts) {
  Object.assign(sheet, { list, index, opts });
  const item = sheetItem();
  const block = hasOwn(item, 'repeat');
  view.sheetTitle.textContent = block ? 'Repeat block' : 'Interval';
  view.sheetInterval.hidden = block;
  view.sheetBlock.hidden = !block;
  const countdown = choicesCountdown(`Inherit (${countdownLabel(opts.countdown)})`);
  const halfway = choicesHalfway(`Inherit (${onOff(opts.halfway)})`);
  if (block) {
    view.blRepeat.value = textValue(item.repeat);
    fillSelect(view.blCountdown, countdown, item.countdown);
    fillSelect(view.blHalfway, halfway, item.halfway);
  } else {
    view.ivName.value = textValue(item.name);
    view.ivDuration.value = durationText(item.duration);
    view.ivComment.value = textValue(item.comment);
    fillSelect(view.ivCountdown, countdown, item.countdown);
    fillSelect(view.ivHalfway, halfway, item.halfway);
    renderPalette();
  }
  sheetChanged();
  openDialog(view.sheet);
}

function renderPalette() {
  const swatches = [['', 'Automatic'], ...PALETTE.map((c, i) => [c, PALETTE_NAMES[i]])];
  const buttons = swatches.map(([color, label]) => {
    const btn = el('button', color ? 'swatch' : 'swatch auto');
    btn.type = 'button';
    btn.setAttribute('role', 'radio');
    btn.setAttribute('aria-label', label);
    btn.dataset.color = color;
    if (color) btn.style.backgroundColor = color;
    btn.addEventListener('click', () => setColor(color));
    return btn;
  });
  const custom = el('label', 'swatch custom');
  const input = el('input');
  input.type = 'color';
  input.setAttribute('aria-label', 'Custom colour');
  input.addEventListener('input', () => setColor(input.value));
  custom.append(input);
  view.ivPalette.replaceChildren(...buttons, custom);
  markPalette();
}

function setColor(color) {
  const item = sheetItem();
  if (color) item.color = color;
  else delete item.color;
  markPalette();
  sheetChanged();
}

function markPalette() {
  const item = sheetItem();
  let current = '';
  if (hasOwn(item, 'color')) {
    try {
      current = parseColor(item.color, '');
    } catch (e) {
      current = null;  // invalid: nothing selected
    }
  }
  for (const btn of view.ivPalette.querySelectorAll('button')) {
    btn.setAttribute('aria-checked', String(btn.dataset.color === current));
  }
  const custom = view.ivPalette.querySelector('.custom');
  const isCustom = current !== null && current !== '' && !PALETTE.includes(current);
  custom.classList.toggle('checked', isCustom);
  custom.style.backgroundColor = isCustom ? current : '';
  if (current) custom.firstChild.value = current;
}

function moveSheetItem(by) {
  const { list, index } = sheet;
  const j = index + by;
  if (j < 0 || j >= list.length) return;
  [list[index], list[j]] = [list[j], list[index]];
  sheet.index = j;
  sheetChanged();
}

function duplicateSheetItem() {
  sheet.list.splice(sheet.index + 1, 0, JSON.parse(JSON.stringify(sheetItem())));
  teChanged();
  view.sheet.close();
}

function deleteSheetItem() {
  const item = sheetItem();
  if (hasOwn(item, 'repeat') && !confirm('Delete this repeat block and everything in it?')) return;
  sheet.list.splice(sheet.index, 1);
  teChanged();
  view.sheet.close();
}

function saveTimerEditor() {
  if (te.tab === 'json') {
    try {
      te.draft = JSON.parse(view.teJson.value);
    } catch (e) {
      teError(`Not valid JSON: ${e.message}`);
      return;
    }
  }
  const d = te.draft;
  if (isPlainObject(d) && !cleanName(d.name)) {
    teError('Give the timer a name.');
    if (te.tab === 'form') view.teName.focus();
    return;
  }
  try {
    parseTimer(d, te.index === null ? entries.length : te.index);
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    teError(e.message);
    return;
  }
  const file = timersFile();
  if (!file) {
    teError(BROKEN_FILE);
    return;
  }
  if (te.index === null) {
    if (file.list.length >= LIMITS.timers) {
      teError(`You can have at most ${LIMITS.timers} timers.`);
      return;
    }
    file.list.push(d);
  } else {
    file.list[te.index] = d;
  }
  if (!writeTimersFile(file, teError)) return;
  te.original = draftText();
  history.back();
}

function formatTimerJson() {
  try {
    view.teJson.value = JSON.stringify(JSON.parse(view.teJson.value), null, 2);
    teError('');
  } catch (e) {
    teError(`Not valid JSON: ${e.message}`);
  }
}

// ---------------------------------------------------------------- wiring

function init() {
  // GitHub Pages can't send frame-ancestors (ignored in a <meta> CSP), so refuse to run inside another
  // site's frame, where it could be overlaid to trick taps on Delete or Restore default.
  if (window.top !== window.self) {
    document.body.textContent = 'Klepsydra can\'t run inside a frame. Open it directly.';
    return;
  }
  Object.assign(view, {
    listView: $('list-view'), list: $('timer-list'), message: $('message'), download: $('download'),
    fileInput: $('file-input'), editor: $('editor-view'), editorText: $('editor-text'),
    editorError: $('editor-error'), timer: $('timer-view'), time: $('time'), ringArc: $('ring-arc'),
    summary: $('summary'), curName: $('cur-name'), curDur: $('cur-dur'), curComment: $('cur-comment'), nextCard: $('next-card'), nextName: $('next-name'),
    nextDur: $('next-dur'), nextComment: $('next-comment'),
    play: $('btn-play'), themeColor: document.querySelector('meta[name="theme-color"]'),
    infoTemplate: $('info-button'), details: $('details'), detailsName: $('details-name'),
    detailsDesc: $('details-desc'), detailsMeta: $('details-meta'), detailsOutline: $('details-outline'),
    icons: $('icons'), empty: $('empty'), openHelp: $('open-help'), openSettings: $('open-settings'), manageDone: $('manage-done'),
    settings: $('settings-view'), settingsMessage: $('settings-message'), manage: $('manage'),
    newTimer: $('new-timer'), import: $('import'), importInput: $('import-input'),
    timerEditor: $('timer-editor'), teTitle: $('te-title'), teError: $('te-error'), teForm: $('te-form'),
    teJson: $('te-json'), teJsonTools: $('te-json-tools'), teTabForm: $('te-tab-form'), teTabJson: $('te-tab-json'),
    teSummary: $('te-summary'), teName: $('te-name'), teDesc: $('te-desc'), tePrepare: $('te-prepare'),
    teCountdown: $('te-countdown'), teHalfway: $('te-halfway'), teOptionsSummary: $('te-options-summary'),
    teItems: $('te-items'),
    sheet: $('sheet'), sheetTitle: $('sheet-title'), sheetInterval: $('sheet-interval'), sheetBlock: $('sheet-block'),
    sheetError: $('sheet-error'), sheetUp: $('sheet-up'), sheetDown: $('sheet-down'),
    ivName: $('iv-name'), ivDuration: $('iv-duration'), ivComment: $('iv-comment'), ivCountdown: $('iv-countdown'),
    ivHalfway: $('iv-halfway'), ivPalette: $('iv-palette'),
    blRepeat: $('bl-repeat'), blCountdown: $('bl-countdown'), blHalfway: $('bl-halfway'),
  });
  for (const footer of document.querySelectorAll('footer.build-info')) {
    footer.append($('build-info').content.cloneNode(true));
  }
  for (const placeholder of document.querySelectorAll('svg[data-icon]')) {
    placeholder.replaceWith(icon(placeholder.dataset.icon));
  }

  // Settings
  view.openSettings.addEventListener('click', openSettings);
  $('settings-back').addEventListener('click', () => history.back());
  view.manage.addEventListener('click', () => openScreen('manage', { replace: true }));
  view.manageDone.addEventListener('click', () => history.back());
  view.newTimer.addEventListener('click', () => openTimerEditor(null, { replace: true }));
  view.import.addEventListener('click', () => view.importInput.click());
  view.importInput.addEventListener('change', onImport);
  view.download.addEventListener('click', download);
  $('restore').addEventListener('click', () => view.fileInput.click());
  view.fileInput.addEventListener('change', onUpload);
  $('edit').addEventListener('click', openEditor);
  $('reset-default').addEventListener('click', restoreDefaultTimers);

  // Whole-file JSON editor
  $('editor-cancel').addEventListener('click', () => history.back());
  $('editor-save').addEventListener('click', saveEditor);
  $('editor-format').addEventListener('click', formatEditor);
  $('editor-default').addEventListener('click', restoreDefault);

  // Timer editor
  $('te-cancel').addEventListener('click', () => history.back());
  $('te-save').addEventListener('click', saveTimerEditor);
  view.teTabForm.addEventListener('click', switchToForm);
  view.teTabJson.addEventListener('click', switchToJson);
  $('te-format').addEventListener('click', formatTimerJson);
  view.teName.addEventListener('input', () => { te.draft.name = view.teName.value; teChanged(); });
  view.teDesc.addEventListener('input', () => {
    if (view.teDesc.value.trim()) te.draft.description = view.teDesc.value;
    else delete te.draft.description;
    teChanged();
  });
  view.tePrepare.addEventListener('input', () => {
    if (view.tePrepare.value === '') delete te.draft.prepare;
    else te.draft.prepare = Number(view.tePrepare.value);
    teChanged();
  });
  view.teCountdown.addEventListener('change', () => { setOption(te.draft, 'countdown', view.teCountdown.value); teChanged(); });
  view.teHalfway.addEventListener('change', () => { setOption(te.draft, 'halfway', view.teHalfway.value); teChanged(); });

  // Interval / block sheet
  view.ivName.addEventListener('input', () => { sheetItem().name = view.ivName.value; sheetChanged(); });
  view.ivDuration.addEventListener('input', () => { sheetItem().duration = durationValue(view.ivDuration.value); sheetChanged(); });
  view.ivComment.addEventListener('input', () => {
    if (view.ivComment.value.trim()) sheetItem().comment = view.ivComment.value;
    else delete sheetItem().comment;
    sheetChanged();
  });
  view.blRepeat.addEventListener('input', () => {
    sheetItem().repeat = view.blRepeat.value === '' ? 0 : Number(view.blRepeat.value);
    sheetChanged();
  });
  for (const [select, key] of [[view.ivCountdown, 'countdown'], [view.ivHalfway, 'halfway'],
                               [view.blCountdown, 'countdown'], [view.blHalfway, 'halfway']]) {
    select.addEventListener('change', () => { setOption(sheetItem(), key, select.value); sheetChanged(); });
  }
  view.sheetUp.addEventListener('click', () => moveSheetItem(-1));
  view.sheetDown.addEventListener('click', () => moveSheetItem(1));
  $('sheet-duplicate').addEventListener('click', duplicateSheetItem);
  $('sheet-delete').addEventListener('click', deleteSheetItem);
  $('sheet-done').addEventListener('click', () => view.sheet.close());

  window.addEventListener('beforeunload', (e) => {
    const top = topScreen();
    if ((top === 'json' && editorDirty()) || (top === 'edit' && teDirty())) e.preventDefault();
  });
  view.time.addEventListener('click', togglePlay);
  view.play.addEventListener('click', togglePlay);
  $('btn-next').addEventListener('click', next);
  $('btn-prev').addEventListener('click', prev);
  $('btn-reset').addEventListener('click', reset);
  $('btn-close').addEventListener('click', () => history.back());
  $('btn-info').addEventListener('click', () => openDetails(run.timer, run.done ? null : current().step));
  $('details-close').addEventListener('click', () => view.details.close());
  for (const d of [view.details, view.sheet]) {
    d.addEventListener('click', (e) => { if (e.target === d) d.close(); });  // the backdrop
    // However the dialog closes (✕, Done, backdrop, Esc, back button), drop its history entry exactly once.
    d.addEventListener('close', () => {
      if (dialogInHistory === d) history.back();  // onPopState then only clears dialogInHistory
    });
  }
  window.addEventListener('popstate', onPopState);

  document.addEventListener('keydown', (e) => {
    if (!run.timer || view.details.open || e.altKey || e.ctrlKey || e.metaKey) return;
    const actions = { ' ': togglePlay, ArrowRight: next, ArrowLeft: prev, Escape: () => history.back() };
    if (!hasOwn(actions, e.key)) return;
    e.preventDefault();
    actions[e.key]();
  });

  window.addEventListener('resize', () => { if (run.timer) fitTime(); });  // rotation, split screen

  document.addEventListener('visibilitychange', () => {
    // Browsers drop the lock when the page is hidden; take it again on return.
    if (document.visibilityState === 'visible') syncWakeLock();
  });

  // rAF stops in background tabs; this keeps beeps and interval changes going (browsers slow it to ~1/s).
  setInterval(tick, 250);
  requestAnimationFrame(loop);
  initKeepAwakeSetting();
  loadConfig();
  showVersion();

  // Offline support and "install app"; only available on HTTPS or localhost.
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => {});
}

init();
