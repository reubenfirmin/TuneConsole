# Direct Focus production integration (#641)

The user authorized adoption of the exact #615 direct Reading/Studying candidate
after reviewing #637/#638. The backend now defaults to this specialist for Focus
recommendation suitability, composing with Cruise, Release, Relax, Uplift and
Energize. Implementation and author verification are complete; independent
acceptance remains pending. This is a later product decision and does not alter
any frozen research criterion or prior retain result.

## Model and runtime behavior

The two ridge heads use 494 CAL500 songs and 1,677 original responses. They
predict Reading and Studying from the existing 200-dimensional MusiCNN embedding,
clip each excerpt's predictions to [0,1], average excerpts, then take the equal
mean of the two activities. Both heads retain alpha 1000 and the exact frozen
coefficients and preprocessing. No fitting occurred during integration.

- Source export: `local/focus-615-20261030/round-1/exports/direct.json`, SHA256
  `39f6741b989bd1d665d4414506230d9b7b936285fbb52bf78b24af30e75da098`.
- Source plan SHA256:
  `5af390b1c1173e4d2978f6ca34835b955a4d81a6206c71965fb855f2d1545486`.
- Bundled artifact: `src/yt_playlist/rec/data/focus-vibes.json`, 36,994 bytes,
  SHA256 `695f07272d12d218cefa15aca7ec3ee5105592d7ca428248c99c2a5c6f1ca0a8`.
- Source attribution and original CAL500 terms are retained in
  `src/yt_playlist/rec/data/focus-vibes.NOTICE.md`. The artifact carries no audio
  or individual annotations; this integration establishes no new source rights.

`audio_vibe_focus` adds independent `concentrate` suitability. Shared six-way
classification, banner thresholds, both shared radio-intensity channels and the
five other specialist scores are preserved. Scores rank songs within Focus;
they remain uncalibrated for individual listeners and are not mixed-scale
six-way classification probabilities.

The same bounded loading, validation, optional failure handling and versioned
preparation pattern applies as for the other specialists. Model hash, routing
version and mode enter `vibe_preparation.version()`, so existing captures and
their propagated scores refresh without audio extraction. Invalid artifacts or
incompatible embedding signatures retain the shared Focus fallback. Incomplete
captures retain the existing provider/heuristic behavior. Production inference
does not load labels or research modules.

Default mode is active. Set `YT_PLAYLIST_AUDIO_VIBE_FOCUS_MODE=off` for rollback.
`YT_PLAYLIST_AUDIO_VIBE_FOCUS_MODEL` overrides the bundled path; an empty value
disables it. Environment values precede `.env.local`. Disabling the shared model
also disables all specialists, consistently with existing behavior.

Research evidence remains: browser NDCG 0.498598 versus shared 0.399852 on the
same 450 songs, paired difference +0.098747 [0.004644, 0.204600]. Top-list lift
gain is uncertain. No claims of new accuracy, fresh confirmation, personalized
ranking, or superiority of the exploratory residual/PCA variants follow from
this coefficient packaging work.

## Author validation

Artifact root: `local/focus-production-641-20261030/`.

- **Coefficient replay:** 494 historical and 494 browser external cases, plus
  159 first/three/all capture cases from the frozen 84-song cohort, including 11
  partial songs. Exact JSON head/preprocessing equality also passes. Maximum
  native or final-score error is 5.56e-17. Load time 0.865 ms; median cached-feature
  scoring 0.141 ms, maximum 0.402 ms. Peak process RSS 653,266,944 bytes includes
  research dependencies and source data. Timing excludes extraction/startup.
- **Focused integration checks:** 242 passed initially; one new test failed
  because it compared separately generated captures with different sample IDs.
  Correcting the test to reuse the same capture produced a passing focused
  rerun. All 243 selected checks therefore have passing results; no production
  code changed after the initial run. Coverage includes all six specialists,
  missing/malformed models, signature mismatch, clipping before averaging,
  mode/artifact invalidation, partial captures, missing metadata, continuing
  updates, propagation counts, feedback/history preservation and rollback.
- **Fresh isolated database replay:** all 84 actual captured songs complete
  metadata-only, first-excerpt, full-capture, adoption and rollback phases.
  Seventy-three complete recordings receive changed Focus scores; eleven partial
  recordings retain fallback. Adoption takes 3.704 seconds of work, rollback
  3.086 seconds; the largest adoption slice is 0.800 seconds. Other fits and
  intensity lineage remain identical; Home consumes prepared Focus, history
  survives rollback, and foreign keys are valid. No live database is opened.
- **Installed wheel:** 25 production files match the checkout, all six bundled
  specialists load under default settings, and 159 real capture cases agree
  exactly. Networking is blocked and no research modules are imported. The wheel
  is a local development build, not a published release.

Commands:

```text
./experiments/run.py focus-package --directory experiments/audio_lab/local/focus-production-641-20261030/export-1 --install
uv run pytest tests/test_audio_vibe_focus.py tests/test_audio_vibe_cruise.py tests/test_audio_vibe_release.py tests/test_audio_vibe_relax.py tests/test_audio_vibe_uplift.py tests/test_audio_vibe_energize.py tests/test_audio_vibe_model.py tests/test_audio_vibe_external.py tests/rec/test_vibe_prediction.py
uv run pytest tests/test_audio_vibe_focus.py::test_provenance_input_bounds_and_disabled_shared
./experiments/run.py focus-production --directory experiments/audio_lab/local/focus-production-641-20261030/replay-1
./experiments/run.py focus-wheel --directory experiments/audio_lab/local/focus-production-641-20261030/installed-1 --wheel experiments/audio_lab/local/focus-production-641-20261030/wheel-1/yt_playlist-0.1.17.dev29+g2a2af74d1.d20261030-py3-none-any.whl
```

Build uses the declared `hatchling.build.build_wheel` backend with cached
dependencies and networking blocked; `wheel-1/build.json` records package
versions and cache paths. The project venv lacks the backend, and a preliminary
offline uv invocation encountered its read-only global cache. Reading the
previously cached build dependencies completed the build without changing the
environment or requesting broader permissions.

Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1, threadpoolctl 3.7.0; one
numerical thread in research replay. Production scoring uses only standard
library arithmetic. No global suite or global-suite lock was used.

| Evidence | SHA256 |
|---|---|
| Export report | `61594e0c64a501a3add71fa02e76af5bcbb0f32e7413f4c13c0d89567f4adc2a` |
| Export receipt | `71487eefa6f6c8cf23badc577c68a64c2b42b199dd58bee98533e4337bf980ec` |
| Isolated preparation report | `38151acb185fbfc7b4385fc1eb2aa42d09eaad56754fd978f95f67012c285205` |
| Built wheel | `aef299657b92ccaeceafb8fbecdc51e8159a7fbb7b934edb1658811db612a48a` |

## Review boundary

#641 is In review. These are author checks and retained workspace artifacts,
not independent reviewer acceptance, CI or release evidence. No live service
restart, deployment, commit or push occurred. Broader actual-account/rendered
consumer acceptance remains with #573/#624; capture acceptance with #424 and
output evaluation with #405/#587. Review should verify exact candidate identity,
serving order, default composition, bounded refresh and rollback against the
preserved requirements. All changes remain available for review and commit.
