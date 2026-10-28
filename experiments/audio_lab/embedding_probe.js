// Explicit synthetic numerical control, copied only by the browser test fixture.
var regeneratorRuntime;
importScripts('vendor/tf.min.js');
self.onmessage = async () => {
  let original, augmented;
  try {
    await tf.setBackend('cpu');
    const {embeddingGraph} = await import('./embedding_graph.js');
    const graph = await fetch('vendor/musicnn/model.json').then(r => r.json());
    original = await tf.loadGraphModel('vendor/musicnn/model.json');
    augmented = await tf.loadGraphModel({load: async () => ({...embeddingGraph(graph),
      weightSpecs: graph.weightsManifest[0].weights,
      weightData: await fetch('vendor/musicnn/group1-shard1of1.bin').then(r => r.arrayBuffer()),
    })});
    const result = tf.tidy(() => {
      const input = tf.tensor3d(Float32Array.from({length: 187 * 96}, (_, i) => 2 + Math.sin(i * .17)), [1, 187, 96]);
      const args = {'model/Placeholder': input, 'model/Placeholder_1': tf.scalar(false, 'bool')};
      const [before, relu] = original.execute(args, ['model/Sigmoid', 'model/dense/Relu']);
      const [after, embedding] = augmented.execute(args, ['model/Sigmoid', 'model/dense/BiasAdd']);
      return {tag_error: before.sub(after).abs().max().dataSync()[0],
        relu_error: embedding.relu().sub(relu).abs().max().dataSync()[0],
        embedding_shape: embedding.shape, embedding_min: embedding.min().dataSync()[0]};
    });
    self.postMessage(result);
  } catch (error) { self.postMessage({error: error.message}); }
  finally { original?.dispose(); augmented?.dispose(); }
};
