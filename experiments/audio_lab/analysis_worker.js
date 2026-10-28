import {describe, VERSION} from './features.js';

self.onmessage = ({data}) => {
  const started = performance.now();
  try {
    const descriptors = describe(data.samples, data.rate);
    self.postMessage({id: data.id, descriptors, derivation_version: VERSION,
      wall_ms: performance.now() - started, sample_count: data.samples.length});
  } catch (error) {
    self.postMessage({id: data.id, error: error.message});
  } finally {
    data.samples.fill(0);
  }
};
