// Explicit synthetic feature comparison; no classifier or music labels.
importScripts('runtime_loader.js');
let ready;
self.onmessage = async ({data}) => {
  try {
    const model = await (ready ||= loadResearchRuntime());
    const bands = data.frames.map(samples => {
      const frame = new model.runtime.VectorFloat();
      let result;
      try {
        samples.forEach(value => frame.push_back(value));
        result = model.essentia.TensorflowInputMusiCNN(frame);
        return Array.from({length: result.bands.size()}, (_, i) => result.bands.get(i));
      } finally { result?.bands.delete(); frame.delete(); }
    });
    self.postMessage({runtime_version: model.runtime_version, bands});
  } catch (error) {
    self.postMessage({error: String(error.message || error)});
  }
};
