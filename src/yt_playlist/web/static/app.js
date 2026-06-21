// Keep --topbar-h in sync with the real navbar height so sticky table headers pin right below it.
function syncTopbarH() {
  const tb = document.querySelector('.topbar');
  if (tb) document.documentElement.style.setProperty('--topbar-h', tb.offsetHeight + 'px');
}
window.addEventListener('DOMContentLoaded', syncTopbarH);
window.addEventListener('resize', syncTopbarH);

// Alpine component factories for the dashboard tabs (loaded globally via base.html).
function groupCard() {
  return {
    state: 'idle', err: '',
    async keep(id) {
      if (this.state === 'working') return;
      this.state = 'working'; this.err = '';
      try {
        const fd = new FormData(); fd.append('keep', id);
        const r = await fetch('/dupe/keep-one', { method: 'POST', body: fd });
        const j = await r.json();
        // reload so dependent sections (e.g. overlaps that referenced a deleted copy) recompute
        if (j.ok) { location.reload(); }
        else { this.state = 'idle'; this.err = (j.errors || []).join(' · ') || 'failed'; }
      } catch (e) { this.state = 'idle'; this.err = String(e); }
    },
  };
}
function emptyRow() {
  return {
    state: 'idle', err: '',
    async del(id) {
      if (this.state === 'deleting') return;
      this.state = 'deleting'; this.err = '';
      try {
        const fd = new FormData(); fd.append('playlist', id);
        const r = await fetch('/playlist/delete-empty', { method: 'POST', body: fd });
        const j = await r.json();
        if (j.ok) { this.state = 'gone'; } else { this.state = 'idle'; this.err = j.error || 'failed'; }
      } catch (e) { this.state = 'idle'; this.err = String(e); }
    },
  };
}
function hideRow() {
  return {
    state: 'idle', open: false, mx: 0, my: 0,
    async hide(a, b) {
      if (this.state === 'hiding') return;
      this.state = 'hiding';
      try {
        const fd = new FormData(); fd.append('a', a); fd.append('b', b);
        const r = await fetch('/overlaps/suppress', { method: 'POST', body: fd });
        if ((await r.json()).ok) { this.state = 'gone'; } else { this.state = 'idle'; }
      } catch (e) { this.state = 'idle'; }
    },
  };
}
function staleRow() {
  return {
    state: 'idle',
    async _post(url, fd) {
      this.state = 'working';
      try {
        const r = await fetch(url, { method: 'POST', body: fd });
        if ((await r.json()).ok) { this.state = 'gone'; } else { this.state = 'idle'; }
      } catch (e) { this.state = 'idle'; }
    },
    dismiss(ytm) {
      if (this.state === 'working') return;
      const fd = new FormData(); fd.append('ytm', ytm);
      return this._post('/rediscover/dismiss', fd);
    },
    snooze(ytm, days) {
      if (this.state === 'working') return;
      const fd = new FormData(); fd.append('ytm', ytm); fd.append('days', days);
      return this._post('/rediscover/snooze', fd);
    },
  };
}
function ajaxRow() {
  // Generic "POST then drop this row" for restore-style actions (unhide, stop-ignoring).
  return {
    gone: false, busy: false,
    async go(url, data) {
      if (this.busy) return;
      this.busy = true;
      try {
        const fd = new FormData();
        Object.entries(data).forEach(([k, v]) => fd.append(k, v));
        const r = await fetch(url, { method: 'POST', body: fd });
        if ((await r.json()).ok) { this.gone = true; } else { this.busy = false; }
      } catch (e) { this.busy = false; }
    },
  };
}
function saveAlbum(saved) {
  // Save/unsave a YouTube album to your library, then reload so both album tables refresh.
  return {
    saved: !!saved, busy: false,
    label() { return this.busy ? '…' : (this.saved ? 'Unsave' : 'Save'); },
    async toggle(browseId) {
      if (this.busy) return;
      this.busy = true;
      const url = this.saved ? '/collection/unsave-album' : '/collection/save-album';
      try {
        const fd = new FormData(); fd.append('browse_id', browseId);
        const j = await (await fetch(url, { method: 'POST', body: fd })).json();
        if (j.ok) { location.reload(); return; }
      } catch (e) {}
      this.busy = false;
    },
  };
}
function rowSort() {
  // Generic click-to-sort for a static-row table; reorders <tr class="srow"> by data-<key>.
  // Numeric when both values parse as numbers, else locale string compare.
  return {
    key: '', dir: 1,
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; } else { this.key = k; this.dir = 1; }
      const tb = this.$refs.body;
      if (!tb) return;
      Array.from(tb.querySelectorAll('tr.srow'))
        .sort((x, y) => {
          const a = x.dataset[k] || '', b = y.dataset[k] || '';
          const na = parseFloat(a), nb = parseFloat(b);
          const numeric = a !== '' && b !== '' && !isNaN(na) && !isNaN(nb);
          return (numeric ? na - nb : a.localeCompare(b)) * this.dir;
        })
        .forEach(r => tb.appendChild(r));
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },
  };
}
function overlapSort() {
  // Client-side sort of the overlaps table by reordering the per-row <tbody> nodes
  // (which preserves each row's Alpine state — pie menu, hide animation, etc.).
  return {
    key: 'shared', dir: -1,   // default: most-overlapping first
    tail: 0, tailMax: 0, _below: [], confirmOpen: false,
    start() { this.apply(); },
    sortBy(k) {
      if (this.key === k) { this.dir = -this.dir; }
      else { this.key = k; this.dir = (k === 'shared') ? -1 : 1; }
      this.apply();
    },
    apply() {
      const tbl = this.$refs.tbl;
      if (!tbl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row'));
      rows.sort((x, y) => {
        if (this.key === 'shared') return (+x.dataset.shared - +y.dataset.shared) * this.dir;
        return (x.dataset[this.key] || '').localeCompare(y.dataset[this.key] || '') * this.dir;
      });
      rows.forEach(r => tbl.appendChild(r));   // re-append in order; moving keeps node state
    },
    // Anchored to a TABLE ROW (not the scroll position): hide this row + every row after it
    // in the current sort order. The control lives on each row, so the cut point is a row.
    dismissFromRow(rowEl) {
      const tbl = this.$refs.tbl;
      if (!tbl || !rowEl) return;
      const rows = Array.from(tbl.querySelectorAll('tbody.ov-row')).filter(r => r.style.display !== 'none');
      const idx = rows.indexOf(rowEl);
      if (idx < 0) return;
      this._below = rows.slice(idx);
      this.tail = this._below.length;
      this.tailMax = this._below.reduce((m, r) => Math.max(m, +r.dataset.shared), 0);
      this.confirmOpen = true;
    },
    async confirmDismiss() {
      this.confirmOpen = false;
      if (!this._below.length) return;
      const pairs = this._below.map(r => [r.dataset.ay, r.dataset.by]);
      try {
        const fd = new FormData(); fd.append('pairs', JSON.stringify(pairs));
        await fetch('/overlaps/suppress-many', { method: 'POST', body: fd });
      } catch (e) {}
      location.hash = 'overlaps'; location.reload();
    },
    ind(k) { return this.key === k ? (this.dir === 1 ? ' ▲' : ' ▼') : ''; },
  };
}
function playlistsTab(rows) {
  return {
    rows, sel: {}, sortKey: 'title', sortDir: 1, split: false, busy: false,
    groupModal: false, groupName: '', delModal: false,
    init() {
      // remember view preferences across reloads (the tab reloads after group/delete)
      try {
        this.split = localStorage.getItem('pl.split') === '1';
        this.sortKey = localStorage.getItem('pl.sortKey') || 'title';
        this.sortDir = +localStorage.getItem('pl.sortDir') || 1;
      } catch (e) {}
      this.$watch('split', v => { try { localStorage.setItem('pl.split', v ? '1' : '0'); } catch (e) {} });
      this.$watch('sortKey', v => { try { localStorage.setItem('pl.sortKey', v); } catch (e) {} });
      this.$watch('sortDir', v => { try { localStorage.setItem('pl.sortDir', v); } catch (e) {} });
    },
    selected() { return this.rows.filter(r => this.sel[r.id]); },
    count() { return this.selected().length; },
    toggle(id) { this.sel[id] = !this.sel[id]; },
    sortBy(key) {
      if (this.sortKey === key) { this.sortDir = -this.sortDir; }
      else { this.sortKey = key; this.sortDir = ['count', 'listens', 'last'].includes(key) ? -1 : 1; }
    },
    ind(key) { return this.sortKey === key ? (this.sortDir === 1 ? ' ▲' : ' ▼') : ''; },
    cmp(a, b) {
      const k = this.sortKey, numeric = (k === 'count' || k === 'listens' || k === 'last');
      const r = numeric ? ((a[k] || 0) - (b[k] || 0))
                        : String(a[k] || '').localeCompare(String(b[k] || ''));
      return (r ? r * this.sortDir : a.title.localeCompare(b.title));   // stable tiebreak by title
    },
    sorted() { return [...this.rows].sort((a, b) => this.cmp(a, b)); },
    sections() {
      // when split, partition by group name (Ungrouped last); else one "" section
      if (!this.split) return [{ name: '', rows: this.sorted() }];
      const m = {};
      this.sorted().forEach(r => { const g = r.group || 'Ungrouped'; (m[g] = m[g] || []).push(r); });
      return Object.keys(m)
        .sort((a, b) => a === 'Ungrouped' ? 1 : b === 'Ungrouped' ? -1 : a.localeCompare(b))
        .map(n => ({ name: n, rows: m[n] }));
    },
    selectAll(on) { this.rows.forEach(r => { this.sel[r.id] = on; }); },
    fmtLast(ts) {
      if (!ts) return '—';
      const days = Math.floor((Date.now() / 1000 - ts) / 86400);
      if (days <= 0) return 'today';
      if (days === 1) return 'yesterday';
      if (days < 30) return days + 'd ago';
      if (days < 365) return Math.floor(days / 30) + 'mo ago';
      return Math.floor(days / 365) + 'y ago';
    },
    merge() {
      if (this.count() < 2) return;
      location.href = '/merge?ids=' + this.selected().map(r => r.id).join(',') + '&return=/';
    },
    async _post(url, data) {
      const fd = new FormData();
      Object.entries(data).forEach(([k, v]) => fd.append(k, v));
      const r = await fetch(url, { method: 'POST', body: fd });
      return r.json();
    },
    openGroup() { if (this.count()) { this.groupName = ''; this.groupModal = true; } },
    async doGroup() {
      this.groupModal = false;
      await this._post('/playlists/group', { ids: this.selected().map(r => r.id).join(','), name: this.groupName });
      location.reload();
    },
    async doDelete() {
      this.delModal = false;
      await this._post('/playlists/delete', { ids: this.selected().map(r => r.id).join(',') });
      location.reload();
    },
  };
}
function moveTab(fromId, toId) {
  return {
    from: fromId, to: toId, rows: {},
    canMove() { return this.from != null && this.to != null && this.from !== this.to; },
    st(id) { if (!this.rows[id]) this.rows[id] = { state: 'idle', err: '', msg: '' }; return this.rows[id]; },
    async run(id, copyOnly) {
      if (!this.canMove()) return;
      const r = this.st(id);
      if (r.state === 'working') return;
      r.state = 'working'; r.err = ''; r.msg = '';
      try {
        const fd = new FormData();
        fd.append('playlist', id); fd.append('target_identity', this.to);
        if (copyOnly) fd.append('copy_only', '1');
        const resp = await fetch('/move/run', { method: 'POST', body: fd });
        const j = await resp.json();
        if (j.ok) { r.state = copyOnly ? 'done' : 'gone'; r.msg = j.message || 'done'; }
        else { r.state = 'idle'; r.err = j.error || 'failed'; }
      } catch (e) { r.state = 'idle'; r.err = String(e); }
    },
  };
}
function _reloadOverlaps() { location.hash = 'overlaps'; location.reload(); }
async function _ignoreExcept(ytm, a, b) {
  const fd = new FormData(); fd.append('ytm', ytm); fd.append('a', a); fd.append('b', b);
  await fetch('/overlaps/ignore-except', { method: 'POST', body: fd });
}
async function muteOtherOverlaps(a, b) {
  // keep the a–b pair, mute every other overlap involving EITHER a or b, then reload
  try { await _ignoreExcept(a, a, b); await _ignoreExcept(b, a, b); } catch (e) {}
  _reloadOverlaps();
}
async function neverShowOverlaps(ytm) {
  // ignore a playlist's overlaps entirely (including this pair), then reload
  const fd = new FormData(); fd.append('ytm', ytm);
  try { await fetch('/overlaps/ignore', { method: 'POST', body: fd }); } catch (e) {}
  _reloadOverlaps();
}
// distinct colors for member badges A,B,C…N (index → palette)
function memberColor(i) {
  const palette = ['#7c6cff', '#4fd6e0', '#ff6b8b', '#6bffab', '#f4c66a', '#c08cff', '#ff9d5c', '#5cc8ff'];
  return palette[i % palette.length];
}
function mergeEditor(members, tracks, returnTo) {
  return {
    members, tracks, returnTo: returnTo || '/cleanup',
    keep: String(members[0].id), busy: false, err: '', inc: {}, pick: {},
    // Two independent axes:
    //   sort: 'alpha' (by title) | 'playlist' (order-preserving merge by position)
    //   mode: 'interleaved' (all together) | 'ducks' (shared first, odd ducks pushed to the end)
    sort: 'playlist', mode: 'interleaved',
    init() { this.tracks.forEach(t => { this.inc[t.tid] = true; }); },   // default: include everything
    presentCount(t) { return t.present.filter(Boolean).length; },
    // Effective normalized position: a per-track chosen playlist's position, else the average.
    effPos(t) {
      const p = this.pick[t.tid];
      if (p != null && t.npos && t.npos[p] != null) return t.npos[p];
      const vals = (t.npos || []).filter(v => v != null);
      return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 1;
    },
    pickPos(t, i) {
      if (!t.present[i] || this.presentCount(t) < 2) return;   // nothing to choose
      this.pick[t.tid] = (this.pick[t.tid] === i) ? null : i;  // toggle
    },
    isPicked(t, i) { return this.pick[t.tid] === i; },
    isIn(t) { return !!this.inc[t.tid]; },
    toggle(t) { this.inc[t.tid] = !this.inc[t.tid]; },
    setAll(v) { this.tracks.forEach(t => { this.inc[t.tid] = v; }); },
    count() { return this.tracks.filter(t => this.isIn(t)).length; },
    letters(t) { return this.members.filter((m, i) => t.present[i]).map(m => m.letter); },
    colorOf(letter) { return memberColor(letter.charCodeAt(0) - 65); },
    posOf(t, letter) { return (t.pos && t.pos[letter.charCodeAt(0) - 65]) || ''; },   // 1-based index in that playlist
    fmtDur(s) {
      if (s == null || isNaN(s) || s <= 0) return '';
      s = Math.round(s);
      const m = Math.floor(s / 60), sec = s % 60;
      return m + ':' + String(sec).padStart(2, '0');
    },
    ordered() {
      const byTitle = (a, b) => (a.title || '').localeCompare(b.title || '');
      const byPos = (a, b) => this.effPos(a) - this.effPos(b) || byTitle(a, b);
      const within = this.sort === 'playlist' ? byPos : byTitle;   // sort axis
      const cnt = t => t.present.filter(Boolean).length;
      if (this.mode === 'ducks') {                                   // shared first, odd ducks last
        const grp = t => (cnt(t) >= 2 ? 0 : 1);
        return [...this.tracks].sort((a, b) => (grp(a) - grp(b)) || within(a, b));
      }
      return [...this.tracks].sort(within);                         // interleaved: one combined list
    },
    async apply() {
      if (this.busy) return; this.busy = true; this.err = '';
      const vids = this.ordered().filter(t => this.isIn(t)).map(t => t.video_id).filter(Boolean);
      const ids = this.members.map(m => m.id).join(',');
      const fd = new FormData();
      fd.append('ids', ids); fd.append('result', vids.join(',')); fd.append('keep', this.keep);
      try {
        const r = await fetch('/merge/apply', { method: 'POST', body: fd });
        const j = await r.json();
        if (j.ok) {
          const sep = this.returnTo.includes('?') ? '&' : '?';
          let u = this.returnTo + sep + 'flash=' + encodeURIComponent(j.message);
          if (j.playlist) u += '&flash_pl=' + encodeURIComponent(j.playlist);
          location.href = u;
        } else { this.busy = false; this.err = j.error || 'failed'; }
      } catch (e) { this.busy = false; this.err = String(e); }
    },
  };
}
function syncPanel() {
  return {
    running: false,
    lines: [],
    push(ev) {
      const pip = ev.type === 'err' ? '✗' : ev.type === 'done' || ev.type === 'end' ? '✓'
                : ev.type === 'step' ? '›' : '•';
      const cls = ev.type === 'err' ? 'err' : (ev.type === 'done' || ev.type === 'end') ? 'done' : '';
      this.lines.push({ text: ev.text || '', pip, cls });
      this.$nextTick(() => { const l = this.$refs.log; if (l) l.scrollTop = l.scrollHeight; });
    },
    async start() {
      if (this.running) return;
      this.running = true; this.lines = [];
      this.push({ type: 'info', text: 'starting sync…' });
      let job;
      try {
        const r = await fetch('/sync', { method: 'POST' });
        job = (await r.json()).job_id;
      } catch (e) { this.push({ type: 'err', text: String(e) }); this.running = false; return; }
      const es = new EventSource(`/sync/events/${job}`);
      es.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === 'end') {
          es.close(); this.running = false;
          if (!ev.error) { this.push({ type: 'done', text: 'reloading…' }); setTimeout(() => location.reload(), 700); }
          return;
        }
        this.push(ev);
      };
      es.onerror = () => { es.close(); this.running = false; this.push({ type: 'err', text: 'stream interrupted' }); };
    },
  };
}
