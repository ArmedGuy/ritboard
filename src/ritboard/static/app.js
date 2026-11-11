// Ritboard UI. No build step, no dependencies.
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const SVGNS = 'http://www.w3.org/2000/svg';

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}
function s(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  return el;
}

// Keys saved before the rename (Ritbord) carry over once.
try {
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k.startsWith('ritbord:') && localStorage.getItem('ritboard:' + k.slice(8)) == null) {
      localStorage.setItem('ritboard:' + k.slice(8), localStorage.getItem(k));
    }
  }
} catch { /* storage off */ }

const LS = {
  get(k, d) { try { const v = localStorage.getItem('ritboard:' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('ritboard:' + k, JSON.stringify(v)); } catch { /* storage off */ } },
};

// ---------------------------------------------------------------- api
async function req(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : res.statusText;
    toast(msg);
    throw new Error(msg);
  }
  return data;
}
const api = {
  get: (p) => req('GET', p),
  post: (p, b) => req('POST', p, b ?? {}),
  patch: (p, b) => req('PATCH', p, b),
  put: (p, b) => req('PUT', p, b),
  del: (p) => req('DELETE', p),
};
const W = () => `/api/w/${encodeURIComponent(S.ws)}`;
// Findings are files; their ids are paths inside the workspace, so they go in the query.
const FID = (id) => `${W()}/finding?id=${encodeURIComponent(id)}`;

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

// ---------------------------------------------------------------- state
const S = {
  workspaces: [],
  ws: null,
  data: null,
  boardId: null,
  selected: new Set(),   // finding ids selected on the board
  treeSel: null,
  expanded: new Set(LS.get('expanded', [])),
  sideTab: LS.get('sideTab', 'tree'),
  filter: '',
  view: { zoom: 1, x: 0, y: 0 },
  geom: new Map(),       // finding id -> {x,y,w,h,z} for the current board
  cardEls: new Map(),
  drag: null,
  pendingRefresh: false,
  editing: null,         // finding id, or 'new'
  space: false,
  disp: Object.assign({ device: true, grid: true, snap: true, theme: 'dark', ui: 'medium' }, LS.get('display', {})),
};

const by = {
  finding: new Map(),
  children: new Map(),
  missing: new Map(),    // cards whose file is gone
};
const TEXT_KINDS = ['md', 'text', 'csv', 'html', 'url'];

function board() { return S.data?.boards.find((b) => b.id === S.boardId) || null; }
function settings() { return S.data?.settings || { grid: 10, categories: {} }; }
function catColor(cat) { return (cat && settings().categories[cat]) || '#9aa0a6'; }
function dprFactor() { return S.disp.device ? 1 / (window.devicePixelRatio || 1) : 1; }
function eff() { return S.view.zoom * dprFactor(); }
function isOOB(g, b = board()) { return b && (g.x < 0 || g.y < 0 || g.x + g.w > b.width || g.y + g.h > b.height); }

function index() {
  by.finding.clear();
  by.children.clear();
  by.missing.clear();
  for (const f of S.data.findings) {
    by.finding.set(f.id, f);
    const k = f.parent_id || '';
    if (!by.children.has(k)) by.children.set(k, []);
    by.children.get(k).push(f);
  }
  S.geom.clear();
  for (const p of S.data.placements) {
    if (p.board_id !== S.boardId) continue;
    S.geom.set(p.finding_id, { x: p.x, y: p.y, w: p.w, h: p.h, z: p.z });
    if (p.missing) by.missing.set(p.finding_id, { id: p.finding_id, title: p.finding_id, kind: 'missing', tags: [], updated_at: '' });
  }
}
function findingOrGhost(id) { return by.finding.get(id) || by.missing.get(id); }

let lastProblems = '';
function showProblems() {
  const p = (S.data.problems || []).join(' ');
  if (p && p !== lastProblems) toast(p);
  lastProblems = p;
}

// ---------------------------------------------------------------- loading
async function loadWorkspaces() {
  S.workspaces = await api.get('/api/workspaces');
  renderWsSelect();
}

async function selectWorkspace(id) {
  S.ws = id;
  LS.set('ws', id);
  S.selected.clear(); S.treeSel = null;
  closeEditor();
  clearCards();
  sock.subscribe();
  await loadState();
}

let loading = null;
async function loadState() {
  if (!S.ws) return;
  if (S.drag) { S.pendingRefresh = true; return; }
  if (loading) { S.pendingRefresh = true; return; }
  loading = (async () => {
    try {
      const data = await api.get(`${W()}/state`);
      if (data.workspace.id !== S.ws) return;
      S.data = data;
      if (!S.data.boards.some((b) => b.id === S.boardId)) {
        const remembered = LS.get('board:' + S.ws);
        S.boardId = S.data.boards.some((b) => b.id === remembered) ? remembered : S.data.boards[0]?.id || null;
        loadView();
      }
      index();
      for (const id of [...S.selected]) if (!S.geom.has(id)) S.selected.delete(id);
      render();
      showProblems();
    } finally {
      loading = null;
      if (S.pendingRefresh && !S.drag) { S.pendingRefresh = false; loadState(); }
    }
  })();
  return loading;
}
let refreshTimer;
function scheduleRefresh() { clearTimeout(refreshTimer); refreshTimer = setTimeout(loadState, 40); }

function selectBoard(id) {
  saveView();
  S.boardId = id;
  LS.set('board:' + S.ws, id);
  S.selected.clear();
  clearCards();
  loadView();
  index();
  render();
}

let uiTimer;
function reportUi() {
  clearTimeout(uiTimer);
  uiTimer = setTimeout(() => api.post('/api/ui', { workspace: S.ws, board: S.boardId }).catch(() => {}), 200);
}

// ---------------------------------------------------------------- live updates
const sock = {
  ws: null, delay: 500,
  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    ws.onopen = () => { this.delay = 500; $('#conn').classList.add('on'); this.subscribe(); scheduleRefresh(); loadWorkspaces(); };
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.type === 'reload') location.reload();
      else if (m.type === 'workspaces') loadWorkspaces();
      else if (m.workspace === S.ws) scheduleRefresh();
    };
    ws.onclose = () => {
      $('#conn').classList.remove('on');
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 8000);
    };
  },
  subscribe() { if (this.ws?.readyState === 1 && S.ws) this.ws.send(JSON.stringify({ subscribe: S.ws })); },
};

// ---------------------------------------------------------------- view (pan/zoom)
function viewKey() { return `view:${S.ws}:${S.boardId}`; }
function loadView() { S.view = Object.assign({ zoom: 1, x: 0, y: 0 }, LS.get(viewKey(), {})); }
let viewSaveTimer;
function saveView() { clearTimeout(viewSaveTimer); if (S.ws && S.boardId) LS.set(viewKey(), S.view); }
// How far past each board edge you may pan, in screen pixels.
const PAN_MARGIN = 160;
function clampView() {
  const b = board(); if (!b) return;
  const vp = $('#viewport').getBoundingClientRect();
  const k = eff();
  const clamp = (v, span, size) => Math.min(Math.max(v, Math.min(0, span - size) - PAN_MARGIN), Math.max(0, span - size) + PAN_MARGIN);
  S.view.x = clamp(S.view.x, vp.width, b.width * k);
  S.view.y = clamp(S.view.y, vp.height, b.height * k);
}
function applyView() {
  clampView();
  const st = $('#stage');
  st.style.transform = `translate(${S.view.x}px, ${S.view.y}px) scale(${eff()})`;
  // Drop the fine mat lines when they would be closer than 8 screen pixels.
  $('#board').classList.toggle('coarse', eff() * matSpacing().minor < 8);
  clearTimeout(viewSaveTimer);
  viewSaveTimer = setTimeout(saveView, 300);
  updateFrames();
  renderStatus();
}
function setZoom(z, cx, cy) {
  z = Math.min(8, Math.max(0.03, z));
  const vp = $('#viewport').getBoundingClientRect();
  cx ??= vp.width / 2; cy ??= vp.height / 2;
  const before = eff();
  const bx = (cx - S.view.x) / before, by_ = (cy - S.view.y) / before;
  S.view.zoom = z;
  const after = eff();
  S.view.x = cx - bx * after;
  S.view.y = cy - by_ * after;
  applyView();
}
// The part of the window not covered by the bars.
function openArea() {
  const vp = $('#viewport').getBoundingClientRect();
  const body = document.body.classList;
  const top = body.contains('chrome-off') ? 0 : $('#topbar').getBoundingClientRect().height;
  const left = body.contains('chrome-off') || body.contains('side-off') ? 0 : $('#sidebar').getBoundingClientRect().width;
  return { left, top, width: vp.width - left, height: vp.height - top };
}
function fitView() {
  const b = board(); if (!b) return;
  const a = openArea();
  const k = Math.min(a.width / b.width, a.height / b.height) * 0.98;
  S.view.zoom = k / dprFactor();
  S.view.x = a.left + (a.width - b.width * k) / 2;
  S.view.y = a.top + (a.height - b.height * k) / 2;
  applyView();
}
function actualSize() { S.view = { zoom: 1, x: 0, y: 0 }; applyView(); }
function toBoard(clientX, clientY) {
  const vp = $('#viewport').getBoundingClientRect();
  const k = eff();
  return { x: (clientX - vp.left - S.view.x) / k, y: (clientY - vp.top - S.view.y) / k };
}
function snap(v, ev) {
  const g = settings().grid || 1;
  if (!S.disp.snap || ev?.altKey || g <= 1) return Math.round(v);
  return Math.round(v / g) * g;
}

// ---------------------------------------------------------------- render
function render() {
  renderTabs();
  renderBoard();
  renderSidebar();
  renderStatus();
  if (S.editing && S.editing !== 'new' && !by.finding.has(S.editing)) closeEditor();
  else if (S.editing && S.editing !== 'new') refreshEditorPlaceButton();
  reportUi();
}

function renderWsSelect() {
  const sel = $('#ws-select');
  sel.replaceChildren(
    ...S.workspaces.map((w) => h('option', { value: w.id, selected: w.id === S.ws, disabled: !!w.missing, title: w.path },
      w.missing ? `${w.name} (folder missing)` : w.name)),
    h('option', { value: '__new' }, '＋ New workspace…'),
    h('option', { value: '__open' }, '📂 Open folder…'),
  );
  sel.value = S.ws || '';
}

function renderTabs() {
  const tabs = $('#tabs');
  tabs.replaceChildren(...(S.data?.boards || []).map((b) =>
    h('button', {
      class: b.id === S.boardId ? 'on' : '',
      title: 'Double-click to rename',
      onclick: () => b.id !== S.boardId && selectBoard(b.id),
      ondblclick: async () => {
        const name = prompt('Board name', b.name);
        if (name && name !== b.name) await api.patch(`${W()}/boards/${encodeURIComponent(b.id)}`, { name });
      },
    }, b.name, h('small', {}, `${b.width}×${b.height}`))));
}

function renderBoard() {
  const b = board();
  const el = $('#board');
  if (!b) { el.style.width = el.style.height = '0px'; clearCards(); return; }
  el.style.width = b.width + 'px';
  el.style.height = b.height + 'px';
  el.style.backgroundColor = b.background || '';
  const { minor, major } = matSpacing();
  el.classList.toggle('grid', !!S.disp.grid);
  el.style.setProperty('--minor', minor + 'px');
  el.style.setProperty('--major', major + 'px');
  buildRuler(b, major);
  const ov = $('#overlay');
  ov.setAttribute('width', b.width); ov.setAttribute('height', b.height);

  const seen = new Set();
  for (const [fid, g2] of S.geom) {
    const f = findingOrGhost(fid);
    if (!f) continue;
    seen.add(fid);
    let card = S.cardEls.get(fid);
    if (!card) { card = makeCard(fid); S.cardEls.set(fid, card); $('#cards').append(card); }
    updateCard(card, f, g2);
  }
  for (const [fid, card] of S.cardEls) if (!seen.has(fid)) { card.remove(); S.cardEls.delete(fid); }
  applyView();
}

// Cutting-mat lines: minor every 20 px (or the snap grid if coarser), major every 5 minor.
function matSpacing() {
  const g = settings().grid || 10;
  const minor = g >= 20 ? g : 20;
  return { minor, major: minor * 5 };
}

let rulerKey = '';
function buildRuler(b, major) {
  const key = `${b.width}x${b.height}/${major}`;
  if (key === rulerKey) return;
  rulerKey = key;
  const spans = [];
  for (let x = major; x < b.width; x += major) spans.push(h('span', { style: { left: x + 3 + 'px', top: '3px' } }, String(x)));
  for (let y = major; y < b.height; y += major) spans.push(h('span', { style: { left: '3px', top: y + 3 + 'px' } }, String(y)));
  $('#ruler').replaceChildren(...spans);
}

function clearCards() { for (const c of S.cardEls.values()) c.remove(); S.cardEls.clear(); }

function makeCard(fid) {
  return h('div', { class: 'card', 'data-id': fid },
    h('div', { class: 'card-head' }, h('span', { class: 'title' }), h('span', { class: 'src' }), h('span', { class: 'kind' }),
      h('a', { class: 'open', target: '_blank', rel: 'noopener noreferrer', title: 'Open in a new window', hidden: true }, '↗')),
    h('div', { class: 'card-body' }),
    h('div', { class: 'resize-handle' }));
}

function fmtCount(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
function artifactLabel(f) {
  const label = `artifact · ${fmtCount(f.files || 0, 'file')}`;
  return f.pending ? `${label} · ${f.pending} on the way` : label;
}

// claude.ai links are refused when posted now; cards made before that show a note instead of a blank frame.
function refusesFraming(url) {
  try {
    const u = new URL(url);
    return /(^|\.)claude\.(ai|site)$/i.test(u.hostname);
  } catch { return false; }
}

const KIND_LABEL = { md: 'md', text: 'txt', csv: 'table', html: 'html', url: 'web', image: 'img', folder: 'folder', file: 'file', missing: 'missing' };

function updateCard(card, f, g) {
  if (!S.drag || !S.drag.ids?.includes(f.id)) {
    Object.assign(card.style, { left: g.x + 'px', top: g.y + 'px', width: g.w + 'px', height: g.h + 'px', zIndex: 1 + g.z });
  }
  card.style.setProperty('--cat', catColor(f.category));
  card.classList.toggle('sel', S.selected.has(f.id));
  card.classList.toggle('oob', !!isOOB(g));
  card.classList.toggle('missing', f.kind === 'missing');
  $('.title', card).textContent = f.title;
  $('.kind', card).textContent = f.kind === 'artifact' ? artifactLabel(f) : (KIND_LABEL[f.kind] || f.kind);
  $('.src', card).textContent = f.source || '';
  const open = $('a.open', card);
  const href = f.kind === 'artifact' ? f.url : (f.kind === 'image' || f.kind === 'file') ? f.src
    : f.kind === 'url' && /^https?:/i.test(f.content || '') ? f.content : null;
  open.hidden = !href;
  if (href) open.href = href;
  card.title = [f.category && `Category: ${f.category}`, f.tags?.length && `Tags: ${f.tags.join(', ')}`].filter(Boolean).join('\n');
  const ver = f.kind === 'folder'
    ? f.updated_at + '|' + (by.children.get(f.id) || []).map((c) => c.id + c.title).join(',')
    : f.updated_at + f.kind + (f.kind === 'artifact' ? '|' + f.stamp : '') + (f.src ? '|' + f.src : '');
  if (card.dataset.ver !== ver) {
    card.dataset.ver = ver;
    fillBody($('.card-body', card), f);
  }
}

function fillBody(body, f) {
  body.className = 'card-body';
  body.replaceChildren();
  delete body.dataset.loaded;
  delete body.dataset.noframe;
  if (f.kind === 'md' || f.kind === 'text' || f.kind === 'csv') {
    body.classList.add('prose');
    body.innerHTML = f.rendered || '';
  } else if (f.kind === 'image') {
    body.classList.add('framed');
    body.append(h('img', { src: f.src || f.content, alt: f.title, draggable: false }));
  } else if (f.kind === 'file') {
    body.classList.add('framed');
    body.dataset.noframe = '1';
    body.append(h('div', { class: 'ph noframe' }, h('div', {}, f.id), h('small', {}, fmtBytes(f.size || 0)),
      h('a', { href: f.src, target: '_blank', rel: 'noopener noreferrer' }, 'Open ↗')));
  } else if (f.kind === 'missing') {
    body.classList.add('framed');
    body.dataset.noframe = '1';
    body.append(h('div', { class: 'ph noframe' }, h('div', {}, 'This file is gone: moved or deleted outside Ritboard.'),
      h('small', {}, 'Select the card and press Delete to take it off the board, or put the file back.')));
  } else if (f.kind === 'folder') {
    const kids = by.children.get(f.id) || [];
    body.append(kids.length
      ? h('ul', { class: 'folder-list' }, kids.map((k) => h('li', {}, k.title)))
      : h('div', { class: 'ph' }, 'Empty folder'));
  } else if (f.kind === 'url' && refusesFraming(f.content)) {
    body.classList.add('framed');
    body.dataset.noframe = '1';
    body.append(h('div', { class: 'ph noframe' },
      h('div', {}, 'This is a claude.ai link, which can’t be shown inside Ritboard.'),
      h('a', { href: f.content, target: '_blank', rel: 'noopener noreferrer' }, 'Open in a new window ↗'),
      h('small', {}, 'Ask the agent that posted it to publish the HTML as a Ritboard artifact.')));
  } else {
    body.classList.add('framed');
    body.append(h('div', { class: 'ph' }, f.kind === 'url' ? (f.content || 'No URL') : f.title));
  }
  updateFrames();
}

function wrapHtml(content) {
  if (/<html|<body|<!doctype/i.test(content)) return content;
  const css = getComputedStyle(document.documentElement);
  const v = (n) => css.getPropertyValue(n).trim();
  return `<!doctype html><meta charset="utf-8"><style>:root{color-scheme:${S.disp.theme === 'light' ? 'light' : 'dark'}}`
    + `body{font:14px/1.5 system-ui,sans-serif;margin:12px;color:${v('--ink')};background:${v('--card')}}`
    + `svg text:not([fill]){fill:currentColor}a{color:${v('--a')}}table{border-collapse:collapse}td,th{border:1px solid ${v('--rule')};padding:4px 8px}</style>` + content;
}

// Live iframes only when the card is on screen and big enough to read.
let framesQueued = false;
function updateFrames() {
  if (framesQueued) return;
  framesQueued = true;
  requestAnimationFrame(() => {
    framesQueued = false;
    const k = eff();
    const vp = $('#viewport').getBoundingClientRect();
    const v0 = toBoard(vp.left, vp.top), v1 = toBoard(vp.right, vp.bottom);
    for (const [fid, card] of S.cardEls) {
      const f = by.finding.get(fid);
      if (!f || (f.kind !== 'html' && f.kind !== 'url' && f.kind !== 'artifact')) continue;
      const g = S.geom.get(fid);
      if (!g) continue;
      const body = $('.card-body', card);
      if (body.dataset.noframe) continue;
      const visible = g.x < v1.x && g.x + g.w > v0.x && g.y < v1.y && g.y + g.h > v0.y;
      const readable = k * Math.min(g.w, g.h) > 80;
      if (visible && readable && !body.dataset.loaded) {
        body.dataset.loaded = '1';
        const frame = f.kind === 'html'
          ? h('iframe', { sandbox: 'allow-scripts allow-popups allow-forms allow-modals', srcdoc: wrapHtml(f.content || ''), loading: 'lazy' })
          : f.kind === 'artifact'
          ? h('iframe', {
            src: f.url, loading: 'lazy', title: f.title,
            sandbox: 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads allow-presentation',
            allow: 'clipboard-write; fullscreen',
          })
          : h('iframe', {
            class: 'web', src: f.content, loading: 'lazy', referrerpolicy: 'no-referrer',
            sandbox: 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-presentation',
            allow: 'clipboard-write; fullscreen',
          });
        body.replaceChildren(frame);
      } else if (!readable && body.dataset.loaded) {
        delete body.dataset.loaded;
        body.replaceChildren(h('div', { class: 'ph' }, f.title));
      }
    }
  });
}

// ---------------------------------------------------------------- sidebar
function renderSidebar() {
  $$('.side-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.sideTab));
  const body = $('#side-body');
  const scroll = body.scrollTop;
  body.replaceChildren();
  if (!S.data) return;
  const q = S.filter.trim().toLowerCase();
  const match = (f) => !q || f.title.toLowerCase().includes(q) || (f.category || '').toLowerCase().includes(q) || f.tags.some((t) => t.toLowerCase().includes(q));

  if (S.sideTab === 'tree') {
    if (q) {
      const hits = S.data.findings.filter(match);
      body.append(...hits.map((f) => node(f, 0, false)));
      if (!hits.length) body.append(h('div', { class: 'empty' }, 'Nothing matches.'));
    } else {
      const walk = (pid, depth) => {
        for (const f of by.children.get(pid) || []) {
          body.append(node(f, depth, true));
          if (S.expanded.has(f.id)) walk(f.id, depth + 1);
        }
      };
      walk('', 0);
      if (!S.data.findings.length) body.append(h('div', { class: 'empty' }, 'No findings yet. Agents add them over MCP, or use ＋ Finding.'));
    }
  } else if (S.sideTab === 'cats') {
    const groups = new Map();
    for (const f of S.data.findings) {
      if (f.kind === 'folder' || !match(f)) continue;
      const c = f.category || '';
      if (!groups.has(c)) groups.set(c, []);
      groups.get(c).push(f);
    }
    const names = [...groups.keys()].sort((a, b) => (a === '') - (b === '') || a.localeCompare(b));
    for (const c of names) {
      body.append(h('div', { class: 'group-h' }, h('span', { class: 'cd', style: { background: catColor(c) } }), c || 'Uncategorized', h('span', {}, `(${groups.get(c).length})`)));
      body.append(...groups.get(c).map((f) => node(f, 0, false)));
    }
    if (!names.length) body.append(h('div', { class: 'empty' }, 'No findings.'));
  } else if (S.sideTab === 'arts') {
    renderArtifactsTab(body, q);
  } else {
    const b = board();
    const rows = [...S.geom.entries()].map(([id, g]) => ({ f: findingOrGhost(id), g })).filter((r) => r.f && match(r.f));
    const out = rows.filter((r) => isOOB(r.g));
    if (out.length) {
      body.append(h('div', { class: 'group-h' }, h('span', { class: 'bad' }, `Outside the board (${out.length})`),
        h('button', { class: 'mini', onclick: () => pullIn(out.map((r) => r.f.id)) }, 'Pull all in')));
      body.append(...out.map((r) => node(r.f, 0, false, r.g, true)));
    }
    rows.sort((a, b) => a.g.y - b.g.y || a.g.x - b.g.x);
    body.append(h('div', { class: 'group-h' }, `On ${b?.name || 'board'} (${rows.length})`));
    body.append(...rows.filter((r) => !isOOB(r.g)).map((r) => node(r.f, 0, false, r.g)));
  }
  body.scrollTop = scroll;
}

const KIND_ICON = { md: 'MD', text: 'TXT', csv: 'TBL', html: 'HTM', artifact: 'APP', url: 'WEB', image: 'IMG', folder: 'DIR', file: 'FILE', missing: '??' };

function node(f, depth, tree, g, oob) {
  const kids = by.children.get(f.id) || [];
  const placed = S.geom.has(f.id);
  const el = h('div', {
    class: 'node' + (S.treeSel === f.id ? ' sel' : ''), 'data-id': f.id, draggable: 'true',
    style: { paddingLeft: 6 + depth * 14 + 'px' },
    title: [f.title, f.id !== f.title && f.id, f.category && `Category: ${f.category}`, f.source && `From: ${f.source}`].filter(Boolean).join('\n'),
    onclick: () => treeClick(f.id),
    ondblclick: () => { if (f.kind !== 'missing') openEditor(f.id); },
    ondragstart: (e) => {
      e.dataTransfer.setData('application/x-ritboard-finding', f.id);
      e.dataTransfer.setData('text/plain', f.title);
      e.dataTransfer.effectAllowed = 'copyMove';
    },
  },
  h('span', {
    class: 'tw',
    onclick: (e) => {
      e.stopPropagation();
      if (!kids.length) return;
      S.expanded.has(f.id) ? S.expanded.delete(f.id) : S.expanded.add(f.id);
      LS.set('expanded', [...S.expanded]);
      renderSidebar();
    },
  }, tree && kids.length ? (S.expanded.has(f.id) ? '▾' : '▸') : ''),
  h('span', { class: 'ki' }, KIND_ICON[f.kind] || f.kind),
  h('span', { class: 'tt' }, f.title),
  g ? h('span', { class: 'xy' + (oob ? ' bad' : '') }, `${g.x},${g.y}`) : null,
  oob ? h('button', { class: 'mini', onclick: (e) => { e.stopPropagation(); pullIn([f.id]); } }, 'Pull in') : null,
  f.category ? h('span', { class: 'cd', style: { background: catColor(f.category) } }) : null,
  !g ? h('span', { class: 'ob', title: placed ? 'On this board' : '' }, placed ? '✓' : '') : null);

  if (tree && f.kind === 'folder') {
    el.addEventListener('dragover', (e) => {
      if (!e.dataTransfer.types.includes('application/x-ritboard-finding')) return;
      e.preventDefault(); e.stopPropagation();
      el.classList.add('drop');
    });
    el.addEventListener('dragleave', () => el.classList.remove('drop'));
    el.addEventListener('drop', async (e) => {
      e.preventDefault(); e.stopPropagation();
      el.classList.remove('drop');
      const id = e.dataTransfer.getData('application/x-ritboard-finding');
      if (!id || id === f.id) return;
      S.expanded.add(f.id); LS.set('expanded', [...S.expanded]);
      await api.patch(FID(id), { parent_id: f.id });
    });
  }
  return el;
}

function treeClick(id) {
  S.treeSel = id;
  if (S.geom.has(id)) {
    S.selected = new Set([id]);
    const card = S.cardEls.get(id);
    if (card) { card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash'); }
  }
  renderBoardSelection();
  renderSidebar();
  renderStatus();
}

async function pullIn(ids) {
  const b = board();
  const items = ids.map((id) => {
    const g = S.geom.get(id);
    const w = Math.min(g.w, b.width), hgt = Math.min(g.h, b.height);
    return { finding_id: id, x: Math.max(0, Math.min(g.x, b.width - w)), y: Math.max(0, Math.min(g.y, b.height - hgt)), w, h: hgt };
  });
  await api.put(`${W()}/boards/${encodeURIComponent(S.boardId)}/placements`, items);
}

function renderBoardSelection() {
  for (const [id, card] of S.cardEls) card.classList.toggle('sel', S.selected.has(id));
}

function renderStatus() {
  const st = $('#status');
  const parts = [`${Math.round(S.view.zoom * 100)}%`];
  if (S.disp.device && window.devicePixelRatio !== 1) parts.push(`device px (dpr ${window.devicePixelRatio})`);
  const b = board();
  if (b) parts.push(`${b.width}×${b.height}`);
  if (S.selected.size === 1) {
    const g = S.geom.get([...S.selected][0]);
    if (g) parts.push(`x ${g.x}  y ${g.y}  w ${g.w}  h ${g.h}`);
  } else if (S.selected.size > 1) parts.push(`${S.selected.size} selected`);
  st.textContent = parts.join('  ·  ');
}

// ---------------------------------------------------------------- board interaction
function cardAt(clientX, clientY) {
  const el = document.elementFromPoint(clientX, clientY);
  return el?.closest?.('.card');
}

function setSelected(ids) { S.selected = new Set(ids); renderBoardSelection(); renderStatus(); }

function bringToFront(ids) {
  let z = Math.max(0, ...[...S.geom.values()].map((g) => g.z));
  for (const id of ids) { const g = S.geom.get(id); if (g) g.z = ++z; }
}

async function savePlacements(ids) {
  const items = ids.filter((id) => S.geom.has(id)).map((id) => ({ finding_id: id, ...S.geom.get(id) }));
  if (items.length) await api.put(`${W()}/boards/${encodeURIComponent(S.boardId)}/placements`, items);
}

function onPointerDown(e) {
  if (!board()) return;
  const vp = $('#viewport');
  const t = e.target;
  // Pan: middle button, or space held.
  if (e.button === 1 || (e.button === 0 && S.space)) {
    e.preventDefault();
    S.drag = { mode: 'pan', sx: e.clientX, sy: e.clientY, vx: S.view.x, vy: S.view.y };
    vp.classList.add('panning');
    vp.setPointerCapture(e.pointerId);
    return;
  }
  if (e.button !== 0) return;
  if (t.closest('a[href]')) return;  // let links work; don't start a drag
  const card = t.closest('.card');
  const fid = card?.dataset.id;
  const pt = toBoard(e.clientX, e.clientY);

  if (card && t.classList.contains('resize-handle')) {
    e.preventDefault();
    const g = S.geom.get(fid);
    S.drag = { mode: 'resize', ids: [fid], sx: pt.x, sy: pt.y, g0: { ...g } };
    if (!S.selected.has(fid)) setSelected([fid]);
  } else if (card && t.closest('.card-head')) {
    e.preventDefault();
    if (e.shiftKey || e.ctrlKey || e.metaKey) {
      S.selected.has(fid) ? S.selected.delete(fid) : S.selected.add(fid);
      setSelected([...S.selected]);
      if (!S.selected.has(fid)) return;
    } else if (!S.selected.has(fid)) setSelected([fid]);
    const ids = [...S.selected];
    S.drag = { mode: 'move', ids, sx: pt.x, sy: pt.y, start: new Map(ids.map((id) => [id, { ...S.geom.get(id) }])), lead: fid, moved: false };
  } else if (card) {
    // Click in a card body selects it but leaves the content usable (scrolling, links, selecting text).
    if (!S.selected.has(fid) || S.selected.size > 1) setSelected([fid]);
    S.treeSel = fid;
    renderSidebar();
    return;
  } else if (e.shiftKey || e.ctrlKey || e.metaKey) {
    // Shift/Ctrl-drag on empty space: rubber-band select.
    S.drag = { mode: 'band', sx: pt.x, sy: pt.y, add: true, base: new Set(S.selected) };
  } else {
    // Plain drag on empty space pans; a plain click clears the selection.
    S.drag = { mode: 'pan', sx: e.clientX, sy: e.clientY, vx: S.view.x, vy: S.view.y, click: true };
    vp.classList.add('panning');
  }
  document.body.classList.add('dragging');
  vp.setPointerCapture(e.pointerId);
}

function onPointerMove(e) {
  const d = S.drag;
  if (!d) return;
  if (d.mode === 'pan') {
    S.view.x = d.vx + e.clientX - d.sx;
    S.view.y = d.vy + e.clientY - d.sy;
    applyView();
    return;
  }
  const pt = toBoard(e.clientX, e.clientY);
  const ov = $('#overlay');
  if (d.mode === 'move') {
    const dx = pt.x - d.sx, dy = pt.y - d.sy;
    if (!d.moved && Math.hypot(dx, dy) * eff() < 3) return;
    d.moved = true;
    const lead = d.start.get(d.lead);
    const nx = snap(lead.x + dx, e) - lead.x, ny = snap(lead.y + dy, e) - lead.y;
    for (const [id, g0] of d.start) {
      const g = S.geom.get(id);
      g.x = g0.x + nx; g.y = g0.y + ny;
      const c = S.cardEls.get(id);
      c.style.left = g.x + 'px'; c.style.top = g.y + 'px';
      c.classList.toggle('oob', !!isOOB(g));
    }
    renderStatus();
  } else if (d.mode === 'resize') {
    const id = d.ids[0], g = S.geom.get(id), g0 = d.g0;
    g.w = Math.max(40, snap(g0.x + g0.w + pt.x - d.sx, e) - g0.x);
    g.h = Math.max(30, snap(g0.y + g0.h + pt.y - d.sy, e) - g0.y);
    const c = S.cardEls.get(id);
    c.style.width = g.w + 'px'; c.style.height = g.h + 'px';
    c.classList.toggle('oob', !!isOOB(g));
    d.moved = true;
    renderStatus();
  } else if (d.mode === 'band') {
    const x = Math.min(d.sx, pt.x), y = Math.min(d.sy, pt.y), w = Math.abs(pt.x - d.sx), hh = Math.abs(pt.y - d.sy);
    d.rect = { x, y, w, h: hh };
    ov.replaceChildren(s('rect', { class: 'band', x, y, width: w, height: hh, 'stroke-width': 1 / eff() }));
    const ids = new Set(d.add ? d.base : []);
    for (const [id, g] of S.geom) if (g.x < x + w && g.x + g.w > x && g.y < y + hh && g.y + g.h > y) ids.add(id);
    S.selected = ids;
    renderBoardSelection();
  }
}

async function onPointerUp(e) {
  const d = S.drag;
  if (!d) return;
  const vp = $('#viewport');
  vp.classList.remove('panning');
  document.body.classList.remove('dragging');
  $('#overlay').replaceChildren();
  try { vp.releasePointerCapture(e.pointerId); } catch { /* not captured */ }
  try {
    if (d.mode === 'pan' && d.click && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 3) {
      setSelected([]);
    } else if (d.mode === 'move' && d.moved) {
      bringToFront(d.ids);
      for (const id of d.ids) S.cardEls.get(id).style.zIndex = 1 + S.geom.get(id).z;
      await savePlacements(d.ids);
    } else if (d.mode === 'resize' && d.moved) {
      await savePlacements(d.ids);
    } else if (d.mode === 'band') {
      if (!d.rect || d.rect.w * eff() < 3) { if (!d.add) setSelected([]); }
      renderStatus();
    }
  } finally {
    S.drag = null;
    if (S.pendingRefresh) { S.pendingRefresh = false; loadState(); }
    else if (d.mode !== 'pan') renderSidebar();
  }
}

function onWheel(e) {
  if (!board()) return;
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault();
    const vp = $('#viewport').getBoundingClientRect();
    setZoom(S.view.zoom * Math.exp(-e.deltaY * 0.0015), e.clientX - vp.left, e.clientY - vp.top);
    return;
  }
  // Let scrollable card content scroll.
  const body = e.target.closest?.('.card-body');
  if (body && (body.scrollHeight > body.clientHeight + 1 || body.scrollWidth > body.clientWidth + 1)) return;
  e.preventDefault();
  const dx = e.shiftKey ? e.deltaY : e.deltaX, dy = e.shiftKey ? 0 : e.deltaY;
  S.view.x -= dx; S.view.y -= dy;
  applyView();
}

async function onDrop(e) {
  const id = e.dataTransfer.getData('application/x-ritboard-finding');
  if (!id || !board()) return;
  e.preventDefault();
  const pt = toBoard(e.clientX, e.clientY);
  const item = { finding_id: id, x: snap(pt.x - 20), y: snap(pt.y - 15) };
  api.put(`${W()}/boards/${encodeURIComponent(S.boardId)}/placements`, [item]).then(() => { S.selected = new Set([id]); });
}

let nudgeTimer;
function nudge(dx, dy) {
  const ids = [...S.selected];
  for (const id of ids) {
    const g = S.geom.get(id);
    g.x += dx; g.y += dy;
    const c = S.cardEls.get(id);
    c.style.left = g.x + 'px'; c.style.top = g.y + 'px';
    c.classList.toggle('oob', !!isOOB(g));
  }
  renderStatus();
  clearTimeout(nudgeTimer);
  nudgeTimer = setTimeout(() => savePlacements(ids), 250);
}

function onKey(e) {
  const t = e.target;
  if (t.closest?.('input, textarea, select, [contenteditable], dialog')) {
    if (e.key === 'Escape' && t.closest('#editor')) closeEditor();
    return;
  }
  if (e.key === ' ') { S.space = true; document.body.classList.add('space'); e.preventDefault(); return; }
  const step = e.shiftKey ? (settings().grid || 10) : 1;
  switch (e.key) {
    case 'Delete': case 'Backspace':
      if (S.selected.size) api.post(`${W()}/boards/${encodeURIComponent(S.boardId)}/unplace`, [...S.selected]);
      e.preventDefault(); break;
    case 'ArrowLeft': if (S.selected.size) { nudge(-step, 0); e.preventDefault(); } break;
    case 'ArrowRight': if (S.selected.size) { nudge(step, 0); e.preventDefault(); } break;
    case 'ArrowUp': if (S.selected.size) { nudge(0, -step); e.preventDefault(); } break;
    case 'ArrowDown': if (S.selected.size) { nudge(0, step); e.preventDefault(); } break;
    case 'Escape': setSelected([]); closeEditor(); break;
    case 'Enter': if (S.selected.size === 1) openEditor([...S.selected][0]); break;
    case '\\': toggleSidebar(); break;
    case 'h': case 'H': toggleChrome(); break;
    case 'f': case 'F': fitView(); break;
    case '1': actualSize(); break;
    case '+': case '=': setZoom(S.view.zoom * 1.25); break;
    case '-': setZoom(S.view.zoom / 1.25); break;
    case 'a': if (e.ctrlKey || e.metaKey) { setSelected([...S.geom.keys()]); e.preventDefault(); } break;
    default:
  }
}

function toggleSidebar() { document.body.classList.toggle('side-off'); LS.set('sideOff', document.body.classList.contains('side-off')); }
function toggleChrome() { document.body.classList.toggle('chrome-off'); LS.set('chromeOff', document.body.classList.contains('chrome-off')); }

// ---------------------------------------------------------------- editor
function renderArtifactsTab(body, q) {
  const arts = (S.data.artifacts || []).filter((a) => !q || a.title.toLowerCase().includes(q) || a.id.toLowerCase().includes(q));
  if (!arts.length) {
    body.append(h('div', { class: 'empty' }, q ? 'Nothing matches.'
      : 'No artifacts yet. An artifact is a folder with index.html. Agents publish them with publish_artifact, or make one with ＋ Finding and kind Artifact.'));
    return;
  }
  for (const a of arts) {
    const onBoard = S.geom.has(a.id);
    body.append(h('div', {
      class: 'node art', draggable: 'true',
      title: `${a.title}\n${a.id}/\n${fmtCount(a.files, 'file')} · ${fmtBytes(a.bytes)} · updated ${new Date(a.updated_at).toLocaleString()}\nDrag onto the board to show it.`,
      ondragstart: (e) => {
        e.dataTransfer.setData('application/x-ritboard-finding', a.id);
        e.dataTransfer.setData('text/plain', a.title);
      },
      onclick: () => treeClick(a.id),
      ondblclick: () => openEditor(a.id),
    },
    h('span', { class: 'ki' }, 'APP'),
    h('span', { class: 'tt' }, a.title),
    h('span', { class: 'xy' }, fmtCount(a.files, 'file')),
    onBoard ? h('span', { class: 'ob', title: 'On this board' }, '✓') : h('span', { class: 'ob' }),
    h('a', { class: 'mini-link', href: a.url, target: '_blank', rel: 'noopener', title: 'Open in a new window', onclick: (e) => e.stopPropagation() }, '↗'),
    h('button', {
      class: 'mini', title: 'Move the artifact folder to the trash',
      onclick: async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete artifact “${a.title}”? Its folder moves to .ritboard/local/trash and it leaves every board.`)) return;
        await api.del(FID(a.id) + '&recursive=true');
      },
    }, '✕')));
  }
}

function fmtBytes(n) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`; }

const HINTS = {
  md: 'Markdown. Tables, lists and code blocks work. Raw HTML is shown as text.',
  text: 'Shown as-is in a monospace block.',
  csv: 'First row is the header. Comma, semicolon, tab or | separated.',
  html: 'A one-off HTML snippet or document, saved as an .html file. Runs sandboxed with scripts allowed. For a page with its own files, use Artifact.',
  artifact: 'A folder with index.html and any other files the page uses. It has its own address (↗), and every card showing it reloads when its files change. Scripts run sandboxed.',
  url: 'An external page to frame, saved as a .url shortcut. Many sites refuse framing. claude.ai links are not accepted; make an Artifact with the HTML instead.',
  image: 'An image URL (https://) or a data: URI. Image files in the workspace folder show up by themselves.',
  folder: 'A folder in the workspace. Placed on a board it lists what is in it.',
  file: 'A file Ritboard has no viewer for. Open it with ↗.',
};

function openEditor(id, parentId) {
  S.editing = id;
  const f = id === 'new' ? null : by.finding.get(id);
  if (id !== 'new' && !f) return;
  const form = $('#ed-form');
  $('#editor').hidden = false;
  $('#ed-heading').textContent = f ? 'Finding' : 'New finding';
  form.title.value = f?.title || '';
  form.kind.value = f ? (f.kind === 'file' ? 'text' : f.kind) : (parentId === 'folder' ? 'folder' : 'md');
  form.category.value = f?.category || '';
  form.tags.value = (f?.tags || []).join(', ');
  form.content.value = f?.kind === 'artifact' ? '' : (f?.content || '');
  form.dataset.parent = f ? '' : (parentId && parentId !== 'folder' ? parentId : '');
  // Only text files convert into each other (the extension changes).
  form.kind.disabled = !!f && !TEXT_KINDS.includes(f.kind);
  for (const o of form.kind.options) o.disabled = !!f && TEXT_KINDS.includes(f.kind) && !TEXT_KINDS.includes(o.value);
  form.content.readOnly = false;
  form.dataset.nocontent = f && (['folder', 'file', 'artifact'].includes(f.kind) || (f.kind === 'image' && !f.content)) ? '1' : '';
  delete form.dataset.artifact;
  $('#ed-art').textContent = '';
  if (f?.kind === 'artifact') loadArtifactIntoEditor(f);
  $('#ed-place-wrap').hidden = !!f;
  $('#ed-delete').hidden = !f;
  $('#ed-toggle-place').hidden = !f;
  $('#ed-meta').textContent = f ? `${f.id}${f.kind === 'folder' || f.kind === 'artifact' ? '/' : ''}${f.source ? ' · from ' + f.source : ''} · changed ${new Date(f.updated_at).toLocaleString()}` : '';
  $('#cat-list').replaceChildren(...Object.keys(settings().categories).map((c) => h('option', { value: c })));
  kindChanged();
  refreshEditorPlaceButton();
  setTimeout(() => (f ? form.content : form.title).focus(), 0);
}
async function loadArtifactIntoEditor(f) {
  const form = $('#ed-form');
  form.content.value = 'Loading…';
  form.content.readOnly = true;
  try {
    const a = await api.get(`${W()}/artifact?id=${encodeURIComponent(f.id)}&content=true`);
    if (S.editing !== f.id) return;
    const page = a.files.find((x) => x.path === 'index.html');
    form.content.value = page?.content || '';
    form.dataset.artifact = f.id;
    form.dataset.orig = form.content.value;
    const others = a.files.map((x) => x.path).filter((p) => p !== 'index.html');
    $('#ed-art').replaceChildren(...[
      h('span', {}, `Editing index.html in ${f.id}/.`),
      others.length ? h('span', {}, ` Other files: ${others.join(', ')}.`) : null,
      ' ', h('a', { href: f.url, target: '_blank', rel: 'noopener' }, 'Open ↗'),
    ].filter(Boolean));
  } catch {
    form.content.value = '';
  } finally {
    form.content.readOnly = false;
  }
}

function closeEditor() { S.editing = null; $('#editor').hidden = true; }
function kindChanged() {
  const k = $('#ed-form').kind.value;
  $('#ed-hint').textContent = HINTS[k] || '';
  $('#ed-content-label').firstChild.textContent = k === 'url' ? 'URL ' : k === 'image' ? 'Image URL ' : k === 'artifact' ? 'Page HTML ' : 'Content ';
  $('#ed-content-label').hidden = k === 'folder' || !!$('#ed-form').dataset.nocontent && k !== 'artifact';
}
function refreshEditorPlaceButton() {
  const id = S.editing;
  if (!id || id === 'new') return;
  $('#ed-toggle-place').textContent = S.geom.has(id) ? 'Remove from board' : 'Place on board';
}

async function saveEditor(e) {
  e.preventDefault();
  const form = $('#ed-form');
  const body = {
    title: form.title.value.trim(),
    kind: form.kind.value,
    category: form.category.value.trim() || null,
    tags: form.tags.value.split(',').map((t) => t.trim()).filter(Boolean),
    content: form.content.value,
  };
  if (body.kind === 'artifact') return saveArtifact(form, body);
  if (S.editing === 'new') {
    const f = await api.post(`${W()}/findings`, { ...body, parent_id: form.dataset.parent || null, source: 'you' });
    if (form.place.checked && board()) await api.put(`${W()}/boards/${encodeURIComponent(S.boardId)}/placements`, [{ finding_id: f.id }]);
    S.treeSel = f.id;
    if (f.parent_id) { S.expanded.add(f.parent_id); LS.set('expanded', [...S.expanded]); }
    await loadState();
    openEditor(f.id);
  } else {
    const cur = by.finding.get(S.editing);
    if (form.dataset.nocontent || cur?.kind === 'folder') delete body.content;
    if (cur && TEXT_KINDS.includes(cur.kind) === false) delete body.kind;
    const f = await api.patch(FID(S.editing), body);
    await followRename(S.editing, f.id);
  }
}

// A save can rename the file (new title) and so change the finding's id.
async function followRename(oldId, newId) {
  if (oldId === newId) return;
  for (const set of [S.selected, S.expanded]) if (set.has(oldId)) { set.delete(oldId); set.add(newId); }
  LS.set('expanded', [...S.expanded]);
  if (S.treeSel === oldId) S.treeSel = newId;
  S.editing = newId;
  await loadState();
  openEditor(newId);
}

async function saveArtifact(form, body) {
  if (S.editing === 'new') {
    const out = await api.post(`${W()}/artifacts`, {
      title: body.title, files: { 'index.html': body.content }, parent_id: form.dataset.parent || null,
      category: body.category, tags: body.tags, source: 'you',
      board_id: form.place.checked && board() ? S.boardId : null,
    });
    S.treeSel = out.artifact.id;
    await loadState();
    openEditor(out.artifact.id);
    return;
  }
  const id = S.editing;
  if (form.dataset.artifact === id && body.content !== form.dataset.orig) {
    await api.post(`${W()}/artifact/files?id=${encodeURIComponent(id)}`, { files: { 'index.html': body.content }, source: 'you' });
    form.dataset.orig = body.content;
  }
  const f = await api.patch(FID(id), { title: body.title, category: body.category, tags: body.tags });
  if (f.id !== id) return followRename(id, f.id);
  await loadState();
  if (by.finding.get(id)) loadArtifactIntoEditor(by.finding.get(id));
}

// ---------------------------------------------------------------- settings
function themeMat() {
  // The theme's mat colour as #rrggbb for the colour input.
  const c = getComputedStyle(document.documentElement).getPropertyValue('--mat').trim();
  return /^#[0-9a-f]{6}$/i.test(c) ? c : '#121a23';
}

const PRESETS = [[1920, 1080], [2560, 1440], [3840, 2160], [5120, 2880], [7680, 2160], [7680, 4320], [11520, 2160]];

async function openSettings() {
  const d = $('#settings'), f = $('#set-form'), b = board(), st = settings();
  const ws = S.data?.workspace;
  $('#set-board-name').textContent = b ? `— ${b.name}` : '';
  f.b_name.value = b?.name || '';
  f.b_w.value = b?.width || '';
  f.b_h.value = b?.height || '';
  f.b_bg.value = b?.background || themeMat();
  $('#presets').replaceChildren(...PRESETS.map(([w, hh]) => h('button', { type: 'button', onclick: () => { f.b_w.value = w; f.b_h.value = hh; } }, `${w}×${hh}`)),
    h('button', { type: 'button', title: 'This window in device pixels', onclick: () => { f.b_w.value = Math.round(innerWidth * devicePixelRatio); f.b_h.value = Math.round(innerHeight * devicePixelRatio); } }, 'This screen'));
  f.w_name.value = ws?.name || '';
  f.w_grid.value = st.grid;
  f.w_bw.value = st.board_width;
  f.w_bh.value = st.board_height;
  $('#set-cats').replaceChildren(...Object.entries(st.categories).map(([name, color]) =>
    h('label', {}, h('input', { type: 'color', value: color, 'data-cat': name }), name)));
  f.d_theme.value = S.disp.theme;
  f.d_ui.value = S.disp.ui || 'medium';
  f.d_device.checked = S.disp.device;
  f.d_grid.checked = S.disp.grid;
  f.d_snap.checked = S.disp.snap;
  $('#set-dpr').textContent = `This screen reports devicePixelRatio ${window.devicePixelRatio}. Window is ${innerWidth}×${innerHeight} CSS px = ${Math.round(innerWidth * devicePixelRatio)}×${Math.round(innerHeight * devicePixelRatio)} device px.`;
  const mcp = `${location.origin}/mcp`;
  $('#set-mcp').textContent = mcp;
  $('#set-cc').textContent = `claude mcp add --transport http ritboard ${mcp}`;
  $('#set-folder').textContent = ws?.path || '';
  try { $('#set-data').textContent = (await api.get('/api/info')).data_dir; } catch { /* offline */ }
  d.showModal();
}

function wireSettings() {
  const f = $('#set-form');
  $('#set-board-apply').onclick = async () => {
    const b = board(); if (!b) return;
    await api.patch(`${W()}/boards/${encodeURIComponent(b.id)}`, { name: f.b_name.value.trim() || b.name, width: +f.b_w.value, height: +f.b_h.value, background: f.b_bg.value === themeMat() ? (b.background || null) : f.b_bg.value });
    $('#settings').close();
  };
  $('#set-board-delete').onclick = async () => {
    const b = board(); if (!b) return;
    if (S.data.boards.length <= 1) return toast('A workspace keeps at least one board.');
    if (!confirm(`Delete board “${b.name}”? Findings stay in the workspace.`)) return;
    await api.del(`${W()}/boards/${encodeURIComponent(b.id)}`);
    $('#settings').close();
  };
  $('#set-ws-apply').onclick = async () => {
    const cats = {};
    for (const inp of $$('#set-cats input[type=color]')) cats[inp.dataset.cat] = inp.value;
    await api.patch(`/api/workspaces/${encodeURIComponent(S.ws)}`, {
      name: f.w_name.value.trim() || null,
      settings: { grid: +f.w_grid.value || 10, board_width: +f.w_bw.value, board_height: +f.w_bh.value, categories: cats },
    });
    $('#settings').close();
  };
  $('#set-ws-delete').onclick = async () => {
    const w = S.data?.workspace;
    if (!confirm(`Remove “${w.name}” from Ritboard's list?\n\nThe folder and its files stay where they are:\n${w.path}\n\nOpen it again any time with “Open folder…”.`)) return;
    await api.del(`/api/workspaces/${encodeURIComponent(S.ws)}`);
    $('#settings').close();
    await loadWorkspaces();
    if (S.workspaces[0]) await selectWorkspace(S.workspaces[0].id);
  };
  for (const k of ['d_theme', 'd_ui', 'd_device', 'd_grid', 'd_snap']) {
    f[k].onchange = () => {
      const themeChanged = f.d_theme.value !== S.disp.theme;
      S.disp = { theme: f.d_theme.value, ui: f.d_ui.value, device: f.d_device.checked, grid: f.d_grid.checked, snap: f.d_snap.checked };
      LS.set('display', S.disp);
      applyTheme();
      if (themeChanged) for (const c of S.cardEls.values()) delete c.dataset.ver;  // rebuild framed HTML in new colors
      renderBoard();
    };
  }
}

// ---------------------------------------------------------------- boot
function wire() {
  const vp = $('#viewport');
  // The board position is owned by pan/zoom only; undo any browser scroll (focus, text selection).
  vp.addEventListener('scroll', () => { vp.scrollLeft = 0; vp.scrollTop = 0; });
  vp.addEventListener('pointerdown', onPointerDown);
  vp.addEventListener('pointermove', onPointerMove);
  vp.addEventListener('pointerup', onPointerUp);
  vp.addEventListener('pointercancel', onPointerUp);
  vp.addEventListener('wheel', onWheel, { passive: false });
  vp.addEventListener('dragover', (e) => {
    const t = e.dataTransfer.types;
    if (t.includes('application/x-ritboard-finding')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
  });
  vp.addEventListener('drop', onDrop);
  vp.addEventListener('click', (e) => {
    const a = e.target.closest('.card-body a[href]');
    if (a) { e.preventDefault(); window.open(a.href, '_blank', 'noopener'); }
  });
  vp.addEventListener('dblclick', (e) => {
    // Pointer capture can retarget click events to the viewport, so look up what is under the pointer.
    const t = document.elementFromPoint(e.clientX, e.clientY) || e.target;
    const card = t.closest('.card');
    if (card && t.closest('.card-head')) openEditor(card.dataset.id);
  });
  vp.addEventListener('auxclick', (e) => { if (e.button === 1) e.preventDefault(); });
  window.addEventListener('keydown', onKey);
  window.addEventListener('keyup', (e) => { if (e.key === ' ') { S.space = false; document.body.classList.remove('space'); } });
  window.addEventListener('blur', () => { S.space = false; document.body.classList.remove('space'); });
  // Scaling changes (moving the window to another monitor) change device pixels; resizing does not.
  const watchDpr = () => {
    const mq = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
    mq.addEventListener('change', () => { applyView(); watchDpr(); }, { once: true });
  };
  watchDpr();
  window.addEventListener('focus', reportUi);

  $('#btn-sidebar').onclick = toggleSidebar;
  $('#btn-chrome').onclick = toggleChrome;
  $('#btn-fit').onclick = fitView;
  $('#btn-actual').onclick = actualSize;
  $('#btn-settings').onclick = openSettings;
  $('#btn-add-board').onclick = async () => {
    if (!S.ws) return;
    const name = prompt('New board name', `Board ${(S.data?.boards.length || 0) + 1}`);
    if (!name) return;
    const b = await api.post(`${W()}/boards`, { name });
    await loadState();
    selectBoard(b.id);
  };
  $('#ws-select').onchange = async (e) => {
    const v = e.target.value;
    if (v === '__new' || v === '__open') {
      const answer = v === '__new'
        ? prompt('New workspace name. Its folder goes in your Ritboard folder.')
        : prompt('Folder to use as a workspace (full path on the computer running Ritboard). Its files stay as they are; Ritboard adds a .ritboard folder.');
      if (!answer) { e.target.value = S.ws; return; }
      try {
        const w = v === '__new'
          ? await api.post('/api/workspaces', { name: answer })
          : await api.post('/api/workspaces/open', { path: answer.trim().replace(/^"(.*)"$/, '$1') });
        await loadWorkspaces();
        await selectWorkspace(w.id);
      } finally { renderWsSelect(); }
    } else await selectWorkspace(v);
  };
  $$('.side-tabs button').forEach((b) => (b.onclick = () => { S.sideTab = b.dataset.tab; LS.set('sideTab', S.sideTab); renderSidebar(); }));
  $('#side-search').oninput = (e) => { S.filter = e.target.value; renderSidebar(); };
  const sb = $('#side-body');
  sb.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes('application/x-ritboard-finding')) { e.preventDefault(); sb.classList.add('drop-root'); } });
  sb.addEventListener('dragleave', (e) => { if (e.target === sb) sb.classList.remove('drop-root'); });
  sb.addEventListener('drop', async (e) => {
    sb.classList.remove('drop-root');
    const id = e.dataTransfer.getData('application/x-ritboard-finding');
    if (!id || S.sideTab !== 'tree') return;
    e.preventDefault();
    if (by.finding.get(id)?.parent_id) await api.patch(FID(id), { parent_id: null });
  });
  const parentForNew = () => { const t = by.finding.get(S.treeSel); return t ? (t.kind === 'folder' ? t.id : t.parent_id) : null; };
  $('#btn-new-finding').onclick = () => openEditor('new', parentForNew());
  $('#btn-new-folder').onclick = async () => {
    const name = prompt('Folder name');
    if (!name) return;
    const f = await api.post(`${W()}/findings`, { title: name, kind: 'folder', parent_id: parentForNew(), source: 'you' });
    S.treeSel = f.id;
  };
  $('#ed-form').addEventListener('submit', saveEditor);
  $('#ed-form').kind.addEventListener('change', kindChanged);
  $('#ed-close').onclick = closeEditor;
  $('#ed-toggle-place').onclick = async () => {
    const id = S.editing;
    if (!id || !board()) return;
    if (S.geom.has(id)) await api.post(`${W()}/boards/${encodeURIComponent(S.boardId)}/unplace`, [id]);
    else await api.put(`${W()}/boards/${encodeURIComponent(S.boardId)}/placements`, [{ finding_id: id }]);
  };
  $('#ed-delete').onclick = async () => {
    const f = by.finding.get(S.editing);
    if (!f) return;
    const kids = (by.children.get(f.id) || []).length;
    if (!confirm(`Delete “${f.title}”? The ${f.kind === 'folder' ? 'folder' : 'file'} moves to .ritboard/local/trash and leaves every board.${kids ? `\nWhat is in it moves up a level.` : ''}`)) return;
    await api.del(FID(f.id) + (f.kind === 'artifact' ? '&recursive=true' : ''));
    closeEditor();
  };
  wireSettings();
}

const THEMES = ['dark', 'black', 'light'];
const UI_ZOOM = { small: 0.85, medium: 1, large: 1.25, xlarge: 1.6 };
function applyTheme() {
  if (!THEMES.includes(S.disp.theme)) S.disp.theme = 'dark';
  document.documentElement.dataset.theme = S.disp.theme;
  document.documentElement.style.setProperty('--ui-zoom', UI_ZOOM[S.disp.ui] || 1);
}

async function main() {
  applyTheme();
  if (LS.get('sideOff', false)) document.body.classList.add('side-off');
  if (LS.get('chromeOff', false)) document.body.classList.add('chrome-off');
  wire();
  await loadWorkspaces();
  const remembered = LS.get('ws');
  const params = new URLSearchParams(location.search);
  const wanted = params.get('workspace') || remembered;
  const ws = S.workspaces.find((w) => w.id === wanted) || S.workspaces[0];
  if (ws) await selectWorkspace(ws.id);
  renderWsSelect();
  sock.connect();
}

main().catch((e) => toast(String(e)));
