# Emotify product checkpoint, 28 October 2026

Related issue: #507. The user requested a stopping point with a useful model
wired into the product, followed by a plan for further data use. The broad
experiment campaign is paused, not exhausted. Its caches remain available.

## Selected model and comparison

The selected pipeline is a frozen 200-value MusiCNN embedding, a linear ridge
head trained on 400 Emotify excerpts with nine native human emotion targets,
and a linear six-vibe calibrator. Each regression clips to [0, 1]; the six
outputs then normalize to sum to one. Scores are relative and uncalibrated.
No fitting, dataset lookup, or per-song human-label override occurs at runtime.

The six existing vibes and all original annotations are preserved. The final
calibrator uses 55 developer songs with valid embeddings. The 56th human-labeled
song lacks an embedding and remains preserved; all 63 prediction-labeled silver
songs are excluded from calibration and evaluation.

| Model | Acceptable top choice | Acceptable-label log loss |
| --- | ---: | ---: |
| Embedding-only linear baseline | 34/55, 61.8% | 1.040 |
| **Emotify, linear head and calibrator** | **38/55, 69.1%** | **0.928** |
| 4Q, inner-selected calibration learner | 37/55, 67.3% | 0.990 |
| Emotify plus that 4Q model, equal blend | 36/55, 65.5% | 0.935 |
| Emotify plus that 4Q model, learned weights | 35/55, 63.6% | 0.934 |

These use five artist-group outer folds and training-only inner selection.
The blend was explicitly checked after the user asked. It does not improve the
chosen checkpoint. Exhaustive earlier subset inspection produced a descriptive
72.7% fixed combination on a smaller Jamendo pilot, but worse log loss and no
independent evidence for selecting that post-hoc maximum. It is retained in the
research artifacts, not treated as an established stronger product model.

The 69.1% figure is an exploratory result on a small, previously examined
developer set. It is not accuracy measured across the product's entire library,
nor an assessment of the propagated predictions. Unselected vibe alternatives
are not explicit human rejections, and Emotify artist identities are unavailable.

Within-vibe ranking from the selected model's saved outer-fold predictions:

| Vibe | Positive songs | Embedding baseline AP | Emotify AP |
| --- | ---: | ---: | ---: |
| Relax | 10 | 0.245 | 0.497 |
| Focus | 15 | 0.342 | 0.335 |
| Cruise | 26 | 0.637 | 0.767 |
| Energize | 20 | 0.574 | 0.606 |
| Lift my mood | 13 | 0.471 | 0.660 |
| Let off steam | 5 | 0.129 | 0.302 |
| Macro average | | 0.400 | 0.528 |

Focus remains weak; these rankings use the same limited annotations and do not
establish their quality on unseen users or music collections.

## Product behavior

1. Provider audio traits and catalog metadata establish initial heuristic vibes.
   These sources need not supply a compatible MusiCNN embedding, so they do not
   pass through the Emotify head.
2. With the app open and its extension connected, normal YouTube Music playback
   collects three short excerpts. Only bounded measurements and embeddings leave
   the music tab; raw audio is not stored in the app. Collection runs independently
   of `YT_PLAYLIST_AUDIO_VIBES`, which gates the developer rating UI and feedback API.
3. Complete compatible captures use the bundled Emotify model. Missing or
   incompatible embeddings retain the existing heuristic. Raw measured traits,
   excerpt positions, taxonomy, and developer annotations remain unchanged.
4. Background preparation publishes own measurements and updates artist/genre
   cohorts. Exact artist plus specific genre is preferred; broader artist or
   specific-genre predictions have lower support. Each measured recording has one
   vote. Predictions do not seed further predictions, and a replay or model change
   retracts the old contribution before adding its replacement.

Thus unattended listening improves measured coverage and related-library
predictions without requiring vibe ratings. It does not automatically retrain
the global model; unrated-audio representation learning remains an experimental
next step in the [dataset plan](DATASET_PLAN.md).

The bundled artifact is
[`src/yt_playlist/rec/data/emotify-vibes.json`](../../src/yt_playlist/rec/data/emotify-vibes.json),
51,197 bytes, SHA256
`5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.
It contains coefficients, native target names, and aggregate training/evaluation
provenance, with no developer song identities, labels, audio, or recordings.
Compatible embedding signature:
`8a954566923632879d60fe1786e3c44af8732d7c72fe5fb5b03125c388265940`.

The default works in source and installed packages. A nonempty
`YT_PLAYLIST_AUDIO_VIBE_MODEL` overrides it; an explicit empty setting disables
learned inference. The checkout's old ignored model-path override was removed.
The old `local/vibe-subgenre-model.json` remains available for rollback and was
not overwritten. The model hash participates in the existing bounded background
refresh, so the next application launch can update prepared predictions normally.
No running application was restarted and no live database was written during
this integration.

## Reproducibility and attribution

Export and independently replay the coefficients with:

```sh
.venv/bin/python -m experiments.audio_lab_tools.export_emotify_product
```

The exporter uses the saved consensus export's Emotify expert without refitting.
Research source:
`local/external-20261028/models/consensus-soundtracks-set2+cal500+emotify+pmemo.json`.
The stronger-4Q blend report is
`local/external-20261028/consensus-emotify+4q-emotion__native-ridge__calibrator-mixed-4q-emotion-selected_learner+emotify-linear.report.json`.
All detailed reports and private training inputs remain ignored local artifacts.

Product JSON loading and both stages were replayed on all 55 actual saved gold
embeddings in a controlled compatible extractor envelope. Maximum absolute
difference from independent NumPy formulas was `1.11e-16`. This checks numerical
parity, not predictive accuracy. The replay report is
`local/external-20261028/product-emotify-export.report.json`.

A wheel built with the project's pinned Hatchling 1.29.0 and hatch-vcs 0.5.0
contains the identical artifact and loads it from an isolated extracted package.
Flatpak installs this wheel; macOS collects package data. Actual platform release
builds and distribution are outside this checkpoint.

The public extension archive deliberately excludes `audio_model/`, and its
packaging test enforces that exclusion. Normal collection therefore checks the
installed extension's local model manifest before recording; an unavailable
model stays idle and retries after a minute. Locally prepared extensions collect
independently of the developer rating switch. Shipping this Python checkpoint
does not silently add the optional browser research assets to public packages.
The [upstream licensing page](https://essentia.upf.edu/licensing_information.html)
lists the pretrained models under noncommercial terms and offers a separate
commercial licence. Model distribution, source permissions, representative
resource measurements and broader listening acceptance remain #424 gates.

Focused implementation checks on 28 October:

```sh
uv run pytest tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/test_audio_vibes.py tests/rec/test_vibe_prediction.py
```

132 passed in 108.85 seconds, with one existing Starlette/httpx deprecation
warning. These cover packaged defaults and overrides, malformed/incompatible
fallbacks, unchanged annotations, normal collection APIs with ratings off, and
bounded preparation/propagation. They use isolated databases and controlled
fixtures. The 55-song replay above uses the frozen real diagnostic embeddings.
The subsequent collection/package/browser batch passed 15 checks in 65.63 seconds:

```sh
uv run pytest --test-lock-wait=60 -m '' tests/test_audio_preview.py tests/test_extension_packaging.py tests/browser/test_audio_vibes_browser.py::test_normal_collection_predicts_and_propagates_without_rating_ui tests/browser/test_audio_vibes_browser.py::test_zero_analysis_labels_survive_song_change_navigation_and_second_tab tests/browser/test_audio_vibes_browser.py::test_normal_app_collects_then_shows_one_compact_card_and_autosaves 'tests/browser/test_audio_preview_browser.py::test_app_automatically_captures_and_runs_model_without_capture_click[normal_playback]'
```

This covers missing model manifests and retry behavior, unchanged public ZIP
exclusions, controlled three-excerpt collection through storage/model/propagation
without ratings, existing desktop/mobile developer cards and label persistence,
and actual installed-extension capture/inference on native media. The controlled
extension results establish the full application flow, not acoustic quality.

A separate native-media check on an ordinary app page, using the actual installed
extension and local model with ratings disabled, passed in 58.13 seconds:

```sh
uv run pytest --test-lock-wait=60 -m browser tests/browser/test_audio_vibes_browser.py::test_installed_extension_collects_native_embedding_with_ratings_disabled
```

Natural playback produced a stored excerpt at 30 seconds with 200 finite signed
embedding values, three complete patches and the checkpoint's exact provenance
signature. It loaded no rating modules, wrote no annotation, kept inference in
the music tab, released captured buffers/tracks and left playback running.
The input is a controlled tone, so this establishes plumbing rather than music
classification quality. Runtime: Python 3.14.8, Playwright Chromium 148.0.7778.96.
These checks are not full-suite, live-app restart or release acceptance.

Follow-up issue: [#530](https://github.com/reubenfirmin/TuneConsole/issues/530),
Backlog/P2, contains the complete explored/remaining data inventory and the next
training, consensus, unrated-audio and propagation experiments.

The [official Emotify dataset page](https://www2.projects.science.uu.nl/memotion/emotifydata/)
requests citation of A. Aljanaki, F. Wiering, and R. C. Veltkamp, “Studying emotion
induced by music through a crowdsourcing game,” Information Processing &
Management, 2015. This attribution is included in the artifact; training audio is
not redistributed. The page's usage agreement was checked on 28 October 2026.
