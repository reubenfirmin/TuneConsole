// Home generated mixes. Alpine owns the board's `focus` state; htmx fetches regenerated fragments;
// this layer keeps the focused shell mounted and choreographs the in-place content transition.
function genPayload(id) { return document.querySelector('#' + CSS.escape(id) + ' > .gen-card-body'); }
function genName(id) { const el = genPayload(id); return el ? el.dataset.name : ''; }
function genRecipe(id) { const el = genPayload(id); return el ? (el.dataset.recipe || '') : ''; }

function genReveal(id) {
  requestAnimationFrame(() => {
    document.querySelectorAll('#' + CSS.escape(id) + ' .gen-focus img').forEach((img) => { img.loading = 'eager'; });
    const play = document.querySelector('#' + CSS.escape(id) + ' .gen-play');
    if (play) requestAnimationFrame(() => play.focus({ preventScroll: true }));
  });
}

function genTracks(id) {
  return JSON.stringify(Array.from(document.getElementById(id).querySelectorAll('.gen-row')).map((row) => ({
    video_id: row.dataset.vid, title: row.dataset.title, artist: row.dataset.artist,
    album: row.dataset.album, thumbnail: row.dataset.thumb
  })));
}

const mixRegenerations = new WeakMap();

function finishMixRegeneration(card, failed = false) {
  const state = mixRegenerations.get(card);
  if (!state) return;
  clearTimeout(state.timer);
  state.animations.forEach(animation => animation.cancel());
  delete state.button.dataset.regenerating;
  state.button.removeAttribute('aria-busy');
  card.classList.remove('is-regenerating');
  mixRegenerations.delete(card);
  if (failed) {
    const error = document.createElement('p');
    error.className = 'section-note gen-refresh-error';
    error.setAttribute('role', 'alert');
    error.textContent = 'Couldn’t regenerate this mix. Your mix is still here. Try again.';
    (card.querySelector('.gen-focus') || card.querySelector('.gen-card-body')).prepend(error);
  }
}

function regenerateMix(button) {
  if (!button || button.dataset.regenerating === '1') return;
  const card = button.closest('.gen-card');
  if (!card || mixRegenerations.has(card)) return;
  card.querySelector('.gen-refresh-error')?.remove();
  const state = {button, animations: [], transitioning: false, timer: null};
  mixRegenerations.set(card, state);
  button.dataset.regenerating = '1';
  button.setAttribute('aria-busy', 'true');
  card.classList.add('is-regenerating');
  Array.from(card.querySelectorAll('.gen-focus .gen-row')).forEach((row, index) => {
    state.animations.push(row.animate([
      { opacity: 1, filter: 'saturate(1)', transform: 'translateX(0)' },
      { opacity: .58, filter: 'saturate(.35)', transform: 'translateX(.22rem)' },
      { opacity: 1, filter: 'saturate(1)', transform: 'translateX(0)' }
    ], { duration: 1250, delay: index * 55, iterations: Infinity, easing: 'ease-in-out' }));
  });
  try { htmx.trigger(button, 'mix-regenerate'); }
  catch (_) { finishMixRegeneration(card, true); }
}

document.addEventListener('htmx:afterRequest', event => {
  const card = event.detail.elt?.closest('.gen-card');
  const state = card && mixRegenerations.get(card);
  if (!state || state.button !== event.detail.elt) return;
  // A successful response finishes after the custom transition. Every other completion,
  // including a network error or a response that never reached beforeSwap, releases the guard.
  if (!event.detail.successful || !state.transitioning) finishMixRegeneration(card, true);
});

document.addEventListener('htmx:beforeSwap', (event) => {
  const target = event.detail.target;
  if (!target || !target.matches('.gen-card-body')) return;
  const card = target.closest('.gen-card.is-regenerating');
  const state = card && mixRegenerations.get(card);
  if (!state) return;
  event.detail.shouldSwap = false;
  const response = new DOMParser().parseFromString(event.detail.serverResponse, 'text/html');
  const replacement = response.getElementById(target.id);
  if (event.detail.xhr.status < 200 || event.detail.xhr.status >= 300
      || !replacement?.matches('.gen-card-body') || !replacement.querySelector('.gen-head')) {
    finishMixRegeneration(card, true);
    return;
  }

  state.transitioning = true;
  state.animations.forEach(animation => animation.cancel());
  state.animations = [];
  const rows = Array.from(target.querySelectorAll('.gen-focus .gen-row'));
  const step = 38;
  const duration = 280;
  rows.forEach((row, index) => {
    state.animations.push(row.animate([
      { opacity: 1, transform: 'translateX(0) scale(1)', filter: 'blur(0)' },
      { opacity: 0, transform: 'translateX(1.25rem) scale(.97)', filter: 'blur(5px)' }
    ], { duration, delay: index * step, easing: 'cubic-bezier(.55,0,.8,.35)', fill: 'both' }));
  });

  const sequenceLength = duration + Math.max(0, rows.length - 1) * step;
  state.timer = window.setTimeout(() => {
    const incoming = Array.from(replacement.querySelectorAll('.gen-focus .gen-row'));
    incoming.forEach((row) => { row.style.opacity = '0'; });
    target.replaceChildren(...replacement.childNodes);
    target.dataset.name = replacement.dataset.name || '';
    target.dataset.genres = replacement.dataset.genres || '[]';
    if (replacement.dataset.recipe) target.dataset.recipe = replacement.dataset.recipe;
    else delete target.dataset.recipe;
    htmx.process(target);
    const enterStep = 44;
    incoming.forEach((row, index) => {
      const animation = row.animate([
        { opacity: 0, transform: 'translateX(-1rem) scale(.98)', filter: 'blur(5px)' },
        { opacity: 1, transform: 'translateX(0) scale(1)', filter: 'blur(0)' }
      ], { duration: 400, delay: index * enterStep,
           easing: 'cubic-bezier(.18,.78,.2,1)', fill: 'both' });
      animation.finished.then(() => row.style.removeProperty('opacity')).catch(() => {});
    });
    finishMixRegeneration(card);
    // This animated replacement bypasses HTMX's swap/settle events.
    document.dispatchEvent(new CustomEvent('home:mix-settled'));
  }, sequenceLength + 20);
});

document.addEventListener('htmx:beforeCleanupElement', event => {
  const element = event.detail.elt;
  if (element.matches?.('.gen-card')) finishMixRegeneration(element);
});

