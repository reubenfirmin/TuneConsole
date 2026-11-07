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

Build the CSP-compatible runtime with pinned Essentia/KISS FFT, Emscripten
3.1.64 and Eigen 3.3.7. Prepare clean toolchain/source directories:

```sh
mkdir -p /tmp/tuneconsole-audio-build
git clone --depth 1 --branch 3.1.64 https://github.com/emscripten-core/emsdk.git /tmp/tuneconsole-audio-build/emsdk
/tmp/tuneconsole-audio-build/emsdk/emsdk install 3.1.64
/tmp/tuneconsole-audio-build/emsdk/emsdk activate 3.1.64
git clone --filter=blob:none --no-checkout https://github.com/MTG/essentia.git /tmp/tuneconsole-audio-build/essentia
git -C /tmp/tuneconsole-audio-build/essentia checkout --detach d65623a92ab1d26803c5d8656a3ad7c5b2929031
curl --fail --location --output /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz https://gitlab.com/libeigen/eigen/-/archive/3.3.7/eigen-3.3.7.tar.gz
tar -xzf /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz -C /tmp/tuneconsole-audio-build eigen-3.3.7/Eigen eigen-3.3.7/unsupported/Eigen
```

The builder verifies source/toolchain/header identity and bounds compiler work
with its own lock. Build products stay in these directories or ignored vendor/;
nothing is installed on the system:

```sh
python3.12 scripts/audio_model/build_runtime.py \
  --source /tmp/tuneconsole-audio-build/essentia \
  --emsdk /tmp/tuneconsole-audio-build/emsdk \
  --eigen /tmp/tuneconsole-audio-build/eigen-3.3.7
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
