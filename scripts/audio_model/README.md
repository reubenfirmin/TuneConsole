# Production audio models

The extension's inference source lives in `extension/audio_model/`. The C++ runtime
bindings and asset build tools live here. Production packaging does not read from
`experiments/`.

Model weights, third-party JavaScript, and the compiled runtime are build inputs in
the ignored `extension/audio_model/vendor/` directory. They are required in every
production ZIP, together with their licenses and integrity manifests.

Fetch the versioned models and libraries:

```sh
python3 scripts/audio_model/fetch_model.py
```

Build the CSP-compatible runtime with clean Essentia and emsdk checkouts and Eigen
3.3.7 headers. The build tool checks their pinned revisions and hashes, uses emsdk
3.1.64, and installs nothing on the system:

```sh
python3.12 scripts/audio_model/build_runtime.py \
  --source /path/to/essentia \
  --emsdk /path/to/emsdk \
  --eigen /path/to/eigen-3.3.7
```

Verify all inputs or build the Web Store distribution:

```sh
python3 scripts/audio_model/bundle.py
scripts/build-extension.sh
```

The build stages the five production worker modules, all six model graphs and their
weights and metadata, TensorFlow.js, Meyda, and the CSP-compatible Essentia runtime.
Missing files, incompatible manifests, or mismatched SHA-256 hashes fail the build
before it replaces an existing ZIP. Only manifested runtime artifacts are packaged;
research corpora, probes, and local training output are excluded.
