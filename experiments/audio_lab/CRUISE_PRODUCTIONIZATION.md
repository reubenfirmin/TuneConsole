# Cruise individual model: production preparation

29 October 2026. #530, #570–573. The current scope is one individual model,
Cruise, with the other five vibes served by the best validated shared model.
The user has authorized production integration beyond `experiments/`.
The runtime now lives in `src/yt_playlist/rec/audio_vibe_cruise.py`, with the
exact compact artifact bundled as `rec/data/cruise-vibes.json`. Cruise defaults
to active for compatible captures; the other five vibes retain the validated
shared model. Acceptance checks use isolated data, never the live database.
Cruise is mainlined directly. The agent-added shadow-rollout task #574 is
cancelled; there is no shadow phase or staged-promotion requirement.

## Compact artifact and inference contract (#571)

`./experiments/run.py cruise-package export --directory
experiments/audio_lab/local/mapped-vibes-20261029/cruise-package-3` produced:

- `cruise-specialist.json`: **187,217 bytes**, below the existing 256 KiB
  per-model size limit. Compression preserves all original coefficients.
- `shared-model.json`: the exact configured Emotify checkpoint, SHA256
  `5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.
- A frozen plan, archived runtime/exporter/check source, result report and
  checksummed output receipt. The original `cruise-package-1/2` are preserved.

The package contains only the source-selected PMEmo ridge and DEAM random-feature
ridge native arousal/valence regressors. Each excerpt produces clipped native
attributes; native attributes are averaged across actual available excerpts.
Each source then maps Cruise to the mean of moderate arousal and positive
valence. The two source scores receive equal weight. The encoder, coefficients,
feature order and fixed mapping are unchanged from the research candidate.

The production scorer implements three modes; the research entrypoint imports
that implementation rather than retaining a second inference copy:

| Mode | Selected-vibe suitability | Overall classification |
| --- | --- | --- |
| `off` | All six shared scores | Shared scores |
| `shadow` | All six shared scores; separate Cruise diagnostic | Shared scores |
| `active` | Individual Cruise; five exact shared scores | Shared scores |

Independent Cruise is a within-vibe suitability score. It is not normalized
against the other five scores, inserted into a six-way maximum, or passed
through the old classification threshold. `shared_top_vibe` remains the
shared model's classification. Successful routing explicitly reports
`applied_vibes: ["cruise"]` and the individual artifact hash. A missing, malformed
or incompatible optional Cruise artifact leaves all shared results available.

The bounded loader validates taxonomy, a required SHA256 source-plan identity,
extractor provenance, feature order,
learner kinds, finite coefficients, dimensions, positive scales, payload
checksum and decompression bounds. `route_samples` validates every excerpt's
extractor identity before routing; it rejects mixed provenance. One excerpt is
usable without manufacturing missing captures. This does not change the
product preparation repository's current complete-playback selection policy.

## Observed verification

The third package independently replays **346 recordings / 1,227 excerpts**
from the three frozen supplied-audio pilots against the saved research
predictions. Maximum absolute Cruise error is **2.22e-16**. Five other scores
and shared overall classifications are unchanged on every replay. It separately
checks each PMEmo and DEAM source against its saved source predictions for every
recording, rather than only the average: each source's maximum absolute error is
**2.22e-16** on each of the three pilot cohorts. This includes the actual DEAM
random-feature head. A new negative check proves equal/opposite source errors
cannot pass by canceling in the combined score.

It also reads the original checksummed capture envelopes from the frozen local
library snapshot: **93 playback groups**, with **186** replays covering the
first excerpt alone and all available excerpts. Shared results exactly match
the real product module, with zero changes to the other five suitability
scores. These checks use actual captures, not fabricated provenance. Local
human labels and stored predictions do not supply outcomes for this replay.

Measured elapsed wall time on this machine: approximately **6.2 ms** for the
first artifact load in an already imported interpreter, **4.6 ms** median routing
and **8.7 ms** maximum routing over pilot recordings. These are cached-feature
costs, not CPU-time measurements. They exclude fresh-process startup, the audio
encoder, browser startup and end-to-end preparation. The report names these
`first_artifact_load_wall_seconds` and `route_wall_seconds` and records the scope.

`./experiments/run.py check` passed **116 focused research checks** after adding
the Cruise runtime. Coverage includes actual inference semantics, one/two/three
excerpt routing, unchanged other scores, safe optional-model fallback,
provenance rejection, malformed coefficients and bounded decompression.
The subsequent baseline-specific run passed **118 checks in 4.10 seconds**.
Both package receipts passed `cruise-package verify --directory ...`.

The review corrections use `./experiments/run.py check --test
test_cruise_specialist.py`: **28 passed in 1.83 seconds**, including the reviewer's
missing/wrong-type/invalid-hex source-plan negatives. A new no-fit export uses
the same 187,217-byte artifact, SHA256
`3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb`.
Package3's new receipt is
`7811e05df6e31bf0ea0355ce5d2b58cf906a1c57795e8b0a3b3eb7528baf7e27`.
Export and verification use the normal test/build lock and one numeric thread.

## Shared-model refresh (#570)

The serving comparator is the configured, validated shared checkpoint. A
post-hoc research winner on reused labels does not establish a replacement for
the other five vibes.

`./experiments/run.py cruise-baselines freeze` freezes the enlarged human
corpus and the comparison procedure before fitting. It found **84** compatible
recordings: **76 development / 8 reserved**. The eight additions have no known
artist/recording/title component matches in the original protected cohort,
gold, silver, teacher training or the exact fitted Cruise native cohorts.
This audit cannot exclude unresolved aliases or unknown upstream exposure.
No labels changed on the IDs shared with the original gold snapshot.

The declared controls refresh only the shared model's calibration, and refresh
both its native Emotify head and calibration. Three grouped inner folds select
regularization; five outer development folds diagnose calibration selection.
Reserved groups are absent from both fits. The new native fit excludes the
union of local and external evaluation identities, leaving 260 native songs.
The original native head remains a separate control.

Artifacts are fixed before supplementary reserved outcomes are scored. External
comparison covers all supported mappings in the three audio-backed pilots,
Soundtracks and CAL500, with identical training-disjoint rows for all shared
controls. Cruise comparisons include paired whole-identity-group uncertainty
against every shared control. The reused external cohorts remain confirmation
evidence, not an untouched benchmark. No experimental command promotes a model.

The [completed comparison](CRUISE_BASELINE_RESULTS.md) supports retaining the
checkpoint for the other five vibes. Cruise beats all three shared controls on
each pilot; paired intervals stay positive on the two ManyMusic cohorts and
include zero on Memo2496. Both refreshed shared controls regress on several
other vibes. The eight reserved labels are now scored and consumed: the two
Cruise positives rank first with the individual model, a small supplementary
check. All three shared classifiers choose an acceptable label on six of eight.

## Product integration

1. Implemented: preserve the separate suitability and classification fields through
   `audio_vibe_model` / `audio_vibe_prediction`. Current banners and annotation
   feedback can continue using comparable shared scores. Prepared listening
   modes can consume Cruise suitability through their existing per-vibe fits.
2. Implemented: include the shared artifact hash, Cruise artifact hash, routing mode and
   aggregation-policy version in the preparation identity. Distinguish optional
   fallback from a successfully applied Cruise result. Rollback to `off` must
   invalidate derived Cruise results and restore the shared scorer.
3. Implemented: feed the mixed suitability map into `vibe_preparation`'s own audio
   measurements. Use the existing bounded version/fanout queue and immutable
   measurements. Rebuild propagated scores from original evidence, keeping one
   contribution per recording. Derived predictions are never new truth.
4. Verify stale/new results, continuing captures, missing metadata and compatible
   partial captures on an isolated actual-library snapshot. Exercise Home and
   Mix Radio under ordinary preparation budgets. This inference replay does not
   establish fresh-user startup or consumer acceptance.
5. Keep remaining account, consumer and resource verification in #573. #405/#424
   remain open for unfinished evaluation work; shared checks should reference
   this evidence instead of being repeated. They add no shadow-rollout phase.
   The production route uses Cruise only for within-vibe ranking and supplies an
   immediate `off` rollback; it does not assert calibrated match thresholds. Keep #576–580 in Backlog;
   each other vibe needs its own evidence before replacing the shared model.

Production code and default enablement are implemented. Verification evidence
for this integration is recorded below as checks complete; historical package
reports above describe the earlier experimental runtime.

### Production integration verification

`uv run pytest --test-lock-wait=60 tests/test_audio_vibe_cruise.py
tests/test_audio_vibe_external.py tests/rec/test_vibe_prediction.py` passed
**58 checks in 19.69 seconds**. These include bounded batch=1 rollout, continuing
captures, a replaced artifact, one original recording vote, propagated updates,
unchanged other fits and shared intensity, exact rollback, absent metadata and
preserved human feedback. Numeric fixtures are controlled synthetic inputs.

`./experiments/run.py cruise-production --directory
experiments/audio_lab/local/mapped-vibes-20261029/cruise-production-2` uses
the production preview, loader, preparation worker and serving readers with real
frozen capture files. **186 first/all-available inference cases** preserve the
shared classification and five other scores exactly. The new, isolated database
contains **88 actual recordings**, with **77** complete compatible recordings;
all 77 receive the individual Cruise fit. Incomplete captures remain fallback.
The Home affinity reader consumes the new fits and shared radio intensity is
unchanged. Rollback restores all six fits exactly; immutable measurement history
and relational integrity are preserved. No local human labels supply outcomes.

All phases converge under the unmodified production batch/time budgets:
metadata-only preparation 0.58 seconds, first excerpts 0.24, complete shared
captures 1.68, Cruise rollout **2.18**, rollback **1.70**. The longest rollout
capture slice is **0.42 seconds**. These are elapsed work times excluding
scheduling idle time. All product Python source hashes stay unchanged during
the replay. Runtime: Python 3.14.8, NumPy 2.4.6, SciPy 1.18.1,
threadpoolctl 3.7.0, one numeric thread and normal machine lock.

This cohort reconstructs original captured titles/artists and audio, without
provider or collaborative history. It establishes production artifact routing,
bounded preparation and consumer reads, not a full-account migration, complete
Mix/Radio listening outputs or public release acceptance. The first attempted
replay is preserved separately: its harness rejected a floating capture duration
as integral catalog metadata, before preparation. The corrected harness omits
that optional catalog field; it does not alter captured JSON.

`./experiments/run.py cruise-wheel --directory
experiments/audio_lab/local/mapped-vibes-20261029/cruise-wheel-1` builds an offline
wheel and checks the production module and both bundled JSON artifacts byte for
byte. An isolated fresh process imports the extracted wheel, loads the bundled
Cruise model and scores an embedding without importing `experiments`. This
passes; wheel SHA256 is
`68386820b4e5c8feb2feb4ad45c52dfd6703381f52ec7067235878d9469f83f7`.
It does not test installers or public redistribution.

Broader compatibility checks:
`uv run pytest --test-lock-wait=60 tests/test_audio_vibe_model.py
tests/test_audio_vibe_embeddings.py tests/test_home_vibe.py
tests/test_architecture.py` gives **91 passed / 9 failed in 131.10 seconds**.
The failures occur in concurrent schema/publication work: one fixture writes
the retired `entry_field_choices` view; four Home fixtures reference a missing
recommendation candidate policy; architecture checks find the playback-feedback
import cycle, repository imports of recommendation policy, SQL in model refresh
and unguarded repository connection access. None of these traces enters the new
Cruise scorer. These are recorded as remaining integration failures, not a green
suite or completed rendered-Home acceptance. The focused Cruise/preparation and
real-capture consumer checks above remain successful.

After moving inference into production, `./experiments/run.py check --test
test_cruise_specialist.py` passes **28 checks in 1.24 seconds**. The research
wrapper now exercises the production loader and scorer, including source-plan
provenance, decompression bounds and independent source replay controls.
