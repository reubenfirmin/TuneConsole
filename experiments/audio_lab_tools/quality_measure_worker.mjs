// Research-only acoustic measurements from the exact frozen production modules.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import readline from 'node:readline';
import {pathToFileURL, fileURLToPath} from 'node:url';
import {webcrypto} from 'node:crypto';
import {createRequire} from 'node:module';

const root = path.resolve(process.argv[2]);
globalThis.self = globalThis;
globalThis.location = pathToFileURL(path.join(root, 'model_worker.js'));
globalThis.crypto ??= webcrypto;
globalThis.require = createRequire(import.meta.url);
globalThis.importScripts = (...names) => {
  for (const name of names) {
    const filename = path.resolve(root, name);
    if (!filename.startsWith(root + path.sep)) throw Error('Asset outside frozen runtime');
    vm.runInThisContext(fs.readFileSync(filename, 'utf8'), {filename});
  }
};
globalThis.fetch = async name => {
  const url = new URL(String(name), location);
  if (url.protocol !== 'file:') throw Error('Network disabled in acoustic worker');
  const filename = fileURLToPath(url);
  if (!filename.startsWith(root + path.sep)) throw Error('Asset outside frozen runtime');
  try {
    const data = fs.readFileSync(filename);
    return {ok:true,status:200,json:async()=>JSON.parse(data.toString()),
      arrayBuffer:async()=>data.buffer.slice(data.byteOffset,data.byteOffset+data.byteLength)};
  } catch (error) {
    if (error.code === 'ENOENT') return {ok:false,status:404};
    throw error;
  }
};
importScripts('runtime_loader.js');
const runtime = await loadResearchRuntime();
await runtime.verify('meyda.min.js');
importScripts('vendor/meyda.min.js');
const {measureAudio} = await import(pathToFileURL(path.join(root,'audio_attributes.js')));
console.log('READY ' + JSON.stringify({runtime:runtime.runtime_version,meyda:runtime.bundle.meyda_version}));
for await (const line of readline.createInterface({input:process.stdin})) {
  const request = JSON.parse(line), bytes = fs.readFileSync(request.pcm);
  const pcm = new Float32Array(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength));
  if (![1,2].includes(request.channels) || pcm.length !== 441000*request.channels) throw Error('Invalid measurement PCM');
  const left = new Float32Array(441000), right = request.channels===2 ? new Float32Array(441000):null;
  for(let i=0;i<441000;i++){left[i]=pcm[i*request.channels];if(right)right[i]=pcm[i*2+1];}
  const result = measureAudio({measurement_rate:44100,measurement_left:left,measurement_right:right},runtime,self.Meyda);
  console.log('RESULT ' + JSON.stringify({id:request.id,attributes:result}));
}
