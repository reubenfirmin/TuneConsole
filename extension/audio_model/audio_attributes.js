// Descriptive measurements only. All spectra and PCM stay inside this worker.
export const VERSION = 'audio-attributes-1';
const FEATURES = ['spectralCentroid', 'spectralFlatness', 'spectralRolloff', 'spectralSpread',
  'spectralSlope', 'spectralSkewness', 'spectralKurtosis', 'spectralCrest',
  'perceptualSpread', 'perceptualSharpness', 'loudness'];
const NAMES = {spectralCentroid: 'spectral_centroid_hz', spectralFlatness: 'spectral_flatness',
  spectralRolloff: 'spectral_rolloff_hz', spectralSpread: 'spectral_spread_hz',
  spectralSlope: 'spectral_slope', spectralSkewness: 'spectral_skewness',
  spectralKurtosis: 'spectral_kurtosis', spectralCrest: 'spectral_crest',
  perceptualSpread: 'perceptual_spread', perceptualSharpness: 'perceptual_sharpness',
  loudness: 'bark_loudness'};

export function measureAudio(data, model, meyda) {
  const left = data.measurement_left, right = data.measurement_right;
  if (data.measurement_rate !== 44100 || left.length !== 441000 || (right && right.length !== left.length))
    throw Error('Audio measurements require ten seconds of 44.1 kHz mono/stereo');
  if (typeof model.essentia.AudioAttributes !== 'function' || !meyda)
    throw Error('Rebuild the audio-2 runtime and prepare the extension with Meyda first.');
  const native = model.essentia.AudioAttributes(left, right || null);
  let squares = 0, peak = 0, clipped = 0, crossings = 0;
  const channels = right ? 2 : 1, mono = new Float32Array(left.length);
  for (let i = 0; i < left.length; i++) {
    for (const value of right ? [left[i], right[i]] : [left[i]]) {
      if (!Number.isFinite(value)) throw Error('Nonfinite measurement input');
      squares += value * value; peak = Math.max(peak, Math.abs(value));
      clipped += Math.abs(value) >= .999;
    }
    mono[i] = right ? (left[i] + right[i]) / 2 : left[i];
    if (i && (mono[i] >= 0) !== (mono[i - 1] >= 0)) crossings++;
  }
  const rms = Math.sqrt(squares / (left.length * channels));
  const values = {...native, rms, peak, rms_dbfs: rms ? 20 * Math.log10(rms) : null,
    peak_dbfs: peak ? 20 * Math.log10(peak) : null,
    crest_factor: rms ? peak / rms : null,
    peak_normalized_rms_dbfs: peak && rms ? 20 * Math.log10(rms / peak) : null,
    clipped_fraction: clipped / (left.length * channels), zero_crossings_per_second: crossings / 10};
  const size = 2048, hop = 1024, totals = {}, counts = {};
  meyda.sampleRate = 44100; meyda.bufferSize = size; meyda.windowingFunction = 'hanning';
  try {
    for (let offset = 0; offset + size <= mono.length; offset += hop) {
      const frame = mono.subarray(offset, offset + size);
      if (!frame.some(value => Math.abs(value) > .000001)) continue;
      const result = meyda.extract(FEATURES, frame);
      for (const [feature, name] of Object.entries(NAMES)) {
        let value = feature === 'loudness' ? result.loudness.total : result[feature];
        if (['spectralCentroid', 'spectralSpread'].includes(feature)) value *= 44100 / size;
        if (Number.isFinite(value)) {
          totals[name] = (totals[name] || 0) + value;
          counts[name] = (counts[name] || 0) + 1;
        }
      }
    }
  } finally { mono.fill(0); }
  for (const name of Object.values(NAMES)) values[name] = counts[name] ? totals[name] / counts[name] : null;
  return {version: VERSION, sample_rate: 44100, channels, seconds: 10,
    runtime: model.runtime_version, meyda_version: model.bundle.meyda_version,
    scope: 'excerpt', values};
}
