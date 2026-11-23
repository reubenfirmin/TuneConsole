// Production audio inference. All model and runtime assets are packaged locally.
// Native tag scores are retained as predictions; they are never mapped to intensity.
// TF.js 3.18's bundled regenerator assigns this global in strict mode. Declare
// the binding so its bootstrap does not fall back to Function("..."). The vendor
// artifact remains byte-for-byte unchanged and is still checked against its hash.
var regeneratorRuntime;
let ready;
importScripts('runtime_loader.js');
async function initialize() {
  const started = performance.now();
  const loaded = await loadAudioRuntime();
  for (const name of ['tf.min.js', 'musicnn/model.json',
    'musicnn/group1-shard1of1.bin', 'musicnn/metadata.json']) {
    await loaded.verify(name);
  }
  importScripts('vendor/tf.min.js');
  if (loaded.bundle.meyda_version) {
    await loaded.verify('meyda.min.js');
    importScripts('vendor/meyda.min.js');
  }
  await tf.setBackend('cpu');
  await tf.ready();
  const {embeddingGraph} = await import('./embedding_graph.js');
  const graph = embeddingGraph(await fetch('vendor/musicnn/model.json').then(r => r.json()));
  const [model, metadata] = await Promise.all([
    tf.loadGraphModel({load: async () => ({...graph,
      weightSpecs: graph.weightsManifest[0].weights,
      weightData: await fetch('vendor/musicnn/group1-shard1of1.bin').then(r => r.arrayBuffer()),
    })}),
    fetch('vendor/musicnn/metadata.json').then(r => r.json()),
  ]);
  return {...loaded, model, metadata, startup_ms: performance.now() - started};
}

async function predict(samples, rate, model) {
  if (rate !== 16000) throw Error('Model input must be resampled to 16 kHz');
  const patchSize = 187, bands = 96, hop = 256, frameSize = 512;
  const frames = Math.floor((samples.length - frameSize) / hop) + 1;
  const patches = Math.floor(frames / patchSize);
  if (patches < 1) return {status: 'abstained_short_excerpt', scores: null};
  // Only full patches: no zero padding presented as real music coverage.
  const mel = new Float32Array(patches * patchSize * bands);
  for (let i = 0; i < patches * patchSize; i++) {
    const frame = new model.runtime.VectorFloat();
    let result;
    try {
      for (let j = 0; j < frameSize; j++) frame.push_back(samples[i * hop + j]);
      result = model.essentia.TensorflowInputMusiCNN(frame);
      if (result.bands.size() !== bands) throw Error('Unexpected mel feature shape');
      for (let j = 0; j < bands; j++) mel[i * bands + j] = result.bands.get(j);
    } finally { result?.bands.delete(); frame.delete(); }
  }
  let output, embeddings;
  try {
    [output, embeddings] = tf.tidy(() => {
      const input = tf.tensor3d(mel, [patches, patchSize, bands]);
      return model.model.execute({'model/Placeholder': input, 'model/Placeholder_1': tf.scalar(false, 'bool')},
        ['model/Sigmoid', 'model/dense/BiasAdd']);
    });
    const matrix = await output.array();
    if (matrix.length !== patches || matrix.some(row => row.length !== model.metadata.classes.length
        || row.some(value => !Number.isFinite(value)))) throw Error('Unexpected prediction shape');
    const embeddingMatrix = await embeddings.array();
    if (embeddingMatrix.length !== patches || embeddingMatrix.some(row => row.length !== 200
        || row.some(value => !Number.isFinite(value) || Math.abs(value) > 1e6)))
      throw Error('Unexpected embedding shape');
    const embedding = {version: 'musicnn-embedding-1', layer: 'model/dense/BiasAdd',
      graph_policy: 'restore-fused-preactivation-1', pooling: 'mean_full_patches', patches,
      values: Array.from({length: 200}, (_, i) =>
        embeddingMatrix.reduce((sum, row) => sum + row[i], 0) / patches)};
    return {status: 'native_tags_only', embedding,
      scores: Object.fromEntries(model.metadata.classes.map((name, index) =>
      [name, matrix.reduce((sum, row) => sum + row[index], 0) / patches])), patches,
      analysed_seconds: ((patches * patchSize - 1) * hop + frameSize) / rate,
      embedding_layer: 'model/dense/BiasAdd', embedding_policy: 'restore-fused-preactivation-1',
      feature_policy: 'musicnn-512-256-full-patches-1', reliability: null};
  } finally { output?.dispose(); embeddings?.dispose(); mel.fill(0); }
}

self.onmessage = async ({data}) => {
  const started = performance.now();
  try {
    const {describe, VERSION} = await import('./features.js');
    const descriptors = describe(data.samples, data.rate);
    const model = await (ready ||= initialize());
    const inference = performance.now();
    let audio_attributes = null;
    if (data.measurement_left) {
      const {measureAudio} = await import('./audio_attributes.js');
      audio_attributes = measureAudio(data, model, self.Meyda);
    }
    const prediction = await predict(data.samples, data.rate, model);
    self.postMessage({id: data.id, descriptors, derivation_version: VERSION,
      audio_attributes,
      wall_ms: performance.now() - started, sample_count: data.samples.length,
      prediction: {...prediction, kind: 'prediction', model: 'msd-musicnn-1',
        model_version: model.metadata.version, inference_ms: performance.now() - inference,
        startup_ms: model.startup_ms, backend: tf.getBackend(),
        runtime_version: model.runtime_version,
        tensor_bytes: tf.memory().numBytes, tensor_memory_is_process_peak: false,
        bundle: model.bundle}});
  } catch (error) {
    self.postMessage({id: data.id, error: String(error.message || error), error_stack: error.stack});
  } finally {
    data.samples.fill(0);
    data.measurement_left?.fill(0);
    data.measurement_right?.fill(0);
  }
};
