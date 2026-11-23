# Audio experiments

Experiments that shaped the models. See the [dataset index](datasets/README.md)
for labels and sources. Evidence paths below are relative to this directory;
full results and frozen inputs live in ignored artifacts. Commands: `../run.py --help`.

**Local six-vibe fitting.** On 40 labeled tracks, hard-example fitting reached
26 acceptable choices but nested selection reached only 22. Alternative-label
losses did not help. Local-set optimization was unreliable, motivating external
native-label supervision. Evidence: `local/external-20261028/`.

**Native attributes and shared classification.** MusiCNN ridge predicted native
Emotify/PMEmo/Soundtracks labels better than constants. Calibrating Emotify heads
on 55 human-labeled songs beat embedding-only classification, 38/55 versus 34/55,
yielding `emotify-vibes.json`. The heuristic still ranked better.
Evidence: `local/external-20261028/`.

**Broader replacement search.** Thirteen sources, alternate learners, PCA,
multitask fitting and unlabeled consistency produced no broad replacement.
Per-vibe AP selection also lost to shared (.397/.516), motivating fixed native
attribute mappings evaluated on external targets. Evidence:
`local/external-20261028/checkpoint-1/`, `local/per-vibe-20261029/`.

**Cruise.** Equal PMEmo/DEAM valence-arousal experts, mapped to positive valence
and moderate arousal, beat shared on ManyMusic/Memo and became `cruise-vibes.json`.
Shared refits regressed other vibes: retain shared classification and separate
specialist ranking. Evidence: `local/mapped-vibes-20261029/common-target-1/`.

**Audio preparation.** Common-input browser/native inference matched, but
independent FFmpeg preparation changed Cruise scores by up to .116. Browser
ranking gains held on ManyMusic/Memo and failed on Soundtracks. Keep extraction
and aggregation fixed. Evidence: `local/preprocessing-quality-20261029/run-1/`.

**Focus.** Emotion proxies gave uncertain gains. Direct CAL500 Reading/Studying
ridge heads reached browser NDCG .499 versus shared .400. Their clipped average
became `focus-vibes.json`, despite missing the frozen margin over its predecessor.
Evidence: `local/focus-615-20261030/round-1/`.

**Release.** Adding power worsened aggression-based anger ranking (.534/.711).
The Jamendo aggression ridge became `release-vibes.json` by explicit adoption
with uncertain gain; aggression remains a catharsis proxy. An artist-extension
attempt acquired no new audio. Evidence: `local/release-614-20261030/round-2/`.

**Energize.** Activation, joy-inclusive consensus and exercise mapping failed
advancement gates. The no-joy arousal/energy/power consensus became
`energize-vibes.json` by explicit adoption accepting domain losses.
Evidence: `local/energize-616-20261030/round-1/`.

**Relax.** Fixed calmness, low tension/energy, tenderness and valence-arousal
consensus improved mean NDCG by .043 over prior embedding. Learned weights failed
transfer limits; fixed consensus became `relax-vibes.json`.
Evidence: `local/relax-617-20261030/round-1/`.

**Uplift.** Fixed cross-source positivity improved mean NDCG by .042 but regressed
generated music. It became `uplift-vibes.json` by explicit adoption accepting that
tradeoff. Arousal-conditioned sigmoid gating subsequently failed ranking and
domain limits and was discarded. Evidence: `local/uplift-618-20261030/round-1/`,
`local/uplift-gate-712-20261101/round-2/`.

**Focus controls and count likelihoods.** Emotion residuals and task-specific
slopes gave no reliable ranking gain. Listener models improved endorsement
prediction, not demonstrated music ranking. Count likelihoods improved while
ranking lost to ridge; retain direct heads. Evidence: `local/focus-controls-637-638-20261030/round-1/`,
`local/focus-counts-711-20261101/round-1/`.

**More training windows.** Using all 1,695 ManyMusic windows with total weight
one per recording failed native ranking and domain gates. Generated recordings
had only one window and provided an unchanged control. Retain one-window recipes.
Evidence: `local/many-windows-707-20261101/round-5/`.

**MERT representations.** Final-layer MERT and intermediate layers 5/6 failed
every mapped-vibe gate on 288 matched recordings. Native-target improvements
did not translate into suitability; retain MusiCNN.
Evidence: `local/encoder-comparison-708-20261101/round-3/`,
`local/mert-layers-809-20261101/round-2/`.

**Reserved Memo confirmation.** On 120 previously reserved recordings,
Cruise/Relax comparisons were inconclusive and Uplift/Energize hit regression
limits. No specialist upgrade was established; this reserve is now consumed.
Evidence: `local/memo-confirmation-710-20261101/round-1/`.

**Chroma.** Adding 72 CQT summaries looked promising on 32 reused CAL500 songs,
but the larger protected comparison gained only .016 NDCG with uncertain gain
and worse lift/MSE. Discard this recipe.
Evidence: `local/chroma-810-20261101/round-1/`, `local/focus-confirmation-815-20261101/round-1/`.

**Mid-level properties.** Seven human properties were predictable, but their
predicted bottlenecks worsened emotion errors and augmentation failed every
vibe gate. True properties helped some tasks but are unavailable at inference;
discard the predicted heads.
Evidence: `local/midlevel-811-20261101/pilot-1/`.

**Classical transfer.** WCMED Uplift rank transfer improved NDCG by .208;
Energize remained uncertain. Rank-transformed targets, unavailable processed
audio and unverified proxies prevented adoption; retain specialists.
Evidence: `local/wcmed-812-20261101/round-3/`.

**Mood regulation.** SiTunes initial mood predicted final mood; music/context
descriptors added no reliable gain. Unverified audio windows and no untreated
comparison prevented a music intervention model.
Evidence: `local/situnes-813-20261101/round-2/`.

**Whole-output ablation.** Metadata/signal/combined Mix and Radio comparisons
exposed coverage/scale gaps: combined Relax/Focus cards used no direct specialist
slots. Positive-only labels could not establish precision.
Evidence: `local/output-evaluation-587-20261030/round-4/`.

**Joint metadata pooling.** Gaussian artist/genre/album pooling, learned metadata
and richer descriptors improved measurement MAE by 14–16% but failed unseen
ranking checks. Correlated/conditional sources and fixed offsets did not resolve
the losses. Reject those campaigns. Evidence: `../../build/test-reports/implementation819-descriptors-development-20261106/`
and `../../build/test-reports/implementation819-conditional-source-development-20261106/`.

**Pooling evaluation and human transfer.** Ranking across separately calibrated
folds tested an ordering no served model produces. Corrected within-model
evaluation and product-format fits passed numerical gates, but reused human
checks retained retrieval losses. A sealed 30-recording comparison was mixed,
with no Uplift positives and no precision evidence; numerical fit alone did not
establish adoption. Evidence: `../../build/test-reports/implementation819-within-model-selection-20261106/`
and `../../build/test-reports/implementation819-product-human-campaign-20261106/`.

**Shared classifier retirement.** Production now runs the six specialists
independently and reports their separate suitability scores without a six-way
winner. Intensity retains the strongest activation/force rule using Energize and
Release, with component lineage and explicit correction precedence. Shared-model
gating and fallback were removed; unavailable specialists leave measurements
absent. Historical shared coefficients and replay code remain under
`audio_lab_tools/legacy/`, outside the shipped application. Frozen source archives
remain the authority for reproducing earlier comparisons.

**AcousticBrainz and Focus.** A held-out adjustment using instrumental and
relaxed probabilities gained .068 NDCG on 65 matched CAL500 recordings and .046
across all 494, with intervals spanning losses. Instrumental alone had the
largest mixed-cohort point gain (.050). Cached probabilities for the same
recording covered 111 of 302 library Focus scores and 1 of 37 dated 2023 or
later. The newer library replay measured rank changes without human judgments.
Retain the Focus specialist. See `local/focus-acousticbrainz-1275/round-2/`.

**Fitting AcousticBrainz to specialist scores.** On 136 matched library recordings
from 96 credited artists, one ridge regression per vibe using eight classifier
probabilities reduced mean absolute error from .224 to .058 and improved mean
NDCG from .814 to .885 across five artist-disjoint folds. The comparison replayed
the existing fill-in with audio hidden and reproduced all six stored scores for
each of 200 independently checked recordings. The target scores came from
rerunning the shipped specialists on saved samples. Tempo, loudness and dynamic
complexity did not improve the average result. Against the newer fill-in in the
working tree, the fitted model also reduced error for every vibe, with mixed
ranking results. Keep the classifier-only model as the first candidate for testing
local adjustment. The [study](../audio_lab_tools/studies/acousticbrainz_vibe_mapping.py)
and `../../build/test-reports/acousticbrainz-vibe-mapping-20261119-r2/` contain the
replay command, frozen settings, match exclusions and held-out predictions.

**Nonlinear AcousticBrainz models.** Gaussian kernel ridge regression and nearest
neighbours used the same eight classifier fields and the same five artist splits.
Three inner folds selected settings per vibe. Mean absolute error was .0592 for
kernel ridge and .0608 for neighbours, compared with .0583 for linear ridge.
Mean NDCG was .880, .871 and .885 respectively. Keep linear ridge for the first
local adjustment experiment. The [study](../audio_lab_tools/studies/acousticbrainz_vibe_learners.py)
and `local/acousticbrainz-vibe-learners-20261119/` contain the settings, source
snapshot and held-out predictions for this comparison on the same library cohort.

**Weighting strong specialist scores.** Giving the highest fifth of training
scores double or quadruple weight raised estimates for strong Uplift recordings.
On the same 136 recordings, the original fit still had lower mean absolute error
(.058 versus .062 and .067) and higher mean specialist scores for its top picks
(.446 versus .443 and .439). Tuning the unweighted fit for top picks also lost
ground. Four newly sampled recordings with verified AcousticBrainz matches and
no artists in the training set gave .063 mean absolute error for the original
fit and .187 for the existing fill-in. Double weighting picked the strongest
fresh Uplift recording and raised overall error to .069. Keep the original fit.
The [study](../audio_lab_tools/studies/acousticbrainz_vibe_weighting.py),
`local/acousticbrainz-vibe-weighting-20261119/` and
`../../build/test-reports/acousticbrainz-vibe-weighting-inputs-20261119/` preserve
the settings, input collection recipes and predictions.

**Bundled AcousticBrainz fit and listener adjustment.** Export the original
unweighted classifier-only fit trained on the 136 verified pairs. In chronological
replay, use the oldest 68 recordings for the starting fit, the next 34 to choose
the penalty for local adjustment, and the last 34 to verify that choice.
With a penalty of 30, the adjustment reduced mean absolute error on the last
34 from .0564 to .0559. The exported starting fit scored .063 on the four fresh
pairs versus .187 for the former fallback. The
[export study](../audio_lab_tools/studies/acousticbrainz_vibe_export.py) binds
the coefficients and measurement versions to the frozen input digests.

**On Air listening references.** Scored eleven of twelve catalog uploads with
frozen MusiCNN features and the bundled Energize and Release specialists at the
three standard positions. The other upload was private. Both heads rated
Electric Six's “Dance Commander” higher than the listener's positive White Lies
example. A ranking that favors higher scores on both heads preserves that
ordering. The source IDs, decoder, artifact hashes and
per-excerpt outputs are in `../../build/test-reports/1346-evidence/direct-scores.json`.
