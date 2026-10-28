// Classic-worker helper shared by inference and the explicit parity probe.
self.loadResearchRuntime = async function () {
  const bundle = await fetch('vendor/bundle.json').then(r => r.json());
  const legacy = new URL(self.location.href).searchParams.get('runtime') === 'legacy';
  // Chrome rejects fetch for a missing packaged file instead of returning 404.
  // Only absence of the optional manifest selects the original negative control;
  // malformed manifests and mismatched rebuilt artifacts still fail closed.
  const response = legacy ? null : await fetch('vendor/runtime.json').catch(() => null);
  if (response && !response.ok && response.status !== 404) throw Error('Cannot read rebuilt runtime manifest');
  const rebuilt = response?.ok ? await response.json() : null;
  if (rebuilt && (!['essentia-csp-musicnn-1', 'essentia-csp-audio-2'].includes(rebuilt.runtime)
      || !rebuilt.flags.includes('-sDYNAMIC_EXECUTION=0'))) throw Error('Unexpected rebuilt runtime');
  const runtimeName = rebuilt ? 'essentia-csp.js' : 'essentia-wasm.js';
  const selected = rebuilt ? rebuilt.files : bundle.files;
  const verify = async (name, files = bundle.files) => {
    const expected = files.find(file => file.path === name);
    const response = await fetch('vendor/' + name);
    if (!response.ok) throw Error('Missing research artifact: ' + name);
    const bytes = await response.arrayBuffer();
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    const digest = Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, '0')).join('');
    if (!expected || bytes.byteLength !== expected.bytes || digest !== expected.sha256) throw Error('Research artifact changed: ' + name);
  };
  await verify(runtimeName, selected);
  let runtime;
  if (rebuilt) {
    importScripts('vendor/essentia-csp.js');
    runtime = await self.EssentiaCSP();
  } else {
    await new Promise((resolve, reject) => {
      self.Module = {onRuntimeInitialized: resolve, onAbort: reject};
      importScripts('vendor/essentia-wasm.js');
    });
    runtime = self.Module;
  }
  return {runtime, verify, bundle: {...bundle, runtime_build: rebuilt,
    installed_total_bytes: bundle.total_bytes + (rebuilt?.files.reduce((sum, file) => sum + file.bytes, 0) || 0)},
    runtime_version: rebuilt?.runtime || 'essentia-js-819c00e',
    essentia: new runtime.EssentiaJS(false)};
};
