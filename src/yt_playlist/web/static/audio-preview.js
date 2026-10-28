import {AudioPreviewSession} from './audio-preview-session.js';

let mountedRoot = null, cleanup = () => {};
function mountAudioPreview() {
  const root = document.querySelector('[data-audio-preview]');
  if (root === mountedRoot) return;
  cleanup();
  mountedRoot = root;
  if (!root) return;
  const find = name => root.querySelector(`[data-preview-${name}]`);
  const start = find('start'), stop = find('stop'), status = find('status');
  let snapshot = null, receivedAt = 0, result = null, busy = false;

  function scoreRow(label, score, range = [0, 1]) {
    const row = document.createElement('li'), name = document.createElement('span');
    name.textContent = label;
    const meter = document.createElement('meter');
    meter.min = range[0]; meter.max = range[1]; meter.value = score;
    meter.setAttribute('aria-label', label + ' model score');
    const number = document.createElement('output'); number.textContent = score.toFixed(2);
    row.append(name, meter, number); return row;
  }

  function playing() {
    const data = window.playbackStatus?.audioSnapshot || snapshot;
    const now = data?.connected && data.now_playing;
    return now ? {...now, at: receivedAt - Math.max(0, now.snapshot_age || 0) * 1000} : null;
  }

  function update() {
    const current = playing();
    find('track').textContent = current ? [current.title, current.artist].filter(Boolean).join(' · ') : 'Waiting for music…';
    if (start) start.disabled = busy || !session.port || !session.validPlaying();
  }

  let session;
  session = new AudioPreviewSession({playback: playing,
    extensionIds: root.dataset.modelAvailable === 'true' ? root.dataset.extensionIds.split(',') : [],
    changed(state) {
      if (!start || !session) return;
      busy = ['requesting', 'capturing', 'analysing'].includes(state.phase);
      if (state.phase === 'capturing' && state.seconds === 0) {
        result = null;
        find('result').hidden = true;
        find('export').disabled = true;
      }
      stop.disabled = !busy;
      status.textContent = state.message;
      find('progress').hidden = !busy;
      find('progress').value = state.seconds || 0;
      update();
    },
    result(value) {
      result = value;
      find('result').hidden = false;
      find('export').disabled = false;
      find('summary').textContent = `Ten-second excerpt while TuneConsole showed ${value.observed_playback.title || 'this song'}. `
        + `The model used ${value.prediction.analysed_seconds.toFixed(2)} seconds of complete patches. `
        + `Analysis inside the YouTube Music tab took ${(value.wall_ms / 1000).toFixed(1)} seconds. No audio was sent to this page.`
        + (value.observed_playback.metadata_verified ? '' : ' The playback title and artist are unverified.');
      const labels = {relaxed: 'Relaxed', aggressive: 'Aggressive', happy: 'Happy', sad: 'Sad',
        arousal: 'Activation', valence: 'Positive emotional tone'};
      const traits = value.prediction.moods.traits;
      find('moods').replaceChildren(...Object.entries(labels).filter(([key]) => traits[key])
        .map(([key, label]) => scoreRow(label, traits[key].score, traits[key].range)));
      const scores = Object.entries(value.prediction.scores || {}).sort((a, b) => b[1] - a[1]);
      find('tags').replaceChildren(...scores.slice(0, 10).map(([tag, score]) => scoreRow(tag, score)));
      find('native').textContent = JSON.stringify(Object.fromEntries(scores), null, 2);
    },
  });

  function apply(data) {
    snapshot = data;
    receivedAt = Date.now();
    update();
    if (session.current) session.check(session.current);
    session.tryAutomatic();
  }
  const playbackChanged = event => apply(window.playbackStatus?.audioSnapshot || event.detail);
  window.addEventListener('bridge-status', playbackChanged);
  if (window.playbackStatus?.audioSnapshot) apply(window.playbackStatus.audioSnapshot);
  else update();
  if (root.dataset.modelAvailable === 'true') {
    start.addEventListener('click', () => {
      result = null;
      find('result').hidden = true;
      find('export').disabled = true;
      session.start();
    });
    stop.addEventListener('click', () => session.stop());
    find('export').addEventListener('click', () => {
      if (!result) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2) + '\n'], {type: 'application/json'}));
      const link = document.createElement('a');
      link.href = url; link.download = 'tuneconsole-audio-preview.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }
  cleanup = () => {
    window.removeEventListener('bridge-status', playbackChanged);
    session.destroy();
    mountedRoot = null;
    cleanup = () => {};
  };
}
window.addEventListener('app-page-changed', mountAudioPreview);
window.addEventListener('app-page-leaving', () => cleanup());
window.addEventListener('pagehide', () => cleanup());
mountAudioPreview();
