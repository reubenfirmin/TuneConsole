# Audio lab

Research tools live in `../audio_lab_tools/`; this directory also contains the
unpacked browser reference harness. Run tools from the repository root with
`./experiments/run.py --help`. Research artifacts, source snapshots, predictions
and detailed run logs stay under ignored `local/`.

Follow the [experiment documentation policy](../README.md). Dataset logs record
what was acquired and learned; model logs explain selected recipes and rejected
alternatives. Ticket handoffs and progress diaries belong in the issue tracker.
The dated logs summarize existing evidence, not a fresh evaluation or acceptance.

## Model provenance

| Model | Selected recipe and history |
| --- | --- |
| [Shared classifier](models/shared.md) | Emotify native emotions plus local six-vibe calibration; classification/intensity baseline and rejected broad replacements |
| [Joint pooling](models/pooling.md) | Inactive artist/genre transfer candidates, categorical holdout lineage and failed ranking gates |
| [Cruise](models/cruise.md) | Equal PMEmo/DEAM VA experts, moderate-arousal/positive-valence mapping |
| [Focus](models/focus.md) | Direct CAL500 Reading/Studying ridge heads; failed count/chroma follow-ups |
| [Relax](models/relax.md) | Fixed Emotify/Soundtracks/ManyMusic/Memo consensus |
| [Release](models/release.md) | Exact protected Jamendo aggression ridge head; rejected power blend and inconclusive artist extension |
| [Uplift](models/uplift.md) | Fixed positivity consensus, accepted domain tradeoff; rejected arousal gate |
| [Energize](models/energize.md) | Fixed no-joy arousal/energy/power consensus, accepted domain losses |
| [Encoders](models/encoders.md) | MusiCNN extraction identity; MERT, chroma and mid-level-property comparisons |

All six specialists default active on compatible captures. Independent scores
rank within a vibe; shared classification and radio intensity keep their own
scales. Optional-model failure preserves fallback and other specialists.
`YT_PLAYLIST_AUDIO_VIBE_<NAME>_MODE=off` restores that shared score;
`YT_PLAYLIST_AUDIO_VIBE_<NAME>_MODEL` overrides its path (empty disables it).
Use `FOCUS` for the `concentrate` vibe. The shared model's setting is
`YT_PLAYLIST_AUDIO_VIBE_MODEL`; disabling it also disables specialists.
Environment values override the checkout's ignored `.env.local`.

`yt_playlist.rec.audio_vibe_runtime` owns common bounded artifact reads,
validation and prediction helpers. Each specialist owns target mappings,
weights, aggregation and artifact contract. Production uses `apply()`;
research routing belongs to `audio_lab_tools/cruise_specialist.py`.
Legacy correction formats `audio-vibe-corrections-1/2/3` and their trainer
consumers remain supported. This documentation cleanup retires no runtime code.

## Dataset logs

| Data | Log |
| --- | --- |
| Native emotion/activity sources | [CAL500](datasets/cal500.md), [Emotify/Emotify+](datasets/emotify.md), [PMEmo](datasets/pmemo.md), [DEAM](datasets/deam.md), [Soundtracks](datasets/soundtracks.md), [Jamendo](datasets/jamendo.md) |
| VA transfer and later studies | [ManyMusic](datasets/manymusic.md), [Memo2496](datasets/memo2496.md), [WCMED](datasets/wcmed.md), [mid-level properties](datasets/midlevel.md), [SiTunes](datasets/situnes.md) |
| Expanded native sources | [Coimbra](datasets/coimbra.md), [4Q](datasets/4q.md), [VGMIDI](datasets/vgmidi.md), [MERGE](datasets/merge.md), [EMMA previews](datasets/emma.md), [AMG1608](datasets/amg1608.md), [NTWICM](datasets/ntwicm.md), [MERP](datasets/merp.md) |
| Local and activity evidence | [Library captures/labels/providers](datasets/library.md), [Moods and Activities](datasets/moods-and-activities.md), [Help Me study!](datasets/help-study.md) |
| Unselected sources | [Acquisition leads and gaps](datasets/leads.md) |

[Pipeline and consumer evaluation](BROWSER_EVALUATION_424.md) records decoder
parity, resource costs, whole-output ablations and their limits. The original
[preprocessing report](PREPROCESSING_QUALITY_424.md) remains unchanged because
later experiment freezes hash it. Other root-level plans, the Release label
audit and the supplemental chroma protocol likewise retain their original
names and exact bytes as executable evidence inputs. Their historical report
filenames refer to earlier prose now consolidated here; Git history retains it.

## Reproduce research

```sh
./experiments/run.py status
./experiments/run.py evidence
./experiments/run.py audit manymusic
./experiments/run.py candidates verify
./experiments/run.py encoder-comparison verify
./experiments/run.py check --test test_focus_counts.py
```

Each model/dataset log names its own evidence path and verifier. `status` reads
saved state; it does not prove liveness or integrity. A verifier checks frozen
receipts/inputs/outputs without fitting. Fresh fits require a new frozen directory
and exact executable/runtime checks; preserve old plans, archives and failures.
Post-run source captures do not establish prospective source provenance.
Research intervals condition on their recorded fits/groups and do not erase
previous inspection, missing artist identities or backbone-training overlap.

Experiments and focused checks do not acquire the global-suite lock. Use
`./experiments/run.py check --test FILE.py` for research checks and
`uv run pytest PATH` for focused product checks. Respect current execution holds
and use read-only source access or isolated snapshots for real-library work.
Never initialize a Store, migrate or run tests against the user's database.

## Browser assets and reference harness

The normal app preview is **Tools > Audio analysis preview** (`/audio-preview`).
The production extension captures a ten-second excerpt during playback and
analyses it in an extension-owned frame/worker in the Music tab. Only bounded
measurements/embeddings leave the tab; raw audio is discarded. Stop, navigation,
source changes and cancellation release the capture and worker.

The optional developer rating UI uses `YT_PLAYLIST_AUDIO_VIBES=1`. Normal
collection is separate: three natural-playback excerpts near 30 seconds,
halfway and two-thirds, without seeking. Preserve each excerpt's provenance and
position. Ratings are development labels, not preference overrides or propagation
evidence. A complete capture need not contain a compatible embedding.

Fetch assets and build the CSP-compatible runtime before packaging/staging:

```sh
python3 scripts/audio_model/fetch_model.py
```

Assets and licences go to ignored `extension/audio_model/vendor/`; downloaded
models are hash-verified. The original dynamic-code-generation runtime fails
MV3 CSP. The local rebuild uses pinned Essentia/KISS FFT, Emscripten 3.1.64,
Eigen 3.3.7 and `DYNAMIC_EXECUTION=0`:

```sh
mkdir -p /tmp/tuneconsole-audio-build
git clone --depth 1 --branch 3.1.64 https://github.com/emscripten-core/emsdk.git /tmp/tuneconsole-audio-build/emsdk
/tmp/tuneconsole-audio-build/emsdk/emsdk install 3.1.64
/tmp/tuneconsole-audio-build/emsdk/emsdk activate 3.1.64
git clone --filter=blob:none --no-checkout https://github.com/MTG/essentia.git /tmp/tuneconsole-audio-build/essentia
git -C /tmp/tuneconsole-audio-build/essentia checkout --detach d65623a92ab1d26803c5d8656a3ad7c5b2929031
curl --fail --location --output /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz https://gitlab.com/libeigen/eigen/-/archive/3.3.7/eigen-3.3.7.tar.gz
tar -xzf /tmp/tuneconsole-audio-build/eigen-3.3.7.tar.gz -C /tmp/tuneconsole-audio-build eigen-3.3.7/Eigen eigen-3.3.7/unsupported/Eigen
python3.12 scripts/audio_model/build_runtime.py --source /tmp/tuneconsole-audio-build/essentia --emsdk /tmp/tuneconsole-audio-build/emsdk --eigen /tmp/tuneconsole-audio-build/eigen-3.3.7
```

The builder verifies pinned sources/toolchain/headers and records runtime hashes;
its separate build lock bounds compiler work. For extension packaging behavior
and source-specific distribution terms, use [extension documentation](../../extension/README.md)
and retained artifact notices rather than old checkpoint packaging claims.

Stage the separate Chrome 116+ reference harness:

```sh
python3 scripts/audio_model/bundle.py --destination /tmp/tuneconsole-audio-lab
cp experiments/audio_lab/*.js experiments/audio_lab/*.html experiments/audio_lab/*.css experiments/audio_lab/manifest.json /tmp/tuneconsole-audio-lab/
```

Load unpacked from `chrome://extensions`. Open its reference tab, choose a
permitted local audio file (up to 64 MiB), record source permission and start
playback. Click the lab toolbar icon to grant capture (Ctrl+Shift+Y also works).
It attempts up to three nonoverlapping windows near beginning/midpoint/end;
short audio may supply fewer. Missed windows are not silently substituted.
Stop releases tracks/offscreen state without stopping the player; export JSON
for evidence. File SHA256 is not verified recording association.

For the separate access-only diagnostic, stage a disposable development extension
and inject `audio_probe.js` through its service-worker console as documented in
[extension diagnostics](../../extension/README.md). Production ZIPs omit this
probe. It tests access/nonzero capture, not model quality.

## Local correction candidates

The retained trainers read SQLite without Store initialization and write
candidate/report files only:

```sh
uv run python -m experiments.audio_lab_tools.train_vibes --db ISOLATED_SNAPSHOT --output /tmp/vibe-model.json --report /tmp/vibe-evaluation.json
uv run python -m experiments.audio_lab_tools.robust_vibes --db ISOLATED_SNAPSHOT --embeddings --output /tmp/embedding-vibes.json --report /tmp/embedding-evaluation.json
```

Use the saved feature dataset to replay exact inputs. Keep whole tracks and
artists out of both selection and fitting; acceptable alternatives are
disjunctive labels, and unselected vibes are not explicit unsuitability votes.
Final training fit and individual development maxima do not establish held-out
benefit. Historical correction/semisupervised/gold-silver code remains available;
its rejected results are summarized in [shared provenance](models/shared.md).
