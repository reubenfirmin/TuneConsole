# Energize production integration (#631)

The user selected #616's no-joy consensus on 30 October 2026, favoring its wider
range of labeled material after reviewing the reported gains and losses. The
backend bundles the exact frozen fit and defaults to active Energize suitability,
consistent with Cruise, Release, Relax and Uplift. This is an author handoff for
independent review. No application restart or deployment was performed.

The original research recommendation remains **retain**. The no-joy variant was
a declared diagnostic, and its remaining losses do not establish an overall
upgrade. The subsequent product adoption accepts that tradeoff; broader source
coverage is not proof of better generalization. Browser grouped NDCG was:

| Reference | Shared production | No-joy consensus |
| --- | ---: | ---: |
| Soundtracks energy | 0.84344 | 0.85145 |
| ManyMusic natural arousal | 0.79242 | 0.79176 |
| ManyMusic generated arousal | 0.90020 | 0.93017 |
| Memo arousal | 0.93122 | 0.92397 |
| CAL500 Exercising | 0.25251 | 0.27286 |

Memo top-list lift falls 0.017. Historical natural/exercise NDCG falls from
0.81900/0.28996 to 0.78843/0.25788. These are previously inspected populations,
not new confirmation or accuracy percentages. The [research report](ENERGIZE_CONSENSUS_RESULTS.md)
and [label audit](ENERGIZE_LABEL_AUDIT.md) retain the uncertainty, reference
definitions, transfer conditions and limitations.

## Exact model and serving

Source: `local/energize-616-20261030/round-1/models/full.json`, SHA256
`0801f0919f21f9ca7bcc1ad35d7ec872bc60db0c74227ab9423cfd3e9266660e`.
Plan SHA256:
`0500878ae3cc15b57cb840b3e5c4f0723029c026da7938ef53d0ae523cb35d42`.
The export selects only `consensus_no_joy`, with no fitting or weight search.

| Source | Training rows | Native attribute | Weight |
| --- | ---: | --- | ---: |
| DEAM | 1,800 | arousal | 0.125 |
| PMEmo | 766 | arousal | 0.125 |
| Soundtracks | 470 | energy | 0.25 |
| Emotify | 400 | power | 0.25 |
| ManyMusic natural | 188 | arousal | 0.0625 |
| ManyMusic generated | 95 | arousal | 0.0625 |
| Memo pilot | 63 | arousal | 0.125 |

Seven native heads across six source families use **3,782 source rows**, not a
claim of that many distinct independent recordings. Source-balanced arousal
has total weight 50%; Soundtracks energy and Emotify power have 25% each.
ManyMusic's two strata have equal shares. Joy, CAL500 and the exercise mappings
are absent from the production bundle.

Training used mean embeddings per annotated recording. Serving predicts and
clips each excerpt's native output, averages excerpts and applies the fixed
weights, matching the other specialists. The two aggregation rules are recorded
separately. No local annotations or research modules are runtime dependencies.

Artifact: `src/yt_playlist/rec/data/energize-vibes.json`, **119,514 bytes**, SHA256
`ee27a94000a37b0252ca9f3b580e727268ce9d04fb8a28d9341b7263379cdc20`.
The adjacent notice retains attribution and unresolved source-terms status;
integration does not establish distribution clearance.

## Product behavior

`audio_vibe_energize.py` uses bounded pure-Python inference. It validates exact
native targets, weights, aggregation, procedure, taxonomy, extractor identity,
finite coefficients and positive scales. The artifact limit is 256 KiB; inputs
are limited to 128 compatible 200-dimensional excerpts.

Five specialists now supply independent suitability, with Focus retaining its
shared score. Shared six-way classification, labels and thresholds stay intact.
Preparation identity includes Energize's routing version, mode and model hash.
Existing captures refresh in bounded batches, replacing each recording's vote
and rebuilding propagation from original evidence. History and feedback remain.

Radio intensity retains **shared Energize and shared Release** scores, rather
than comparing independently scaled specialist outputs. The new internal
`shared_energize` channel follows the existing `shared_release` pattern through
measurement, cohort propagation, persisted recording fits and serving. Both
appear only in `intensity_fits`, outside the six user-facing `fits`. Existing
profiles remain readable while background preparation adds the new channel;
no schema migration or audio recapture is required. Explicit intensity overrides
remain authoritative.

`YT_PLAYLIST_AUDIO_VIBE_ENERGIZE_MODE=off` restores shared Energize suitability.
`YT_PLAYLIST_AUDIO_VIBE_ENERGIZE_MODEL` overrides the bundled path; an empty
path disables it. Environment settings override `.env.local`. Missing, malformed
or extractor-incompatible optional models retain fallback and other specialists.
Available excerpts can be previewed; incomplete prepared captures retain the
existing provider/heuristic fallback. Missing metadata does not block compatible
complete audio measurements.

## Validation

Evidence root: `local/energize-production-20261030/`.

```sh
./experiments/run.py energize-package --directory experiments/audio_lab/local/energize-production-20261030/package-1 --install
uv run pytest --basetemp=experiments/audio_lab/local/energize-production-20261030/pytest-1 tests/test_audio_vibe_energize.py tests/test_audio_vibe_uplift.py tests/test_audio_vibe_relax.py tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/rec/test_vibe_prediction.py
uv run pytest --basetemp=experiments/audio_lab/local/energize-production-20261030/pytest-2 tests/test_audio_vibe_cruise.py::test_shadow_reports_candidate_but_preparation_uses_shared_scores
./experiments/run.py energize-production --directory experiments/audio_lab/local/energize-production-20261030/replay-2
./experiments/run.py energize-wheel --directory experiments/audio_lab/local/energize-production-20261030/installed-1 --wheel experiments/audio_lab/local/energize-production-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl
```

Use new output directories when repeating commands. No global suite or
global-suite lock was used.

- **Coefficient replay:** 5,586 external preparation cases plus 159
  first/partial/full cases from 84 captured songs. Every retained native output
  and blend agrees with independent frozen-head arithmetic within
  `2.220446049250313e-16`. External cases include multiple preparations of the
  same recordings and CAL500 diagnostic rows; this is export fidelity, not
  held-out accuracy or additional training. No local labels were scored.
- **Resources:** model load 2.648 ms, cached-feature inference median 0.381 ms,
  maximum 2.561 ms; research peak RSS 653,266,944 bytes includes replay arrays.
  All bounds pass. Timing excludes encoder, extraction and application startup.
- **Focused checks:** 232 passed with one outdated Cruise assertion failing in
  113.02 seconds. The assertion excluded only the old Release intensity channel;
  it now checks both shared channels. Its focused rerun passed in 0.84 seconds.
  All 233 selected checks therefore have passing results; no production change
  was needed after the initial run. One existing Starlette/httpx deprecation
  warning remains. Tests cover five-specialist composition, exact no-joy mapping,
  clipping before averaging, invalid artifacts, bounds, defaults, configuration
  identity, bounded updates, propagation, one recording vote, history, feedback,
  annotation isolation, missing metadata, explicit intensity and rollback.
- **Fresh isolated preparation with actual captures:** 84 songs, 232 captures,
  networking blocked. The baseline deliberately uses the previous layout with
  only the shared Release internal channel; adoption adds shared Energize.
  Metadata-only profiles appeared after 0.122 seconds of work; first-excerpt
  processing finished in 0.257 seconds. All 73 complete recordings changed to
  the new Energize score; 11 partial recordings retained fallback. Adoption
  took 2.979 seconds of work, maximum slice 0.606 seconds. Home used the prepared
  score, while other fits and both shared radio inputs remained exact. Rollback
  restored prior fits in 2.660 seconds, preserving history and foreign keys.
  Idle admission time is excluded. `replay-2` confirms all product code/models
  stayed unchanged during the run. `replay-1` passed behavioral checks but was
  invalidated when an unrelated `core/preparation/radio_origins.py` file appeared
  during its whole-runtime hash check; it is retained and superseded.
- **Built wheel:** 159 capture cases passed with all five default specialists
  and zero score error. Twenty-two relevant runtime/model/notice files matched
  exactly. The isolated process blocked networking and imported no research code.
  The build used the declared `hatchling.build.build_wheel` backend with cached
  dependencies and networking blocked; `wheel-1/build.json` records versions and
  paths. Wheel extraction used a temporary directory and was cleaned afterward.

Capture receipt SHA256:
`dd804e7fea3db06c9f2691df34c77589527f268520f45047952b00beaf593abc`.
Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one
numerical thread for research checks. The user's live database was not modified
or used for tests/migrations.

| Evidence | SHA256 |
| --- | --- |
| `package-1/report.json` | `b0917b62a7182698aca06b19cf64512b2a416cb8ec3c076f928110f563897d02` |
| `package-1/receipt.json` | `e1dcc1ebb54bff199dda902c5f1f99687c75abd8ef0aa2ab99cc406da6dbd408` |
| `replay-2/report.json` | `1f4566cb9de187884adcd95269a6c1112596f12e20d401494b3d8a9afd32c02a` |
| `installed-1/report.json` | `973ab9ea90c5fb11a049ca091953f3b33f5e6a51ac3213bf207b3f3d6ee1ef88` |
| wheel | `0ea43c706a8c4e4610fd26b793f45b8dfc26d2beda187013088634473949a886` |

## Review boundary

These are author checks using independent numerical calculations, controlled
fixtures and the frozen capture cohort. Independent acceptance and broader
account/rendered workflow verification remain with the reviewer. Reuse #424
capture acceptance and #573's broader consumer review; #624 owns existing
broader consumer defects and #405/#587 own output evaluation. No new listening
or shadow phase. Changes remain uncommitted and unpushed.
