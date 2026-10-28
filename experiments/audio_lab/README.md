# Audio lab (#406)

The [external dataset plan for the vibe model](DATASET_PLAN.md)
describes how each emotion dataset will be used, prioritizing supplied audio
before datasets that need YouTube recording matching.

The user-facing POC is **Tools > Audio analysis preview** in the normal
TuneConsole app (`/audio-preview`). Play music through On Air or Mix Tapes, then
open the preview. The normal extension automatically captures a ten-second excerpt,
stops its capture tracks, and runs the model in an extension-owned frame and worker
inside the YouTube Music tab. The app receives mood and native tag scores only. No capture
click, sharing picker, extra extension or local audio file is required. Each
playing song gets one attempt while the preview is open; **Analyse again** retries
the current song and **Stop analysis** cancels it. One job runs across preview pages.
Reload the normal extension and restart the local app after updating these files.

The normal app also has an opt-in `YT_PLAYLIST_AUDIO_VIBES=1` experiment, configurable
in the checkout's ignored `.env.local`. It samples ten seconds at 30 seconds,
halfway and two-thirds through normal playback, with no seeking. A bottom-right
banner combines the native tags and mood models into experimental predictions and
collects the same vibe choices as Mix Tapes. Samples and judgments persist as
metadata separately from production track profiles and recommendation learning.

For the separate popup-free access investigation, `audio_probe.js` is retained for
explicit developer injection into the bridge's isolated YouTube Music world and
for browser tests. Normal startup no longer installs this access-only diagnostic.
Its **TuneConsole audio access check** banner tries three seconds and reports DRM,
cross-origin restrictions, nonzero audio, or an inconclusive silent signal.
**Copy report** exports diagnostics only. This access-only diagnostic does not run
the model; the app preview above uses a separate ten-second capture. Live user reports confirmed
both click-started and automatic YTM capture. The automatic path captured and
decoded 3.008 seconds of stereo PCM at 48 kHz without a live AudioContext,
capture click or sharing picker while playback continued. Page activation was
present in that live run; separate browser tests cover zero page activation.
See `extension/README.md` for the procedure and limitations.

Audio is processed in memory inside the YouTube Music tab and discarded after analysis.
Stop, disconnected capture, page closure, observed track changes, pauses, clock jumps and
stale/disconnected playback cancel pending work. The captured stream does not
establish recording identity: exports explicitly mark the selected source
unverified and never enter the catalog observation adapter. Displayed TuneConsole
metadata is context, not verified recording association. This POC leaves On Air
and Mix Tapes selection unchanged.

This directory also retains the separate, unpacked Chrome 116+ extension as a
controlled reference/compatibility test harness. It is not the normal app UI.
Python research tools live in the sibling `experiments/audio_lab_tools/` directory
so their bytecode caches cannot make this extension unloadable in Chrome.

## Run the reference experiment

1. Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**,
   and select this directory (`experiments/audio_lab`).
2. Pin **TuneConsole audio lab (experiment)**. Click its toolbar icon to open the
   reference tab.
3. Choose a local audio file (up to 64 MiB), enter its title/artist/album, document
   its source permission or licence, and confirm permission. Alternatively choose
   **Use generated test tone** for a synthetic capture-path check.
4. While the reference tab is active, click the lab's toolbar icon again to
   enable capture, then start playback normally. Chrome grants access on this
   explicit action. Ctrl+Shift+Y invokes the same action if available.
5. Let the recording play naturally. Complete windows appear as measurements.
   Stop with the toolbar icon or **Stop analysis**, then export the JSON report.

Stop releases the offscreen document and media tracks without pausing the player.
Navigation, closing the reference tab and restarting the worker/extension stop
capture. Permission and capture do not automatically resume. A missing reference
heartbeat also stops capture. Browser background throttling can cause abstention.

The extension has no host permissions, remote media inputs, injected content
scripts, production bridge connection or server upload. It accepts only its own
reference page. Local file identity is a SHA-256 of file bytes, not a verified
MusicBrainz recording or YouTube upload association. Source permission is a
declaration; technical access is not proof of permission on another source.

## Sampling and evidence

`reference-windows-1` attempts up to three nonoverlapping windows separated by at
least two seconds: near the beginning, midpoint and end. Each is at most ten
seconds. Short recordings may supply a shorter window or fewer windows. No
automatic seeking, hidden playback or background downloader is used. Starting
capture after a window begins misses that window; the lab never substitutes a
later excerpt while claiming the earlier interval.

The audio route is restored directly to an AudioContext destination before the
analysis branch starts. A worklet downmixes bounded 100 ms blocks. One sampler
holds at most ten seconds, and one analysis worker processes a completed window
at a time. Identity/epoch changes, pauses, buffering, seeks, non-unit playback
rates, stale state and discontinuities discard incomplete windows. A fresh state
after the last frame must validate a window before it is analysed. Raw capture
samples are discarded after processing; exported JSON contains descriptors only.
Once that tail check validates a complete window, its analysis can finish after
playback pauses or ends. Stopping analysis, revoking permission or changing the
reference still cancels pending inference and suppresses late results.

The default descriptors are RMS, peak, RMS dBFS, crest factor, zero crossings per second
and clipped fraction. Digital silence has no finite dBFS or crest factor and is
reported as null for those values. These are **signal measurements**, not shared
intensity, vocal-presence predictions, confidence or vibe labels. The export
deliberately leaves intensity and recording association unknown; the default
signal-only export also leaves model version unknown.
No recommendation policy consumes this experiment's results.

## Optional CPU model experiment

Fetch the isolated research assets explicitly, then reload the unpacked extension:

```sh
python experiments/audio_lab_tools/fetch_model.py
```

This downloads approximately 7 MiB of code, model weights, metadata and licence
files into the ignored `vendor/` directory. It downloads no recordings. The
bundle manifest records exact URLs, versions, sizes and SHA-256 digests. The
installer checks existing artifacts against that manifest; the worker verifies
the artifacts used for inference before loading them. The bundle is for local
research only and is excluded from source control and the production package.

The **Try the optional local MusiCNN research model** checkbox uses a compatible
runtime when `vendor/runtime.json` is present. Build it explicitly using the recipe
below, then reload the extension. Native tag predictions are unvalidated research
output. Leave the checkbox unchecked for signal measurements alone.

Without the rebuild, this remains a negative compatibility experiment: the
original pinned Essentia runtime generates JavaScript functions dynamically and
MV3 rejects it. The error releases capture without stopping playback.

The explicit benchmark also serves the local research bundle on a loopback web
origin, outside MV3. That ordinary web baseline permits the runtime's dynamic
function generation and processes three generated ten-second sine-wave arrays.
It measures Essentia/WASM feature extraction and MSD-MusiCNN inference on
TensorFlow.js's CPU backend. This separates browser CPU feasibility from extension
compatibility; it cannot establish that the model works inside the extension.

The worker retains the 50 native tag scores as unvalidated predictions. It does
not connect the separate voice/instrumental head or turn tag scores into vocal
presence or shared intensity. Only complete 187-frame patches are inferred; the
actual analysed duration is reported separately. The rebuilt per-frame extractor
is checked against the original pinned runtime. Full-window alignment against the
model's training/reference pipeline remains a gate before interpreting quality.

Successful worker responses include model startup wall time, feature/inference
wall time, backend and TensorFlow tensor allocation. Tensor allocation is not
peak process memory, wall time is not measured CPU time, and this synthetic
experiment does not measure battery impact. Stop terminates the worker even if
model initialization or inference is pending.
An analysis job has a 30 second experiment timeout; this is a cancellation guard,
not an accepted production CPU budget.

```sh
uv run pytest -m browser -s tests/bench_audio_lab_model.py --test-lock-wait=60
```

### Measured synthetic baseline, 2026-10-25

Chromium 148.0.7778.96 (Playwright revision 1223), Linux x86_64, AMD Ryzen 7
5700G. TensorFlow.js 3.18.0, CPU backend, Essentia.js commit
`819c00e8f034dc709e3d330ed8ac9ed37dfdb767`, MSD-MusiCNN version 1.

| Check | Result |
| --- | --- |
| Natural-playback capture | Three intervals: 2–11.99465, 37.5–47.5, 63–73 seconds |
| Actual capture coverage | 29.99465 / 75 seconds (39.9929%) |
| Optional research bundle | 7,199,352 bytes, including unused voice head and licence files |
| Local web model startup | 323.8 ms |
| Feature extraction plus inference, three trials | 7,701.1, 8,213.6, 12,572.0 ms |
| Model coverage per ten-second synthetic input | 8.992 seconds; three full patches |
| Tensor allocation after each trial | 3,148,708 bytes; not process peak memory |
| Model in the installed MV3 extension | Blocked by CSP; capture released and playback continued |

All three captured windows matched the expected generated-tone RMS within 0.01.
The ordinary web baseline returned 50 finite native tag scores per trial without
external requests. These are three synthetic trials on one desktop, not a
representative hardware budget or a quality evaluation. Inference sometimes took
longer than its ten-second input; it must remain separate from the audible route.
True CPU time, process peak memory and battery impact remain unmeasured.

The original prebuilt runtime is **no-go for MV3 integration**. This historical
baseline is retained as a negative control alongside the rebuilt runtime below.
Audio-derived vibe coverage remains unproven; no shared trait is adopted.

### Rebuild without dynamic code generation

The research binding in `essentia_csp.cpp` exposes only MusiCNN preprocessing and
its dependencies. It uses unmodified, pinned Essentia C++ source and KISS FFT,
compiled with Emscripten 3.1.64 and `DYNAMIC_EXECUTION=0`. The extension keeps its
existing CSP. TensorFlow.js 3.18's regenerator bootstrap is given its expected
global binding before import, avoiding its `Function(...)` fallback without
changing the downloaded library. The CPU backend does not need that fallback.

Prepare a local build directory with Python 3.12, git, curl, tar and pkg-config
available. These commands install no system packages and require no root access:

```sh
mkdir -p /tmp/tuneconsole-audio-build
git clone --depth 1 --branch 3.1.64 https://github.com/emscripten-core/emsdk.git /tmp/tuneconsole-audio-build/emsdk
/tmp/tuneconsole-audio-build/emsdk/emsdk install 3.1.64
/tmp/tuneconsole-audio-build/emsdk/emsdk activate 3.1.64
git clone --filter=blob:none --no-checkout https://github.com/MTG/essentia.git /tmp/tuneconsole-audio-build/essentia
git -C /tmp/tuneconsole-audio-build/essentia checkout --detach d65623a92ab1d26803c5d8656a3ad7c5b2929031
curl --fail --location --output /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz https://gitlab.com/libeigen/eigen/-/archive/3.3.7/eigen-3.3.7.tar.gz
tar -xzf /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz -C /tmp/tuneconsole-audio-build eigen-3.3.7/Eigen eigen-3.3.7/unsupported/Eigen
python3.12 experiments/audio_lab_tools/build_runtime.py --source /tmp/tuneconsole-audio-build/essentia --emsdk /tmp/tuneconsole-audio-build/emsdk --eigen /tmp/tuneconsole-audio-build/eigen-3.3.7
```

Allow roughly 2 GiB for the local compiler and build workspace. The Eigen archive
SHA-256 is `d56fbad95abf993f8af608484729e3d87ef611dd85b3380a8bad1d5cbc373a57`;
the builder also verifies the extracted header tree, source revisions and active
compiler version. It shares the repository's machine test lock and runs two
compiler processes at most. There is no upstream-source patch or system install.

The ignored `vendor/runtime.json` records source/toolchain revisions, flags,
binding/header hashes and the resulting artifact digest. The worker checks that
digest before import. Original artifacts remain available for comparisons via
the explicit benchmark; `fetch_model.py` preserves the separately built runtime.
Exports include runtime identity and provenance, so findings from different
builds retain distinct identities.

### Audio attributes runtime

The current optional build is `essentia-csp-audio-2`. It adds
`RhythmExtractor2013` (multifeature, native confidence) and `LoudnessEBUR128` to the
existing MusiCNN preprocessing binding. Both receive ten seconds at 44.1 kHz;
loudness preserves stereo and represents mono with one active channel. The
model still receives a separate 16 kHz mono rendering. Offline rendering creates
no live audio output. Refresh both the fetched assets and the runtime before
running `prepare_extension.py`.

The pinned Meyda 5.6.3 bundle and its MIT license are included in the verified
local research assets. Its scalar spectral/perceptual measurements use Hann
windows of 2048 samples and a 1024-sample hop. Nonfinite or unavailable scalars
become JSON null. No MFCC, chroma, spectra or raw audio leave the YouTube tab.
One versioned 200-value frozen embedding is retained per excerpt for classifier
training. The worker erases its input buffers after analysis.

The flagged normal-app popup exposes 27 audio measurements plus all six mood
outputs and 50 music tags per excerpt. Values and provenance are saved in
immutable JSON files; `sample_attributes` links each file to its recording,
capture time and playback occurrence. Current human judgments in
`audio_session_labels` refer to those samples by `run_id` and frozen sample IDs.
Schema 61 externalizes existing samples without changing their attributes.
The mapper waits for three saved samples from one playback occurrence; model
scores and their sample links live in `measurements` and `measurement_samples`.
See `extension/README.md` for database location, sampling and label behavior.

### Fit local listening corrections

Popup answers are internal development annotations for training and evaluating
the audio-to-vibe model. They are not product preferences or per-track vibe
overrides. Recommendation ranking and artist/genre propagation must consume
audio measurements and model-derived scores, without using raw annotation rows
as ranking or propagation evidence.

The normal app can optionally apply a small learned correction to its existing
experimental vibe scores. Fit it explicitly from the saved listening judgments:

```sh
uv run python -m experiments.audio_lab_tools.train_vibes \
  --db ~/.local/share/yt-playlist/state.db \
  --output /tmp/vibe-model.json --report /tmp/vibe-evaluation.json
```

The trainer opens SQLite in read-only mode and never initializes the app or runs
migrations. Fitting uses one numerical thread and runs independently of the
repository test/build lock. Samples and developer feedback are joined by exact
track video ID. Their playback runs, collection order and feedback-time sample
IDs do not need to match. Each track uses its latest current-vocabulary answer
and the latest compatible captured excerpt at each sampling point, including
captures from replays where no new answer was entered. One or two captured
points are enough to train; missing excerpts are never fabricated. The report
records the selected sample IDs, collection times, source playback IDs and point
coverage independently of the annotation's provenance. Historical answers and
prediction snapshots remain unchanged. Tracks with no available measurements
and tracks whose latest answer is empty/cleared are excluded. Each track counts
once, so another play cannot leak into a validation training fold.
Earlier vocabularies are not translated into current targets.

Developer choices are acceptable alternatives for one displayed prediction:
selecting Cruise and Concentrate means either prediction earns full credit.
The primary metric is single-prediction accuracy, not multilabel F1 or exact-set
agreement. A matching runner-up cannot rescue an incorrect leading prediction.
Unselected acceptable alternatives never count as missed predictions.

The candidates are the unchanged heuristic, an intercept correction, and
regularized joint corrections with fixed penalties of 1 and 10. The default
objective is negative log likelihood of the summed probability of acceptable
choices; it does not require every choice to receive a high score. Libra loss
is an explicit comparison candidate. Both implement disjunctive supervision
as discussed by [Zombori et al., JMLR 2024](https://jmlr.org/papers/volume25/23-0868/23-0868.pdf).
The learned
corrections use ordered-arc fits, mood measurements and observed rhythm, relative
level and spectral attributes, plus the exact video's selected catalog metadata:
provider tempo, energy, danceability, mood, acoustic/instrumental, loudness,
dynamics, key and scale, and specific subgenre/year. Native recording-level party and
aggression observations are included where available. Titles, artists and song
IDs are not features, and no predicted artist/genre vibes or human annotations
are used as input features.

Genre features use exact known subgenres, such as Detroit Techno or Deep House.
Broad families/umbrella genres (including Electronic, Techno, House, Rock and
Ambient) supply no genre feature or presence flag. A subgenre never activates
its parents or siblings. Each training fold builds its vocabulary from its own
songs; held-out/unseen subgenres contribute no genre predictor. The final JSON
exports that vocabulary so inference uses exactly the same columns.

Catalog and captured-audio features receive identical standardization and the
same regularization penalty. There is no hand-set metadata discount. Missing
catalog values use the training fold's observed mean, with explicit presence
flags; measured zero remains distinct from missing. The catalog reader and
feature encoder are shared by offline training and runtime inference. The
versioned JSON stores the feature order, normalization and learned coefficients;
previous audio-only artifacts remain readable during comparison.

Centering/scaling and missing-value imputation are calculated within each
training fold. Joint models fit all six outputs with L2 regularization and
export a softmax score link, preserving the ordering of even large logits.
The legacy independent-binary comparison still requires three positive and
three unselected training songs per label. Existing artifacts keep their
original sigmoid score formula. Scores remain uncalibrated.

The report compares audio-only and combined inputs on the same songs, folds and
penalties, and records catalog coverage and catalog-input hashes. It compares
every candidate using leave-one-song-out predictions, including correct and
incorrect leading suggestions, acceptable alternatives, and individual song
predictions. Candidate selection uses these same development folds; its
winning score is not an independent acceptance result. Collect fresh feedback
after fitting to establish whether it improves future listening. If the original
heuristic wins, the exported artifact contains no learned corrections.

For a nested comparison of alternative-label training and legacy baselines:

```sh
uv run python -m experiments.audio_lab_tools.robust_vibes \
  --db ~/.local/share/yt-playlist/state.db \
  --output /tmp/robust-vibes.json --report /tmp/robust-evaluation.json
```

This uses the same track-based loader, one numerical thread, and no shared
test/build lock. It saves a frozen feature dataset alongside the report. Pass
`--dataset /tmp/robust-evaluation.dataset.json` instead of `--db` to repeat the
comparison with exactly those inputs while playback and sample collection continue.

The bounded candidate set compares the heuristic, the legacy ridge and
hard-example models, and joint acceptable-set NLL and Libra losses with
penalties of 1 and 10. Each joint loss is tested with and without the existing
heuristic as a fixed logit offset. Difficulty weighting in the legacy baseline
uses only training-track answers and unchanged heuristic errors, normalized to
preserve total weight. Legacy adversarial fitting remains available through an
explicit `objective='legacy_binary'` API option; it is not the default objective
for acceptable alternatives.

Each outer evaluation excludes one whole track, including its answer, from
both fitting and method selection. Up to five inner song folds select the method
using only the other tracks; the default uses fewer folds for a four- or
five-track dataset. Normalization, subgenre vocabulary, class support
and difficulty weights are fitted inside those inner folds. The `nested` score
evaluates that selection procedure. The individual `development_results` remain
diagnostics and cannot establish the winning method's independent accuracy.
This separation addresses [selection bias from tuning on evaluation results](https://jmlr.csail.mit.edu/papers/v11/cawley10a.html).

All sampled tracks with current feedback, including large contradictions, are
included in the final fit. They also train the other tracks' evaluation fits.
`training_fit` reports whether known answers were corrected;
`known_wide_divergence` reports the correction of saved severe cases separately
from their held-out results. A correction on a known training track and success
on an unseen track answer different questions. With this small development
dataset, even nested validation still requires future-track listening evidence.
The command writes a candidate JSON and never activates it.

The report also separates very wide disagreements from close calls. A wide
divergence has an unacceptable leading prediction and a gap of at least 0.5
between its score and the highest score for an acceptable choice. A leading
prediction matching any choice is agreement. This is an explicit review
threshold on uncalibrated scores, not a probability or a semantic distance
between label names. Sigmoid and softmax gaps are on different scales, so
cross-model gap counts do not establish comparable confidence. Every
candidate's held-out results include disagreement counts, gaps and song scores.

`divergence_review` retains the actual feedback-time prediction, its model
identity and the recomputed heuristic separately. It flags learned reversals
where a saved model produced a wide divergence while the heuristic's leading
label matched the human answer. Missing feedback-time predictions remain unknown,
including answers given before sampling finished. For the songs with saved wide
divergences, it reports held-out performance for every audio-only and combined
candidate on the same cohort. These are descriptive development diagnostics;
they do not change training weights, model inputs or candidate selection and
are not an independent acceptance set. In particular, a good heuristic result
cannot erase a failure introduced by the model the developer actually saw.

The trainer reads a database snapshot in read-only mode and fits with one CPU
thread. It runs independently of the shared test/build lock.

The unattended semi-supervised experiment uses additional captured songs
without requiring more feedback:

```sh
uv run python -m experiments.audio_lab_tools.semisupervised_vibes \
  --db ~/.local/share/yt-playlist/state.db \
  --output /tmp/semi-vibes.json --report /tmp/semi-evaluation.json
```

It freezes existing human answers and up to 512 compatible unlabeled captured
tracks, including legacy and partial captures. A bounded nearest-neighbor graph
of attributes and catalog metadata penalizes sharply different classifier logits
on similar songs. It generates no synthetic answers and never treats saved
predictions as labels. More ordinary playback captures can supply additional
unlabeled measurements; repeated epochs over unchanged data add no evidence.

The run compares the existing supervised methods, graph regularization on
training songs alone, and graph regularization with the additional unlabeled
captures. Every inner and outer validation song is excluded entirely from
normalization, vocabulary, fitting and graph construction. Selection occurs in
inner song folds. The graph weights 0.1 and 1 are a bounded experiment, not an
exhaustive search. Similar measured features need not imply similar listening
vibes, so an improvement must be measured rather than assumed.

The report also exports predictions for the unlabeled songs for inspection;
these predictions are never fed back into fitting. It includes a replayable
`.dataset.json` containing both pools. Use
`--dataset /tmp/semi-evaluation.dataset.json` instead of `--db` to replay it.
Exported coefficients use the existing runtime; the graph is needed only during
offline fitting. The command does not change the database, activate a model or
schedule recurring work. Predictions remain distinct from human judgments.

The first frozen comparison (2026-10-28 UTC) used 56 labeled tracks and 63
unlabeled captured tracks. With nested leave-one-song-out evaluation and five
inner song folds, supervised selection scored 37/56 (66.1%), the training-only
graph 32/56 (57.1%), and the graph with additional unlabeled songs 36/56
(64.3%). The extra captures improved on the training-only graph but did not beat
supervised training. The final selected candidate was `legacy_hard_10`; it was
not activated. Its predictions for all 63 unlabeled tracks and the comparison
are saved under `local/vibe-semisupervised-20261027T225742Z-*`. The saved model
replayed all 119 tracks from 319 raw captures with identical scores. These
results use a different frozen set from the earlier embedding comparison and
do not establish performance on future songs.

The gold/silver experiment instead trains on the human answers as gold and the
existing model's top-one predictions as silver:

```sh
uv run python -m experiments.audio_lab_tools.gold_silver_vibes \
  --dataset local/vibe-semisupervised-20261027T225742Z-input.json \
  --predictions local/vibe-semisupervised-20261027T225742Z-current-predictions.json \
  --teacher-report local/vibe-subgenre-evaluation.json \
  --output /tmp/gold-silver-model.json --report /tmp/gold-silver-report.json
```

Paths above are relative to this directory. The teacher artifact, its training
manifest and all supplied predictions must match before freezing both tiers.
Replay the report's `.dataset.json` with `--snapshot`. Gold targets weigh 1;
silver weights 0.1, 0.25, 0.5 and 1 are compared with ordinary supervised
candidates, two losses and L2 penalties 1 and 10. All 63 supplied predictions
are retained without confidence filtering or iterative relabeling. Normalization,
subgenre vocabulary and minimum binary class support use training gold only.

The fixed teacher was trained on 30 of the 56 gold songs. Those 30 remain
training-only in every split. Nested outer leave-one-out evaluation and inner
five-fold selection evaluate only the remaining 26 human answers, keeping their
features and targets out of fitting. This avoids indirectly leaking a held-out
answer through the fixed teacher's silver labels. Accuracy is never calculated
against silver labels. The command exports gold-only and gold/silver models as
well as the overall selected model, without activation or database writes.

The frozen-feature experiment retains MusiCNN embeddings (#507).
`model_worker.js` computes the 200-value `model/dense/BiasAdd` output for each
patch, uses it for mood heads, and captures its mean across full patches.
[Essentia documents these pretrained embedding outputs](https://essentia.upf.edu/tutorial_tensorflow_auto-tagging_classification_embeddings.html).
A small regularized classifier on frozen music embeddings is supported by
[Ma and Lerch's few-shot music-tagging study](https://arxiv.org/abs/2409.07730);
this is a research direction, not a measured gain for this dataset.

The 2026-10-27 comparison on the frozen 40-track dataset
(`b25f5e9bf37a0117682e677a940032033305b566a888216f0fca1b222542d2ef`)
found 21/40 correct for the heuristic, 24/40 for legacy ridge, 26/40 for the
legacy hard-example model, at best 23/40 for joint acceptable-set NLL, and at
best 19/40 for Libra. These are individual-candidate development results.
Nested evaluation of the selection procedure gave 22/40 (55%); the selected
final candidate was `legacy_hard_10`, fitting all 40 known answers correctly.
Training accuracy does not establish generalization. The report and replayable
dataset are in `local/vibe-alternatives-evaluation.json` and
`local/vibe-alternatives-evaluation.dataset.json`; the candidate remains inactive.
These accuracy figures replace the earlier multilabel F1 interpretation, and
the alternative-label losses have not demonstrated a gain on these inputs.

CNN inference stays in the browser. Each excerpt's `embedding` records
`musicnn-embedding-1`, the pre-activation layer, restored-graph policy, full-patch
mean pooling, patch count and 200 finite signed values. Model identity and the
graph/weight hashes remain in the sample's provenance. The extension permits
only that bounded payload; the API also checks patch/time coverage. Immutable
sample files preserve each excerpt independently, including its position.
No raw audio upload or backend CNN execution is needed.

Run the nested comparison after recapturing at least four labeled tracks:

```sh
uv run python -m experiments.audio_lab_tools.robust_vibes \
  --db ~/.local/share/yt-playlist/state.db --embeddings \
  --output /tmp/embedding-vibes.json --report /tmp/embedding-evaluation.json
```

This selects the latest compatible embedding-capable excerpt at each point,
joins existing track feedback independently of playback, and compares existing
attributes, embeddings alone, and both together on exactly those tracks and
excerpts. Partial captures are included without filling missing points. The
first embedding representation is the equal mean of available excerpt vectors;
the original vectors and temporal positions remain available for future work.
The embedding models train only a small L2-regularized classifier with penalties
of 1 or 10; the CNN is frozen. Normalization is fitted within training folds.
Embedding model JSON uses a separate feature version and provenance digest;
missing/incompatible embeddings fall back to the heuristic. Existing artifacts
and older captures retain their existing behavior. The report records missing
embedding skips and the number of embedding-capable tracks. Historical captures
require resampling; fresh-track feedback is needed to establish an actual gain.

The command writes plain JSON and does not activate its output. To explicitly
use a reviewed artifact, start the app with
`YT_PLAYLIST_AUDIO_VIBE_MODEL=/absolute/path/vibe-model.json` in its environment.
The same setting in the checkout's ignored `.env.local` works with the ordinary
app command. An explicit environment value overrides that file; an empty value
disables the learned correction. Keep personal artifacts in the ignored
`experiments/audio_lab/local/` directory when they should survive `/tmp` cleanup.
The model is limited to the listening experiment; it does not change Home mix
ranking or catalog profiles. Its digest is frozen alongside the prediction when
feedback is saved. Missing, invalid, incompatible or unavailable-measurement
inputs fall back to the existing rules. Changed heuristic versions, upstream
models or measurement runtimes require a compatible fit. Model scores remain
uncalibrated, and sample order and the underlying excerpt evidence are preserved.

`test_native_attributes_match_known_levels_and_rhythm_without_audio_output`
checks known mono/stereo loudness, a 6 dB gain change, gain-independent normalized
RMS, spectral centroid, silence and a 120 BPM percussion pattern. These controls
use numeric arrays in a worker and never connect an audio output.

### Historical measured rebuilt runtime, 2026-10-25

Same desktop/browser/model/backend as the historical baseline above. The new
runtime is `essentia-csp-musicnn-1`; its pinned C++ source differs from the original
prebuilt artifact's unknown C++ revision, so numerical comparisons are explicit.

| Check | Installed MV3 result |
| --- | --- |
| Rebuilt embedded runtime | 711,277 bytes; original runtime 2,562,667 bytes |
| Installed research assets, retaining original/unused controls | 7,910,629 bytes |
| Startup on the first captured window | 166.9 ms |
| Browser resampling per window | 11.9, 9.0, 9.8 ms |
| Feature extraction plus inference per window | 9,043.4, 9,929.5, 14,156.4 ms |
| Tensor allocation after each window | 3,148,708 bytes; not process peak memory |
| Actual capture coverage | 29.984875 / 75 seconds (39.9798%) |
| Actual model coverage | 26.976 / 75 seconds (35.968%); 8.992 seconds per window |
| Native predictions | All 50 finite scores per window; reliability unknown |
| External requests during capture/inference | None |

The final window completes inference after the player ends. Its captured samples
had already passed the fresh tail-state check; no seek or hidden playback is
needed. Eight synthetic frames (silence, four tones, impulse, seeded noise and a
mixed tone), each with 96 bands, matched the original extractor with maximum
absolute difference 0.00001215935. All 50 native classifier scores were exactly
equal for a separate ten-second generated-tone input in the rebuilt MV3 worker
and original ordinary-web worker. These limited comparisons do not validate the
whole training/reference preprocessing pipeline or listening quality.

The CSP blocker is resolved for this local research runtime on the tested
Chromium build. CPU cost remains substantial and variable: the slowest window
took longer than its captured audio. Representative hardware/resource budgets,
permission/licence resolution and human-judged benefit remain required before
production adoption.

The explicit model checks also cover rejection of a mismatched runtime digest,
the original runtime's CSP failure and cancellation during inference on Stop,
permission revocation and reference-metadata changes. The updated focused checks
are recorded in #423: **46 regression checks passed in 21.87 seconds** and
**6 model checks passed in 80.16 seconds**, plus the separately run feature-parity
and three-window capture checks described above. Repeating the build produced
the same artifact digest.
The full suite remains review work under `AGENTS.md`.

To prepare a blind listening packet from permitted reference exports:

```sh
uv run python -m experiments.audio_lab_tools.evaluation --output /tmp/audio-evaluation export.json
```

`listener.json` follows #405's fit/pair judgment format and starts unlabelled.
`evidence.json` holds source identities, predictions and artist/album holdouts
separately. `report.json` describes measured costs and the outstanding promotion
gates. Synthetic references are excluded from listening packets. The adapter
also constructs the existing `MetadataObservation` objects with item scope and
native units; it never opens or updates the live catalog. Recording association
remains unknown and explicit catalog corrections are not changed.

Capture intervals use the reference player's clock and are explicitly marked as
estimates, with a 300 ms drift tolerance. They are not sample-accurate recording
timestamps. Transport staleness and clock discontinuities cause abstention;
upstream preprocessing and capture-clock alignment still require reference checks.

## Verification

```sh
uv run pytest -m '' tests/test_audio_preview.py tests/browser/test_audio_preview_browser.py --test-lock-wait=60
uv run pytest tests/test_audio_lab.py --test-lock-wait=60
uv run pytest tests/test_audio_lab_evaluation.py --test-lock-wait=60
uv run pytest -m browser tests/browser/test_audio_lab_browser.py --test-lock-wait=60
```

All capture-test browsers use Chromium's `--disable-audio-output`. On Linux that
selects `FakeAudioManager` before PulseAudio/ALSA initialization: synthetic audio
still renders and can be captured, but no hardware audio output opens. This also
applies to the explicit model benchmarks through their shared browser fixture.
The ordinary `--mute-audio` flag cannot replace it because that zeros tab capture.
Manual playback in the real app/browser remains audible.

The normal-app browser checks install the actual session bridge, capture a native
media element on a restrictive YTM fixture, run the rebuilt local model and check
the score export. They separately cover zero page activation and normal background
music playback, without a capture click or autoplay-policy override. Cancellation
checks cover Stop, track/source changes, closed pages, pending decode, inference
and extension reload. Broker checks reject other origins, ports, paths and frames,
and enforce one job across preview pages. The older lab/evaluation/installed-extension
checks also passed with fake hardware output (18 tests, 18.03 seconds).
Final automatic model/extension coverage: 65 passed, 1 skipped across the unit,
zero-activation model and remaining regression runs recorded in #423. The skip
requires Chrome 155's re-enable event. Desktop and 390 px screenshots were inspected.
Automatic capture and the normal app/model path were confirmed on live YTM by
the user, including a ten-second result for Take Me Baby (8.992 model seconds,
9.2 seconds analysis). Those general tag scores establish execution, not useful
vibe detection. #423 stays In Progress while the tab-local mood follow-up is
implemented and validated.
The unit checks cover permission denial, explicit invocation, changed identity,
stale state, discontinuities, short windows, bounded buffers and cleanup. The
installed-extension checks use a generated sine wave. They establish lifecycle
and signal plumbing only. They cannot establish listening benefit, music-only
capture on third-party sites, speaker audibility, battery impact or model cost.

## Remaining investigation

### Tab-local mood preview

The normal app preview now receives metadata only. Capture and offline decoding
stay in YTM's isolated content world. An extension-owned frame embedded in that
same tab owns the dedicated model worker; PCM moves through a private MessagePort
and is transferred into the worker, then discarded. Runtime ports and the app
receive scores, diagnostics and bounded frozen embeddings, never audio or
spectrograms. The app no
longer serves model scripts or starts a model worker. Cancellation removes the
frame and worker as well as releasing tracks and zeroing decoded PCM.

Four dedicated mood heads (relaxed, aggressive, happy, sad) and the DEAM version 1
arousal/valence head consume `model/dense/BiasAdd` embeddings. The TF.js conversion
fused that pre-activation output with ReLU; `embedding_graph.js` restores a parallel
BiasAdd-only readout with the same inputs, weights and bias in memory. Downloaded
artifacts and the native tag path stay unchanged. A separate numerical control
compares original/augmented tag scores and verifies that ReLU of the restored
embedding matches the original fused output, including negative pre-activations.
Each full patch is
predicted separately; exports preserve per-patch values, native units, model
versions and artifact hashes, then show their mean. Binary mood outputs use 0–1;
DEAM outputs retain the native 1–9 scale without clipping exported values. These
are uncalibrated predictions, not confidence percentages or production vibe traits.
No genre-to-chill mapping is introduced.

Prepare the normal unpacked extension explicitly:

```sh
python3 experiments/audio_lab_tools/fetch_model.py
python3 experiments/audio_lab_tools/prepare_extension.py
```

The existing CSP-compatible runtime must already be built using the recipe above.
The preparer checks artifact hashes and copies worker source plus the needed
runtime/models/licences into ignored `extension/audio_model/`. It does not fetch
anything. Re-run it after worker changes, then reload the normal bridge extension
and reload the normal app preview. The optional model directory is excluded from
Web Store packaging. No CDN or remote JavaScript execution is introduced.

Listen across styles and compare relaxed versus intense tracks within each style.
Repeat on separated parts of changing songs before trusting a whole-track trait.
The preview still samples one window per song; **Analyse again** can sample a later
section. Representative blind reference judgments, held-out benefit and resource
cost remain #424's gate. Only tracks actually played can gain audio evidence.

The read-only preview does not require verified title/artist metadata to start:
live playback can be healthy while artist-format matching leaves that flag false.
Exports preserve the observed metadata verification state and unverified recording
association; production metadata trust rules and recommendations remain unchanged.
The matching unit and installed-browser checks pass (2 checks, 27.04 seconds).

Live tab-local mood output was confirmed for the observed title **Boleite**:
8.992 model seconds, 13.4 seconds of analysis, relaxed 0.36, aggressive 0.51,
happy/sad both rounded to 0.00, arousal 5.13 and valence 5.19. The user's judgment
is fairly neutral, not aggressive. An averaged binary score near 0.5 is weak or
mixed classification evidence, not a half-intensity aggression measurement.
Keep that distinction explicit and retain this judgment as evaluation evidence;
do not turn it into a verified recording association or adopted vibe label.

Keep the rebuilt runtime confined to the optional local research installation
until reference preprocessing validation and listening evaluation establish its limits.
The original prebuilt runtime remains rejected for MV3 integration.

The listening gate is a 20–30 recording reference corpus with documented permissions
and human judgments, artist/album holdouts, a metadata baseline and one candidate
model. Include quiet intros, changing arrangements, gentle electronic tracks,
intense instrumentals and weak/conflicting metadata. Use #405's evaluation format
and keep model outputs separate from the blind judgment packet. Do not generate
listening labels from model predictions or synthetic signal tests.

Essentia.js/MusiCNN remains a candidate, not a selected production dependency.
Measure feature extraction **and** embedding/classifier inference on browser CPU,
including startup, bundle size, memory and warm inference. The voice/instrumental
head takes 200-dimensional MSD-MusiCNN embeddings, not raw audio; its small head
size is not the cost of the entire pipeline. Neither that classifier nor tempo
or RMS is automatically a perceived-intensity estimator.

Licensing needs an artifact-specific review before distribution. At investigation
time, the [model index](https://essentia.upf.edu/models.html) describes CC BY-NC-SA
4.0, while the [licensing page](https://essentia.upf.edu/licensing_information.html)
describes CC BY-NC-ND 4.0. Essentia.js describes AGPL-3.0. Record the exact model
artefacts, metadata, licence files, hashes and versions used; resolve the conflicting
licence statements before adopting or distributing a candidate. Optional local
research downloads require the separate installer above.

Production audio analysis remains a no-go until permitted source access,
human-judged incremental benefit and acceptable measured resource costs have been
demonstrated. This does not reject the approach; it preserves the metadata baseline
while the investigation gathers evidence for both On Air and Mix Tapes.

References:

- [Chrome tab capture and output routing](https://developer.chrome.com/docs/extensions/reference/api/tabCapture)
- [Chrome offscreen lifecycle](https://developer.chrome.com/docs/extensions/reference/api/offscreen)
- [Web Audio cross-origin restrictions](https://www.w3.org/TR/webaudio-1.0/#MediaElementAudioSourceNode-security)
- [Essentia.js](https://mtg.github.io/essentia.js/)
- [Voice/instrumental model metadata](https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-msd-musicnn-1.json)
