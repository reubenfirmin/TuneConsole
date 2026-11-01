# Browser audio evaluation (#424)

29 October 2026. This report separates numerical agreement, decoding sensitivity,
resource cost and human musical-fit evidence. Cruise remains the active individual
backend scorer; the other five vibes retain the validated shared model. This
evaluation does not introduce another enablement stage.

The subsequent [preprocessing quality experiment](PREPROCESSING_QUALITY_424.md)
completed 1,310 recordings and 2,191 inputs across all five production-baseline
cohorts, comparing unchanged models against external human targets. Cruise's
advantage survives browser preparation on the three primary cohorts; its
supplementary Soundtracks loss remains. All twenty preparation contrasts have
intervals including zero, which does not establish equivalence.

## Fixed references and method

`local/evaluation-424-20261029/run-2/plan.json` freezes **24 supplied recordings
and 32 ten-second model inputs** before inference. Selection uses fixed Emotify
IDs and SHA256 ordering of recording IDs, without using targets or model scores:

| Source | Distinct recordings | Excerpts | Human annotations |
| --- | ---: | ---: | --- |
| Emotify | 8 | 16 | Nine induced-emotion vote fractions |
| ManyMusic natural | 4 | 4 | Released thirty-second listener valence/arousal |
| ManyMusic generated | 4 | 4 | Released listener valence/arousal, separate stratum |
| Memo2496 pilot | 8 | 8 | Expert valence/arousal trajectories |

Four fixed Emotify recordings retain early, center and late observations in
order. Other references use the center chunk of the already frozen annotated
interval. The complete ID, audio hash, source annotation unit, start and supplied
artist/title fields are in the plan. Emotify IDs are 1, 2, 101, 102, 201, 202,
301 and 302. Memo's publisher removed artist metadata; its file identities and
audio hashes do not establish artist-independent quality evaluation.

The browser receives a lossless stereo 48 kHz WAV containing 10.05 real seconds;
its fixed render consumes exactly the first ten seconds. The extra 50 ms avoids
rounding a resampled input to 440,999 samples. No missing samples are padded.
The first attempt's exact-ten-second container was rejected before inference;
`run-1/` is retained as a failed harness attempt, not included in the results.

The experiment snapshots the current production worker, weights, runtime and
capture/frame files. It uses the production 44.1 kHz decode and 16 kHz mono
offline-render policy. The native reference independently runs Essentia's C++
`TensorflowInputMusiCNN` and original `.pb` graphs on the **exact browser PCM**.
Both use full nonoverlapping 187-frame patches, 512-sample frames and hop 256.
Published default-wrapper overlap is a different policy, already reported in
`EXTERNAL_EXPERIMENTS.md`; agreement here does not erase that distinction.

Two independent FFmpeg decodes of the same WAV measure input-policy sensitivity:
its original `-ac 1` policy, and explicit arithmetic stereo averaging. Their
embeddings and current Cruise scores are compared with the browser PCM results.
Those observations do not refit a model or change the historical training
features. All retained capture containers are also decoded again to test exact
browser-input reproducibility.

Eight additional runs exercise actual production element capture, PCM
MediaRecorder, frame handoff and current packaged inference: four fixed Emotify
recordings with ordinary affinity, then the same recordings with one CPU.
The driver starts after affinity is set, and the actual browser root affinity
is checked. One-core affinity is a sensitivity check on this desktop, not a
second hardware class. The production capture, frame and model files run in a
minimal MV3 audit manifest with the production extension key and CSP. The player
serves supplied audio at a controlled YouTube Music origin. This checks that
component path; it does not exercise the normal background scheduler or a signed-in
YouTube Music account. Network requests outside the controlled player/extension
are blocked; microphone, display capture and live audio contexts are forbidden.
This harness retains raw audio only in ignored experiment outputs.

## Budgets declared before running

| Observation | Budget |
| --- | ---: |
| Matched-PCM maximum absolute embedding error | 0.0005 |
| Matched-PCM maximum tag-score error | 0.0001 |
| Matched-PCM maximum mood-score error | 0.0002 |
| Worker wall time per ten-second excerpt | 25 seconds |
| Capture plus analysis wall time | 45 seconds |
| Browser process-tree CPU time | 25 core-seconds |
| Browser process-tree peak PSS | 768 MiB |

These are explicit local evaluation limits, below the production frame's
30-second worker timeout. They are not measured laptop battery limits. Resource
sampling is every 100 ms; PSS apportions shared pages and can miss shorter peaks.
Worker startup is included. Native-reference startup and diagnostic graphs are
excluded from browser costs. This desktop has no battery, so no battery-life
claim follows from its CPU measurements.

## Completed 32-excerpt comparison

All 32 current-worker/native comparisons pass the declared numerical limits.
Maximum absolute differences on identical PCM are **0.0000209014** for the
200-value embedding, **0.0000010282** for the 50 tags and **0.0000015994** for
the mood scores. Every current-worker run passes the local resource budgets.

| Cohort | Worker wall seconds | Browser CPU seconds | Peak browser PSS MiB |
| --- | ---: | ---: | ---: |
| Emotify, 16 excerpts | 9.82–11.44 | 10.73–12.34 | 325.5–384.7 |
| ManyMusic natural, 4 excerpts | 10.37–10.56 | 11.25–11.44 | 384.2–405.3 |
| ManyMusic generated, 4 excerpts | 10.28–10.79 | 11.13–11.67 | 396.4–410.5 |
| Memo2496, 8 excerpts | 10.26–11.23 | 11.09–12.19 | 376.2–419.4 |

Runtime: Chromium 148.0.7778.96, Python 3.14.8, NumPy 2.4.6 and native
`essentia-tensorflow==2.1b6.dev1438`; AMD Ryzen 7 5700G, Linux 7.2.7,
16 allowed logical CPUs. The worker is new for each excerpt; the browser is
reused within each command. Process memory therefore includes retained browser
pages and cache, rather than only the model's tensor allocation.

### Independent decoding is measurably different

The following are maximum **absolute Cruise score changes** against native
inference on browser PCM. They are sensitivity measurements, not accuracy gains:

| Cohort | Original FFmpeg mono | Explicit arithmetic mono |
| --- | ---: | ---: |
| Emotify | 0.02731 | 0.02739 |
| ManyMusic natural | 0.05261 | 0.01129 |
| ManyMusic generated | 0.01780 | 0.00663 |
| Memo2496 | 0.06456 | **0.11582** |

Arithmetic averaging reduces PCM RMS error across these inputs, but it does
**not** guarantee a smaller model-score difference. Memo2496:1096 is the largest
counterexample: arithmetic-decoded and browser-PCM Cruise differ by 0.11582,
with embedding maximum difference 2.35434. The residual is not explained by
matching channel weights alone. The original decoder's largest embedding
difference is 2.29526. Full PCM residual and frequency summaries are retained.

All four retained capture containers reproduce their original browser PCM
**bit for bit**: RMS and maximum sample differences are zero. Independent
FFmpeg decoding still differs. Its default mono policy has projected amplitude
ratios of 1.383–1.416; explicit arithmetic averaging leaves residual RMS errors
from 0.0000228 to 0.02377. Matching the browser's intermediate sample rates in
FFmpeg does not remove those residuals. This rules out a changed capture replay
as the explanation, without claiming the remaining decoder mechanism is solved.

A supplementary replay of the exact shared model's scalar stages observes one
shared top-choice change across 32 original-decoder comparisons and two with
arithmetic averaging. Maximum shared-score changes are 0.07714 and 0.11852,
respectively. This diagnostic records its runtime source separately; it does
not refit, select a decoder or take a six-way maximum over mixed Cruise/shared
score scales. Agreement of the encoder on identical PCM therefore **passes**;
equivalence of the complete browser and historical FFmpeg pipelines **does not**.

The ordered browser Cruise observations also vary across a recording: early,
center and late ranges are 0.06616, 0.08042, 0.03656 and 0.01383 for the four
fixed Emotify IDs. Keep the actual excerpt coverage and arc. Their minute-level
annotations do not certify a particular ten-second section or an unheard song.

## Completed capture and constrained-CPU checks

All eight controlled MV3 capture runs pass the declared numerical, worker,
capture, CPU and memory limits. The maximum matched-PCM errors are 0.0000100732
for embeddings, 0.0000003006 for tags and 0.0000013412 for moods.

| Profile | Runs | Capture plus analysis seconds | Worker seconds | Browser CPU seconds | Peak browser PSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ordinary, 16 allowed CPUs | 4 | 20.99–21.28 | 10.07–10.29 | 11.71–11.97 | 491.3–509.4 |
| One allowed CPU | 4 | 23.37–25.24 | 12.27–14.05 | 10.67–12.17 | 491.8–507.8 |

The recorded browser-root affinities match each declared profile. Playback
position advances through capture and inference in every run, with no reported
waiting, stalled or media-error events, and remains unpaused when results arrive.
All decoded channel buffers are zeroed, captured stream tracks end and the
analysis iframe is removed. These are observed playback and cleanup checks;
there is no human audibility rating or battery measurement. A single pinned CPU
on this desktop does not establish performance on a low-power laptop.

## Human quality and product coverage

The existing external comparisons already use human dataset labels transformed
by fixed attribute-to-vibe rules. They do not evaluate against our predictions.
The [baseline comparison](CRUISE_BASELINE_RESULTS.md) reports Cruise NDCG of
0.8358 versus 0.6669 on natural ManyMusic, 0.8943 versus 0.7022 on generated
ManyMusic, and 0.8646 versus 0.8156 on Memo. The Memo difference remains
uncertain. The other five vibes retain the validated shared model because the
refreshed shared controls regress on several supported tasks. Focus uses actual
Reading/Studying votes; unsupported source/vibe pairs remain absent.

Those are ranking results on the documented frozen feature policy. Numerical
agreement on a common PCM input does not make a different decoder's quality
identical. The [completed larger comparison](PREPROCESSING_QUALITY_424.md)
now measures quality under browser preparation directly. Its primary-cohort
Cruise NDCG is 0.8393 versus shared 0.6616, 0.8697 versus 0.6988 and 0.8841
versus 0.7845. Soundtracks reverses that comparison: 0.6963 versus 0.8128.
Neither this 24-recording technical sample nor the larger previously inspected
cohorts are a new unseen holdout.
Emotify is part of existing shared-model training and supplies no new shared
holdout accuracy here. No labels or thresholds are selected from these runs.

The [production replay](CRUISE_PRODUCTIONIZATION.md) supplies actual captured
library evidence: 93 playback groups, 186 partial/all-available inference cases,
88 recordings in an isolated preparation database, 77 complete compatible
recordings receiving Cruise, and exact preservation of five other fits and
shared intensity. Missing/incomplete capture remains fallback. This is coverage
of that captured snapshot, not coverage of the whole account or unheard music.

The user explicitly declined additional human listening, pairwise intensity and
whole-mix judgments. Those requirements are **won't fix** under #584, not
outstanding acceptance gates. Future user feedback consists of track labeling
and issue reports when behavior is wrong. Existing external human annotations
and local labels remain the quality references. #587 retains automated
label-based and complete-output comparisons, with unsupported subjective
outcomes reported as unknown. #573 owns actual-account startup and consumer
acceptance separately. Neither follow-up blocks review of this completed browser
evaluation. No model score is substituted for a human judgment, and sparse
metadata is not counted as a false recording association. The controlled
supplied-file joins do not estimate real-world version-matching errors.

## Exact artifacts and distribution terms

The snapshot records every file hash. Its packaged runtime is
`essentia-csp-audio-2`, 1,277,103 bytes, SHA256
`1fee0eeb91fdf6f0a655646b9a4c81f7b770e6cd7f28fcb0f1d72771c7e9748f`.
The build manifest names Essentia revision
`d65623a92ab1d26803c5d8656a3ad7c5b2929031`, Emscripten 3.1.64, KISS FFT and
Eigen 3.3.7. MusiCNN's weight shard is SHA256
`7ebfb970c7389b6276ffc34f0d7fc80ba2765e5310829f3618ad5b977326667d`.
The five original mood heads, their metadata, weights and native graph checksums
are pinned separately. The new compact Cruise artifact is separately identified
in the plan; its weights are not the upstream MusiCNN weights.
The complete snapshotted `audio_model` directory is about 6.2 MB of logical
storage (`du -sb` includes directory entries); that is not a compressed download
size. The snapshot includes the code, TensorFlow.js, runtime, heads and notices.

| Component | Observed terms and evidence |
| --- | --- |
| Rebuilt Essentia runtime | AGPL-3.0 text in `vendor/essentia-LICENSE`; exact build source/flags in `scripts/audio_model/`. KISS FFT is BSD style; Eigen is MPL-2.0. Audit notices for the actual linked build when packaging. |
| TensorFlow.js 3.18.0 | Apache-2.0 notice embedded in exact `vendor/tf.min.js`; retain its full licence and any required notices in a distribution. |
| Meyda 5.6.3 | MIT text in `vendor/meyda-LICENSE`, SHA256 `15be9b78aa3f0b48a2ef63359b0cacfd73e61ac9244818db8086d858fc7f8121`. |
| MusiCNN and pretrained mood weights | The exact `vendor/model-LICENSE` is SHA256 `f0c076094cc08acb265ea3304ec522cf331917d19bdd271ed69f10ef174f3ea4`. Its heading and full model legal text specify CC BY-NC-ND 4.0; its introductory ShareAlike link and adaptation summary conflict with that text. |

UPF's current [licensing page](https://essentia.upf.edu/licensing_information.html)
states CC BY-NC-ND 4.0 for pretrained models and offers proprietary terms on
request. The [model directory](https://essentia.upf.edu/models/) still serves
the inconsistent introductory wording. For this audit, use CC BY-NC-ND 4.0,
matching both the bundled full legal text and the current upstream statement;
do not infer a ShareAlike grant from that link. The
[legal code](https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en)
permits noncommercial sharing subject to its conditions and explicitly allows
technical modifications necessary for other media/formats in section 2(a)(4).
NoDerivatives therefore does not by itself establish that a format conversion
requires another agreement. Commercial use or sharing adapted material beyond
that permission would require a separate basis. Preserve attribution and notices.
This audit has not established a present licensing blocker for the noncommercial
evaluation, nor established that our separately fitted Cruise coefficients are
adapted upstream weights. Earlier wording that required generic licensor
clarification before proceeding was too broad. Nobody has contacted the licensor
or obtained a separate agreement as part of this experiment.

Audio permission is separate. Natural ManyMusic rows preserve their individual
Jamendo licence URLs and publisher credits. The [ManyMusic publisher](https://manymusic.net/stim/)
states CC BY-NC-SA 4.0 for generated audio, metadata and ratings. The
[Memo2496 record](https://figshare.com/articles/dataset/Memo2496/25827034)
lists CC BY 4.0; stripped performer identities remain unknown. Emotify's
[research release](https://www2.projects.science.uu.nl/memotion/emotifydata/)
supplies the excerpts and requests citation of Aljanaki, Wiering and Veltkamp
(2015). That page does not grant a recording-specific redistribution licence.
These eight rows retain a missing individual licence URL explicitly.
Dataset-level terms do not replace missing recording-specific credits. No audio
from this campaign is added to a public package.

## Decision and remaining work

Keep the mainlined Cruise backend scorer and the validated shared model for the
other five vibes. This evaluation adds no new encoder work for Cruise: it uses
embeddings the extension already sends. The small backend scoring cost and the
browser's roughly eleven seconds of CPU work are different parts of the path.

The current encoder passes numerical agreement under a common input and frame
policy. It does not establish equality with historical independently decoded
training inputs. The completed preprocessing-quality comparison reproduces the
historical metrics and evaluates browser-prepared inputs on all five baseline
cohorts. Active-model NDCG differences range from -0.0339 to +0.0248; all twenty
conditional intervals include zero. That is not a demonstration of equivalence
or a basis for selecting a decoder. Future revisions should retain a frozen
decode/downmix/resample contract and compare against human external targets
and the current artifacts using the same documented annotation windows.
If the input policy changes, version it and regenerate the affected features;
do not relabel old caches or apply a gain adjustment selected on these results.
Arithmetic averaging alone is not an accepted repair: the Memo counterexample
shows why waveform closeness is insufficient.

Do not adopt additional predicted traits or claim better whole-list quality
from these numerical checks. The existing external ranking evidence supports
the current Cruise choice. Additional human listening evaluation is won't fix
at the user's direction; whole-sequence listening benefit remains unmeasured.
#424's implementation and experiments are complete and ready for independent
review. Automated follow-up (#587) and actual-account acceptance (#573) retain
their own scopes. Respect the observed model terms when distributing the
package; this audit adds no licensing-clarification gate.
No shadow phase or staged promotion is proposed.

## Reproduce and inspect

```sh
./experiments/run.py audio-evaluation freeze --directory experiments/audio_lab/local/evaluation-424-20261029/NEW
./experiments/run.py audio-evaluation decode --directory experiments/audio_lab/local/evaluation-424-20261029/NEW
./experiments/run.py audio-evaluation compare --directory experiments/audio_lab/local/evaluation-424-20261029/NEW --limit 32
./experiments/run.py audio-evaluation capture --directory experiments/audio_lab/local/evaluation-424-20261029/NEW --limit 8
./experiments/run.py audio-evaluation-report --directory experiments/audio_lab/local/evaluation-424-20261029/NEW
./experiments/run.py check --test test_audio_evaluation.py
```

Heavy commands acquire the normal machine test/build lock. Completed case
receipts are checked before reuse. The verifier checks plan, input, snapshot and
output hashes, rejects changed budgets, and reports missing/failed cases instead
of converting partial execution into a pass. The focused checks cover target-
independent distinct-recording selection, mutated evidence, nonfinite/out-of-
budget errors and incomplete waveforms.

Final verification reports **32/32 comparisons, 8/8 capture cases and the four
retained-container decodes complete**, with empty numerical, resource and
playback failure lists. Every capture also passes the separately reported
25-second worker budget. `complete` describes execution of this frozen technical
campaign; it does not certify human quality, whole-account acceptance or licences.
The focused integrity checks pass: **10 tests in 0.53 seconds**. The research
entrypoint's syntax check also passes for 120 Python files. No full test suite
was run by this implementation agent.

The plan SHA256 is
`9e5bb513f3d45d65d13f180d149bdf1733c4b4d2c96246b2849c95ca45537303`.
Retained local evidence is in
`local/evaluation-424-20261029/run-2/{plan,summary,details}.json`, its per-case
receipts and `evaluation-source/manifest.json`. The latter binds archived
evaluation/runtime source to the plan; the plan pins exact audio, source-feature,
native-graph, model and extension snapshot hashes. Use the explicit directory
above when inspecting this run; the original command default points to the
preserved first attempt.
