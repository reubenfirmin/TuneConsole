(() => {
  const dialog = document.querySelector('[data-early-adopter-notice]');
  if (!dialog) return;
  const key = `tuneconsole.early-adopter-notice.${dialog.dataset.earlyAdopterNotice}`;
  let dismissed = false;
  try { dismissed = localStorage.getItem(key) === 'dismissed'; } catch (_) {}
  const acknowledge = () => {
    // Dismiss immediately. A failed acknowledgement cannot trap the listener
    // or turn an informational notice into a technical error surface.
    try { localStorage.setItem(key, 'dismissed'); } catch (_) {}
    dialog.close();
    dialog.remove();
    fetch('/early-adopter-notice/dismiss', {
      method: 'POST', signal: AbortSignal.timeout(3000),
    }).catch(error => console.debug('Notice acknowledgement deferred', error));
  };
  dialog.querySelector('[data-dismiss-early-adopter]').addEventListener('click', acknowledge);
  dialog.addEventListener('cancel', event => { event.preventDefault(); acknowledge(); });
  if (dismissed) acknowledge();
  else dialog.showModal();
})();
