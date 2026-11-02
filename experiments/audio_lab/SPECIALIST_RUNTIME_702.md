# Specialist runtime extraction (#702)

The six specialists share `yt_playlist.rec.audio_vibe_runtime` for configuration,
bounded FAO artifact reads, coefficient validation and prediction, identity, and
failure-isolated suitability/provenance merging. Each specialist still owns its
artifact contract, target names, source mappings, fixed weights and aggregation.
Each retains a two-entry artifact cache keyed by FAO identity and logical size.
Cruise retains shadow mode and its historical bounded read policy; the other
five retain their exact-length read and active/off policy. Environment values,
including empty values, still override `.env.local`.

`route()` and `route_samples()` belong to the research adapter
`experiments/audio_lab_tools/cruise_specialist.py`. Product inference uses
`apply()`. Export provenance records the neutral helper, and installed-wheel
checks require it. No artifact, fitting procedure, model version, composition
order, six-way classification or intensity calculation was changed.

## Frozen real-capture replay

Evidence is in `local/specialist-cleanup-702-20261101/`. Before production edits,
`specialist_cleanup_702.py::test_freeze` recorded all outputs and source hashes.
The retained baseline cannot be overwritten by the harness. Inputs are the
receipt-checked #615 frozen library captures, with no live database access.

The post-extraction replay established exact nested-output equality for 84 real
recordings (11 partial recordings): 159 composed predictions, 477 research
routing cases across off/shadow/active, and 13 individual-model failure, off,
and shadow cases. It compared model identities and all seven artifact hashes
as well as native values, final scores, shared classification, intensity and
provenance. Local human labels were not scored and no model was fitted.

Baseline SHA256:
`e63865b297046dd715d150a2bb3ae1147eff015a223102a5145f6214bfc37c34`.
Reproduce the comparison with:

```sh
./experiments/run.py investigate specialist_cleanup_702.py -k test_replay -s
```

## Legacy correction consumers retained

The local correction branch in `audio_vibe_model.py` is still supported and is
not dead code. Its `audio-vibe-corrections-1`, `-2` and `-3` contracts, feature
vectors, source signatures, margins, correction scores and classifier validation
remain unchanged. `YT_PLAYLIST_AUDIO_VIBE_MODEL` can still select a correction
artifact; runtime application checks its heuristic/extractor compatibility and
returns `local_learned_correction` provenance. The bundled shared Emotify model
also remains responsible for comparable six-way classification and intensity
inputs; its removal belongs to #642.

Concrete research consumers:

- `audio_lab_tools/train_vibes.py`: uses runtime vectors, source signatures,
  feature-version constants, margins and scoring to train/export/replay correction
  artifacts, including embedding variants.
- `robust_vibes.py`: calls the trainer and runtime correction scores for nested
  whole-song selection and held-out predictions.
- `gold_silver_vibes.py`: verifies teacher exports, reproduces correction scores,
  and evaluates gold/silver candidates through the same runtime.
- `semisupervised_vibes.py`: uses the trainer/robust pipeline and runtime scores
  for graph-based development comparisons.
- `emotify_input_comparison.py`: consumes the correction feature ordering while
  comparing frozen feature representations.

Focused tests retain direct coverage in `test_audio_vibe_model.py`,
`test_audio_vibe_embeddings.py`, `test_audio_vibe_alternatives.py`,
`test_audio_vibe_divergence.py`, `test_audio_vibe_robust_training.py`,
`test_audio_vibe_gold_silver.py`, and `test_audio_vibe_semisupervised.py`.
This inventory authorizes no retirement of correction formats or consumers.

## Focused validation

All commands used the checkout's Python 3.14 environment. Full-suite execution
and independent acceptance belong to the review agent.

- `uv run pytest tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_relax.py tests/test_audio_vibe_uplift.py tests/test_audio_vibe_energize.py tests/test_audio_vibe_focus.py tests/test_audio_vibe_external.py`: **181 passed**, 32.67 s.
- `uv run pytest tests/test_audio_vibe_runtime.py`: **55 passed**, 2.19 s. This
  covers plain/compressed logical hashes, same-size atomic replacement cache
  invalidation, all six supported optional-model exception types for each
  specialist, empty environment overrides, and logical read bounds. A subsequent
  focused check after updating pytest parameter collection passed all 36 failure
  cases, with 19 deselected (1.68 s).
- `./experiments/run.py check --test test_cruise_specialist.py --test test_cruise_baselines.py --test test_suitability_contract.py --test test_release_mapping.py --test test_relax_consensus.py --test test_uplift_consensus.py --test test_energize_consensus.py --test test_focus_mapping.py`: **87 passed**, 67.42 s.
- `./experiments/run.py check --syntax-only`: **246 research Python files parsed**.

Using `local/specialist-cleanup-702-20261101/` as the explicit evidence directory:

- `cruise-wheel --directory …/wheel` built the working-tree wheel offline and
  verified its bundled Cruise model in an isolated interpreter. Extracted files
  were scoped to a `TemporaryDirectory` and removed on completion.
- `focus-wheel --directory …/installed-six --wheel …/wheel/yt_playlist-0.1.19.dev5+gae164de47.d20261102-py3-none-any.whl`
  checked all six specialists across 159 real-capture cases: exact native score,
  suitability and provenance equality, default active modes, no experiment
  imports, no installed research routing entrypoints, and socket connections
  blocked. Extracted files were removed on completion. Wheel SHA256:
  `db2b5aaf36ff8a24b44adde31081c8c413fa2aab0d2d1238cb318c42151809ef`.
- `cruise-package export --directory …/cruise-export` exercised the moved
  adapter and source archival with no refitting: 346 external recordings / 1,227
  excerpts, maximum blend and per-source replay error `2.220446049250313e-16`,
  zero changed non-Cruise scores. Another 93 real playback groups / 186 partial
  and available-excerpt replays preserved shared scores exactly. The exported
  artifact remained byte-identical to the incumbent.
- `release-package --directory …/release-export` exercised the updated neutral
  dependency archive with no refitting: the exported artifact remained
  byte-identical to the incumbent; 159 real-capture cases had maximum error
  `1.1102230246251565e-16` against the independent vectorized head calculation.

These reports establish code and inference parity. They make no new claim about
musical quality, new-model adoption, or the separate #569/#583 integration work.
The wheel includes the shared working tree's preexisting FAO adoption changes;
the installed report records the checked source hashes. This extraction preserves
that work and does not create a commit or push.
