# Relax production integration (#629)

The user selected #617's fixed consensus with per-excerpt prediction averaging
on 30 October 2026. The backend now bundles that exact model and defaults to
active Relax suitability, using the same preparation, fallback and rollback
path as Cruise and Release. This is an author implementation handoff for
independent review, not a deployment or whole-account acceptance claim.

## Model and inference

The frozen full fit is
`local/relax-617-20261030/round-1/models/full.json`, SHA256
`46ca8cc085adaab92eed16fa51b49ac4de311996a8b9492f144a5cf70aec7851`.
The research plan SHA256 is
`c37bc4c30e8d42ae34d50a7f3aedebf898e61f61060dd01bc2caf2648abe3cf2`.
No refitting or local-label scoring occurred during production integration.

| Source | Retained training rows | Native attributes | Fixed weight |
| --- | ---: | --- | ---: |
| Emotify | 400 | calmness, tension | 0.5 |
| Soundtracks | 470 | energy, tension, tenderness | 0.25 |
| ManyMusic natural | 188 | valence, arousal | 0.0625 |
| ManyMusic generated | 95 | valence, arousal | 0.0625 |
| Memo2496 pilot | 63 | valence, arousal | 0.125 |

The eleven ridge heads become five multi-output coefficient matrices after
checking that each source's preprocessing is identical across its heads. Native
predictions are clipped to [0, 1] separately for every excerpt and then averaged.
Mappings are `(calmness + 1 - tension) / 2`,
`(2 - energy - tension + tenderness) / 3`, and
`(1 - arousal + valence) / 2`, respectively. Applying these linear maps and
weights after averaging is equivalent to averaging per-excerpt Relax scores.
ManyMusic retains half its source weight per stratum. The learned-weight model
is not exported; it failed the predeclared transfer limits.

Training used mean embeddings. The artifact records that separately from the
authorized serving order, `mean_clipped_native_then_fixed_map`. The subsequent
same-head aggregation diagnostic in [the research report](RELAX_CONSENSUS_RESULTS.md)
found negligible ranking changes; it did not refit or create new confirmation.
The research report retains grouped outer-fold and omitted-corpus results,
uncertainty, source exposure and missing compatible pilot comparisons.

Bundled artifact: `src/yt_playlist/rec/data/relax-vibes.json`, **113,117 bytes**,
SHA256 `4fc43b28a71304ababa8514100f8080752f308a37f397681b127332febb5fbbd`.
The adjacent notice preserves attribution and source-terms status, including
ManyMusic NC/SA and natural-recording terms. Distribution clearance is not
established by this integration.

## Product behavior

`audio_vibe_relax.py` is bounded pure-Python inference without research imports
or annotation access. It validates native target order, all five sources, the
exact fixed weights, finite coefficients, positive scales, extractor identity,
taxonomy, aggregation and file/excerpt bounds. Runtime artifacts are limited to
256 KiB and captures to 128 compatible 200-dimensional excerpts.

The existing shared scorer still supplies six-way classification, thresholds
and radio intensity. Independent Cruise, Release and Relax suitability are
composed without comparing their scales. The other three fits remain shared.
The bounded preparation version includes Relax's routing version, mode and
artifact hash. Updates replace each recording's contribution and rebuild
propagation from original evidence while preserving history and feedback.

`YT_PLAYLIST_AUDIO_VIBE_RELAX_MODE=off` restores shared Relax suitability.
`YT_PLAYLIST_AUDIO_VIBE_RELAX_MODEL` selects an alternate artifact; an empty
path disables it. Environment settings override `.env.local`. Missing or invalid
optional artifacts preserve both working specialists and the shared Relax
score. Disabling the shared scorer retains the existing heuristic behavior.
Partial captures retain the existing preparation policy; explicit preview calls
can score available excerpts before a complete capture is present.

## Validation

Retained evidence root: `local/relax-production-20261030/`.

```sh
./experiments/run.py relax-package --directory experiments/audio_lab/local/relax-production-20261030/package-1 --install
uv run pytest tests/test_audio_vibe_relax.py tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_external.py tests/test_audio_vibe_model.py tests/test_audio_vibe_embeddings.py tests/rec/test_vibe_prediction.py
./experiments/run.py relax-production --directory experiments/audio_lab/local/relax-production-20261030/replay-2
./experiments/run.py relax-wheel --directory experiments/audio_lab/local/relax-production-20261030/installed-2 --wheel experiments/audio_lab/local/relax-production-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl
```

Use fresh directory names when repeating evidence commands. No global suite or
test lock was used.

- **Coefficient replay:** all 2,032 supported external recording/preparation
  cases plus 159 first/partial/full capture cases from 84 songs. Every native
  output and final blend agrees with independent research-head arithmetic;
  maximum absolute error `6.245004513516506e-16`. These full-fit replay cases
  establish export fidelity, not held-out accuracy. Local labels are not scored.
- **Resources:** artifact load 2.47 ms; cached-feature inference median 0.442 ms,
  maximum 2.637 ms across the replay cases. Research process peak RSS 653,266,944
  bytes includes loaded replay arrays. These timings exclude the encoder,
  audio extraction and application startup.
- **Focused tests:** 168 passed in 122.37 seconds. Checks cover three-specialist
  composition, clip-before-average behavior, malformed and wrong-policy
  artifacts, provenance, default activation, local rollback, bounded refresh,
  missing metadata, continuing captures, one recording vote, immutable history,
  human feedback, annotation isolation and consumer score selection. One existing
  Starlette/httpx deprecation warning was reported.
- **Actual captures in a fresh database:** the stable `replay-2` run covers all
  84 songs with network access blocked. Metadata-only profiles appeared after
  0.117 seconds of work; first-excerpt preparation completed in 0.252 seconds.
  All 73 complete captures received the new Relax score; 11 partial captures
  retained their fallback. Rollout completed in 2.506 seconds of work, with a
  maximum preparation slice of 0.526 seconds. Home consumed the prepared score.
  Cruise, Release, other fits, classification and intensity were unchanged.
  Rollback restored all prior fits exactly in 2.205 seconds, preserving immutable
  history and valid foreign keys. Idle admission time is excluded.
- **Installed package:** 159 real-capture cases passed in a fresh isolated Python
  process using wheel files and default modes, with network connections blocked
  and no experiment imports. All three specialist scorers applied. Relax output
  matched checkout inference exactly. The package check compared fifteen
  relevant runtime, model and notice files byte for byte.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0.
The research replay uses one numerical thread.

| Evidence | SHA256 |
| --- | --- |
| `package-1/report.json` | `27452f63f7b1b43005501627e205a2b0a24bab9392421246ac065be03716c241` |
| `package-1/receipt.json` | `ed82bc0cf51649b8b7fb408c68e43bb73e012f4e144e69430f7f4b743d4edeba` |
| `replay-2/report.json` | `dda3c99468aa31fe34e485bbc975b8ea6b8f27eb194277f484b610d4b8cf065c` |
| built wheel | `4b6c841aad07683386b282bb046e387ee729eaf9025955f52664974f2fd91497` |

## Retained attempts and build environment

`replay-1` passed its behavioral assertions but was marked incomplete because
`repos/history.py` changed during execution. The report and isolated database
are retained. `replay-2` passed with all product Python files unchanged.

The ordinary offline `uv build` could not write its shared cache. Retrying with
a writable cache and no isolation found no installed hatchling. The successful
offline build invoked the declared `hatchling.build.build_wheel` backend using
already-cached dependencies read-only. `wheel-1/build.json` records exact versions:
hatchling 1.29.0, hatch-vcs 0.5.0, packaging 26.2, pathspec 1.1.1, pluggy 1.6.0,
trove-classifiers 2026.9.21.13, setuptools-scm 10.3.4, vcs-versioning 2.6.0,
setuptools 84.2.1. No dependencies or build configuration were changed.

`installed-1` stopped during extraction because the repository filesystem ran
out of space. Only its incomplete extraction was removed. The package check now
extracts into a temporary directory on `/tmp` and cleans it afterward;
`installed-2` passed and retains its report and exact capture input.

## Review boundary

These are implementation checks, controlled fixtures and the frozen actual
capture cohort, not independent reviewer acceptance or a full-account/rendered
consumer review. Reuse #424 capture acceptance and #573's remaining account and
rendered workflow checks; #624 tracks previously reported broader consumer
failures. #405/#587 own whole-output evaluation. No new listening or shadow phase
is required. Changes remain uncommitted; the running app has not been reloaded
or deployed as part of this handoff.
