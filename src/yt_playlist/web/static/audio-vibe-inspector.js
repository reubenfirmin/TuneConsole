// Only saved metadata reaches the app. Raw audio and spectra stay in YouTube Music.
const FIELDS = {
  integrated_lufs: ['Integrated loudness', 'LUFS'],
  momentary_lufs_mean: ['Mean momentary loudness', 'LUFS'],
  short_term_lufs_mean: ['Mean short-term loudness', 'LUFS'],
  excerpt_loudness_range_lu: ['Excerpt loudness range', 'LU'],
  tempo_bpm: ['Estimated tempo', 'BPM'],
  tempo_confidence: ['Tempo confidence', 'out of 5.32'],
  beat_interval_cv: ['Beat interval variation', ''], beat_count: ['Detected beats', ''],
  rms: ['RMS amplitude', ''], peak: ['Peak amplitude', ''],
  rms_dbfs: ['RMS level', 'dBFS'], peak_dbfs: ['Peak level', 'dBFS'],
  crest_factor: ['Crest factor (peak / RMS)', ''],
  peak_normalized_rms_dbfs: ['Peak-normalized RMS', 'dBFS'],
  clipped_fraction: ['Samples at or above 0.999 amplitude', 'fraction'],
  zero_crossings_per_second: ['Zero crossings', 'per second'],
  spectral_centroid_hz: ['Spectral centroid (brightness)', 'Hz'],
  spectral_flatness: ['Spectral flatness (noisiness)', ''],
  spectral_rolloff_hz: ['Spectral rolloff', 'Hz'],
  spectral_spread_hz: ['Spectral spread', 'Hz'], spectral_slope: ['Spectral slope', ''],
  spectral_skewness: ['Spectral skewness', ''], spectral_kurtosis: ['Spectral kurtosis', ''],
  spectral_crest: ['Spectral crest', ''], perceptual_spread: ['Perceptual spread', ''],
  perceptual_sharpness: ['Perceptual sharpness', ''], bark_loudness: ['Bark loudness', 'arbitrary units'],
};

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function table(title, rows) {
  const section = element('details');
  section.append(element('summary', title));
  const body = element('tbody'), result = element('table');
  for (const [name, value] of rows) {
    const row = element('tr');
    const heading = element('th', name); heading.scope = 'row';
    row.append(heading, element('td', value)); body.append(row);
  }
  result.append(body); section.append(result);
  return section;
}

function number(value) {
  if (value === null || value === undefined) return 'Unavailable';
  return Math.abs(value) < .001 && value !== 0 ? value.toExponential(3)
    : value.toLocaleString(undefined, {maximumFractionDigits: 4});
}

function measurementSummary(samples) {
  const measured = samples.filter(sample => sample.audio_attributes?.values);
  const available = measured.reduce((total, sample) => total + Object.keys(FIELDS)
    .filter(key => sample.audio_attributes.values[key] != null).length, 0);
  const summary = [`${measured.length} of ${samples.length} excerpts include audio measurements; ${available} of ${samples.length * Object.keys(FIELDS).length} scalar values available.`];
  const values = key => measured.map(sample => sample.audio_attributes.values[key]).filter(Number.isFinite);
  const range = numbers => `${number(Math.min(...numbers))} to ${number(Math.max(...numbers))}`;
  const tempos = values('tempo_bpm'), loudness = values('integrated_lufs');
  if (tempos.length) {
    summary.push(`Tempo across excerpts: ${range(tempos)} BPM${Math.max(...tempos) / Math.min(...tempos) > 1.15 ? ' (estimates vary)' : ''}.`);
    const confidence = values('tempo_confidence');
    if (confidence.length) summary.push(`Native tempo confidence: ${range(confidence)}; this is not a probability.`);
  }
  if (loudness.length) summary.push(`Excerpt loudness: ${range(loudness)} LUFS.`);
  const replacements = samples.filter(sample => sample.capture_reason === 'catch_up').length;
  if (replacements) summary.push(`${replacements} replacement excerpt${replacements === 1 ? '' : 's'} captured after the planned position.`);
  return summary.join(' ');
}

export function renderInspector(container, samples) {
  const key = samples.map(sample => sample.id).join(',');
  if (container.dataset.samples === key) return;
  container.dataset.samples = key;
  if (!container.children.length) container.append(element('p',
    'Measurements describe each 10-second excerpt, not the whole song. Loudness is measured from captured playback. Peak normalization removes gain, but does not measure perceived loudness. Tempo may be half or double the beat. Classifier scores are not measured emotions.'));
  let quality = container.querySelector('[data-vibe-measurement-summary]');
  if (!quality) {
    quality = element('p'); quality.dataset.vibeMeasurementSummary = '';
    container.insertBefore(quality, container.children[1] || null);
  }
  quality.textContent = measurementSummary(samples);
  const existing = new Map([...container.querySelectorAll('[data-vibe-sample]')]
    .map(section => [section.dataset.vibeSample, section]));
  const ids = new Set(samples.map(sample => sample.id));
  for (const [id, section] of existing) if (!ids.has(id)) section.remove();
  for (const sample of samples) {
    if (existing.has(sample.id)) continue;
    const section = element('section');
    section.dataset.vibeSample = sample.id;
    section.append(element('h3', `Sample at ${sample.position_seconds.toFixed(1)}s` +
      (sample.capture_reason === 'catch_up' ? ` (replacement for ${sample.target_seconds.toFixed(1)}s)` : '')));
    const attributes = sample.audio_attributes;
    if (attributes) {
      section.append(element('p', `${attributes.sample_rate.toLocaleString()} Hz · ${attributes.channels} channels · ${attributes.seconds}s · ${attributes.runtime} · Meyda ${attributes.meyda_version}`));
      const measurements = table(`Audio attributes (${Object.keys(FIELDS).length})`,
        Object.entries(FIELDS).map(([key, [label, unit]]) =>
          [label, number(attributes.values[key]) + (attributes.values[key] == null || !unit ? '' : ` ${unit}`)]));
      measurements.open = true;
      section.append(measurements);
    } else section.append(element('p', 'Audio attributes unavailable: this excerpt used the earlier extension build.'));
    section.append(table('Mood model outputs (6)', Object.entries(sample.traits).map(([name, value]) =>
      [name, number(value.score) + (['arousal', 'valence'].includes(name) ? ' (DEAM scale)' : '')])),
    table('Music tags (50)', Object.entries(sample.tags).sort((a, b) => b[1] - a[1]).map(([key, value]) => [key, number(value)])));
    container.append(section);
  }
}
