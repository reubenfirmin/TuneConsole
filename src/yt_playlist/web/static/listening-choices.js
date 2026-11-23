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
      const card = entry.target;
      const identity = observed.get(card);
      if (!card.isConnected || !identity) { observer.unobserve(card); continue; }
      const {width, height} = entry.intersectionRect;
      if (entry.isIntersecting && width > 0 && height > 0 && entry.intersectionRatio >= .25 && !document.hidden) {
        send(card, 'visible').then(response => {
          if (response.ok && observed.get(card) === identity) observer.unobserve(card);
        }).catch(() => {});
      }
    }
  }, {threshold: .25});
  function observe(card) {
    const {choiceOpportunity, choiceOption} = card.dataset;
    const identity = choiceOpportunity && choiceOption && choiceOpportunity + ':' + choiceOption;
    if (observed.get(card) === identity) return;
    observer.unobserve(card);
    if (identity) {
      observed.set(card, identity);
      observer.observe(card);
    } else {
      observed.delete(card);
    }
  }
  function scan() {
    document.querySelectorAll('[data-choice-opportunity]').forEach(observe);
  }
  document.addEventListener('DOMContentLoaded', scan);
  document.addEventListener('htmx:afterSwap', scan);
  document.addEventListener('listening-options', scan);
  document.addEventListener('home:mix-settled', event => observe(event.target));
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
