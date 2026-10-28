// Developer-only labeling UI. Audio collection has its own entry point.
const version = new URL(import.meta.url).search;
const TARGETS = ['relax', 'concentrate', 'cruise', 'energize', 'uplift', 'release'];

export async function createCards({taxonomy, ...context}) {
  const [{renderInspector}, {AudioVibeCards}] = await Promise.all([
    import('./audio-vibe-inspector.js' + version), import('./audio-vibe-cards.js' + version),
  ]);
  const compatible = value => value?.taxonomy_version === taxonomy && Array.isArray(value.samples)
    && Object.keys(value.choices || {}).length === TARGETS.length
    && TARGETS.every(key => typeof value.choices[key]?.attribute_label === 'string' && value.choices[key].attribute_label);
  return new AudioVibeCards({...context, taxonomy, renderInspector, compatible});
}
