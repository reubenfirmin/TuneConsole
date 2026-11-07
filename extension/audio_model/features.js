export const VERSION = 'signal-descriptors-1';

// Source-native signal measurements. These are deliberately not perceived intensity.
export function describe(samples, rate) {
  let squares = 0, peak = 0, crossings = 0, clipped = 0;
  if (!samples.length || !Number.isFinite(rate) || rate <= 0) throw Error('Empty or invalid audio');
  for (let i = 0; i < samples.length; i++) {
    const x = samples[i];
    if (!Number.isFinite(x)) throw Error('Nonfinite audio');
    squares += x * x;
    peak = Math.max(peak, Math.abs(x));
    clipped += Math.abs(x) >= .999;
    crossings += i > 0 && (x >= 0) !== (samples[i - 1] >= 0);
  }
  const rms = Math.sqrt(squares / samples.length);
  return {rms, peak, rms_dbfs: rms > 0 ? 20 * Math.log10(rms) : null,
    crest_factor: rms > 0 ? peak / rms : null,
    zero_crossings_per_second: crossings * rate / samples.length,
    clipped_fraction: clipped / samples.length};
}
