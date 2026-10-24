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

// One owner for deferred hx-confirm requests. Native modality makes the background inert;
// the owner also contains Tab/shortcuts and releases the request if its invoker disappears.
let pendingConfirmation = null;
document.addEventListener('htmx:confirm', (e) => {
  const question = e.detail.question;
  if (!question) return;                       // no hx-confirm on this element: proceed normally
  e.preventDefault();                          // suppress htmx's native confirm
  if (pendingConfirmation) return;

  const invoker = e.detail.elt || e.target;
  if (!invoker?.isConnected) return;
  let issueRequest = e.detail.issueRequest;
  const dialog = document.createElement('dialog');
  dialog.className = 'modal shared-confirmation';
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'shared-confirmation-title');
  dialog.setAttribute('aria-describedby', 'shared-confirmation-question');
  dialog.innerHTML =
    '<h3 id="shared-confirmation-title">Confirm</h3><p id="shared-confirmation-question"></p>' +
    '<div class="modal-actions">' +
    '<button type="button" class="btn-ghost" data-act="cancel">Cancel</button>' +
    '<button type="button" data-act="ok" autofocus>Confirm</button>' +
    '</div>';
  dialog.querySelector('p').textContent = question;   // textContent: never inject markup

  let finished = false;
  const close = (confirmed = false) => {
    if (finished) return;
    finished = true;
    observer.disconnect();
    document.removeEventListener('htmx:beforeCleanupElement', onCleanup);
    dialog.removeEventListener('close', onClose);
    const request = confirmed && invoker.isConnected ? issueRequest : null;
    issueRequest = null;
    pendingConfirmation = null;
    dialog.close();
    dialog.remove();
    if (invoker.isConnected) invoker.focus({preventScroll: true});
    request?.(true);                            // skip htmx's confirm check, avoiding recursion
  };
  const onCleanup = event => {
    if (event.detail.elt?.contains(invoker)) close();
  };
  const onClose = () => close();
  const observer = new MutationObserver(() => {
    if (!invoker.isConnected || !dialog.isConnected) close();
  });
  dialog.addEventListener('keydown', event => {
    event.stopPropagation();                   // background document/window shortcuts stay idle
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const buttons = [...dialog.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement);
      event.preventDefault();
      buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
    }
  });
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('close', onClose);
  dialog.addEventListener('click', event => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right
        || event.clientY < bounds.top || event.clientY > bounds.bottom)) close();
  });
  dialog.querySelector('[data-act="cancel"]').addEventListener('click', () => close());
  dialog.querySelector('[data-act="ok"]').addEventListener('click', () => close(true));
  document.addEventListener('htmx:beforeCleanupElement', onCleanup);
  document.body.appendChild(dialog);
  pendingConfirmation = dialog;
  observer.observe(document.body, {childList: true, subtree: true});
  dialog.showModal();
});
