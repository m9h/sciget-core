// SPDX-License-Identifier: BSD-3-Clause
// Sciget web UI. Native ES module, no dependencies, works under CSP default-src 'self'.

import { api, ApiError, captureToken, getToken } from './api.js';

const CVMFS_MODES = [
  { value: 'auto', label: 'Automatic',
    text: 'Use CVMFS from this computer if it is set up, otherwise mount it inside the container.' },
  { value: 'host', label: 'From this computer',
    text: 'Use /cvmfs already mounted on this computer. Fastest, but the CVMFS client must be installed.' },
  { value: 'container', label: 'Inside the container',
    text: 'Mount CVMFS inside the container. Works without setup here, but the container needs extra permissions.' },
  { value: 'off', label: 'Off',
    text: 'Do not use CVMFS. Only the tools built into the image are available.' },
];
const CVMFS_LABEL = { host: 'from this computer', container: 'inside the container', off: 'off', auto: 'automatic' };

const STATE_LABEL = {
  pulling: 'Downloading', starting: 'Starting', ready: 'Ready', stopped: 'Stopped', error: 'Error',
};
const BUSY = new Set(['pulling', 'starting']);
const ACTIVE = new Set(['pulling', 'starting', 'ready']);
const ENGINE_LABEL = { podman: 'Podman', docker: 'Docker', apptainer: 'Apptainer' };

const UNAUTHORIZED_MSG =
  'This page has no valid access key. Go back to the terminal and open the address that `sciget serve` printed ' +
  '(it ends in #token=…). Reloading this tab will not help once the key is lost or the service restarted.';

const state = {
  domains: [],
  engines: [],
  settings: null,
  sessions: new Map(),
  startDomain: null,
  logsFor: null,
  view: 'home',
};

// ---------- small DOM helpers ----------

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function announce(msg) {
  const live = $('live');
  live.textContent = '';
  // A fresh text node makes screen readers re-read identical messages.
  window.setTimeout(() => { live.textContent = msg; }, 50);
}

function showBanner(msg) {
  $('banner-text').textContent = msg;
  $('banner').hidden = false;
}

function hideBanner() {
  $('banner').hidden = true;
}

/** Report an API failure. Returns the readable message. */
function handleError(err, context) {
  if (err instanceof ApiError && err.status === 401) {
    stopPolling();
    showBanner(UNAUTHORIZED_MSG);
    return UNAUTHORIZED_MSG;
  }
  const msg = err instanceof ApiError ? err.message : `Something went wrong: ${err}`;
  if (err instanceof ApiError && err.code === 'offline') showBanner(msg);
  if (context) console.warn(context, err);
  return msg;
}

function safeHttpUrl(raw) {
  try {
    const u = new URL(raw, location.href);
    return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
  } catch {
    return null;
  }
}

function domainTitle(name) {
  const d = state.domains.find((x) => x.name === name);
  return d ? d.title : name;
}

function formatTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso || '';
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

// ---------- views ----------

function setView(view, focus = true) {
  state.view = view;
  for (const tab of document.querySelectorAll('.tab')) {
    if (tab.dataset.view === view) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }
  for (const panel of document.querySelectorAll('.view')) {
    panel.hidden = panel.dataset.panel !== view;
  }
  try { sessionStorage.setItem('sciget.view', view); } catch { /* ignore */ }
  if (view === 'sessions') refreshSessions();
  if (view === 'settings') { renderSettings(); refreshEngines(); }
  if (focus) $('main').focus();
}

// ---------- home ----------

function favorites() {
  return new Set((state.settings && state.settings.favorites) || []);
}

function domainCard(d) {
  const fav = favorites().has(d.name);
  const titleId = `dom-${d.name}-${Math.random().toString(36).slice(2, 7)}`;
  return h('li', { class: 'card', 'aria-labelledby': titleId },
    h('div', { class: 'card-head' },
      h('h3', { id: titleId, text: d.title }),
      h('button', {
        type: 'button', class: 'icon-btn star', 'aria-pressed': fav ? 'true' : 'false',
        'aria-label': `Favourite ${d.title}`, title: fav ? 'Remove from favourites' : 'Add to favourites',
        onclick: () => toggleFavorite(d.name),
      }, fav ? '★' : '☆')),
    h('p', { class: 'card-desc', text: d.description || '' }),
    h('p', { class: 'card-meta' }, `Version ${d.default_version || 'latest'}`),
    h('div', { class: 'card-actions' },
      h('button', { type: 'button', class: 'btn primary', onclick: () => openStart(d),
        'aria-label': `Start ${d.title}` }, 'Start')));
}

function renderHome() {
  $('domains-loading').hidden = true;
  const list = $('domain-list');
  list.replaceChildren(...state.domains.map(domainCard));
  if (!state.domains.length) {
    list.replaceChildren(h('li', { class: 'empty', text: 'No domains are installed.' }));
  }

  const favs = favorites();
  const favDomains = state.domains.filter((d) => favs.has(d.name));
  $('fav-section').hidden = favDomains.length === 0;
  $('fav-list').replaceChildren(...favDomains.map(domainCard));

  const remotes = [];
  for (const d of state.domains) {
    for (const raw of d.remote_urls || []) {
      const href = safeHttpUrl(raw);
      if (href) remotes.push({ d, href });
    }
  }
  $('remote-section').hidden = remotes.length === 0;
  $('remote-list').replaceChildren(...remotes.map(({ d, href }) =>
    h('li', {},
      h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: 'remote-link' },
        h('span', { class: 'remote-title', text: d.title }),
        h('span', { class: 'remote-host', text: new URL(href).host }),
        h('span', { class: 'visually-hidden', text: ' (opens in a new tab)' })))));
}

async function toggleFavorite(name) {
  const favs = favorites();
  const adding = !favs.has(name);
  if (adding) favs.add(name); else favs.delete(name);
  try {
    state.settings = await api('PUT', '/settings', { favorites: [...favs] });
    renderHome();
    announce(`${domainTitle(name)} ${adding ? 'added to' : 'removed from'} favourites.`);
  } catch (err) {
    announce(`Could not update favourites. ${handleError(err)}`);
  }
}

// ---------- shared form bits ----------

function fillEngineSelect(select, selected, { includeAuto = true } = {}) {
  const opts = [];
  if (includeAuto) opts.push(h('option', { value: 'auto' }, 'Automatic'));
  for (const e of state.engines) {
    const label = ENGINE_LABEL[e.kind] || e.kind;
    const note = e.available ? (e.rootless ? 'rootless' : 'needs root') : 'not available';
    opts.push(h('option', { value: e.kind, disabled: !e.available }, `${label} (${note})`));
  }
  // Keep a saved choice visible even if the engine list did not report it.
  if (selected && selected !== 'auto' && !state.engines.some((e) => e.kind === selected)) {
    opts.push(h('option', { value: selected }, `${ENGINE_LABEL[selected] || selected} (not found)`));
  }
  select.replaceChildren(...opts);
  select.value = selected || 'auto';
}

function fillCvmfsRadios(container, name, selected) {
  container.replaceChildren(...CVMFS_MODES.map((m) => {
    const id = `${name}-${m.value}`;
    return h('div', { class: 'radio' },
      h('input', { type: 'radio', id, name, value: m.value, checked: m.value === (selected || 'auto'),
        'aria-describedby': `${id}-text` }),
      h('label', { for: id, text: m.label }),
      h('p', { id: `${id}-text`, class: 'hint', text: m.text }));
  }));
}

function radioValue(container) {
  const el = container.querySelector('input[type=radio]:checked');
  return el ? el.value : 'auto';
}

// ---------- start dialog ----------

function openStart(domain) {
  const s = state.settings || {};
  state.startDomain = domain;
  $('start-title').textContent = `Start ${domain.title}`;
  $('start-desc').textContent = domain.description || '';
  fillEngineSelect($('start-engine'), s.engine || 'auto');
  $('start-version').value = domain.default_version || '';
  $('start-data').value = s.storage_dir || '';
  fillCvmfsRadios($('start-cvmfs'), 'start-cvmfs', s.cvmfs || 'auto');
  $('start-gpu').checked = Boolean(s.gpu);
  $('start-error').hidden = true;
  $('start-submit').disabled = false;
  $('start-dialog').showModal();
  $('start-engine').focus();
}

async function submitStart(ev) {
  ev.preventDefault();
  const d = state.startDomain;
  if (!d) return;
  const s = state.settings || {};
  const body = { domain: d.name };
  const engine = $('start-engine').value;
  const cvmfs = radioValue($('start-cvmfs'));
  const version = $('start-version').value.trim();
  const dataDir = $('start-data').value.trim();
  const gpu = $('start-gpu').checked;
  // Fields that match Settings are left out, so the server applies its own defaults.
  if (engine !== (s.engine || 'auto')) body.engine = engine;
  if (cvmfs !== (s.cvmfs || 'auto')) body.cvmfs = cvmfs;
  if (version && version !== d.default_version) body.version = version;
  if (dataDir) body.data_dir = dataDir;
  if (gpu !== Boolean(s.gpu)) body.gpu = gpu;

  const errEl = $('start-error');
  errEl.hidden = true;
  $('start-submit').disabled = true;
  try {
    const session = await api('POST', '/sessions', body);
    upsertSession(session, { announceChange: false });
    $('start-dialog').close();
    announce(`Starting ${d.title}. This can take a few minutes the first time.`);
    setView('sessions');
  } catch (err) {
    errEl.textContent = handleError(err);
    errEl.hidden = false;
    $('start-submit').disabled = false;
  }
}

// ---------- sessions ----------

function upsertSession(s, { announceChange = true } = {}) {
  const prev = state.sessions.get(s.id);
  state.sessions.set(s.id, s);
  if (announceChange && prev && prev.state !== s.state) {
    const title = domainTitle(s.domain);
    const msgs = {
      pulling: `${title}: downloading the image.`,
      starting: `${title}: starting.`,
      ready: `${title} is ready. Choose Open to use it.`,
      stopped: `${title} has stopped.`,
      error: `${title} could not start. ${s.error || 'Open the logs to see why.'}`,
    };
    announce(msgs[s.state] || `${title}: ${s.state}.`);
  }
  renderSessions();
  ensurePolling();
}

function sessionCard(s) {
  const title = domainTitle(s.domain);
  const label = STATE_LABEL[s.state] || s.state;
  const href = s.state === 'ready' ? safeHttpUrl(s.url) : null;
  const actions = [];
  if (href) {
    actions.push(h('a', { class: 'btn primary', href, target: '_blank', rel: 'noopener noreferrer',
      'aria-label': `Open ${title} in a new tab` }, 'Open'));
  }
  if (ACTIVE.has(s.state)) {
    actions.push(h('button', { type: 'button', class: 'btn danger', onclick: () => stopSession(s),
      'aria-label': `Stop ${title}` }, 'Stop'));
  }
  actions.push(h('button', { type: 'button', class: 'btn', onclick: () => openLogs(s),
    'aria-label': `Show logs for ${title}` }, 'Logs'));

  const facts = [
    ['Version', s.version],
    ['Engine', ENGINE_LABEL[s.engine] || s.engine],
    ['Data folder', s.data_dir || 'none'],
    ['CVMFS', CVMFS_LABEL[s.cvmfs] || s.cvmfs],
    ['GPU', s.gpu ? 'on' : 'off'],
    ['Started', formatTime(s.created)],
  ];

  return h('li', { class: `session state-${s.state}` },
    h('div', { class: 'session-head' },
      h('h2', { class: 'session-title', text: title }),
      h('span', { class: `chip chip-${s.state}` },
        h('span', { class: 'chip-dot', 'aria-hidden': 'true' }), label)),
    BUSY.has(s.state)
      ? h('p', { class: 'muted' }, s.state === 'pulling'
        ? 'Downloading the image. The first start can take several minutes.'
        : 'Starting the desktop. This page updates when it is ready.')
      : null,
    s.state === 'error'
      ? h('p', { class: 'form-error' }, s.error || 'The session failed. Open the logs to see why, then start it again.')
      : null,
    h('dl', { class: 'facts' }, facts.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: v ?? '' })])),
    h('div', { class: 'card-actions' }, actions));
}

function renderSessions() {
  const all = [...state.sessions.values()].sort((a, b) => {
    const act = Number(ACTIVE.has(b.state)) - Number(ACTIVE.has(a.state));
    return act || String(b.created).localeCompare(String(a.created));
  });
  $('session-list').replaceChildren(...all.map(sessionCard));
  $('sessions-empty').hidden = all.length > 0;
  const active = all.filter((s) => ACTIVE.has(s.state)).length;
  const count = $('session-count');
  count.hidden = active === 0;
  count.textContent = `${active}`;
  count.title = `${active} running`;
}

async function refreshSessions() {
  try {
    const list = await api('GET', '/sessions');
    const seen = new Set();
    for (const s of list) { seen.add(s.id); upsertSession(s); }
    for (const id of [...state.sessions.keys()]) if (!seen.has(id)) state.sessions.delete(id);
    renderSessions();
    hideBannerIfOk();
  } catch (err) {
    handleError(err, 'sessions');
  }
}

async function stopSession(s) {
  const title = domainTitle(s.domain);
  const ok = window.confirm(`Stop ${title}? Programs running in it will close. Files in your data and storage folders are kept.`);
  if (!ok) return;
  try {
    const updated = await api('DELETE', `/sessions/${encodeURIComponent(s.id)}`);
    upsertSession(updated);
  } catch (err) {
    announce(`Could not stop ${title}. ${handleError(err)}`);
  }
}

// Poll about once a second, only while some session is pulling or starting.
let pollTimer = null;

function ensurePolling() {
  const busy = [...state.sessions.values()].some((s) => BUSY.has(s.state));
  if (busy && pollTimer === null) pollTimer = window.setTimeout(pollOnce, 1000);
}

function stopPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
}

async function pollOnce() {
  pollTimer = null;
  const busy = [...state.sessions.values()].filter((s) => BUSY.has(s.state));
  for (const s of busy) {
    try {
      upsertSession(await api('GET', `/sessions/${encodeURIComponent(s.id)}`));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        state.sessions.delete(s.id);
        renderSessions();
      } else {
        handleError(err, 'poll');
        if (err instanceof ApiError && (err.status === 401 || err.code === 'offline')) return;
      }
    }
  }
  ensurePolling();
}

// ---------- logs ----------

async function loadLogs() {
  const s = state.logsFor;
  if (!s) return;
  const body = $('logs-body');
  body.textContent = 'Loading…';
  try {
    const res = await api('GET', `/sessions/${encodeURIComponent(s.id)}/logs?tail=200`);
    const lines = (res && res.lines) || [];
    body.textContent = lines.length ? lines.join('\n') : 'No log output yet.';
    body.scrollTop = body.scrollHeight;
  } catch (err) {
    body.textContent = handleError(err);
  }
}

function openLogs(s) {
  state.logsFor = s;
  $('logs-title').textContent = `Logs: ${domainTitle(s.domain)}`;
  $('logs-dialog').showModal();
  $('logs-close').focus();
  loadLogs();
}

// ---------- settings ----------

function renderSettings() {
  const s = state.settings;
  if (!s) return;
  fillEngineSelect($('set-engine'), s.engine || 'auto');
  fillCvmfsRadios($('set-cvmfs'), 'set-cvmfs', s.cvmfs || 'auto');
  $('set-gpu').checked = Boolean(s.gpu);
  $('set-storage').value = s.storage_dir || '';
  $('settings-status').textContent = '';
}

function renderEngines() {
  const rows = state.engines.map((e) => h('tr', {},
    h('th', { scope: 'row', text: ENGINE_LABEL[e.kind] || e.kind }),
    h('td', {}, h('span', { class: `pill ${e.available ? 'ok' : 'no'}`, text: e.available ? 'Yes' : 'No' })),
    h('td', { text: e.available ? (e.rootless ? 'Yes' : 'No, needs root') : '—' }),
    h('td', { text: e.version || '—' })));
  if (!rows.length) {
    rows.push(h('tr', {}, h('td', { colspan: '4', text: 'No container engine found. Install Podman to run sessions.' })));
  }
  $('engine-rows').replaceChildren(...rows);
}

async function refreshEngines() {
  try {
    state.engines = await api('GET', '/engines');
    renderEngines();
    if (state.view === 'settings' && state.settings) {
      fillEngineSelect($('set-engine'), $('set-engine').value || state.settings.engine);
    }
  } catch (err) {
    handleError(err, 'engines');
  }
}

async function submitSettings(ev) {
  ev.preventDefault();
  const status = $('settings-status');
  const storage = $('set-storage').value.trim();
  if (!storage) {
    status.textContent = 'Type the full path of a storage folder.';
    $('set-storage').focus();
    return;
  }
  const body = {
    engine: $('set-engine').value,
    cvmfs: radioValue($('set-cvmfs')),
    gpu: $('set-gpu').checked,
    storage_dir: storage,
  };
  status.textContent = 'Saving…';
  try {
    state.settings = await api('PUT', '/settings', body);
    renderSettings();
    status.textContent = 'Saved. New sessions use these settings.';
  } catch (err) {
    status.textContent = `Not saved. ${handleError(err)}`;
  }
}

// ---------- boot ----------

function hideBannerIfOk() {
  if (getToken()) hideBanner();
}

function wire() {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => setView(tab.dataset.view));
  }
  $('refresh-sessions').addEventListener('click', refreshSessions);
  $('start-form').addEventListener('submit', submitStart);
  $('start-cancel').addEventListener('click', () => $('start-dialog').close());
  $('settings-form').addEventListener('submit', submitSettings);
  $('logs-refresh').addEventListener('click', loadLogs);
  $('logs-close').addEventListener('click', () => $('logs-dialog').close());
  $('logs-dialog').addEventListener('close', () => { state.logsFor = null; });
  window.addEventListener('focus', () => { if (state.view === 'sessions') refreshSessions(); });
}

async function boot() {
  captureToken();
  wire();
  if (!getToken()) {
    showBanner(UNAUTHORIZED_MSG);
    $('domains-loading').textContent = 'Not connected.';
    return;
  }
  try {
    const [health, domains, engines, settings, sessions] = await Promise.all([
      api('GET', '/health'),
      api('GET', '/domains'),
      api('GET', '/engines'),
      api('GET', '/settings'),
      api('GET', '/sessions'),
    ]);
    state.domains = domains || [];
    state.engines = engines || [];
    state.settings = settings;
    $('core-version').textContent = health && health.version ? `core ${health.version}` : '';
    for (const s of sessions || []) state.sessions.set(s.id, s);
    renderHome();
    renderSessions();
    renderEngines();
    ensurePolling();
    let view = 'home';
    try { view = sessionStorage.getItem('sciget.view') || 'home'; } catch { /* ignore */ }
    setView(['home', 'sessions', 'settings'].includes(view) ? view : 'home', false);
  } catch (err) {
    $('domains-loading').textContent = 'Not connected.';
    handleError(err, 'boot');
  }
}

boot();
