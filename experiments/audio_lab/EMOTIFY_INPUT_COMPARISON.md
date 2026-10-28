# Emotify input comparison and #507 cleanup, 28 October 2026

This updates [the original checkpoint](MODEL_CHECKPOINT.md). The embedding model
remains the best tested Emotify pipeline for choosing a vibe on this diagnostic
set. Provider-only inference and within-vibe song ranking have different results;
the 69.1% figure must not be presented as their accuracy.

## Comparison

The rerun uses all 400 saved Emotify excerpts and the same 55 developer songs
with compatible embeddings. Five outer folds separate normalized developer
artist names; three training-only inner folds choose ridge penalties. Native
Emotify evaluation separates recordings because artist identities are unavailable.
All seven input families use identical gold songs and folds. The 56th human song
without an embedding remains preserved, and all 63 silver songs remain excluded.

Saved sample IDs and content hashes identify the actual captured measurements.
Their embeddings replay exactly against the frozen gold snapshot. Attributes
use the same 27 native units as external extraction; six browser mood scores use
the same normalization. Captured attributes and moods use medians across excerpts.
The provider experiment uses actual frozen catalog BPM and relaxed, aggressive,
happy and sad fields. It does not substitute captured audio for missing metadata
or equate provider loudness/energy with different sampled-audio units.

Each Emotify row below trains an external emotion head and a six-vibe calibrator.
Direct gold models omit the external head. Top choice accepts any human-selected
vibe; lower acceptable-label log loss and higher ranking AP are better.

| Pipeline | Native emotion MAE | Acceptable top choice | Log loss | Macro ranking AP |
| --- | ---: | ---: | ---: | ---: |
| **Emotify embedding** | **0.1103** | **38/55 (69.1%)** | **0.928** | **0.528** |
| Emotify attributes + embedding | 0.1114 | 36/55 (65.5%) | 1.042 | 0.439 |
| Emotify attributes + embedding + moods | 0.1114 | 35/55 (63.6%) | 1.062 | 0.449 |
| Emotify browser moods | 0.1140 | 34/55 (61.8%) | 1.305 | 0.455 |
| Emotify attributes + moods | 0.1165 | 32/55 (58.2%) | 1.122 | 0.409 |
| Emotify provider-shared fields, including missing inputs | 0.1162 | 23/55 (41.8%) | 1.223 | 0.256 |
| Emotify attributes | 0.1286 | 17/55 (30.9%) | 1.196 | 0.393 |
| Direct gold embedding baseline | — | 34/55 (61.8%) | 1.040 | 0.400 |
| Direct gold attributes + moods | — | 37/55 (67.3%) | 1.008 | 0.468 |
| Existing captured-audio heuristic | — | 28/55 (50.9%) | 1.130 | 0.602 |

The captured-audio heuristic ranks songs better overall than the selected model
on these annotations, despite worse top-choice accuracy. Its AP uses raw served
fits; the learned pipeline uses its normalized exported scores. The model is not
an established improvement for every recommendation objective. These are small,
previously examined developer diagnostics, not prospective population estimates.

Only 35 of the 55 songs have any shared provider input; 12 have all five. On those
35, provider transfer chooses an accepted vibe for 15 songs versus 8 for a
catalog-only heuristic reconstructed from the frozen fields, but has worse macro
ranking AP (0.244 versus 0.338). On the 12 complete cases it chooses 7 versus 4.
These subset results reuse the saved held-out predictions. The browser and
provider mood extractors differ, and these small subsets do not establish a
reliable replacement for the product's initial metadata heuristics. No provider
model was activated in this cleanup.

## Actual library evidence

A read-only transaction at 17:16 UTC found 7,549 distinct library recordings with
a video ID. There are 3,666 with at least one shared provider field and 1,723 with
all five; relaxed/happy/sad each cover 2,682. These are input availability counts,
not accuracy measurements.

Of 111 library recordings with a complete capture, 70 have compatible embeddings
for the bundled model; 41 do not. For 63 of those 70, all six current prepared
audio scores exactly equal independently evaluated bundled-model scores (maximum
absolute error zero). Seven still have older or missing prepared results.
A model hash stamped on a legacy fallback is not proof of learned inference.

Prepared sources in that snapshot include provider measurements (1,975
recordings), audio measurements (111), artist plus genre (582), artist (529),
genre (1,350), and heuristic (1,489). Source counts can overlap across vibes.
This demonstrates that all three evidence layers exist in the actual library;
it does not measure the accuracy of propagated scores or establish complete
refresh of serving caches.

The bundled artifact remains unchanged, SHA256
`5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.

## Fixes and verification

Explicit, supported catalog intensity corrections now take precedence over
audio-derived energize/release fits in both consumer scoring and provenance.
The integration regression uses the public correction writer, sample API and
preparation worker; corrections before capture and after capture both survive,
and raw samples remain unchanged. Focused checks passed: **50 tests, 28.40s**.

```sh
uv run pytest --test-lock-wait=60 tests/rec/test_musical_character.py tests/rec/test_vibe_prediction.py
```

The developer card now reports an unavailable collection connection instead of
silently displaying zero checks. This covers missing browser connection support,
an old extension rejecting the collection port, and a missing optional model.
It recovers after the extension becomes available. Collection remains separate
from the developer rating switch; normal playback still collects with ratings off.
The session, browser and installed-extension checks passed: **10 tests, 85.47s**.

```sh
uv run pytest --test-lock-wait=60 -m '' tests/test_audio_preview.py tests/browser/test_audio_vibes_browser.py::test_stale_extension_explains_zero_checks_and_recovers_after_reload tests/browser/test_audio_vibes_browser.py::test_normal_collection_predicts_and_propagates_without_rating_ui tests/browser/test_audio_vibes_browser.py::test_zero_analysis_labels_survive_song_change_navigation_and_second_tab tests/browser/test_audio_vibes_browser.py::test_installed_extension_collects_native_embedding_with_ratings_disabled
```

Runtime: Python 3.14.8, Playwright Chromium. Browser recovery and three-layer
propagation use controlled fixtures; native capture uses controlled media and
real local extension assets. They do not establish new live-user capture or
classification quality. Tests use isolated databases and respected the shared
lock. `git diff --check` also passed. No full-suite or release acceptance is claimed.

## Live collection and remaining handoff

The live bridge was healthy and music was playing. The browser's saved sampler
advanced through songs with empty attempts/points, while the newest stored sample
was still at 16:01 UTC. Chrome recorded the unpacked extension's last update at
15:00 UTC; the collection connection changed at 15:49. A stale worker rejecting
the new port is the leading explanation, rather than a preparation queue delay.
The live worker's loaded source was not directly inspected, so this remains an
inference. The user was instructed to reload the unpacked extension and app page.
Successful collection in the user's actual browser after that reload remains
unverified. Source fixes and fresh-browser tests do not establish that step.

The next agent should verify that reload and actual new samples, then independently
review #507 against its acceptance notes. Keep #507 In Progress until the remaining
live evidence is established. The broader dataset/consensus and unlabeled-learning
plan remains in [#530](https://github.com/reubenfirmin/TuneConsole/issues/530) and
[DATASET_PLAN.md](DATASET_PLAN.md); distribution and broader listening gates remain
in #424. No new campaign or taxonomy change was started.

The original checkpoint's no-live-DB-write statement refers to its original
integration. Afterwards, an attempted activation requested `/recs/rebuild` and
reprioritized 100 existing capture jobs; that did not establish the intended model
coverage or fix capture. This cleanup made no live database writes, changed no
labels, left the bundled model unchanged, and made no commit or push. Other agents'
shared workspace changes were preserved.

## Reproduction

```sh
.venv/bin/python -m experiments.audio_lab_tools.emotify_input_comparison --db /home/rfirmin/.local/share/yt-playlist/state.db
.venv/bin/python -m experiments.audio_lab_tools.emotify_input_comparison --db /home/rfirmin/.local/share/yt-playlist/state.db --summarize
```

The first run reads the database in read-only mode to locate immutable sample
files; later runs can use their saved content addresses without opening it.
The second command adds a read-only library snapshot and provider subset metrics.
Reports, fold assignments, sample hashes, per-vibe AP and held-out predictions are
in ignored `local/external-20261028/emotify-input-comparison.{report,predictions}.json`.
All fitting is single-threaded and writes research artifacts only.
