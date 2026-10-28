// Only browser-visible options are exposure. Hidden Home panels do not contribute.
(() => {
  const observed = new WeakMap();
  function send(el, kind) {
    const card = el.closest('[data-choice-opportunity]');
    if (!card || !card.dataset.choiceOpportunity || !card.dataset.choiceOption) return Promise.resolve({ok: false});
    return fetch('/home/listening-response', {method: 'POST', keepalive: true,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        opportunity_id: card.dataset.choiceOpportunity, option_id: card.dataset.choiceOption, kind
      })});
  }
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.target.isConnected) { observer.unobserve(entry.target); continue; }
      if (entry.isIntersecting && entry.intersectionRatio >= .25 && !document.hidden) {
        send(entry.target, 'visible').then(response => {
          if (response.ok) observer.unobserve(entry.target);
        }).catch(() => {});
      }
    }
  }, {threshold: .25});
  function scan() {
    document.querySelectorAll('[data-choice-opportunity]').forEach(card => {
      const identity = card.dataset.choiceOpportunity + ':' + card.dataset.choiceOption;
      if (observed.get(card) !== identity) { observed.set(card, identity); observer.observe(card); }
    });
  }
  document.addEventListener('DOMContentLoaded', scan);
  document.addEventListener('htmx:afterSwap', scan);
  document.addEventListener('listening-options', scan);
  document.addEventListener('htmx:configRequest', event => {
    // One ID per deliberate request: a transport retry retains it, another click gets another ID.
    if (event.detail.path === '/album/play') event.detail.parameters.action_id = crypto.randomUUID();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) document.querySelectorAll('[data-choice-opportunity]').forEach(card => {
      observer.unobserve(card); observer.observe(card);
    });
  });
  document.addEventListener('click', event => {
    const action = event.target.closest('[data-choice-action]');
    if (action) send(action, action.dataset.choiceAction).catch(() => {});
  });
  document.addEventListener('keydown', event => {
    if (['Enter', ' '].includes(event.key) && event.target.matches('[data-choice-action][role="button"]')) {
      send(event.target, event.target.dataset.choiceAction).catch(() => {});
    }
  });
})();
