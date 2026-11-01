# Uplift production integration (#630)

The user selected #618's fixed consensus on 30 October 2026 after reviewing its
comparison with the shared production scorer. The backend now bundles that
exact fit and defaults to active Uplift suitability, following Cruise, Release
and Relax. This is an author implementation handoff for independent review;
the running application has not been reloaded or deployed.

The frozen research recommendation remains **retain**: both consensus procedures
failed the predeclared ManyMusic regression limits. The later adoption is an
explicit product decision accepting that tradeoff, not a retroactive passing
experiment. Browser-prepared grouped NDCG against the shared scorer was:

| Reference | Shared | Fixed consensus |
| --- | ---: | ---: |
| Soundtracks | 0.64734 | 0.71462 |
| ManyMusic natural | 0.70473 | 0.76192 |
| ManyMusic generated | 0.91344 | 0.89136 |
| Memo | 0.88963 | 0.97344 |

These are ranking metrics on previously inspected populations, not accuracy or
new confirmation. Memo contains 63 instrumental recordings. The
[research report](UPLIFT_CONSENSUS_RESULTS.md) retains the original rules,
uncertainty, omitted-corpus results and semantic limits.

## Model and inference

Frozen full fit: `local/uplift-618-20261030/round-1/models/full.json`, SHA256
`a1f1087b75f844eef74be62b39d627df611be9c2edabdc99920cbb591342f737`.
Plan SHA256:
`2bf73a9f8f1360f50173f2cb3311dd1f31f9584a0d51054749704e5f28bb5b66`.
No refitting, learned-weight substitution or local-label scoring occurred.

| Source | Retained training rows | Native attributes | Fixed weight |
| --- | ---: | --- | ---: |
| DEAM | 1,800 | valence | 0.125 |
| PMEmo | 766 | valence | 0.125 |
| Soundtracks | 470 | valence, happiness | 0.25 |
| Jamendo human | 216 | happy | 0.125 |
| Emotify | 400 | joyful activation | 0.125 |
| ManyMusic natural | 188 | valence | 0.0625 |
| ManyMusic generated | 95 | valence | 0.0625 |
| Memo2496 pilot | 63 | valence | 0.125 |

The nine ridge heads become eight source coefficient matrices after checking
that each source's preprocessing is identical across its heads. Native outputs
are clipped to [0, 1] per excerpt and then averaged. Soundtracks averages its
valence and happiness outputs; the other sources use their single attribute.
The final fixed blend gives ManyMusic half its source weight per stratum.
Training used mean embeddings; the artifact records that separately from
serving aggregation, `mean_clipped_native_then_fixed_map`.

Bundled artifact: `src/yt_playlist/rec/data/uplift-vibes.json`, **140,485 bytes**,
SHA256 `b29217c7f706c9a5eb0716c7cd8d175ec4a85e65f309806ef66351710e7c97af`.
The adjacent notice preserves attribution and unresolved source-terms status.
This integration does not establish distribution clearance.

## Product behavior

`audio_vibe_uplift.py` performs bounded pure-Python inference without research
imports or annotation access. It validates source/target order, exact weights,
finite coefficients, positive scales, extractor identity, taxonomy and
aggregation. Artifacts are limited to 256 KiB; inputs are bounded at 128
compatible 200-dimensional excerpts.

Uplift composes with Cruise, Release and Relax as independent suitability.
Focus and Energize retain shared suitability; the shared scorer still supplies
six-way classification, thresholds and radio intensity. Preparation includes
the Uplift routing version, mode and artifact hash, so changes replace each
recording's contribution and rebuild propagation from original evidence.
Immutable history and human feedback are preserved.

`YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODE=off` restores shared Uplift suitability
without new capture. `YT_PLAYLIST_AUDIO_VIBE_UPLIFT_MODEL` selects an alternative
artifact; an empty path disables it. Environment settings override `.env.local`.
Missing or invalid optional artifacts preserve the existing fallback and other
specialists. Explicit previews score available excerpts; incomplete prepared
captures retain the existing fallback policy. Missing metadata does not prevent
inference from compatible complete captures.

## Validation

Evidence root: `local/uplift-production-20261030/`.

```sh
./experiments/run.py uplift-package --directory experiments/audio_lab/local/uplift-production-20261030/package-1 --install
uv run pytest --basetemp=experiments/audio_lab/local/uplift-production-20261030/pytest-1 tests/test_audio_vibe_uplift.py tests/test_audio_vibe_relax.py tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/rec/test_vibe_prediction.py
./experiments/run.py uplift-production --directory experiments/audio_lab/local/uplift-production-20261030/replay-1
./experiments/run.py uplift-wheel --directory experiments/audio_lab/local/uplift-production-20261030/installed-1 --wheel experiments/audio_lab/local/uplift-production-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl
```

Use new directory names when repeating evidence commands. Experiments and
focused checks did not acquire the global-suite lock; no full suite was run.

- **Coefficient replay:** 4,814 external recording/preparation cases plus 159
  first/partial/full capture cases from 84 songs. Every native output and final
  blend matched independent frozen-head arithmetic within
  `3.3306690738754696e-16`. External cases include historical and browser
  preparations of the same recordings; they are not 4,814 distinct songs.
  This checks export fidelity, not held-out accuracy. No local labels were scored.
- **Resources:** load 3.299 ms; cached-feature inference median 0.505 ms, maximum
  2.461 ms. Research peak RSS 653,266,944 bytes includes replay arrays. These
  timings exclude the encoder, extraction and application startup.
- **Focused tests:** 197 passed in 131.37 seconds, with one existing
  Starlette/httpx deprecation warning. Coverage includes four-specialist
  composition, exact mapping, clip-before-average behavior, invalid artifacts,
  provenance, defaults, local rollback, bounded refresh, missing metadata,
  continuing captures, one recording vote, history, feedback, annotation
  isolation and Home score selection. Database edge cases use controlled fixtures.
- **Actual captures in a fresh isolated database:** 84 songs and 232 saved
  captures, with networking blocked. Metadata-only profiles appeared after
  0.132 seconds of work; first-excerpt preparation completed in 0.252 seconds.
  All 73 complete recordings received the new Uplift score; 11 partial
  recordings retained fallback. Rollout completed in 2.694 seconds of work,
  with a maximum preparation slice of 0.566 seconds. Home consumed prepared
  Uplift suitability; other fits, classification and intensity were unchanged.
  Rollback restored prior fits exactly in 2.422 seconds, preserving history
  and valid foreign keys. Idle admission time is excluded. The report confirms
  all product Python files and models stayed unchanged throughout the replay.
- **Installed wheel:** 159 real-capture cases passed in a fresh isolated Python
  process using default modes. All four specialists applied; Uplift matched
  checkout inference exactly. Nineteen relevant runtime/model/notice files
  matched byte for byte. Networking was blocked, with no experiment imports.

The capture receipt SHA256 is
`7d8870d9578b13f4a00e06b524fbbc1c24fc64b2cab9cfaacb7499c4767adfce`.
The user's live database was not modified or used for tests or migrations.
Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.
The research replay uses one numerical thread.

| Evidence | SHA256 |
| --- | --- |
| `package-1/report.json` | `02b5775576c6a8a9d0332a60f83cb81402b1b767c9696c09d15099856a395dec` |
| `package-1/receipt.json` | `e4e07cc9f0dddf53ba528d46ede39fb1b86964c9943e56755c6729968b534847` |
| `replay-1/report.json` | `c97a417f287973f8f3f76ddeda717ce22190532954ff746d03970ba9f628c5fd` |
| `installed-1/report.json` | `e9dcbc33253b2a9a9bdcb7e485f7848914cf53b9a8d9b7e47a5627fd03fbbe86` |
| built wheel | `6cf1f877af5651952a1f02cbbbbd8202d52746999136325dbfd3e0e00eb1af45` |

The offline build used the declared `hatchling.build.build_wheel` backend and
already-cached dependencies read-only, with network connections blocked.
`wheel-1/build.json` records exact versions and paths. No dependencies or build
configuration changed. Wheel extraction used a temporary directory beneath
`installed-1` and was cleaned afterward.

## Review boundary

These are author implementation checks, controlled fixtures and the frozen
actual capture cohort. Independent acceptance and broader account/rendered
workflow verification remain with the reviewer. Reuse #424 capture acceptance
and #573's full-account/rendered consumer work; #624 owns previously reported
broader consumer defects. #405/#587 own whole-output evaluation. No new
listening or shadow phase is required. Changes remain uncommitted and unpushed.
