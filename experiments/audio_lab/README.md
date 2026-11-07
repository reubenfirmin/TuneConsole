# Audio experiments

Chronological results, recipes and decisions. Run tools from the repository root
with `./experiments/run.py --help`; code lives in `../audio_lab_tools/`. Evidence
paths below are relative to this directory unless they start with `build/`.
Ignored artifacts contain exact inputs, source versions, terms, hashes and full
reports; they may be absent from a fresh checkout. Original prose remains in Git.
The other root Markdown files are frozen verifier inputs: preserve their bytes.
The [dataset index](datasets/README.md) defines each collection and links its
published source, downloads and the subset used here.
Setup belongs in the [extension reference](../../extension/README.md#audio-runtime-and-reference-harness)
and [model settings](../../src/yt_playlist/rec/README.md#audio-model-settings).

**Oct 25, 2026 — Browser extractor.** The original Essentia runtime failed MV3
CSP; replace it with pinned Essentia/KISS FFT and `DYNAMIC_EXECUTION=0`. Retain
200-value signed MusiCNN `model/dense/BiasAdd` embeddings, averaged over full
nonoverlapping 187-frame patches; ten seconds supplies 8.992 model seconds.
This extraction policy differs from the published overlapping wrapper. Build
identity and numerical checks are retained in the preprocessing evidence below.

**Oct 27 — Local correction objectives.** On 40 labeled tracks, legacy
hard-example fitting reached 26 acceptable choices, but nested whole-track
procedure selection reached only 22. Alternative-label NLL/Libra did not improve
it. Reject the individual development maximum as evidence of generalization.
Local labels are acceptable alternatives: unselected vibes remain unknown,
and teacher predictions add no human votes. Evidence: `local/external-20261028/`.

**Oct 28 — Native emotion/activity prediction.** MusiCNN ridge beat training
means on Emotify (MAE .11303/.14503), PMEmo (.08597/.14853) and Soundtracks Set 2
(.1121/.1890). CAL500 raw activity votes improved macro AP to .4171 from .2746;
sparse-label MAE concealed that ranking signal. Preserve native targets: felt
emotion, excerpt emotion, chorus VA and whole-song activity are distinct.
Evidence: `local/external-20261028/`.

**Oct 28 — Shared classifier selection (#507).** Nine emotion heads trained on
400 Emotify excerpts, calibrated to six vibes using 55 human-labeled songs,
gave 38/55 acceptable choices versus embedding-only 34/55. Select
[`emotify-vibes.json`](../../src/yt_playlist/rec/data/emotify-vibes.json); exclude
63 prediction-labeled songs. Combined acoustic inputs and Emotify/4Q blends
did not improve it. The heuristic still had better ranking AP (.602/.528), so
this reused developer result supports classification only. Evidence: `local/external-20261028/`.

**Oct 28–29 — Expanded native sources (#530).** Acquired Coimbra (903 clips),
4Q (900), VGMIDI (76 usable renders), MERGE (3,553), matched EMMA previews,
AMG1608 (404 existing-audio intersections), NTWICM (55) and MERP (50 after DEAM
exclusions). Several native tasks beat constants; Emotify+ embedding MAE did
not (.12523/.12445). None established a shared six-vibe upgrade. MERGE/4Q overlap,
weak song/excerpt joins and missing artist identities limit independence.
Exact versions and exclusions: `local/external-20261028/checkpoint-1/`.

**Oct 29 — Broad replacement search (#530).** Alternate learners/calibrators,
expert subsets, aligned targets, PCA/whitening, multitask fits and unlabeled
consistency failed to establish a broad replacement; local soft-target selection
chose zero weight. On 18 reserved additions excluding prior silver exposure,
shared scored 12/18 versus consensus/PCA 13/18, with inconclusive paired gains
and no Release positives. Retain shared; that reserve is consumed.
Evidence: `local/external-20261028/checkpoint-1/`.

**Oct 29 — Independent per-vibe selection.** Four-source AP-selected heads
scored macro .397 versus incumbent .516; expanding to thirteen sources did not
justify replacement. Prefer declared native-target mappings to another maximum
selected on reused local labels. Evidence: `local/per-vibe-20261029/round-1/`
and `all-sources-1/`; protocols: [initial](PER_VIBE_ROUND.md),
[expanded](PER_VIBE_ALL_SOURCES.md).

**Oct 29 — Cruise transfer and adoption (#570–573).** ManyMusic supplied
188 natural/95 generated recordings; Memo supplied 63 labeled pilot recordings.
Equal PMEmo/DEAM VA experts achieved Cruise NDCG .8358/.8943/.8646 respectively.
Select [`cruise-vibes.json`](../../src/yt_playlist/rec/data/cruise-vibes.json):
PMEmo ridge (762 rows), DEAM random-feature ridge (1,798), clipped native
predictions averaged across excerpts before moderate-arousal/positive-valence
mapping and equal source averaging. Connected-component repairs supersede
affected AMG/NTW transfers. Evidence: `local/mapped-vibes-20261029/common-target-1/`.

**Oct 29 — Shared refresh and score contract.** Calibration-only and full shared
refits regressed other vibes and did not remove Cruise's primary advantage.
Keep shared classification/radio intensity separate from independent within-vibe
ranking; no shared .65 strong-match threshold transfers to specialists.
Verify with `./experiments/run.py cruise-baselines verify` and `candidates verify`.
Evidence: `local/mapped-vibes-20261029/cruise-baselines-1/`;
contract: [score semantics](VIBE_SCORE_CONTRACT.md).

**Oct 29 — Browser inference/resources (#424).** On identical browser PCM,
24 recordings/32 inputs matched native inference within .0000209014 embedding
error. Eight MV3 captures passed bounded lifecycle/resource checks; capture plus
analysis took 20.99–25.24 seconds and browser peak PSS 491.3–509.4 MiB.
Independent FFmpeg preparation failed decoder equivalence, including a .11582
Cruise difference. Accept common-input inference parity only. Evidence:
`local/evaluation-424-20261029/run-2/`.

**Oct 29–30 — Preprocessing quality (#424).** Across 1,310 recordings/2,191
inputs, Cruise retained browser-prepared ranking gains on natural/generated
ManyMusic and Memo but lost on supplementary Soundtracks. All twenty paired
preparation-contrast intervals included zero; this does not establish pipeline
equivalence. Preserve caches and version changed feature contracts. Exact
cohorts, measurements and commands: [frozen preprocessing report](PREPROCESSING_QUALITY_424.md).

**Oct 30 — Initial specialist fits (#576–580).** Focus's browser NDCG gain
.0314 had interval [−.0437,.1558]; Jamendo aggression's Release gain also crossed
zero. Relax's anchor was promising, while Energize/Uplift had domain failures.
All frozen decisions retained their predecessors. These fits motivated the
comparisons below; later adoption does not turn them into passing experiments.
Evidence: `local/individual-20261030/remaining-1/` and `local/individual-20261030/focus-1/`.

**Oct 30 — Release mapping/adoption (#614/#623).** Aggression/power blending
regressed browser anger ranking (.5342 versus .7112 for the protected aggression
head); improved native MSE did not rescue it. User selected the exact #577
187-row Jamendo aggression ridge, alpha 1000, as
[`release-vibes.json`](../../src/yt_playlist/rec/data/release-vibes.json), accepting
its uncertain gain. Clip per-excerpt predictions, then average. Aggression is
not measured anger relief. Evidence: `local/release-614-20261030/round-2/`.

**Oct 30 — Direct Focus versus emotion mapping (#615).** Two alpha-1000 ridge
heads trained on 494 CAL500 songs/1,677 listener responses predict Reading and
Studying fractions; clip per excerpt, average, then equally combine activities.
Direct browser NDCG .49860 beat shared .39985, but missed the frozen improvement
margin over the prior. Emotion consensus/hybrid did not improve direct mapping.
This exact export later became
[`focus-vibes.json`](../../src/yt_playlist/rec/data/focus-vibes.json).
Evidence: `local/focus-615-20261030/round-1/`.

**Oct 30 — Energize consensus/adoption (#616/#631).** Activation, joy-inclusive
consensus and exercise mapping failed advancement gates. User chose the no-joy
diagnostic as [`energize-vibes.json`](../../src/yt_playlist/rec/data/energize-vibes.json),
accepting domain losses including Memo lift −.017. Weights: DEAM/PMEmo arousal
.125 each, Soundtracks energy .25, Emotify power .25, ManyMusic natural/generated
arousal .0625 each, Memo pilot arousal .125. Clip native excerpt predictions,
average, then weight. Evidence: `local/energize-616-20261030/round-1/`.

**Oct 30 — Relax consensus/adoption (#617/#629).** Fixed consensus improved
equal-corpus NDCG by .04310 over prior embedding and .02887 over refitted anchor;
learned weights failed transfer limits. Select
[`relax-vibes.json`](../../src/yt_playlist/rec/data/relax-vibes.json): Emotify
(calmness + 1 − tension)/2 at .5; Soundtracks (2 − energy − tension + tenderness)/3
at .25; VA map (1 − arousal + valence)/2 from ManyMusic natural/generated at
.0625 each and Memo at .125. Clip native predictions per excerpt before averaging.
Evidence: `local/relax-617-20261030/round-1/`.

**Oct 30 — Uplift consensus/adoption (#618/#630).** Fixed positivity improved
mean NDCG by .04249 but failed domain limits, including generated-music regression.
User accepted that tradeoff in
[`uplift-vibes.json`](../../src/yt_playlist/rec/data/uplift-vibes.json).
Weights: DEAM/PMEmo valence .125 each, Soundtracks mean valence/happiness .25,
Jamendo happy/Emotify joy .125 each, ManyMusic natural/generated valence .0625
each, Memo valence .125. Clip native excerpt predictions, average, then weight.
Evidence: `local/uplift-618-20261030/round-1/`.

**Oct 30 — Focus representation/task/listener controls (#637/#638/#641).**
CAL500 residuals retained signal beyond linear emotion directions, but added
ranking gains were uncertain; task-specific slopes did not help. Moods and
Activities listener history improved endorsement MSE (.11046 to .09562), without
establishing personal music ranking. Help Me study! had only 18 complete episodes
and no verified track/window targets. No independent Focus benchmark emerged.
User then selected exact #615. Evidence: `local/focus-controls-637-638-20261030/round-1/`.

**Oct 30 — Provider and lineage audits (#585/#586).** Seventeen bounded
ReccoBeats requests across twelve cases added no verified recordings or fields;
GetSongBPM lacked configured access and was unmeasured. Reject provider adoption
from this pilot. Stored lineage/retraction checks establish provenance and
consistency only; predictions, provider scores and developer labels remain
distinct evidence. Reports: `local/provider-585/` and `local/evidence-586/`.

**Oct 30 — Whole-output ablations (#587).** An isolated 14,332-recording snapshot
produced metadata/signal/combined Mix and Radio outputs with zero violations in
81 restriction cases. Original publication/Radio budgets failed; continuations
took 265–276/48–57 seconds. Combined Relax/Focus cards used no direct specialist
slots, exposing coverage/scale gaps. The 85 positive-only local labels and absent
external crosswalk cannot establish list precision. Evidence:
`local/output-evaluation-587-20261030/round-4/` and `local-1/`.

**Nov 1 — Runtime helper extraction (#702).** Shared runtime helpers preserved
exact outputs for 84 real recordings, 159 composed predictions and 477 research
routing cases; installed-wheel replay retained all six specialists and seven
artifact identities. Accept inference parity, with no new quality claim.
Evidence: `local/specialist-cleanup-702-20261101/`; reproduce with
`./experiments/run.py investigate specialist_cleanup_702.py -k test_replay -s`.

**Nov 1 — More training windows (#707).** All 1,695 ManyMusic windows/3,000
responses were accounted for with total weight one per recording. Native ranking
and domain gates failed; retain one-window recipes. Generated music, with one
window per recording, remained the unchanged control. Evidence:
`local/many-windows-707-20261101/round-5/`; `./experiments/run.py many-windows verify`.

**Nov 1 — MERT representations (#708/#809).** Matched nested comparisons used
288 recordings/576 intervals across nine strata. Final-layer MERT (768 values)
and intermediate layers 5/6 (1,536) failed every mapped-vibe advancement gate,
despite intermediate native MSE improving 16/24 targets. Retain MusiCNN. Early
freezers omitted executable entrypoints; post-run archives cannot supply missing
prospective provenance. Evidence: `local/encoder-comparison-708-20261101/round-3/`
and `local/mert-layers-809-20261101/round-2/`.

**Nov 1 — Release artist extension (#709).** A frozen 41-track/34-artist candidate
pool yielded zero new audio after four HTTP503 responses. Result: inconclusive
acquisition, with no held-out aggression labels opened. Empty-extension refits
cannot establish added-artist benefit; preserve the cohort for a bounded retry.
Evidence: `local/release-extension-709-20261101/round-3/`;
`./experiments/run.py release-extension verify`.

**Nov 1 — Reserved Memo confirmation (#710).** Of 128 reserved recordings,
120 in 119 components had sufficient real audio; no padding or backfill.
Cruise/Relax comparisons were inconclusive, while Uplift/Energize hit regression
limits in both preparations. Retain specialists; absent artists and same-corpus
reuse limit independence. Evidence: `local/memo-confirmation-710-20261101/round-1/`;
`./experiments/run.py memo-confirmation verify`.

**Nov 1 — Focus count likelihoods (#711).** Binomial/beta-binomial heads improved
likelihood but lost ranking/lift: browser NDCG .48120/.45485 versus ridge .49860.
Discard count heads. Only 42 of CAL500's 1,677 responses carry listener IDs,
so unidentified listener dependence remains. Evidence:
`local/focus-counts-711-20261101/round-1/`; `./experiments/run.py focus-counts verify`.

**Nov 1 — Uplift arousal gate (#712).** Nine-pair sigmoid gating failed ranking,
uncertainty and domain limits. Stronger cross-source exclusions merged eight
additional components; refitted controls remain distinct from exposed full-model
replays. Discard gate. Evidence: `local/uplift-gate-712-20261101/round-2/`;
`./experiments/run.py uplift-gate verify`.

**Nov 1 — Chroma pilot and confirmation (#810/#815).** Adding 72 CQT summaries
looked strong on 32 reused CAL500 songs, but the protected full-recipe comparison
on common450 gained only .01637 NDCG [−.03879,.06567], with worse lift/MSE.
Discard this chroma recipe; no browser port justified. Other vibes also failed
advancement. Evidence: `local/chroma-810-20261101/round-1/` and
`local/focus-confirmation-815-20261101/round-1/`; `focus-confirmation verify`.

**Nov 1 — Predicted mid-level properties (#811).** On 360 Soundtracks excerpts
in 45 film/audio components, all seven properties beat mean prediction, but
their bottleneck worsened all emotion errors and augmentation failed every
vibe gate. True human-property inputs helped some tasks but are unavailable at
inference. Discard predicted-property heads. Evidence:
`local/midlevel-811-20261101/pilot-1/`; `./experiments/run.py midlevel-pilot verify`.

**Nov 1 — Classical rank transfer (#812).** Reconstructed 285 eligible WCMED
excerpts from 56 recordings/18 components. Uplift rank NDCG gain .20844 had a
positive interval; Energize's .06627 was uncertain. Keep specialists: processed
audio is unavailable, published values are rank transforms, and Cruise/Relax
require unverified cross-axis proxies. Round 3 excludes an ambiguous repeated ID.
Evidence: `local/wcmed-812-20261101/round-3/`; `wcmed-study verify`.

**Nov 1 — Pre/post mood feasibility (#813).** SiTunes supplied 1,406 paired
observations from 30 people/305 items. Initial mood predicted held-out final
mood better than unchanged mood; context/music descriptors added no reliable
gain. Only eleven audio-intersection tracks had unverified recording/windows.
Select no intervention head; association without an untreated comparison is not
causal benefit. Evidence: `local/situnes-813-20261101/round-2/`; `situnes-study verify`.

**Nov 1 — Additional-source audits.** Song Describer offered an overlap-
filtered caption pool but no acquired audio; EMID clips were too short; MusAV
required access. HumMusQA, PepMusic, Brain-focus and other activity/regulation
leads supplied no eligible independent Focus or cathartic Release benchmark.
Do not turn textual matches, session outcomes or duplicated/augmented ratings
into new recording votes. Exact access findings and rejected joins:
`local/datasets/discovery/` and `local/focus-confirmation-815-20261101/sources/`.

**Nov 6 — Joint pooling baseline (#819).** Inactive Gaussian artist/genre/album
pooling uses each original recording once. Source/support mixtures improved
measurement MAE 14.4% but failed five ranking cells. Learned masked metadata
improved MAE 15.4%; richer descriptors improved 15.6%, yet unseen Relax/Cruise/
Energize rankings still failed. Reject these campaigns. Evidence:
`build/test-reports/implementation819-learned-metadata-development-20261106/`
and `build/test-reports/implementation819-descriptors-development-20261106/`.

**Nov 6 — Paired and conditional measurement sources (#819).** Seventy verified
audio/provider pairs showed large score offsets, without identifying true
suitability. Correlated-source fits completed 58/60 tasks and failed unseen
Cruise ranking; conditional-source fits completed 60/60 with 12.2% MAE gain but
failed Focus/Cruise ranking and source-specific coverage. Reject adoption.
Evidence: `build/test-reports/implementation819-paired-source-development-20261106/`
and `build/test-reports/implementation819-conditional-source-development-20261106/`.

**Nov 6 — Ranking estimand correction (#819).** Pooling predictions from
separately calibrated folds created rankings no product model serves. Within-model
diagnostics removed those failures without qualifying earlier campaigns; a
separately frozen scalar run then passed numerical gates (15.28% MAE gain,
90.38–93.82% coverage). Reused human checks still lost Focus retrieval at ten:
3/11 to 1/11 known, 4/18 to 1/18 unseen. Evidence:
`build/test-reports/implementation819-within-model-selection-20261106/`
and `build/test-reports/implementation819-metadata-human-single-model-20261106/`.

**Nov 6 — Human transfer alternatives (#819).** Paired-source and fixed-offset
recipes did not resolve Focus/Cruise losses. Source/genre central-difference and
exact-gradient implementations exceeded ten-second fit limits; forward
differences plus symbolic-geometry reuse completed twelve fits in 2.45–6.79s
but retained human retrieval losses. Reject adoption; symbolic caching stores no
posterior. Evidence: `build/test-reports/implementation819-source-genre-forward-human-20261106/`
and the paired/offset human-single-model reports.

**Nov 6 — Product-format qualification (#819).** Format three improved MAE
14.80% but failed coverage and unseen ranking. Format four reproduced the
scalar's passing numerical gates in 60/60 unmodified runtime fits (2.38–6.69s).
That development result does not establish human or operational acceptance;
the candidate remains inactive. Evidence:
`build/test-reports/implementation819-runtime-within-model-20261106b/`
and `build/test-reports/implementation819-calibrated-runtime-campaign-20261106/`.

**Nov 6 — Sealed human comparison (#819).** The latest 140 annotation revisions
resolve to 115 positive recordings: 85 development and 30 now-consumed reserved
identities. All 24 product tasks excluded human identities from fitting. Known
Focus/Cruise/Release retrieval improved slightly, Energize regressed; unseen
Energize improved 3/12 to 5/12, other counts stayed fixed, and Uplift had no
positive target. Mixed positive-only evidence cannot establish precision or a
general human gain. Evidence: `build/test-reports/implementation819-product-human-campaign-20261106/`.

**Nov 6 — Storage/recovery rehearsals (#819).** Controlled actual-library
publication stored a Relax model in 334,929 FAO bytes; 128 targets needed compact
generation references. Unchanged publication reused IDs, corruption was rejected,
and independent restoration preserved models/provenance and conditioning
receipts. Accept those storage contracts only. Evidence:
`build/test-reports/implementation819-artifacts-independent-restore-20261106/`
and `build/test-reports/implementation819-conditioned-artifacts-20261106/`.

**Nov 6 — Scheduling/startup rehearsals (#819).** Warm-library format-four work
initially published no linked scores in 900 seconds. A corrected 300-second
rehearsal readied eight models at 140.13s and published 152 linked predictions
from 146.43s, preserving useful foreground content. Neither phase completed
refresh. Fresh-user startup, completed-refresh latency and remaining recovery/
resource gates remain unproven. Evidence:
`build/test-reports/implementation819-product-score-delivery-operations-20261106/`;
storage/publication ownership: [FAO contract](../retained_copy/FAO-OWNERS.md#shared-vibe-model-candidate-819).
