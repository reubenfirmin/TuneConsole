// ── Taste-model visualization tooltips ──────────────────────────────────────
// A single floating div for plain text or structured data-tip JSON (title + labelled rows).
// Document-level delegation so it also covers the lazily htmx-swapped embedding-engine panel.
function initVizTooltip() {
  let tip = null, current = null, hideTimer = null, plainText = '';
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  // A copy glyph so the user can grab the tooltip's text verbatim (to paste back as an example).
  const COPY_ICON = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<rect x="9" y="9" width="11" height="11" rx="2"/>'
    + '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  function cancelHide() { clearTimeout(hideTimer); }
  function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hide, 240); }
  function hide() { if (tip) tip.classList.remove('on'); current = null; }

  function ensure() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'viz-tip';
      tip.setAttribute('role', 'tooltip');
      // Interactive: the cursor can leave the source and move INTO the tip (to click copy)
      // without it vanishing.
      tip.addEventListener('mouseenter', cancelHide);
      tip.addEventListener('mouseleave', hide);
      tip.addEventListener('click', (e) => {
        const btn = e.target.closest('.vt-copy');
        if (!btn || !navigator.clipboard) return;
        navigator.clipboard.writeText(plainText).then(() => {
          btn.classList.add('done');
          setTimeout(() => btn.classList.remove('done'), 1200);
        }).catch(() => {});
      });
      document.body.appendChild(tip);
    }
    return tip;
  }
  function render(d) {
    if (d.text !== undefined) {
      plainText = String(d.text);
      ensure().textContent = plainText;
      return;
    }
    const dot = /^#[0-9a-fA-F]{3,8}$/.test(d.dot || '') ? d.dot : '';
    const lines = [d.title];
    if (d.desc) lines.push(d.desc);
    let h = '<button class="vt-copy" type="button" title="Copy to clipboard" aria-label="Copy">'
          + COPY_ICON + '</button>';
    h += '<div class="vt-title' + (d.accent ? ' ' + d.accent : '') + '"'
       + (dot ? ' style="--dot:' + dot + '"' : '') + '>' + esc(d.title) + '</div>';
    if (d.desc) h += '<div class="vt-desc">' + esc(d.desc) + '</div>';
    for (const row of (d.rows || [])) {
      const value = row.arrow
        ? String(row.v).split(' to ').map(esc).join(' <span class="vt-change">to</span> ')
        : esc(row.v);
      h += '<div class="vt-row' + (row.div ? ' vt-div' : '') + '">'
         + '<span class="vt-l">' + esc(row.l) + '</span>'
         + '<span class="vt-v' + (row.t ? ' ' + row.t : '') + '">' + value + '</span></div>';
      if (row.n) h += '<div class="vt-n">' + esc(row.n) + '</div>';
      lines.push(row.l + ': ' + row.v + (row.n ? ', ' + row.n : ''));
    }
    plainText = lines.join('\n');
    ensure().innerHTML = h;
  }
  function place(e) {
    if (!tip) return;
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + pad, y = e.clientY + pad;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = Math.max(8, y) + 'px';
  }

  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) return;
    cancelHide();                       // re-entering a source (or hopping between them) keeps it up
    if (el === current) return;
    let d; try { d = JSON.parse(el.getAttribute('data-tip')); } catch (_) { return; }
    current = el; render(d); ensure().classList.add('on'); place(e);
  });
  // Pinned (no cursor-follow) so it's reachable; a short grace lets the pointer cross into the tip.
  document.addEventListener('mouseout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && el === current && !el.contains(e.relatedTarget)) scheduleHide();
  });
  // Keyboard users get the same structured readout. Place it beside the focused mark rather than
  // requiring pointer coordinates; focusout mirrors the mouseleave grace period.
  document.addEventListener('focusin', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (!el) return;
    cancelHide();
    let d; try { d = JSON.parse(el.getAttribute('data-tip')); } catch (_) { return; }
    current = el; render(d); ensure().classList.add('on');
    const r = el.getBoundingClientRect();
    place({ clientX: r.right, clientY: r.top });
  });
  document.addEventListener('focusout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && el === current && !el.contains(e.relatedTarget)) scheduleHide();
  });
  // A scroll or htmx swap can pull the hovered element out from under the cursor.
  document.addEventListener('scroll', hide, true);
  document.body.addEventListener('htmx:beforeSwap', hide);
}
window.addEventListener('DOMContentLoaded', initVizTooltip);

