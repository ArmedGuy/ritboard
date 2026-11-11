// Ritboard artifact runtime. Loaded into every artifact page before its own scripts.
// It gives the page a small per-artifact database with the same shape claude.ai artifacts
// use: `const db = await window.claude.use("db"); db.collection("plan").onSnapshot(...)`.
// Data lives in the workspace file, next to the artifact. Ritboard pushes changes over a
// server-sent event stream: database writes by other viewers or agents arrive at once, and
// a page opened on its own reloads when its files change. (Inside a board card the board
// reloads it.) If the stream drops, the database falls back to polling.
(() => {
  'use strict';
  const m = location.pathname.match(/^(\/w\/[^/]+\/a\/[^/]+)\//);
  if (!m) return;
  const base = m[1] + '/_db';
  const me = document.currentScript;
  const loadedVersion = Number(me && me.dataset.version) || 0;  // state of the files this page came from
  let topLevel = true;
  try { topLevel = window.self === window.top; } catch { topLevel = false; }
  let live = false;  // event stream connected
  const pollMs = () => (live ? 15000 : 1500);

  async function call(method, path, params, body) {
    const q = new URLSearchParams(params).toString();
    const res = await fetch(`${base}${path}?${q}`, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error((data && data.detail) || res.statusText);
      err.code = res.status === 404 ? 'not_found' : res.status === 409 ? 'conflict' : res.status === 413 ? 'quota_exceeded' : 'failed';
      throw err;
    }
    return data;
  }

  class DocSnapshot {
    constructor(ref, row) { this.ref = ref; this.id = ref.id; this._row = row; }
    get exists() { return !!this._row; }
    get version() { return this._row ? this._row.version : null; }
    data() { return this._row ? structuredClone(this._row.data) : undefined; }
    get(field) { return this._row ? this._row.data[field] : undefined; }
  }

  class DocRef {
    constructor(coll, id) { this.parent = coll; this.id = id; this.path = `${coll.path}/${id}`; }
    async get() {
      try { return new DocSnapshot(this, await call('GET', '/doc', { c: this.parent.path, id: this.id })); }
      catch (e) { if (e.code === 'not_found') return new DocSnapshot(this, null); throw e; }
    }
    async set(data, opts) { const r = await call(opts && opts.merge ? 'PATCH' : 'PUT', '/doc', { c: this.parent.path, id: this.id }, data); this.parent._kick(); return r; }
    async update(data) { const r = await call('PATCH', '/doc', { c: this.parent.path, id: this.id }, data); this.parent._kick(); return r; }
    async delete() { await call('DELETE', '/doc', { c: this.parent.path, id: this.id }); this.parent._kick(); }
    collection(name) { return new Collection(`${this.path}/${name}`); }
    onSnapshot(next, error) {
      return this.parent.onSnapshot((snap) => {
        const row = snap._rows.get(this.id) || null;
        next(new DocSnapshot(this, row));
      }, error);
    }
  }

  class QuerySnapshot {
    constructor(coll, rows, changes) {
      this._rows = rows;
      this.docs = [...rows.values()].map((r) => new DocSnapshot(new DocRef(coll, r.id), r));
      this.size = this.docs.length;
      this.empty = this.size === 0;
      this._changes = changes;
      this.metadata = { fromCache: false, hasPendingWrites: false };
    }
    docChanges() { return this._changes; }
    forEach(fn) { this.docs.forEach(fn); }
  }

  const collections = new Map();
  class Collection {
    constructor(path) {
      this.path = path;
      this.id = path.split('/').pop();
      this._listeners = new Set();
      this._rows = new Map();
      this._rev = -1;
      this._timer = null;
      this._busy = false;
    }
    doc(id) { return new DocRef(this, id || newId()); }
    async add(data) { const ref = this.doc(); await ref.set(data); return ref; }
    async get() { const rows = await this._fetch(true); return new QuerySnapshot(this, rows, []); }
    onSnapshot(next, error) {
      const l = { next, error, primed: false };
      this._listeners.add(l);
      if (this._rev >= 0) this._emit(l, [...this._rows.values()].map((r) => ({ type: 'added', r })));
      this._poll();
      return () => { this._listeners.delete(l); if (!this._listeners.size) clearTimeout(this._timer); };
    }
    _kick() { if (this._listeners.size) { clearTimeout(this._timer); this._poll(); } }
    async _fetch(force) {
      const r = await call('GET', '', { c: this.path, rev: force ? -1 : this._rev });
      if (r.unchanged) return null;
      this._rev = r.rev;
      return new Map(r.docs.map((d) => [d.id, d]));
    }
    async _poll() {
      if (this._busy) return;
      this._busy = true;
      try {
        const rows = await this._fetch(false);
        if (rows) {
          const changes = [];
          for (const [id, r] of rows) {
            const old = this._rows.get(id);
            if (!old) changes.push({ type: 'added', r });
            else if (old.version !== r.version) changes.push({ type: 'modified', r });
          }
          for (const [id, r] of this._rows) if (!rows.has(id)) changes.push({ type: 'removed', r });
          this._rows = rows;
          for (const l of this._listeners) this._emit(l, l.primed ? changes : [...rows.values()].map((r) => ({ type: 'added', r })));
        } else {
          for (const l of this._listeners) if (!l.primed) this._emit(l, [...this._rows.values()].map((r) => ({ type: 'added', r })));
        }
      } catch (e) {
        for (const l of this._listeners) if (l.error) try { l.error(e); } catch { /* listener threw */ }
      } finally {
        this._busy = false;
        if (this._listeners.size) this._timer = setTimeout(() => this._poll(), pollMs());
      }
    }
    _emit(l, changes) {
      l.primed = true;
      const snap = new QuerySnapshot(this, this._rows, changes.map(({ type, r }) => ({ type, doc: new DocSnapshot(new DocRef(this, r.id), r) })));
      try { l.next(snap); } catch (e) { console.error(e); }
    }
  }

  function newId() {
    const a = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    let s = '';
    for (let i = 0; i < 20; i++) s += a[Math.floor(Math.random() * a.length)];
    return s;
  }

  const db = {
    collection(path) {
      if (!collections.has(path)) collections.set(path, new Collection(path));
      return collections.get(path);
    },
    doc(path) {
      const i = path.lastIndexOf('/');
      return this.collection(path.slice(0, i)).doc(path.slice(i + 1));
    },
  };

  // Push from Ritboard.
  function connect() {
    if (!window.EventSource) return;
    const es = new EventSource(m[1] + '/_events');
    es.addEventListener('hello', (e) => {
      live = true;
      const { latest } = JSON.parse(e.data);
      if (topLevel && loadedVersion && latest && latest !== loadedVersion) location.reload();
    });
    es.addEventListener('db', () => { for (const c of collections.values()) c._kick(); });
    es.addEventListener('version', (e) => {
      const { latest } = JSON.parse(e.data);
      if (topLevel && latest && latest !== loadedVersion) location.reload();
    });
    es.addEventListener('deleted', () => { es.close(); });
    es.onerror = () => { live = false; };  // EventSource retries by itself
  }
  connect();

  const claude = window.claude || {};
  claude.ritboard = true;
  claude.use = async (name) => {
    if (name === 'db') return db;
    throw Object.assign(new Error(`"${name}" is not available in Ritboard artifacts`), { code: 'unavailable' });
  };
  window.claude = claude;
})();
