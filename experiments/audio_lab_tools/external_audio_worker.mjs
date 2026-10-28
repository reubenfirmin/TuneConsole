// Offline research harness for the unmodified Radio feature/model worker.
// Only reads local research assets and explicitly supplied PCM files.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import readline from 'node:readline';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {webcrypto} from 'node:crypto';
import {createRequire} from 'node:module';

const lab = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../audio_lab');
globalThis.self = globalThis;
globalThis.location = pathToFileURL(path.join(lab, 'model_worker.js'));
globalThis.crypto ??= webcrypto;
globalThis.require = createRequire(import.meta.url);
globalThis.regeneratorRuntime = undefined;
globalThis.importScripts = (...names) => {
  for (const name of names) {
    const filename = path.resolve(lab, name);
    if (!filename.startsWith(lab + path.sep)) throw Error('Research script outside lab');
    if (name === 'vendor/tf.min.js' && process.env.EXTERNAL_TFJS_NATIVE === '1') {
      const native = require(path.join(lab, 'local/node/node_modules/@tensorflow/tfjs-node'));
      // Keep the production extractor unchanged. This research process substitutes
      // the equivalent native kernels for its explicitly requested CPU backend.
      globalThis.tf = {...native, setBackend: backend =>
        native.setBackend(backend === 'cpu' ? 'tensorflow' : backend)};
      continue;
    }
    vm.runInThisContext(fs.readFileSync(filename, 'utf8'), {filename});
  }
};
globalThis.fetch = async name => {
  const url = new URL(String(name), location);
  if (url.protocol !== 'file:') throw Error('Network disabled in offline research worker');
  const filename = fileURLToPath(url);
  if (!filename.startsWith(lab + path.sep)) throw Error('Research asset outside lab');
  try {
    const data = fs.readFileSync(filename);
    return {ok: true, status: 200, json: async () => JSON.parse(data.toString()),
      arrayBuffer: async () => data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)};
  } catch (error) {
    if (error.code === 'ENOENT') return {ok: false, status: 404};
    throw error;
  }
};
function floats(filename) {
  const bytes = fs.readFileSync(filename);
  if (bytes.byteLength % 4) throw Error('Incomplete PCM float');
  return new Float32Array(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
}
await import(pathToFileURL(path.join(lab, 'model_worker.js')));
const lines = readline.createInterface({input: process.stdin});
for await (const line of lines) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  try {
    const data = {id: request.id, samples: floats(request.model_pcm), rate: 16000};
    if (request.measurement_pcm) {
      const interleaved = floats(request.measurement_pcm);
      const channels = request.channels;
      data.measurement_rate = 44100;
      data.measurement_left = new Float32Array(interleaved.length / channels);
      data.measurement_right = channels === 2 ? new Float32Array(interleaved.length / channels) : null;
      for (let i = 0; i < data.measurement_left.length; i++) {
        data.measurement_left[i] = interleaved[i * channels];
        if (channels === 2) data.measurement_right[i] = interleaved[i * channels + 1];
      }
    }
    globalThis.postMessage = result => process.stdout.write('RESULT ' + JSON.stringify({
      ...result, research_backend: globalThis.tf?.getBackend(),
    }) + '\n');
    await self.onmessage({data});
  } catch (error) {
    process.stdout.write('RESULT ' + JSON.stringify({id: request.id, error: String(error.stack || error)}) + '\n');
  }
}
