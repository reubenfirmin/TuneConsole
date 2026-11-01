# Browser preparation and external-label quality (#424)

29 October 2026. This experiment holds the production shared and individual
Cruise models fixed and changes how supplied audio becomes model input. It
tests whether earlier external-label quality results carry through the
browser's decode/downmix/resample operations. There is no fitting, decoder
selection, local-label tuning or change to production assets.

**Completed:** all 1,310 recordings and 2,191 inputs, all fifteen production
browser checks, and all twenty supported dataset/vibe quality comparisons.
The individual Cruise model retains its advantage over the shared scorer on
all three primary valence/arousal cohorts under browser preparation. It still
loses on the supplementary Soundtracks target. Keep the mainlined Cruise model
and the shared model for the other five vibes; these findings do not justify a
decoder change. Use the browser-prepared inputs as a fixed additional control
for subsequent individual-model experiments.

## Frozen comparison

| Cohort | Recordings | Ten-second inputs | Label coverage |
| --- | ---: | ---: | --- |
| ManyMusic natural | 188 | 564 | Complete selected thirty-second rated intervals |
| ManyMusic generated | 95 | 285 | Complete selected thirty-second rated intervals |
| Memo2496 pilot | 63 | 378 | Complete fifteen-to-seventy-five-second expert trajectories |
| Soundtracks | 470 | 470 | Historical center ten seconds, human excerpt labels |
| CAL500 | 494 | 494 | Historical center ten seconds, human whole-song activity votes |
| Total | 1,310 | 2,191 | The same rows and analyzed intervals as the production baseline |

These are all five cohorts used by the production baseline, not all datasets
ever studied or all recordings released by their publishers. The primary
identity exclusions retain 186, 95, 63, 470 and 450 recordings respectively.
Prior artist/recording exclusions, five grouped folds, native human targets,
fixed attribute-to-vibe rules and chunk order remain unchanged. These cohorts
were previously inspected; this is a paired preprocessing comparison, not a new
unseen holdout. Unknown artist aliases and upstream pretraining overlap remain
unknown. Soundtracks/CAL500 do not gain complete recording coverage in this run.

Three policies are retained:

- **Stored:** exact historical frozen embeddings used for the published baseline.
- **FFmpeg:** freshly decode each historical ten-second interval with the
  original FFmpeg mono/resample command and run the independent native encoder.
- **Browser:** decode the original supplied audio file to 44.1 kHz in Chrome,
  then render each saved interval separately to sixteen-kilohertz mono using
  the same OfflineAudioContext operations as the extension. No FFmpeg-generated
  container is placed ahead of the browser decoder in this arm.

Both refreshed policies use a persistent independent C++ Essentia frontend and
the original MusiCNN graph, with full nonoverlapping 187-frame patches. Three
fixed recordings per cohort also run the production browser worker on exactly
the prepared PCM. Those fifteen checks must agree within 0.0005 maximum absolute
embedding error. Refreshed FFmpeg embeddings must also agree with the stored
features within that limit. Failed or partial recordings cannot be dropped to
obtain a quality result.

The browser arm uses original files through the production decode/render API
sequence. It does not repeat HTMLMediaElement playback, captureStream and
MediaRecorder for every dataset item. Actual component-capture and cleanup
evidence remains in [the preceding browser evaluation](BROWSER_EVALUATION_424.md).
This distinction matters when applying results to different delivery codecs.

## Models, aggregation and measurements

The exact active model files reproduce the earlier baseline hashes:

- Shared: `5351572e6eb1bc17f80a6519b9b3f1b8461775726591aa847077a4fdb55732a0`.
- Cruise: `3927a3294ad7a1670ef7f81d5c6119943991a699de60a7d5a60022851ba304eb`.

The shared model receives each recording's mean chunk embedding and retains its
existing native/calibration/clipping/normalization stages. Cruise retains its
production policy: predict native attributes for each excerpt, average native
attributes, apply the fixed Cruise mapping, then combine the two experts equally.
The five other suitability scores and overall classification remain shared.
No six-way maximum is taken over mixed shared/individual score scales.

Use the original fold-mean, tie-aware graded NDCG, top lift, Spearman and squared
error. Reproduce the historical shared metrics before interpreting new results.
Compare the active model's browser versus stored preparation for every supported
vibe; compare Cruise versus shared under both preparations where Cruise has
supported targets. Soundtracks Cruise is supplementary to the three primary
valence/arousal cohorts. Unsupported source/vibe pairs remain absent.
Ranking is the primary cross-model comparison. Shared scores compete across
vibes while Cruise scores are independent suitability values; their squared
errors are recorded without treating the two scales as calibrated probabilities.
Within-model changes isolate the observed preparation sensitivity.

Paired intervals use 1,000 identity-group bootstrap draws within the original
folds, seed 424530. They are conditional on the frozen models, prior inspection
and observed identities, with no multiple-comparison correction. Failure to
detect a difference does not establish equivalence. There is no chosen decoder,
retraining or automatic promotion based on these outcomes.

## Reproduce and inspect

```sh
./experiments/run.py preprocessing-quality freeze --directory experiments/audio_lab/local/preprocessing-quality-20261029/NEW
./experiments/run.py preprocessing-quality extract --directory experiments/audio_lab/local/preprocessing-quality-20261029/NEW
./experiments/run.py preprocessing-quality evaluate --directory experiments/audio_lab/local/preprocessing-quality-20261029/NEW
./experiments/run.py preprocessing-quality status --directory experiments/audio_lab/local/preprocessing-quality-20261029/NEW
./experiments/run.py preprocessing-quality-report --directory experiments/audio_lab/local/preprocessing-quality-20261029/NEW
./experiments/run.py check --test test_preprocessing_quality.py
```

The current campaign is `local/preprocessing-quality-20261029/run-1/`, plan SHA256
`f4785070198f06a55cd200556df177d3096fa446c3a630ef687752b2787d1579`.
Its frozen plan archives source, runtime, model and annotation inputs. Per-case
receipts preserve hashes and both embeddings; temporary PCM is removed after a
successful case. Existing cases are verified before reuse. Heavy extraction
and evaluation acquire the shared machine lock and use one native CPU thread.

Harness checks: **13 passed in 1.22 seconds**. Research syntax: **124 files parsed**.

## Completed extraction and numerical controls

All **1,310 recordings / 2,191 ten-second inputs** completed. No failed or
partial recording was dropped. All fifteen production-browser checks and all
historical-cache comparisons pass the declared 0.0005 embedding-error limit.

| Cohort | Historical versus refreshed FFmpeg maximum error | Production-browser maximum error | Browser checks |
| --- | ---: | ---: | ---: |
| ManyMusic natural | 0.00000985 | 0.00001170 | 3/3 |
| ManyMusic generated | 0.00001272 | 0.00001236 | 3/3 |
| Memo2496 pilot | 0.00000676 | 0.00001089 | 3/3 |
| Soundtracks | 0.00000395 | 0.00001287 | 3/3 |
| CAL500 | 0.00000699 | 0.00001503 | 3/3 |

Runtime: Chromium 148.0.7778.96, FFmpeg 7.1.5, Python 3.14.8, NumPy 2.4.6
and `essentia-tensorflow==2.1b6.dev1438` (native runtime reports
`2.1-beta6-dev`), AMD Ryzen 7 5700G, Linux 7.2.7. Extraction used one native
inference thread and the normal shared machine lock. The full extraction after
the five-recording startup check took 1,604.6 seconds. This throughput includes
browser checks and file preparation; it is not a per-track extension budget.

## Completed human-label quality comparison

All twenty historical shared-model NDCG and MSE results reproduce within the
declared 1e-12 tolerance before the new comparison. Refreshed FFmpeg ranking
results equal the stored results. Every paired contrast has 1,000 usable draws.

The following table uses the **active production model** for each vibe: the
individual model for Cruise, shared for the other five. Values are fold-mean
NDCG. The interval concerns browser minus stored preparation with that model
held fixed. Focus is the artifact's `concentrate` label and uses the original
Reading/Studying vote fractions.

| Cohort | Vibe | Stored NDCG | Browser NDCG | Difference | Conditional 95% interval |
| --- | --- | ---: | ---: | ---: | --- |
| ManyMusic natural | Relax | 0.7579 | 0.7571 | -0.0008 | [-0.0201, +0.0103] |
| ManyMusic natural | Cruise | 0.8358 | 0.8393 | +0.0035 | [-0.0289, +0.0420] |
| ManyMusic natural | Energize | 0.7960 | 0.7710 | -0.0250 | [-0.0450, +0.0148] |
| ManyMusic natural | Uplift | 0.6919 | 0.6835 | -0.0084 | [-0.0324, +0.0109] |
| ManyMusic generated | Relax | 0.8607 | 0.8297 | -0.0310 | [-0.0649, +0.0052] |
| ManyMusic generated | Cruise | 0.8943 | 0.8697 | -0.0246 | [-0.0436, +0.0060] |
| ManyMusic generated | Energize | 0.9187 | 0.8963 | -0.0224 | [-0.0344, +0.0283] |
| ManyMusic generated | Uplift | 0.9219 | 0.9395 | +0.0176 | [-0.0143, +0.0423] |
| Memo2496 pilot | Relax | 0.8469 | 0.8468 | -0.0001 | [-0.0243, +0.0046] |
| Memo2496 pilot | Cruise | 0.8646 | 0.8841 | +0.0194 | [-0.0293, +0.0835] |
| Memo2496 pilot | Energize | 0.9160 | 0.9187 | +0.0027 | [0.0000, +0.0478] |
| Memo2496 pilot | Uplift | 0.9162 | 0.9258 | +0.0096 | [-0.0381, +0.0255] |
| Soundtracks | Relax | 0.5799 | 0.5473 | -0.0326 | [-0.0564, +0.0104] |
| Soundtracks | Cruise | 0.7102 | 0.6963 | -0.0139 | [-0.0349, +0.0070] |
| Soundtracks | Energize | 0.8489 | 0.8488 | -0.0000 | [-0.0133, +0.0214] |
| Soundtracks | Uplift | 0.7318 | 0.7513 | +0.0195 | [-0.0100, +0.0468] |
| Soundtracks | Release | 0.6217 | 0.6086 | -0.0131 | [-0.0369, +0.0353] |
| CAL500 | Relax | 0.5209 | 0.5457 | +0.0248 | [-0.0305, +0.0366] |
| CAL500 | Focus | 0.4279 | 0.3940 | -0.0339 | [-0.0577, +0.0259] |
| CAL500 | Energize | 0.3346 | 0.3043 | -0.0302 | [-0.0456, +0.0351] |

Thirteen of twenty point estimates decrease. All twenty intervals include
zero, including Memo Energize's boundary at zero. This experiment therefore
does not establish either a preparation-induced ranking loss or equivalence.
The largest observed decrease is CAL500 Focus (-0.0339); the largest increase
is CAL500 Relax (+0.0248). Do not select a different preparation per dataset or
vibe from these results.

### Cruise versus the shared model

Both columns below now use browser-prepared inputs on identical rows. The
interval concerns individual Cruise minus shared Cruise, not browser minus
stored preparation.

| Cohort | Shared NDCG | Individual NDCG | Gain | Conditional 95% interval |
| --- | ---: | ---: | ---: | --- |
| ManyMusic natural | 0.6616 | 0.8393 | +0.1777 | [+0.1008, +0.2399] |
| ManyMusic generated | 0.6988 | 0.8697 | +0.1708 | [+0.0846, +0.2614] |
| Memo2496 pilot | 0.7845 | 0.8841 | +0.0996 | [+0.0194, +0.1766] |
| Soundtracks, supplementary | 0.8128 | 0.6963 | -0.1165 | [-0.1731, -0.0630] |

The original primary-cohort recommendation survives browser preparation.
Memo's browser comparison has a positive conditional interval; its stored
comparison remains uncertain (+0.0491, [-0.0415, +0.1414]). This is the same
previously inspected pilot, not independent new confirmation of a general gain.
Soundtracks remains material counterevidence: its tension/anger/energy mapping
differs from the primary valence/arousal mapping, but that does not erase the
observed loss. Browser preparation does not repair this transfer weakness.

Browser Cruise top lift is +0.0526, +0.0698 and +0.0581 on the three primary
cohorts, versus -0.0738, -0.0716 and -0.0221 for shared. Soundtracks reverses
that result: -0.0575 for individual and +0.0372 for shared. The full report
retains Spearman, MSE and per-fold values for every comparison. Cruise's
within-model MSE changes from stored to browser are 0.02655 to 0.02583,
0.01243 to 0.01277, 0.01755 to 0.01724 and 0.05356 to 0.05345 respectively.
These errors do not make shared and individual outputs comparable calibrated
probabilities.

### Score sensitivity and product recommendation

Preparation changes individual active scores by as much as **0.08689**
(natural ManyMusic Energize). Twelve retained recording/vibe pairs change by
more than 0.05; these are not twelve distinct recordings. Cruise's maximum
changes by cohort are 0.05846, 0.03233, 0.05807 and 0.05041. The model-input
policies are observably different even though this experiment does not
establish a ranking-quality change. Do not relabel historical caches as
browser-prepared data.

Keep the current browser pipeline and mainlined Cruise model. Retain the best
validated shared model for the other five vibes. For #576–#580, reuse these
browser-prepared inputs and the frozen current artifacts as explicit controls;
compare the next individual candidates under the production preparation and
document any feature-policy change before regenerating training features.
Preserve the Soundtracks Cruise failure as a transfer check for later work.
There is no empirical basis here for an arithmetic-gain correction or another
production preprocessing change.

This completes the preprocessing-quality comparison requested for #424. The
earlier technical capture/resource evidence remains valid within its stated
scope. The user declined additional human listening, pairwise intensity and
whole-mix judgments: #584 is won't fix, and those observations are not required
for acceptance. The user's ongoing feedback is track labeling and future issue
reports. Existing external annotations and local labels remain the quality
references; unmeasured subjective outcomes remain unknown. #587 retains
automated comparisons and #573 retains actual-account engineering acceptance
as separate work. #424 is ready for independent review of its completed scope.

Final receipts are `quality.report.json`, `predictions.npz`,
`evaluation.receipt.json` and the verified compact `progress.report.json`
under the frozen campaign. `status` reports all 1,310 cases complete, no failed
attempts and `quality_evaluated: true`. No production assets or database were
changed, and this implementation agent did not run the full suite.

The user accepted the documented model terms. Licensing is not a blocker for
this experiment; retain the existing notices and use the taste-model UI for a
visible credit if packaging requires it. This comparison does not redistribute
the supplied audio or change the user interface.
