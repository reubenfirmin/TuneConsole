// Runs inside an extension-owned frame embedded in the YTM tab. PCM goes only
// across this private MessagePort and into its worker, never through runtime ports.
(() => {
  let worker, channel, timer;
  function stop() { clearTimeout(timer); worker?.terminate(); channel?.close(); }
  window.addEventListener('pagehide', stop);
  window.addEventListener('message', event => {
    if (channel || event.source !== parent || event.origin !== 'https://music.youtube.com'
        || event.data?.type !== 'tc-audio-preview-init' || typeof event.data.id !== 'string'
        || event.data.id.length > 80 || event.ports.length !== 1) return;
    const id = event.data.id;
    channel = event.ports[0];
    try {
      worker = new Worker('audio_model/model_worker.js');
      worker.onmessage = ({data}) => {
        if (data.id !== id) return;
        channel.postMessage(data);
        stop();
      };
      worker.onerror = () => { channel.postMessage({id, error: 'The tab-local model failed. Prepare the optional model and reload the extension.'}); stop(); };
      let started = false;
      channel.onmessage = ({data}) => {
        if (started || data?.id !== id || data.rate !== 16000
            || !(data.samples instanceof Float32Array) || data.samples.length !== 160000
            || data.measurement_rate !== 44100 || !(data.measurement_left instanceof Float32Array)
            || data.measurement_left.length !== 441000
            || (data.measurement_right !== null && (!(data.measurement_right instanceof Float32Array)
              || data.measurement_right.length !== 441000))) return;
        started = true;
        worker.postMessage(data, [data.samples.buffer, data.measurement_left.buffer,
          ...(data.measurement_right ? [data.measurement_right.buffer] : [])]);
      };
      timer = setTimeout(() => { channel.postMessage({id, error: 'Tab-local analysis timed out.'}); stop(); }, 30000);
      channel.postMessage({id, type: 'ready'});
    } catch (error) { channel.postMessage({id, error: error.message}); stop(); }
  });
})();
