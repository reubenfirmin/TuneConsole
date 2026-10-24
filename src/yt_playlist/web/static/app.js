// Keep --topbar-h in sync with the real navbar height so sticky table headers pin right below it.
function syncTopbarH() {
  const tb = document.querySelector('.topbar');
  if (tb) {
    const rail = getComputedStyle(tb).position === 'fixed' && tb.offsetHeight >= window.innerHeight;
    document.documentElement.style.setProperty('--topbar-h', (rail ? 0 : tb.offsetHeight) + 'px');
  }
}
window.addEventListener('DOMContentLoaded', syncTopbarH);
window.addEventListener('resize', syncTopbarH);

// htmx: a 422 carries an OOB error toast. Process its body while retaining the failed
// request status so optimistic controls roll back. The server sets `HX-Reswap: none` to keep the primary
// target untouched while the OOB toast still lands in #toasts. Bind on `document`
// (not document.body): app.js loads in <head>, before <body> exists.
document.addEventListener('htmx:beforeSwap', (e) => {
  if (e.detail.xhr.status === 422) e.detail.shouldSwap = true;
});

function authBanner(initial) {
  // The "session expired" bar, seeded from the server, and updated live by the sync panel so it
  // pops up during an AJAX sync (no page reload needed).
  return {
    labels: initial || [],
    dismissed: false,
    add(label) { if (label && !this.labels.includes(label)) this.labels.push(label); },
  };
}
// Navbar omnisearch dropdown: open/close + keyboard nav over the HTMX-rendered result rows.
// Visibility is driven by results arriving (htmx:afterSwap, wired in x-init on the form) and by
// focus; we never build markup here. The server owns the dropdown body.
function omniSearch() {
  return {
    open: false,
    active: -1,
    rows() { return Array.from(this.$root.querySelectorAll('.omni-row')); },
    onResults() {
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
      this.open = this.rows().length > 0;
    },
    close() {
      this.open = false;
      this.active = -1;
      this.rows().forEach(r => r.classList.remove('active'));
    },
    move(dir) {
      const rows = this.rows();
      if (!rows.length) { return; }
      this.open = true;
      this.active = (this.active + dir + rows.length) % rows.length;
      rows.forEach((r, i) => r.classList.toggle('active', i === this.active));
      rows[this.active].scrollIntoView({ block: 'nearest' });
    },
    choose() {
      const rows = this.rows();
      const row = rows[this.active] || rows[0];
      if (row) { row.click(); this.close(); }
    },
  };
}

// hx-confirm -> styled modal (replaces the native window.confirm dialog). htmx fires `htmx:confirm`
// for any element carrying hx-confirm; we intercept it, show our own modal, and only issue the
// request if the user confirms. Reuses the .modal-backdrop/.modal/.modal-actions styles.
document.addEventListener('htmx:confirm', (e) => {
  const question = e.detail.question;
  if (!question) return;                       // no hx-confirm on this element: proceed normally
  e.preventDefault();                          // suppress htmx's native confirm

  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML =
    '<div class="modal" role="dialog" aria-modal="true">' +
    '<h3>Confirm</h3><p></p>' +
    '<div class="modal-actions">' +
    '<button type="button" class="btn-ghost" data-act="cancel">Cancel</button>' +
    '<button type="button" data-act="ok">Confirm</button>' +
    '</div></div>';
  backdrop.querySelector('p').textContent = question;   // textContent: never inject markup

  const close = () => { backdrop.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (ev) => { if (ev.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); } };
  backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) close(); });
  backdrop.querySelector('[data-act="cancel"]').addEventListener('click', close);
  backdrop.querySelector('[data-act="ok"]').addEventListener('click', () => {
    close();
    document.removeEventListener('keydown', onKey);
    e.detail.issueRequest(true);               // true = skip the confirm check, avoid recursion
  });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(backdrop);
  backdrop.querySelector('[data-act="ok"]').focus();
});
