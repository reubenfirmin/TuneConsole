// Numeric controls only. No media element, capture, AudioContext or audio output.
importScripts('runtime_loader.js');
self.onmessage = async () => {
  try {
    const model = await self.loadResearchRuntime();
    await model.verify('meyda.min.js');
    importScripts('vendor/meyda.min.js');
    const {measureAudio} = await import('./audio_attributes.js');
    const measure = (left, right = null) => measureAudio({measurement_rate: 44100,
      measurement_left: left, measurement_right: right}, model, self.Meyda).values;
    const tone = Float32Array.from({length: 441000}, (_, i) => .1 * Math.sin(2 * Math.PI * 1000 * i / 44100));
    const half = Float32Array.from(tone, value => value / 2);
    const beat = Float32Array.from({length: 441000}, (_, i) => {
      const phase = i % 22050; // 120 beats/minute; broadband, decaying percussion.
      return phase < 3000 ? .4 * Math.exp(-phase / 600) * Math.sin(i * i * .013) : 0;
    });
    self.postMessage({tone: measure(tone), half: measure(half), stereo: measure(tone, tone),
      silence: measure(new Float32Array(441000)), beat: measure(beat)});
  } catch (error) { self.postMessage({error: error.message}); }
};
